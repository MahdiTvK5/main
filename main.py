import os
import asyncio
import uuid
import json
import time
import datetime
import re
import io
import urllib.parse
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, PreCheckoutQueryHandler
from telegram.request import HTTPXRequest

from config import (
    TOKEN, PROXY_URL, SUPER_ADMIN_ID, PANEL_URL,
    SECURITY_WARNING, format_size, clean_num,
    DB_USER, DB_PASS, DB_NAME, DB_HOST, DB_PORT,
)
import db
from db import (
    init_db, is_admin, get_all_admins, get_setting, update_setting, get_user,
    deduct_balance, credit_balance, add_order, order_belongs_to,
)
from links import order_email, email_from_link, client_key, new_client_key
import pricing
from pricing import KIND_BUY, KIND_RENEW
import rewards
from panel import build_xui, sub_link_for, build_vless_link, build_share_link
import subportal
from keyboards import get_main_keyboard, generate_orders_keyboard, CANCEL_MARKUP


async def get_order_xui(order_id):
    """کلاینت X-UI و config_ip متناسب با پنلِ همان سفارش را برمی‌گرداند."""
    panel = await db.get_panel(await db.get_order_panel_id(order_id))
    return build_xui(panel)


async def migrate_inbound_clients(panel_id, src_inbound_id, dst_inbound_id):
    """انتقال همه‌ی کلاینت‌های یک اینباند به اینباند دیگر — با حفظ UUID و تنظیمات.

    هر کلاینت کپی (همان dict) به مقصد اضافه و از مبدأ حذف می‌شود؛ سپس سفارش‌های DB
    و لینک‌های ذخیره‌شده به اینباند جدید بازنویسی می‌شوند. کلاینت‌هایی که به هر
    دلیل منتقل نشوند روی مبدأ می‌مانند (هیچ داده‌ای از دست نمی‌رود).
    خروجی: (تعداد موفق، تعداد ناموفق، تعداد کلاینت‌های مبدا).
    """
    panel = await db.get_panel(panel_id)
    if not panel:
        return 0, 0, 0
    xui, cfg_ip = build_xui(panel)
    ok, err = await xui.login()
    if not ok:
        raise RuntimeError(f"لاگین به پنل ناموفق: {err}")
    src = await xui.get_inbound(src_inbound_id)
    dst = await xui.get_inbound(dst_inbound_id)
    if not src or not dst:
        raise RuntimeError("اینباند مبدأ یا مقصد روی پنل پیدا نشد.")
    try:
        src_clients = json.loads(src.get('settings') or '{}').get('clients', []) or []
    except ValueError:
        src_clients = []
    try:
        dst_clients = json.loads(dst.get('settings') or '{}').get('clients', []) or []
    except ValueError:
        dst_clients = []
    dst_emails = {str(c.get('email') or '').lower() for c in dst_clients}

    moved, failed = [], 0
    for c in src_clients:
        email = str(c.get('email') or '').strip()
        if not email or email.lower() in dst_emails:
            continue  # قبلاً روی مقصد هست — دست نمی‌زنیم
        added, add_err = await xui.add_raw_client(dst_inbound_id, c)
        if not added:
            logging.warning("مهاجرت %s به اینباند %s ناموفق: %s", email, dst_inbound_id, add_err)
            failed += 1
            continue
        key = client_key(c)
        removed, _del_err = await xui.remove_client(src_inbound_id, key)
        if not removed:
            # کپی روی مقصد ساخته شد ولی حذف از مبدأ نشد — مشکلی برای سرویس ندارد
            logging.warning("حذف %s از اینباند %s ناموفق (کپی روی مقصد برقرار است)", email, src_inbound_id)
        moved.append((email, c))

    if moved:
        # سفارش‌های مربوط به اینباند مبدأ را پیدا و به مقصد منتقل کن
        async with db.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, email FROM orders WHERE panel_id = $1 AND inbound_id = $2",
                panel_id, src_inbound_id,
            )
        by_email = {email: c for email, c in moved}
        refreshed = 0
        for r in rows:
            o_email = order_email(r)
            c = by_email.get(str(o_email).lower()) or by_email.get(o_email)
            if not c:
                continue
            new_link = build_share_link(dst, c, cfg_ip)
            await db.update_order_service(r['id'], config_link=new_link, inbound_id=dst_inbound_id)
            refreshed += 1
        # هر سفارشِ دیگری از این پنل که روی مبدأ بود (ایمیلش منطبق نبود) هم منتقل می‌شود
        await db.db_pool.execute(
            "UPDATE orders SET inbound_id = $1 WHERE panel_id = $2 AND inbound_id = $3",
            dst_inbound_id, panel_id, src_inbound_id,
        )
        logging.info("مهاجرت اینباند %s→%s (پنل %s): %s موفق، %s ناموفق، %s لینک بازسازی",
                     src_inbound_id, dst_inbound_id, panel_id, len(moved), failed, refreshed)
    return len(moved), failed, len(src_clients)


def is_super_admin(user_id):
    """فقط مدیر کل (SUPER_ADMIN_ID در .env)؛ ادمین‌های جدول admins مشمول خرید رایگان نمی‌شوند."""
    return user_id == SUPER_ADMIN_ID


async def _banned_guard(update_or_query, user_id):
    """اگر کاربر بن باشد پیام مناسب می‌دهد و True برمی‌گرداند (یعنی مسیر را قطع کن)."""
    if not await db.is_user_banned(user_id):
        return False
    text = "⛔️ دسترسی شما به ربات مسدود شده است.\nدر صورت اعتراض با پشتیبانی در تماس باشید."
    try:
        if hasattr(update_or_query, 'message') and update_or_query.message:
            await update_or_query.message.reply_text(text)
        elif hasattr(update_or_query, 'edit_message_text'):
            await update_or_query.answer("⛔️ دسترسی شما مسدود است.", show_alert=True)
    except Exception:
        pass
    return True


async def _join_ok(context, user_id):
    """بررسی اجبار عضویت کانال. خاموش/خطا = مجاز (fail-open تا کانالِ خراب همه را قفل نکند)."""
    ch = (await get_setting('force_channel') or '').strip()
    if not ch or ch.lower() in ('off', 'خاموش', 'none', '-'):
        return True
    if await is_admin(user_id):
        return True
    try:
        member = await context.bot.get_chat_member(ch, user_id)
        return member.status in ('member', 'administrator', 'creator')
    except Exception as ex:
        logging.warning("بررسی عضویت کانال ناموفق (%s): %s", ch, ex)
        return True


async def _send_join_prompt(update_or_query, channel):
    """پیام + دکمه‌ی عضویت برای کاربری که هنوز عضو کانال نشده."""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ عضویت در کانال", url=f"https://t.me/{channel.lstrip('@')}")]])
    text = "⭐️ برای استفاده از ربات ابتدا باید عضو کانال شوید.\nسپس /start را دوباره بزنید."
    try:
        if hasattr(update_or_query, 'message') and update_or_query.message:
            await update_or_query.message.reply_text(text, reply_markup=kb)
        else:
            await update_or_query.answer("⭐️ ابتدا عضو کانال شوید.", show_alert=True)
    except Exception:
        pass


async def unique_service_name(xui, base, max_tries=8, max_len=20):
    """نام سرویس یکتا روی پنل.

    اسم دلخواه کاربر عیناً استفاده می‌شود (حس مالکیت)؛ فقط اگر روی پنل تکراری
    باشد، مثل پیشنهاد یوزرنیمِ جیمیل یک شماره‌ی ترتیبی «معنادار» می‌گیرد:
    Fardinn → Fardinn2 → Fardinn3. اگر اسم با عدد تمام شده باشد همان عدد
    ادامه پیدا می‌کند (Fardinn2 → Fardinn3). در صورت خطای پنل، همان base
    برگردانده می‌شود تا جریان خرید بلاک نشود.
    """
    try:
        taken = await xui.get_all_client_emails()
    except Exception:
        return base
    if not taken or base.lower() not in taken:
        return base

    # اگر اسم با عدد تمام می‌شود از همان ادامه بده؛ وگرنه از ۲ شروع کن
    m = re.match(r"^(.*?)(\d+)$", base)
    if m:
        stem, num = m.group(1), int(m.group(2))
    else:
        stem, num = base, 1

    for _ in range(max_tries):
        num += 1
        cand_num = str(num)
        # سقف طول همان سقف ورودی کاربر است؛ در صورت نیاز از انتهای stem کم می‌کنیم
        stem_fit = stem[:max(1, max_len - len(cand_num))]
        cand = f"{stem_fit}{cand_num}"
        if cand.lower() not in taken:
            return cand
    # هشت تلاشِ ترتیبی هم اشغال بود؟ عملاً رخ نمی‌دهد؛ آخرین تلاش با عدد بزرگ‌تر
    return f"{stem}{num}{int(time.time()) % 1000}"


async def safe_answer(query, text=None, alert=False):
    """پاسخ به callback query بدون ریسکِ «query قبلاً پاسخ داده شده».

    هر query در تلگرام فقط یک‌بار قابل answer است؛ اگر پاسخ مصرف شده باشد و پیامی
    برای کاربر داشته باشیم، آن را به‌صورت پیام معمولی می‌فرستیم تا گم نشود.
    """
    try:
        await query.answer(text or "", show_alert=alert)
        return True
    except Exception:
        if text:
            try:
                await query.message.reply_text(text)
            except Exception:
                pass
        return False


def _plan_icon(plan):
    """آیکون/ایموجی ابتدای دکمه‌ی پلن (اگر تنظیم شده باشد)."""
    try:
        icon = (plan['icon'] or '').strip()
    except (KeyError, TypeError):
        icon = ''
    return f"{icon} " if icon else ""


def plan_button_text(plan, price):
    days = plan['duration_days'] or 30
    return f"{_plan_icon(plan)}{plan['name']} | {plan['gb']}GB / {days}روز - {price:,} تومان"


def remember_quote(context, plan_id, kind, quote):
    """قیمتِ نمایش‌داده‌شده را نگه می‌دارد تا در لحظه‌ی تایید همان مبلغ کسر شود."""
    context.user_data['quote'] = {'plan_id': int(plan_id), 'kind': kind, 'quote': quote}


def recall_quote(context, plan_id, kind):
    """قیمت ذخیره‌شده‌ی همین پلن؛ اگر نبود None (صدازننده دوباره محاسبه می‌کند)."""
    saved = context.user_data.get('quote')
    if saved and saved.get('plan_id') == int(plan_id) and saved.get('kind') == kind:
        return saved.get('quote')
    return None


def products_header(role, quoted):
    """سربرگ منوی محصولات: سطح کاربر و اطلاع‌رسانی آفر فعال."""
    lines = [f"🛍 **محصولات** (سطح شما: {pricing.role_label(role)})"]
    if any(q.first_offer for _p, q in quoted):
        lines.append("🔥 آفر خرید اول برای شما فعال است؛ فقط یک‌بار قابل استفاده.")
    elif any(q.has_discount for _p, q in quoted):
        lines.append("🎯 قیمت‌های ویژه برای شما اعمال شده است.")
    lines.append("\nمحصول مورد نظر را انتخاب کنید:")
    return "\n".join(lines)


# ================= محدودیت نرخ (ضدِ اسپم) =================
from collections import defaultdict, deque

_RATE_LIMIT = 10        # حداکثر تعداد رویداد
_RATE_WINDOW = 5.0      # در این بازه (ثانیه)
_user_hits = defaultdict(deque)
_last_rate_prune = 0.0

# فاصله‌ی بین پیام‌های همگانی (سقف تلگرام حدود ۳۰ پیام در ثانیه است)
BROADCAST_DELAY = 0.05


def _prune_rate_hits(now):
    """حذف کاربرانی که مدتی فعال نبوده‌اند تا دیکشنری بی‌نهایت رشد نکند."""
    global _last_rate_prune
    if now - _last_rate_prune < 300:
        return
    _last_rate_prune = now
    for uid in [u for u, dq in _user_hits.items() if not dq or now - dq[-1] > 600]:
        _user_hits.pop(uid, None)


def _rate_ok(user_id):
    """پنجره‌ی لغزان ساده برای جلوگیری از اسپم و فشار روی دیتابیس/پنل."""
    now = time.monotonic()
    _prune_rate_hits(now)
    dq = _user_hits[user_id]
    while dq and now - dq[0] > _RATE_WINDOW:
        dq.popleft()
    if len(dq) >= _RATE_LIMIT:
        return False
    dq.append(now)
    return True


# فیلدهای قابل‌ویرایش پلن: (کلید، برچسب، ستون دیتابیس، عددی‌بودن)
PLAN_FIELDS = [
    ("name", "نام", "name", False),
    ("gb", "حجم (GB)", "gb", True),
    ("duration", "مدت (روز)", "duration_days", True),
    ("icon", "آیکون/ایموجی", "icon", False),
    ("price", "قیمت عادی", "price", True),
    ("vipprice", "قیمت VIP", "vip_price", True),
    ("resprice", "قیمت همکاری", "reseller_price", True),
    ("renewnormal", "قیمت تمدید عادی", "renew_normal_price", True),
    ("renewvip", "قیمت تمدید VIP", "renew_vip_price", True),
    ("renewres", "قیمت تمدید همکاری", "renew_reseller_price", True),
    ("firstprice", "قیمت آفر خرید اول", "first_price", True),
    ("inbound", "اینباند", "inbound_id", True),
    ("panel", "پنل", "panel_id", True),
]
PLAN_FIELD_MAP = {f[0]: f for f in PLAN_FIELDS}


async def admin_menu_markup(user_id):
    """کیبورد پنل مدیریت (یک منبع واحد؛ قبلاً در دو جا کپی شده بود و از هم فاصله می‌گرفت)."""
    status_text = "🟢 باز" if (await get_setting('sales_status')) == 'open' else "🔴 بسته"
    kb = [
        [InlineKeyboardButton("محصولات و پلن‌ها 🛒", callback_data='admin_manage_plans')],
        [InlineKeyboardButton("لیست VIP و ادمین‌ها 📋", callback_data='admin_list_specials')],
        [InlineKeyboardButton("کارت 💳", callback_data='admin_set_card')],
        [InlineKeyboardButton("افزودن VIP 🟢", callback_data='admin_add_vip'), InlineKeyboardButton("حذف VIP 🔴", callback_data='admin_rem_vip')],
        [InlineKeyboardButton("ارسال پیام 📢", callback_data='admin_broadcast')],
        [InlineKeyboardButton("ایمپورت کانفیگ 🔗", callback_data='admin_import_config'), InlineKeyboardButton("پشتیبانی 📞", callback_data='admin_set_support')],
        [InlineKeyboardButton("مدیریت پنل‌ها 🖥", callback_data='admin_manage_panels'), InlineKeyboardButton("اکانت تست 🎁", callback_data='admin_test_menu')],
        [InlineKeyboardButton("📊 گزارش فروش", callback_data='admin_report'), InlineKeyboardButton("🔔 هشدار انقضا", callback_data='admin_set_notify')],
        [InlineKeyboardButton("🎟 کدهای هدیه", callback_data='admin_gift_menu'), InlineKeyboardButton("🔗 سیستم دعوت", callback_data='admin_ref_menu')],
        [InlineKeyboardButton("💰 مدیریت قیمت‌ها", callback_data='admin_pricing'), InlineKeyboardButton("🎯 مدیریت آفرها", callback_data='admin_offers')],
        [InlineKeyboardButton("📈 گزارش مالی", callback_data='admin_finance'), InlineKeyboardButton("🤝 نمایندگان", callback_data='admin_resellers')],
        [InlineKeyboardButton("🚫 بن کاربر", callback_data='admin_ban_menu'), InlineKeyboardButton("🔀 مهاجرت اینباند", callback_data='admin_migrate_menu')],
        [InlineKeyboardButton("⭐️ اجبار عضویت کانال", callback_data='admin_force_channel')],
        [InlineKeyboardButton("💾 بکاپ دیتابیس", callback_data='admin_backup_menu')],
        [InlineKeyboardButton(f"وضعیت فروش: {status_text}", callback_data='admin_toggle_sales')],
    ]
    if user_id == SUPER_ADMIN_ID:
        kb.append([InlineKeyboardButton("افزودن ادمین 👮‍♂️", callback_data='superadmin_add_admin'), InlineKeyboardButton("حذف ادمین ⛔️", callback_data='superadmin_rem_admin')])
    return InlineKeyboardMarkup(kb)


def plan_edit_markup(plan_id):
    rows = [[InlineKeyboardButton(f"✏️ {label}", callback_data=f"pef_{key}_{plan_id}")] for key, label, _col, _isnum in PLAN_FIELDS]
    rows.append([InlineKeyboardButton("✅ پایان", callback_data="pe_done")])
    return InlineKeyboardMarkup(rows)


# ================= مدیریت قیمت‌ها / آفرها / گزارش مالی =================
async def render_pricing_menu(query):
    """جدول قیمت همه‌ی پلن‌ها در سه سطح، به‌همراه قیمت تمدید و آفر خرید اول."""
    plans = await db.list_plans()
    lines = ["💰 **مدیریت قیمت‌ها**", "", "`ID | حجم/روز | همکاری | VIP | عادی | تمدید عادی/VIP/همکاری | خرید‌اول`"]
    for p in plans:
        state = "" if p['is_active'] else " ⛔️"
        lines.append(
            f"`{p['id']}` {_plan_icon(p)}{p['gb']}GB/{p['duration_days']}روز{state}\n"
            f"   🤝 {money(p['reseller_price'])} | 💎 {money(p['vip_price'])} | 👤 {money(p['price'])}"
            f" | ♻️ {money(p['renew_normal_price'])}/{money(p['renew_vip_price'])}/{money(p['renew_reseller_price'])}"
            f" | 🔥 {money(p['first_price'])}"
        )
    if not plans:
        lines.append("\nهنوز پلنی تعریف نشده است.")
    kb = [
        [InlineKeyboardButton("✏️ ویرایش پلن", callback_data='admin_edit_plan'), InlineKeyboardButton("➕ افزودن پلن", callback_data='admin_add_plan')],
        [InlineKeyboardButton("🔁 فعال/غیرفعال کردن پلن", callback_data='admin_toggle_plan')],
        [InlineKeyboardButton("📥 ساخت پلن‌های پیش‌فرض", callback_data='admin_seed_plans')],
        [InlineKeyboardButton("🎯 مدیریت آفرها", callback_data='admin_offers')],
    ]
    await _edit_or_send(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def render_offers_menu(query):
    """فهرست آفرها با وضعیت، مخاطب، بازه و سقف استفاده."""
    offers = await db.list_offers()
    cfg = await pricing.load_config()
    lines = ["🎯 **مدیریت آفرها**", ""]
    lines.append(f"🔥 آفر خرید اول: {'🟢 روشن' if cfg.first_offer_enabled else '🔴 خاموش'}")
    lines.append(f"♻️ قیمت ویژه‌ی تمدید: {'🟢 روشن' if cfg.renew_offer_enabled else '🔴 خاموش'}")
    lines.append("")
    if offers:
        for o in offers:
            value = f"{o['value']}٪" if (o['kind'] or 'percent') == 'percent' else f"{money(o['value'])} تومان"
            window = ""
            if o['starts_at'] or o['ends_at']:
                window = f"\n   📅 {o['starts_at'].strftime('%Y-%m-%d') if o['starts_at'] else '—'} تا {o['ends_at'].strftime('%Y-%m-%d') if o['ends_at'] else '—'}"
            limit = "نامحدود" if not o['max_uses'] else f"{o['used_count']}/{o['max_uses']}"
            lines.append(
                f"`{o['id']}` {'🟢' if o['is_active'] else '🔴'} **{o['name']}** — {value}\n"
                f"   👥 {pricing.AUDIENCES.get(o['audience'], o['audience'])} | استفاده: {limit}{window}"
            )
    else:
        lines.append("هنوز آفری ثبت نشده است. آفرهای سفارشی از پنل وب ساخته می‌شوند.")
    kb = [
        [InlineKeyboardButton(("🔴 خاموش‌کردن آفر خرید اول" if cfg.first_offer_enabled else "🟢 روشن‌کردن آفر خرید اول"), callback_data='admin_toggle_firstoffer')],
        [InlineKeyboardButton(("🔴 خاموش‌کردن قیمت تمدید" if cfg.renew_offer_enabled else "🟢 روشن‌کردن قیمت تمدید"), callback_data='admin_toggle_renewoffer')],
        [InlineKeyboardButton("🔁 فعال/غیرفعال کردن یک آفر", callback_data='admin_toggle_offer')],
        [InlineKeyboardButton("💰 مدیریت قیمت‌ها", callback_data='admin_pricing')],
    ]
    await _edit_or_send(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def render_finance_report(query):
    """گزارش مالی: تفکیک کاربران، فروش دوره‌ای، سهم هر سطح، آفرها و درآمد خالص."""
    r = await db.get_financial_report()
    users = r['users']
    by_role = r['by_role']

    def role_sales(key):
        item = by_role.get(key, {'total': 0, 'count': 0})
        return f"{money(item['total'])} تومان ({item['count']} فروش)"

    msg = (
        f"📈 **گزارش مالی**\n\n"
        f"👥 **کاربران**\n"
        f"عادی: {users.get('normal', 0):,} | VIP: {users.get('vip', 0):,} | نماینده: {users.get('reseller', 0):,}\n\n"
        f"💵 **فروش**\n"
        f"امروز: {money(r['today'])} تومان\n"
        f"۷ روز اخیر: {money(r['week'])} تومان\n"
        f"۳۰ روز اخیر: {money(r['month'])} تومان\n"
        f"کل: {money(r['total'])} تومان ({r['sales_count']} فروش)\n\n"
        f"🏷 **سهم هر سطح**\n"
        f"عادی: {role_sales('normal')}\n"
        f"VIP: {role_sales('vip')}\n"
        f"نماینده: {role_sales('reseller')}\n\n"
        f"🎁 **آفرها و تخفیف‌ها**\n"
        f"استفاده از آفر خرید اول: {r['first_offers']:,}\n"
        f"تعداد تمدیدها: {r['renewals']:,}\n"
        f"مجموع تخفیف داده‌شده: {money(r['discount'])} تومان\n"
        f"هدیه‌ی نمایندگی پرداختی: {money(r['bonus'])} تومان\n"
        f"کدهای هدیه: {money(r.get('gifts', 0))} تومان\n"
        f"پاداش دعوت: {money(r.get('referrals', 0))} تومان\n\n"
        f"💳 **جریان مالی**\n"
        f"کل شارژ حساب‌ها: {money(r['topup'])} تومان\n"
        f"برگشت وجه: {money(r['refunds'])} تومان\n"
        f"**درآمد خالص: {money(r['net'])} تومان**"
    )
    kb = [[InlineKeyboardButton("🔄 بروزرسانی", callback_data='admin_finance')]]
    await _edit_or_send(query, msg, InlineKeyboardMarkup(kb))


async def render_resellers_menu(query):
    """وضعیت نمایندگان و تنظیمات پله‌های تخفیف و هدیه‌ی اولین شارژ."""
    cfg = await pricing.load_config()
    resellers = await db.list_vips(role=pricing.RESELLER)
    lines = [
        "🤝 **نمایندگان**", "",
        f"🎁 هدیه‌ی اولین شارژ: {'🟢 روشن' if cfg.reseller_bonus_enabled else '🔴 خاموش'}",
        f"   حداقل شارژ {money(cfg.reseller_bonus_min)} تومان → {cfg.reseller_bonus_percent}٪ اعتبار هدیه",
        "",
        "📊 **پله‌های تخفیف** (مجموع شارژ ۳۰ روز اخیر)",
        "   سطح ۱: بدون تخفیف اضافه",
        f"   سطح ۲: از {money(cfg.reseller_t2_min)} تومان → {cfg.reseller_t2_discount}٪",
        f"   نماینده VIP: از {money(cfg.reseller_t3_min)} تومان → {cfg.reseller_t3_discount}٪",
        "",
        f"👥 **لیست نمایندگان ({len(resellers)})**",
    ]
    for r in resellers[:20]:
        lines.append(f"`{r['user_id']}` - {r['nickname'] or 'بدون نام'} | موجودی: {money(r['balance'])}")
    if not resellers:
        lines.append("هنوز نماینده‌ای ثبت نشده است.")
    kb = [
        [InlineKeyboardButton("➕ افزودن نماینده", callback_data='admin_add_reseller'), InlineKeyboardButton("➖ حذف نماینده", callback_data='admin_rem_reseller')],
        [InlineKeyboardButton(("🔴 خاموش‌کردن هدیه" if cfg.reseller_bonus_enabled else "🟢 روشن‌کردن هدیه"), callback_data='admin_toggle_resbonus')],
        [InlineKeyboardButton("⚙️ تنظیم مقادیر (پنل وب)", callback_data='ignore')],
    ]
    await _edit_or_send(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def render_referral_menu(query):
    """وضعیت سیستم دعوت و دکمه‌های تنظیم پاداش معرف / دعوت‌شده / حداقل شارژ."""
    cfg = await db.load_referral_config()
    stats = await db.get_referral_stats()
    lines = [
        "🔗 **سیستم دعوت**", "",
        f"وضعیت: {'🟢 روشن' if cfg.enabled else '🔴 خاموش'}",
        f"🎁 پاداش معرف: {money(cfg.referrer_bonus)} تومان",
        f"🎁 هدیه دعوت‌شده: {money(cfg.invitee_bonus)} تومان",
        f"💳 حداقل شارژ برای آزاد شدن پاداش: {money(cfg.min_topup)} تومان" if cfg.min_topup else "💳 حداقل شارژ: بدون حداقل",
        "",
        f"👥 دعوت‌شده‌ها: {stats['invited']:,} | پاداش‌گرفته: {stats['rewarded']:,} | در انتظار: {stats['pending']:,}",
        f"💸 مجموع پرداخت‌شده: {money(stats['paid'])} تومان",
        "",
        "پاداش فقط بعد از اولین شارژِ تاییدشده‌ی دعوت‌شده پرداخت می‌شود و روی هم سوار نمی‌شود.",
    ]
    kb = [
        [InlineKeyboardButton(("🔴 خاموش کن" if cfg.enabled else "🟢 روشن کن"), callback_data='admin_ref_toggle')],
        [InlineKeyboardButton("🎁 پاداش معرف", callback_data='admin_set_refbonus'), InlineKeyboardButton("🎁 هدیه دعوت‌شده", callback_data='admin_set_invitee_bonus')],
        [InlineKeyboardButton("💳 حداقل شارژ", callback_data='admin_set_ref_min')],
    ]
    await _edit_or_send(query, "\n".join(lines), InlineKeyboardMarkup(kb))


def _gift_line(c):
    """یک خط خلاصه برای فهرست کدهای هدیه در ربات."""
    used = f"{c['used_count']}/{c['max_uses']}"
    flag = "🟢" if c['is_active'] is not False else "⏸"
    extra = ""
    expires = c['expires_at']
    if expires:
        extra = f" | تا {expires.strftime('%Y-%m-%d')}"
        now = datetime.datetime.now()
        if getattr(expires, 'tzinfo', None) and expires.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=expires.tzinfo)
        if now > expires:
            flag = "⏰"
    note = f" — {c['note']}" if c['note'] else ""
    return f"{flag} `{c['code']}` | {c['amount']:,} تومان | {used}{extra}{note}"


async def render_gift_menu(query):
    codes = await db.list_gift_codes()
    lines = ["🎟 **کدهای هدیه**", ""]
    if codes:
        lines.extend(_gift_line(c) for c in codes[:25])
        if len(codes) > 25:
            lines.append(f"\n… و {len(codes) - 25} کد دیگر (در پنل وب ببینید)")
    else:
        lines.append("هیچ کدی ثبت نشده است.")
    kb = [
        [InlineKeyboardButton("➕ افزودن کد", callback_data='admin_gift_add'), InlineKeyboardButton("🎲 کد تصادفی", callback_data='admin_gift_random')],
        [InlineKeyboardButton("⏸ فعال/غیرفعال", callback_data='admin_gift_toggle'), InlineKeyboardButton("🗑 حذف کد", callback_data='admin_gift_del')],
    ]
    await _edit_or_send(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def _edit_or_send(query, text, markup=None):
    """ویرایش پیام فعلی و در صورت ناموفق‌بودن، ارسال پیام جدید."""
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode='Markdown')
    except Exception:
        try:
            await query.message.reply_text(text, reply_markup=markup, parse_mode='Markdown')
        except Exception as ex:
            logging.warning("نمایش صفحه‌ی مدیریت ناموفق بود: %s", ex)


# فیلدهای قابل‌ویرایش پنل: (کلید، برچسب، ستون دیتابیس)
PANEL_FIELDS = [
    ("name", "نام", "name"),
    ("url", "آدرس (URL)", "url"),
    ("user", "نام کاربری", "username"),
    ("pass", "رمز عبور", "password"),
    ("ip", "IP کانفیگ", "config_ip"),
    ("sub", "لینک ساب (Sub URL)", "sub_url"),
]
PANEL_FIELD_MAP = {f[0]: f for f in PANEL_FIELDS}


def panel_edit_markup(panel_id):
    rows = [[InlineKeyboardButton(f"✏️ {label}", callback_data=f"panef_{key}_{panel_id}")] for key, label, _col in PANEL_FIELDS]
    rows.append([InlineKeyboardButton("✅ پایان", callback_data="pe_done")])
    return InlineKeyboardMarkup(rows)


def panel_edit_text(panel):
    return (
        f"✏️ **ویرایش پنل** (ID: `{panel['id']}`)\n\n"
        f"📋 نام: {panel['name']}\n"
        f"🔗 URL: {panel['url']}\n"
        f"👤 یوزر: {panel['username']}\n"
        f"🌐 IP کانفیگ: {panel['config_ip']}\n"
        f"🔗 لینک ساب: {panel['sub_url'] or '—'}\n\n"
        f"برای تغییر هر مورد، دکمه‌اش را بزنید."
    )


async def render_test_menu(query):
    enabled = (await get_setting('test_enabled')) == 'on'
    gb = await get_setting('test_gb')
    days = await get_setting('test_days')
    pid = await get_setting('test_panel_id')
    inb = await get_setting('test_inbound_id')
    panel = await db.get_panel(int(pid)) if pid else None
    pname = panel['name'] if panel else "—"
    status = "🟢 روشن" if enabled else "🔴 خاموش"
    msg = (
        f"🎁 **تنظیمات اکانت تست**\n\n"
        f"وضعیت: {status}\n"
        f"💾 حجم: {gb} GB\n"
        f"📅 مدت: {days} روز\n"
        f"🖥 پنل: {pname}\n"
        f"⚙️ اینباند: {inb or '—'}\n\n"
        f"برای فعال‌سازی، حتماً پنل و اینباند را تنظیم کنید."
    )
    kb = [
        [InlineKeyboardButton(("🔴 خاموش کن" if enabled else "🟢 روشن کن"), callback_data='admin_test_toggle')],
        [InlineKeyboardButton("حجم (GB)", callback_data='admin_test_set_gb'), InlineKeyboardButton("مدت (روز)", callback_data='admin_test_set_days')],
        [InlineKeyboardButton("پنل", callback_data='admin_test_set_panel'), InlineKeyboardButton("اینباند", callback_data='admin_test_set_inbound')],
    ]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except Exception:
        pass


def money(value):
    """نمایش مبلغ با جداکننده‌ی هزارگان؛ مقدار NULL را به صفر تبدیل می‌کند."""
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


async def plan_edit_text(plan):
    panel = await db.get_panel(plan['panel_id']) if plan['panel_id'] else None
    pname = panel['name'] if panel else "—"
    return (
        f"✏️ **ویرایش پلن** (ID: `{plan['id']}`)\n\n"
        f"{('آیکون: ' + plan['icon'] + chr(10)) if (plan['icon'] or '').strip() else ''}"
        f"📋 نام: {plan['name']}\n"
        f"💾 حجم: {plan['gb']} GB\n"
        f"📅 مدت: {plan['duration_days']} روز\n"
        f"👤 قیمت عادی: {money(plan['price'])}\n"
        f"💎 قیمت VIP: {money(plan['vip_price'])}\n"
        f"🤝 قیمت همکاری: {money(plan['reseller_price'])}\n"
        f"♻️ تمدید عادی: {money(plan['renew_normal_price'])}\n"
        f"♻️ تمدید VIP: {money(plan['renew_vip_price'])}\n"
        f"♻️ تمدید همکاری: {money(plan['renew_reseller_price'])}\n"
        f"🔥 آفر خرید اول: {money(plan['first_price'])}\n"
        f"⚙️ اینباند: {plan['inbound_id']}\n"
        f"🖥 پنل: {pname}\n"
        f"وضعیت: {'🟢 فعال' if plan['is_active'] else '🔴 غیرفعال'}\n\n"
        f"برای تغییر هر مورد، دکمه‌اش را بزنید."
    )


async def ensure_order_access(query, order_id, admin_status):
    """کنترل دسترسی به سفارش. ادمین‌ها به همه‌چیز دسترسی دارند؛ کاربر عادی فقط به سفارش خودش."""
    if admin_status or await order_belongs_to(order_id, query.from_user.id):
        return True
    await safe_answer(query, "⛔️ این سفارش متعلق به شما نیست.", alert=True)
    return False


async def load_order_service(order_id):
    """سفارش را همراه نام سرویس برمی‌گرداند. خروجی: (row, email) یا (None, "")."""
    row = await db.get_order(order_id)
    if not row:
        return None, ""
    return row, order_email(row)


async def _run_broadcast(context, user_ids, from_chat_id, message_id, progress_msg=None, forward=False):
    """ارسال پیام همگانی با کنترل نرخ و گزارش پیشرفت (خارج از مسیر پردازش آپدیت‌ها).

    forward=True: پیام به‌جای کپی، **فوروارد** می‌شود — فرستنده‌ی نمایشی خودِ ادمین
    می‌ماند و ایموجی‌های پریمیومِ او سالم می‌رسند (کپی/ارسال بات آن‌ها را به ایموجی
    ساده degrade می‌کند).
    """
    sent = failed = 0
    for i, uid in enumerate(user_ids, 1):
        try:
            if forward:
                await context.bot.forward_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
            else:
                await context.bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
            sent += 1
        except Exception as ex:
            failed += 1
            retry_after = getattr(ex, 'retry_after', None)
            if retry_after:  # محدودیت نرخ تلگرام
                await asyncio.sleep(float(retry_after) + 1)
        await asyncio.sleep(BROADCAST_DELAY)
        if progress_msg and i % 50 == 0:
            try:
                await progress_msg.edit_text(f"📢 در حال ارسال... {i} از {len(user_ids)}")
            except Exception:
                pass
    logging.info("BROADCAST sent=%s failed=%s total=%s", sent, failed, len(user_ids))
    if progress_msg:
        try:
            await progress_msg.edit_text(f"✅ پیام به {sent} کاربر ارسال شد." + (f"\n⚠️ ناموفق: {failed}" if failed else ""))
        except Exception:
            pass


async def find_stale_orders(orders):
    """سفارش‌هایی که کلاینت‌شان روی پنل وجود ندارد را پیدا می‌کند (بدون حذف).

    مبنا لیست کلاینت‌های تعریف‌شده‌ی پنل است، نه آمار ترافیک؛ چون کلاینت تازه‌ساخته
    ممکن است هنوز رکورد ترافیک نداشته باشد و اشتباهاً «حذف‌شده» تشخیص داده شود.
    خروجی: (لیست سفارش‌های ناموجود، آیا پنلی بررسی‌نشده مانده).
    """
    by_panel = defaultdict(list)
    for o in orders:
        by_panel[o['panel_id']].append(o)

    stale, unchecked = [], False
    for panel_id, group in by_panel.items():
        panel = await db.get_panel(panel_id) if panel_id else None
        if panel is None and not PANEL_URL:
            unchecked = True
            continue
        xui, _ip = build_xui(panel)
        ok, _ = await xui.login()
        if not ok:
            unchecked = True
            continue
        emails = await xui.get_all_client_emails()
        if emails is None:
            unchecked = True
            continue
        for o in group:
            email = order_email(o)
            # سفارشی که نامش قابل تشخیص نیست را حذف‌کردنی نمی‌دانیم تا داده از دست نرود
            if email and email.strip().lower() not in emails:
                stale.append(o)
    return stale, unchecked


async def fetch_client_for_order(query, order_id):
    """کلاینت روی پنل را برای یک سفارش پیدا می‌کند و خطاها را به کاربر اطلاع می‌دهد.

    خروجی: (xui, cfg_ip, inbound_id, client_dict) یا None در صورت هر خطا.
    """
    row, email = await load_order_service(order_id)
    if not row:
        await query.message.reply_text("❌ سفارش یافت نشد.")
        return None
    if not email:
        await query.message.reply_text("❌ نام این سرویس قابل تشخیص نیست؛ لطفاً به پشتیبانی اطلاع دهید.")
        return None
    xui, cfg_ip = await get_order_xui(order_id)
    is_logged, login_err = await xui.login()
    if not is_logged:
        await query.message.reply_text(f"❌ اتصال به پنل ناموفق بود.\n{str(login_err)[:200]}")
        return None
    inbound_id, _port, client_dict = await xui.get_client_exact_info(email)
    if not client_dict:
        await query.message.reply_text(
            "❌ این کانفیگ روی پنل یافت نشد (احتمالاً حذف شده). می‌توانید با «🗑 حذف از لیست» آن را پاک کنید."
        )
        return None
    return xui, cfg_ip, inbound_id, client_dict


async def refresh_order_link(order_id, xui, inbound_id, client_dict, cfg_ip):
    """لینک ذخیره‌شده را از روی وضعیت واقعیِ کلاینت بازمی‌سازد.

    جایگزینِ متنی روی لینک (replace) برای vmess کار نمی‌کند، چون uuid و نام داخل
    Base64 هستند؛ پس لینک را از اینباند و کلاینت دوباره می‌سازیم.
    """
    inbound = await xui.get_inbound(inbound_id)
    if not inbound:
        return None
    new_link = build_share_link(inbound, client_dict, cfg_ip)
    await db.update_order_service(
        order_id, config_link=new_link, email=client_dict.get('email'), inbound_id=inbound_id,
    )
    return new_link


async def render_order_details(query, order_id, alert_msg=None):
    if not await is_admin(query.from_user.id) and not await order_belongs_to(order_id, query.from_user.id):
        await safe_answer(query, "⛔️ این سفارش متعلق به شما نیست.", alert=True)
        return
    row, email = await load_order_service(order_id)
    if not row:
        await safe_answer(query, "سفارش یافت نشد!", alert=True)
        return
    if not email:
        await safe_answer(query, "❌ نام این سرویس قابل تشخیص نیست؛ لطفاً به پشتیبانی اطلاع دهید.", alert=True)
        return

    xui, _ip = await get_order_xui(order_id)
    is_logged, _ = await xui.login()

    if not is_logged:
        await safe_answer(query, "❌ خطا در اتصال به سرور پنل.", alert=True)
        return

    stats = await xui.get_client_stats(email)
    if stats:
        enable = stats.get('enable', False)
        total = stats.get('total', 0)
        used = stats.get('up', 0) + stats.get('down', 0)
        expiry = stats.get('expiryTime', 0)
        status_text = "فعال 🟢" if enable else "غیرفعال 🔴"
        if expiry > 0 and expiry < int(time.time() * 1000): status_text = "منقضی شده 🔴"
        if total > 0 and used >= total: status_text = "پایان حجم 🔴"
        total_fmt = format_size(total) if total > 0 else "نامحدود"
        used_fmt = format_size(used)
        remain_fmt = format_size(total - used) if total > 0 else "نامحدود"
        exp_str = datetime.datetime.fromtimestamp(expiry / 1000.0).strftime('%Y-%m-%d') if expiry > 0 else "نامحدود"
    else:
        status_text, total_fmt, used_fmt, remain_fmt, exp_str = "نامشخص ⚪️", "نامشخص", "نامشخص", "نامشخص", "نامشخص"
        
    # زمان بروزرسانی همیشه تغییر می‌کند تا ادیت پیام موفق شود و «بروزرسانی» بازخورد دیداری بدهد
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    header = f"{alert_msg}\n\n" if alert_msg else ""

    # پورتال اشتراک: لینک اختصاصی و زنده‌ی همین سرویس (اگر آدرس عمومی تنظیم شده باشد)
    sub_token = await db.ensure_sub_token(order_id)
    portal_link = subportal.portal_url(sub_token) if sub_token else None

    # وضعیت تمدید خودکار این سفارش (تا دکمه حالت فعلی را نشان دهد)
    auto_on = bool(row['auto_renew']) if 'auto_renew' in row.keys() else False
    auto_plan = row['auto_plan_label'] if 'auto_plan_label' in row.keys() else None
    if auto_on and auto_plan:
        auto_btn_text = f"🤖 تمدید خودکار: {auto_plan} ✅"
    elif auto_on:
        auto_btn_text = "🤖 تمدید خودکار: روشن ✅"
    else:
        auto_btn_text = "🤖 تمدید خودکار: خاموش"
    auto_line = ""
    if auto_on:
        auto_line = f"🤖 تمدید خودکار: **روشن**" + (f" ({auto_plan})" if auto_plan else "") + "\n"
    msg = (
        f"{header}🏷 سرویس: `{email}`\n📡 وضعیت: {status_text}\n🔋 کل: {total_fmt}\n"
        f"📊 مصرف: {used_fmt}\n📉 باقی‌مانده: {remain_fmt}\n📅 انقضا: {exp_str}\n"
        + auto_line +
        f"🕐 آخرین بروزرسانی: {ts}"
    )

    kb = [
        [InlineKeyboardButton("تغییر وضعیت 🔄", callback_data=f'toggle_status_{order_id}'), InlineKeyboardButton("تغییر UUID 🔑", callback_data=f'change_uuid_{order_id}')],
        [InlineKeyboardButton("تغییر نام 📝", callback_data=f'rename_conf_{order_id}'), InlineKeyboardButton("📊 بروزرسانی", callback_data=f'refresh_order_{order_id}')],
        [InlineKeyboardButton("📥 دریافت کانفیگ و QR", callback_data=f'get_conf_{order_id}')],
        [InlineKeyboardButton("♻️ تمدید و تغییر پلن سرویس", callback_data=f'renew_menu_{order_id}')],
        [InlineKeyboardButton(auto_btn_text, callback_data=f'auto_renew_{order_id}')],
        [InlineKeyboardButton("🗑 حذف از لیست", callback_data=f'delorder_{order_id}'), InlineKeyboardButton("🔙 بازگشت", callback_data='back_to_orders')],
    ]
    if portal_link:
        kb.insert(0, [InlineKeyboardButton("🌐 پورتال اشتراک", url=portal_link)])
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except Exception as ex:
        logging.warning("render_order_details edit failed: %s", ex)
        if alert_msg:
            try:
                await query.message.reply_text(alert_msg)
            except Exception:
                pass

# ================= هندلرهای اصلی =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if await _banned_guard(update, user_id):
        return
    if not await _join_ok(context, user_id):
        return await _send_join_prompt(update, (await get_setting('force_channel') or '').strip())
    is_new = await db.is_new_user(user_id)
    await get_user(user_id)
    extra = []
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_") and arg[4:].isdigit():
            if is_new:
                await db.set_referrer(user_id, int(arg[4:]))
        elif arg.lower().startswith("gift_"):
            _ok, msg = await db.redeem_gift_code(user_id, arg[5:])
            extra.append(msg)
    context.user_data['state'] = 'none'
    await update.message.reply_text("به ربات OverWallVpn خوش آمدید.", reply_markup=await get_main_keyboard(user_id))
    for msg in extra:
        await update.message.reply_text(msg, reply_markup=await get_main_keyboard(user_id))

async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.message.from_user.id
    if not _rate_ok(user_id):
        return  # ضدِ اسپم: پیام‌های اضافی بی‌صدا نادیده گرفته می‌شوند
    if await _banned_guard(update, user_id):
        return
    balance, nickname, role = await get_user(user_id)
    state = context.user_data.get('state', 'none')
    admin_status = await is_admin(user_id)

    text = update.message.text

    # لغو سراسری: قبل از هر ماشین وضعیت، تا «لغو» هرگز به‌عنوان ورودیِ ویزارد
    # (پیام همگانی، رسید، ایمپورت و…) پردازش نشود.
    if text == 'لغو ❌':
        context.user_data['state'] = 'none'
        context.user_data.pop('broadcast_draft_msg', None)
        context.user_data.pop('new_panel', None)
        await update.message.reply_text("عملیات لغو شد.", reply_markup=await get_main_keyboard(user_id))
        return

    # 📸 Receipts — قبل از گیتِ متن تا عکس‌ها (که text ندارند) هم برسند
    if state.startswith('waiting_receipt_'):
        if not update.message.photo:
            return await update.message.reply_text("❌ لطفاً فقط **عکس رسید** واریزی را ارسال کنید.")
        amount = state.split('_')[2]
        receipt_id = str(uuid.uuid4())[:8]
        async with db.db_pool.acquire() as conn: await conn.execute("INSERT INTO receipts (id, status) VALUES ($1, 'pending')", receipt_id)

        keyboard = [[InlineKeyboardButton(f"✅ تایید {int(amount):,}", callback_data=f"approve_{user_id}_{amount}_{receipt_id}")], [InlineKeyboardButton("❌ رد", callback_data=f"reject_{user_id}_{receipt_id}")]]
        admins = await get_all_admins()
        sent = False
        for adm in admins:
            if adm == 0: continue
            try:
                await context.bot.send_photo(chat_id=adm, photo=update.message.photo[-1].file_id, caption=f"رسید کاربر: `{user_id}`\nمبلغ: {int(amount):,}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
                sent = True
            except Exception as ex:
                logging.warning("ارسال رسید به ادمین %s ناموفق بود: %s", adm, ex)

        context.user_data['state'] = 'none'
        msg = "✅ رسید در صف بررسی قرار گرفت." if sent else "⚠️ سیستم نتوانست رسید را بفرستد."
        await update.message.reply_text(msg, reply_markup=await get_main_keyboard(user_id))
        return

    # 📢 Broadcast — مرحله‌ی ۱: هیچ‌چیز مستقیم ارسال نمی‌شود؛ اول پیش‌نمایش و تأیید
    # (قبل از گیتِ متن تا عکس/ویس/ویدیو هم قابل همگانی‌شدن باشند)
    if state == 'admin_waiting_broadcast' and admin_status:
        context.user_data['broadcast_draft_msg'] = update.message.message_id
        context.user_data['state'] = 'broadcast_confirm'
        preview = text if text else (update.message.caption or '(رسانه)')
        users_count = len(await db.all_user_ids())
        kb = [
            [InlineKeyboardButton("✅ ارسال عادی (تمیز)", callback_data='broadcast_confirm')],
            [InlineKeyboardButton("💠 ارسال فوروارد (ایموجی پریمیوم سالم می‌ماند)", callback_data='broadcast_confirm_fwd')],
            [InlineKeyboardButton("🗑 دورریز", callback_data='broadcast_cancel')],
        ]
        await update.message.reply_text(
            f"📢 پیش‌نمایش پیام همگانی:\n\n«{preview}»\n\n"
            f"این پیام به {users_count} کاربر ارسال شود؟",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    # گیتِ متن: از اینجا به بعد فقط پیام‌های متنی پردازش می‌شوند
    if not text: return

    # ================= اکانت تست رایگان =================
    if text == '🎁 اکانت تست رایگان':
        context.user_data['state'] = 'none'
        if (await get_setting('test_enabled')) != 'on':
            return await update.message.reply_text("⛔️ اکانت تست در حال حاضر فعال نیست.", reply_markup=await get_main_keyboard(user_id))
        if await db.has_test(user_id) and not admin_status:
            return await update.message.reply_text("❌ شما قبلاً اکانت تست دریافت کرده‌اید.", reply_markup=await get_main_keyboard(user_id))
        inbound_setting = await get_setting('test_inbound_id')
        if not inbound_setting:
            return await update.message.reply_text("⛔️ اکانت تست هنوز توسط مدیریت پیکربندی نشده است.", reply_markup=await get_main_keyboard(user_id))
        test_gb = int(await get_setting('test_gb') or 1)
        test_days = int(await get_setting('test_days') or 1)
        panel_id_setting = await get_setting('test_panel_id')
        panel = await db.get_panel(int(panel_id_setting)) if panel_id_setting else None
        xui, cfg_ip = build_xui(panel)
        opid = panel['id'] if panel else None

        if not cfg_ip:
            return await update.message.reply_text("❌ «IP کانفیگ» پنل اکانت تست تنظیم نشده است. لطفاً به ادمین اطلاع دهید.", reply_markup=await get_main_keyboard(user_id))

        if context.user_data.get('processing'):
            return await update.message.reply_text("⏳ یک عملیات در حال انجام است، صبر کنید.")
        context.user_data['processing'] = True
        try:
            is_logged, _ = await xui.login()
            if not is_logged:
                return await update.message.reply_text("❌ خطا در اتصال به پنل.", reply_markup=await get_main_keyboard(user_id))
            inbound = await xui.get_inbound(int(inbound_setting))
            if not inbound:
                return await update.message.reply_text("❌ اینباند اکانت تست پیدا نشد.", reply_markup=await get_main_keyboard(user_id))
            test_name = f"{user_id}_test_{str(uuid.uuid4())[:4]}"
            config_link, err = await xui.add_client(int(inbound_setting), test_name, test_gb, test_days, cfg_ip, inbound=inbound)
            if not config_link:
                return await update.message.reply_text(f"❌ ساخت اکانت تست ناموفق بود.\n{err}", reply_markup=await get_main_keyboard(user_id))
            await add_order(user_id, config_link, opid, email=test_name, inbound_id=int(inbound_setting))
            if not admin_status:
                await db.mark_test_used(user_id)
            encoded_url = urllib.parse.quote(config_link)
            qr_api_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={encoded_url}&margin=20"
            caption = f"🎁 اکانت تست شما ({test_gb}GB / {test_days} روز):\n\n`{config_link}`{SECURITY_WARNING}"
            try:
                await context.bot.send_photo(chat_id=user_id, photo=qr_api_url, caption=caption, parse_mode='Markdown', reply_markup=await get_main_keyboard(user_id))
            except Exception:
                await update.message.reply_text(caption, parse_mode='Markdown', reply_markup=await get_main_keyboard(user_id))
        finally:
            context.user_data['processing'] = False
        return

    MAIN_BUTTONS = ['محصولات 🛍', 'کیف پول من 💰', 'پشتیبانی 📞', 'سفارشات من 📦', 'شارژ حساب 💳', 'مدیریت ⚙️']
    if text in MAIN_BUTTONS:
        context.user_data['state'] = 'none'
            
        if text == 'مدیریت ⚙️' and admin_status:
            await update.message.reply_text(
                "⚙️ پنل مدیریت اختصاصی:",
                reply_markup=await admin_menu_markup(user_id),
            )


        elif text == 'کیف پول من 💰':
            kb = [
                [InlineKeyboardButton("📜 تاریخچه تراکنش‌ها", callback_data='wallet_history')],
                [InlineKeyboardButton("🎟 کد هدیه", callback_data='wallet_giftcode'), InlineKeyboardButton("🔗 دعوت دوستان", callback_data='wallet_referral')],
            ]
            await update.message.reply_text(f"💰 موجودی فعلی: {balance:,} تومان", reply_markup=InlineKeyboardMarkup(kb))
        elif text == 'پشتیبانی 📞': await update.message.reply_text(f"پشتیبانی: {await get_setting('support_id')}")
        
        elif text == 'شارژ حساب 💳':
            context.user_data['state'] = 'waiting_for_amount'
            await update.message.reply_text("مبلغ را به تومان وارد کنید:", reply_markup=CANCEL_MARKUP)
            
        elif text == 'محصولات 🛍':
            if await get_setting('sales_status') == 'closed': return await update.message.reply_text("⛔️ فروش بسته است.")
            if not await _join_ok(context, user_id):
                return await _send_join_prompt(update, (await get_setting('force_channel') or '').strip())
            plans = await db.list_plans(only_active=True)
            if not plans: return await update.message.reply_text("🛒 هنوز هیچ محصولی اضافه نشده است.")
            quoted = await pricing.quote_plans(user_id, plans, KIND_BUY, admin=is_super_admin(user_id))
            # پلنِ بدونِ قیمت برای سطح کاربر (قیمت صفر/خالی) اصلاً نشان داده نمی‌شود
            kb = [
                [InlineKeyboardButton(pricing.button_text(p, q), callback_data=f"prebuy_{p['id']}")]
                for p, q in quoted
                if q.available
            ]
            await update.message.reply_text(
                products_header(role, quoted), reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown',
            )

        elif text == 'سفارشات من 📦':
            orders = await db.get_orders_by_user(user_id)
            if not orders: return await update.message.reply_text("📦 شما سفارشی ندارید.")
            context.user_data['order_search'] = None
            context.user_data['order_page'] = 0
            wait_msg = await update.message.reply_text("در حال دریافت وضعیت از سرور... ⏳")
            keyboard = await generate_orders_keyboard(orders, page=0, search=None)
            await wait_msg.edit_text("✅ سفارش خود را انتخاب کنید:", reply_markup=keyboard)
        return

    # ================= جستجوی سفارش‌ها =================
    if state == 'waiting_order_search':
        context.user_data['state'] = 'none'
        term = text.strip()
        context.user_data['order_search'] = term
        context.user_data['order_page'] = 0
        orders = await db.get_orders_by_user(user_id)
        keyboard = await generate_orders_keyboard(orders, page=0, search=term)
        await update.message.reply_text(f"🔎 نتایج جستجوی «{term}»:", reply_markup=keyboard)
        return

    # ================= اعمال کد هدیه =================
    if state == 'redeem_gift':
        context.user_data['state'] = 'none'
        ok, msg = await db.redeem_gift_code(user_id, text.strip())
        await update.message.reply_text(msg, reply_markup=await get_main_keyboard(user_id))
        return

    # ================= ماشین وضعیت شارژ حساب =================
    if state == 'waiting_for_amount':
        try:
            amount = clean_num(text)
            if amount < 1000:
                await update.message.reply_text("❌ مبلغ نامعتبر! (حداقل 1000 تومان)")
                return
            # انتخاب روش پرداخت: استارز (اگر نرخ تنظیم شده) و/یا کارت
            stars_rate = (await get_setting('stars_toman_rate') or '').strip()
            card = (await get_setting('card_number') or '').strip()
            kb = []
            if stars_rate and stars_rate.isdigit() and int(stars_rate) > 0:
                rate = int(stars_rate)
                kb.append([InlineKeyboardButton(
                    f"⭐️ پرداخت آنلاین با تلگرام (≈{amount // rate + (1 if amount % rate else 0)} استارز)",
                    callback_data=f"stars_pay_{amount}")])
            if card:
                kb.append([InlineKeyboardButton("💳 واریز کارت به کارت + رسید", callback_data=f"card_pay_{amount}")])
            if not kb:
                context.user_data['state'] = 'none'
                return await update.message.reply_text("⛔️ هیچ روش پرداختی تنظیم نشده است. به پشتیبانی اطلاع دهید.", reply_markup=await get_main_keyboard(user_id))
            context.user_data['state'] = 'choosing_pay_method'
            await update.message.reply_text(
                f"💰 مبلغ شارژ: **{amount:,} تومان**\nروش پرداخت را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown',
            )
        except ValueError:
            await update.message.reply_text("❌ لطفاً مبلغ را فقط به صورت عدد (بدون حروف) وارد کنید:")
        return

    # ================= ماشین وضعیت ایمپورت گروهی کانفیگ =================
    if state == 'admin_waiting_import' and admin_status:
        parts = text.split()
        if len(parts) >= 2 and parts[0].isdigit():
            target_user_id = int(parts[0])
            # هر توکن می‌تواند نام کانفیگ (email) یا یک لینک کامل (vless/vmess/trojan) باشد
            wanted = []
            for tok in parts[1:]:
                tok = tok.strip()
                em = email_from_link(tok) if "://" in tok else tok
                if em:
                    wanted.append(em)

            await update.message.reply_text(f"در حال جستجو و ایمپورت {len(wanted)} کانفیگ در سرور... ⏳")
            # کانفیگ در تمام پنل‌های موجود جستجو می‌شود
            panels = await db.get_panels()
            panel_clients = []  # (panel_row_or_None, xui, config_ip, is_logged)
            if panels:
                for prow in panels:
                    x, ip = build_xui(prow)
                    ok, _ = await x.login()
                    panel_clients.append((prow, x, ip, ok))
            else:
                x, ip = build_xui(None)
                ok, _ = await x.login()
                panel_clients.append((None, x, ip, ok))

            if not any(pc[3] for pc in panel_clients):
                await update.message.reply_text("❌ خطا در اتصال به پنل X-UI.", reply_markup=await get_main_keyboard(user_id))
            else:
                await get_user(target_user_id)
                # ایمیل‌های فعلی کاربر برای جلوگیری از ایمپورت تکراری
                rows = await db.get_orders_by_user(target_user_id)
                existing_emails = set()
                for r in rows:
                    em = order_email(r)
                    if em:
                        existing_emails.add(em.strip().lower())

                # پلن‌های هر پنل برای تشخیص خودکار پلنِ سفارش ایمپورتی (بر اساس حجم)
                plans_by_panel = {}
                for prow, _x, _ip, ok in panel_clients:
                    if ok and prow:
                        plans_by_panel[prow['id']] = await db.list_plans(panel_id=prow['id'], only_active=True)

                success_count = 0
                skipped = []        # ایمیل تکراری برای همین کاربر
                owned_by_other = []  # ایمیل قبلاً به کاربر دیگری تعلق دارد
                failed_emails = []
                per_panel = {}

                for email in wanted:
                    if email.lower() in existing_emails:
                        skipped.append(email)
                        continue
                    # چک سراسری: این email ممکن است قبلاً به کاربر دیگری تعلق گرفته باشد
                    owner = await db.find_order_by_email(email)
                    if owner and owner['user_id'] != target_user_id:
                        owned_by_other.append(f"{email} (کاربر {owner['user_id']})")
                        existing_emails.add(email.lower())
                        continue
                    found = False
                    for prow, x, ip, ok in panel_clients:
                        if not ok:
                            continue
                        inbound_id, port, client_dict = await x.get_client_exact_info(email)
                        if client_dict:
                            real_email_from_panel = client_dict.get('email', email)
                            pid = prow['id'] if prow else None
                            pname = prow['name'] if prow else "پیش‌فرض"
                            # لینک را مطابق پروتکل/تنظیماتِ همان اینباند می‌سازیم
                            inbound = await x.get_inbound(inbound_id)
                            if inbound:
                                config_link = build_share_link(inbound, client_dict, ip)
                            else:
                                config_link = build_vless_link(client_dict.get('id', ''), ip, port, real_email_from_panel)
                            # تشخیص خودکار پلن: حجم کلاینت با پلن‌های همان پنل مقایسه می‌شود
                            matched_plan_id = None
                            total_gb = int(client_dict.get('totalGB') or 0) // (1024 ** 3)
                            if pid and total_gb > 0:
                                for p in plans_by_panel.get(pid, []):
                                    if int(p['gb']) == total_gb:
                                        matched_plan_id = p['id']
                                        break
                            await add_order(
                                target_user_id, config_link, pid, email=real_email_from_panel,
                                inbound_id=inbound_id, plan_id=matched_plan_id,
                                user_role=(await db.get_user(target_user_id))[2],
                            )
                            existing_emails.add(real_email_from_panel.strip().lower())
                            per_panel[pname] = per_panel.get(pname, 0) + 1
                            success_count += 1
                            found = True
                            break
                    if not found:
                        failed_emails.append(email)

                context.user_data['state'] = 'none'
                report = f"✅ عملیات ایمپورت پایان یافت.\n\n📦 موفق: {success_count}\n"
                if per_panel:
                    report += "🖥 بر اساس پنل: " + "، ".join([f"{k}: {v}" for k, v in per_panel.items()]) + "\n"
                if skipped:
                    report += f"↩️ تکراری برای همین کاربر (رد شد): {len(skipped)}\n"
                if owned_by_other:
                    report += f"🔒 قبلاً به کاربر دیگری تعلق دارد: {len(owned_by_other)}\n" + "\n".join(f"   • {x}" for x in owned_by_other[:10]) + "\n"
                if failed_emails:
                    report += f"❌ روی هیچ پنلی یافت نشد: {', '.join(failed_emails)}"
                if success_count:
                    report += "\n💡 حجم کلاینت‌ها با پلن‌های هم‌پنل مقایسه شد و پلنِ هم‌خوان به‌صورت خودکار ثبت شد."
                await update.message.reply_text(report, reply_markup=await get_main_keyboard(user_id))
                # کاربر مقصد را از ایمپورت جدید خبردار کن
                if success_count:
                    try:
                        await context.bot.send_message(
                            target_user_id,
                            f"📥 {success_count} سرویس جدید توسط پشتیبانی برای شما ثبت شد.\n"
                            f"از «سفارشات من 📦» ببینیدشان.",
                        )
                    except Exception as ex:
                        logging.warning("اطلاع ایمپورت به کاربر %s ناموفق: %s", target_user_id, ex)
        else:
            await update.message.reply_text("❌ فرمت اشتباه! اول آیدی عددی کاربر، سپس نام کانفیگ‌ها یا لینک‌های کامل.")
        return

    # ================= ماشین وضعیت ویرایش پلن (تعاملی) =================
    if state == 'admin_waiting_edit_plan_id' and admin_status:
        if text.isdigit():
            async with db.db_pool.acquire() as conn: p = await conn.fetchrow("SELECT * FROM plans WHERE id=$1", int(text))
            if not p:
                return await update.message.reply_text("❌ پلنی با این آیدی یافت نشد.")
            context.user_data['state'] = 'none'
            await update.message.reply_text(await plan_edit_text(p), reply_markup=plan_edit_markup(p['id']), parse_mode='Markdown')
        else: await update.message.reply_text("❌ فقط عدد بفرستید.")
        return

    if admin_status and state.startswith('peset_'):
        parts = state.split('_')
        field, plan_id = parts[1], int(parts[2])
        meta = PLAN_FIELD_MAP.get(field)
        if not meta:
            context.user_data['state'] = 'none'
            return
        _key, label, col, isnum = meta
        if isnum:
            try:
                val = clean_num(text)
            except ValueError:
                return await update.message.reply_text("❌ فقط عدد بفرستید.")
            if field == 'panel':
                panels = await db.get_panels()
                if val not in [pr['id'] for pr in panels]:
                    return await update.message.reply_text("❌ آیدی پنل نامعتبر است. دوباره بفرستید:")
        else:
            val = text.strip()
        async with db.db_pool.acquire() as conn:
            # col از لیست ثابت PLAN_FIELDS می‌آید؛ امکان تزریق SQL وجود ندارد
            await conn.execute(f"UPDATE plans SET {col} = $1 WHERE id = $2", val, plan_id)
            p = await conn.fetchrow("SELECT * FROM plans WHERE id=$1", plan_id)
        context.user_data['state'] = 'none'
        if not p:
            return await update.message.reply_text("❌ پلن یافت نشد.", reply_markup=await get_main_keyboard(user_id))
        await update.message.reply_text(f"✅ «{label}» بروزرسانی شد.")
        await update.message.reply_text(await plan_edit_text(p), reply_markup=plan_edit_markup(p['id']), parse_mode='Markdown')
        return

    # ================= ماشین وضعیت افزودن پنل =================
    if admin_status and state.startswith('panel_add_'):
        step = state.split('_')[2]
        if 'new_panel' not in context.user_data: context.user_data['new_panel'] = {}
        np = context.user_data['new_panel']
        if step == 'name':
            np['name'] = text.strip()
            context.user_data['state'] = 'panel_add_url'
            await update.message.reply_text("🔗 آدرس کامل پنل (URL) را وارد کنید:\nمثال: `https://example.com:54321/abcd`", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
        elif step == 'url':
            np['url'] = text.strip()
            context.user_data['state'] = 'panel_add_user'
            await update.message.reply_text("👤 نام کاربری پنل را وارد کنید:", reply_markup=CANCEL_MARKUP)
        elif step == 'user':
            np['username'] = text.strip()
            context.user_data['state'] = 'panel_add_pass'
            await update.message.reply_text("🔑 رمز عبور پنل را وارد کنید:", reply_markup=CANCEL_MARKUP)
        elif step == 'pass':
            np['password'] = text.strip()
            context.user_data['state'] = 'panel_add_ip'
            await update.message.reply_text("🌐 آی‌پی یا دامنه‌ای که در لینک کانفیگ (sni/host) استفاده شود را وارد کنید:", reply_markup=CANCEL_MARKUP)
        elif step == 'ip':
            np['config_ip'] = text.strip()
            context.user_data['state'] = 'panel_add_sub'
            await update.message.reply_text("🔗 لینک ساب (Subscription URL) را وارد کنید.\nاگر ندارید، `-` بفرستید.\nمثال: `https://domain.com:2096/sub`", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
        elif step == 'sub':
            sub_url = '' if text.strip() in ('-', '0', 'خیر', 'ندارد') else text.strip()
            await db.add_panel(np['name'], np['url'], np['username'], np['password'], np['config_ip'], sub_url)
            context.user_data['state'] = 'none'
            await update.message.reply_text(f"✅ پنل «{np['name']}» با موفقیت اضافه شد.", reply_markup=await get_main_keyboard(user_id))
        return

    # ================= ماشین وضعیت برای افزودن پلن =================
    if admin_status and state.startswith('plan_add_'):
        step = state.split('_')[2]
        if 'new_plan' not in context.user_data: context.user_data['new_plan'] = {}
        try:
            if step == 'name':
                context.user_data['new_plan']['name'] = text
                context.user_data['state'] = 'plan_add_gb'
                await update.message.reply_text("🔢 **حجم** پلن را به گیگابایت وارد کنید (فقط عدد):", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
            elif step == 'gb':
                context.user_data['new_plan']['gb'] = clean_num(text)
                context.user_data['state'] = 'plan_add_duration'
                await update.message.reply_text("📅 **مدت‌زمان** پلن را به روز وارد کنید (مثلاً برای یک‌ماهه: 30):", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
            elif step == 'duration':
                context.user_data['new_plan']['duration_days'] = clean_num(text)
                context.user_data['state'] = 'plan_add_price'
                await update.message.reply_text("💰 **قیمت عادی** را به تومان وارد کنید:", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
            elif step == 'price':
                context.user_data['new_plan']['price'] = clean_num(text)
                context.user_data['state'] = 'plan_add_vipprice'
                await update.message.reply_text("💎 **قیمت VIP (تکی)** را به تومان وارد کنید:", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
            elif step == 'vipprice':
                context.user_data['new_plan']['vip_price'] = clean_num(text)
                context.user_data['state'] = 'plan_add_inbound'
                await update.message.reply_text("⚙️ **آیدی اینباند (Inbound ID)** این پلن در پنل سنایی چند است؟ (مثلاً 1 یا 2)", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
            elif step == 'inbound':
                inbound = clean_num(text)
                p = context.user_data['new_plan']
                p['inbound_id'] = inbound
                panels = await db.get_panels()
                if len(panels) <= 1:
                    chosen_pid = panels[0]['id'] if panels else None
                    async with db.db_pool.acquire() as conn:
                        await conn.execute("INSERT INTO plans (name, gb, price, vip_price, inbound_id, duration_days, panel_id) VALUES ($1, $2, $3, $4, $5, $6, $7)", p['name'], p['gb'], p['price'], p['vip_price'], inbound, p.get('duration_days', 30), chosen_pid)
                    context.user_data['state'] = 'none'
                    await update.message.reply_text(f"✅ **پلن `{p['name']}` با موفقیت ذخیره شد!**", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
                else:
                    context.user_data['state'] = 'plan_add_panel'
                    lst = "\n".join([f"🆔 {pr['id']} - {pr['name']}" for pr in panels])
                    await update.message.reply_text(f"🖥 این پلن روی کدام پنل ساخته شود؟ آیدی پنل را بفرستید:\n\n{lst}", reply_markup=CANCEL_MARKUP)
            elif step == 'panel':
                chosen_pid = clean_num(text)
                panels = await db.get_panels()
                if chosen_pid not in [pr['id'] for pr in panels]:
                    return await update.message.reply_text("❌ آیدی پنل نامعتبر است. دوباره بفرستید:")
                p = context.user_data['new_plan']
                async with db.db_pool.acquire() as conn:
                    await conn.execute("INSERT INTO plans (name, gb, price, vip_price, inbound_id, duration_days, panel_id) VALUES ($1, $2, $3, $4, $5, $6, $7)", p['name'], p['gb'], p['price'], p['vip_price'], p['inbound_id'], p.get('duration_days', 30), chosen_pid)
                context.user_data['state'] = 'none'
                await update.message.reply_text(f"✅ **پلن `{p['name']}` با موفقیت ذخیره شد!**", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
        except ValueError: await update.message.reply_text("❌ لطفاً فقط عدد وارد کنید!")
        return

    # ================= تنظیم پارامترهای اکانت تست =================
    if admin_status and state in ('test_set_gb', 'test_set_days', 'test_set_panel', 'test_set_inbound'):
        if not text.isdigit():
            return await update.message.reply_text("❌ فقط عدد بفرستید.")
        keymap = {'test_set_gb': 'test_gb', 'test_set_days': 'test_days', 'test_set_panel': 'test_panel_id', 'test_set_inbound': 'test_inbound_id'}
        if state == 'test_set_panel':
            panels = await db.get_panels()
            if int(text) not in [pr['id'] for pr in panels]:
                return await update.message.reply_text("❌ آیدی پنل نامعتبر است. دوباره بفرستید:")
        await update_setting(keymap[state], text)
        context.user_data['state'] = 'none'
        await update.message.reply_text("✅ ثبت شد. از منوی «اکانت تست 🎁» می‌توانید ادامه دهید.", reply_markup=await get_main_keyboard(user_id))
        return

    # ================= کدهای هدیه (ادمین) =================
    if admin_status and state.startswith('gift_add_'):
        step = state.split('_')[2]
        if 'new_gift' not in context.user_data: context.user_data['new_gift'] = {}
        g = context.user_data['new_gift']
        if step == 'code':
            raw = text.strip()
            if not raw:
                g['code'] = rewards.generate_gift_code()
            else:
                code = rewards.normalize_gift_code(raw)
                if not rewards.is_valid_gift_code(code):
                    return await update.message.reply_text("❌ کد باید ۳ تا ۳۲ حرف انگلیسی یا عدد باشد. خالی بگذارید تا تصادفی ساخته شود.")
                g['code'] = code
            context.user_data['state'] = 'gift_add_amount'
            await update.message.reply_text(f"💰 مبلغ هدیه (تومان) را بفرستید:\nکد: `{g['code']}`", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
        elif step == 'amount':
            try:
                g['amount'] = clean_num(text)
            except ValueError:
                return await update.message.reply_text("❌ فقط عدد بفرستید.")
            if g['amount'] <= 0:
                return await update.message.reply_text("❌ مبلغ باید بزرگ‌تر از صفر باشد.")
            context.user_data['state'] = 'gift_add_uses'
            await update.message.reply_text("🔢 حداکثر تعداد دفعات قابل‌استفاده را بفرستید:", reply_markup=CANCEL_MARKUP)
        elif step == 'uses':
            try:
                max_uses = max(1, clean_num(text))
            except ValueError:
                return await update.message.reply_text("❌ فقط عدد بفرستید.")
            saved = await db.add_gift_code(g['code'], g['amount'], max_uses)
            context.user_data['state'] = 'none'
            if not saved:
                await update.message.reply_text("❌ ساخت کد ناموفق بود.", reply_markup=await get_main_keyboard(user_id))
            else:
                await update.message.reply_text(f"✅ کد هدیه `{saved}` ساخته شد.", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
        return

    if state == 'admin_waiting_del_gift' and admin_status:
        deleted = await db.delete_gift_code(text.strip())
        context.user_data['state'] = 'none'
        await update.message.reply_text("✅ کد حذف شد." if deleted else "❌ کدی با این متن پیدا نشد.", reply_markup=await get_main_keyboard(user_id))
        return

    if state == 'admin_waiting_toggle_gift' and admin_status:
        state_flag = await db.toggle_gift_code_active(text.strip())
        context.user_data['state'] = 'none'
        if state_flag is None:
            await update.message.reply_text("❌ کدی با این متن پیدا نشد.", reply_markup=await get_main_keyboard(user_id))
        else:
            await update.message.reply_text("🟢 کد فعال شد." if state_flag else "⏸ کد غیرفعال شد.", reply_markup=await get_main_keyboard(user_id))
        return

    if state == 'admin_waiting_refbonus' and admin_status:
        try:
            await update_setting('ref_bonus', clean_num(text))
        except ValueError:
            return await update.message.reply_text("❌ فقط عدد بفرستید.")
        context.user_data['state'] = 'none'
        await update.message.reply_text("✅ پاداش معرف تنظیم شد.", reply_markup=await get_main_keyboard(user_id))
        return

    if state == 'admin_waiting_invitee_bonus' and admin_status:
        try:
            await update_setting('ref_invitee_bonus', clean_num(text))
        except ValueError:
            return await update.message.reply_text("❌ فقط عدد بفرستید.")
        context.user_data['state'] = 'none'
        await update.message.reply_text("✅ هدیه دعوت‌شده تنظیم شد.", reply_markup=await get_main_keyboard(user_id))
        return

    if state == 'admin_waiting_ref_min' and admin_status:
        try:
            await update_setting('ref_min_topup', clean_num(text))
        except ValueError:
            return await update.message.reply_text("❌ فقط عدد بفرستید.")
        context.user_data['state'] = 'none'
        await update.message.reply_text("✅ حداقل شارژ دعوت تنظیم شد.", reply_markup=await get_main_keyboard(user_id))
        return

    if state == 'admin_waiting_notify' and admin_status:
        try:
            await update_setting('notify_days', max(1, clean_num(text)))
        except ValueError:
            return await update.message.reply_text("❌ فقط عدد بفرستید.")
        context.user_data['state'] = 'none'
        await update.message.reply_text("✅ آستانه‌ی هشدار انقضا تنظیم شد.", reply_markup=await get_main_keyboard(user_id))
        return

    # ================= ویرایش/انتقال پنل =================
    if state == 'admin_waiting_edit_panel_id' and admin_status:
        if text.isdigit():
            p = await db.get_panel(int(text))
            if not p:
                return await update.message.reply_text("❌ پنلی با این آیدی یافت نشد.")
            context.user_data['state'] = 'none'
            await update.message.reply_text(panel_edit_text(p), reply_markup=panel_edit_markup(p['id']), parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ فقط عدد بفرستید.")
        return

    if admin_status and state.startswith('paneset_'):
        parts = state.split('_')
        field, panel_id = parts[1], int(parts[2])
        meta = PANEL_FIELD_MAP.get(field)
        if not meta:
            context.user_data['state'] = 'none'
            return
        await db.update_panel_field(panel_id, meta[2], text.strip())
        p = await db.get_panel(panel_id)
        context.user_data['state'] = 'none'
        if not p:
            return await update.message.reply_text("❌ پنل یافت نشد.", reply_markup=await get_main_keyboard(user_id))
        await update.message.reply_text(f"✅ «{meta[1]}» بروزرسانی شد.")
        await update.message.reply_text(panel_edit_text(p), reply_markup=panel_edit_markup(p['id']), parse_mode='Markdown')
        return

    if state == 'admin_waiting_move_panel' and admin_status:
        parts = text.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            src, dst = int(parts[0]), int(parts[1])
            if not await db.get_panel(dst):
                return await update.message.reply_text("❌ پنل مقصد وجود ندارد.")
            n_plans, n_orders = await db.move_panel_assets(src, dst)
            context.user_data['state'] = 'none'
            await update.message.reply_text(f"✅ انتقال انجام شد.\n📦 سفارش‌ها: {n_orders}\n🛒 پلن‌ها: {n_plans}", reply_markup=await get_main_keyboard(user_id))
        else:
            await update.message.reply_text("❌ فرمت اشتباه! مثال: `1 2`")
        return

    # ---------- ویزارد مهاجرت اینباند ----------
    if state == 'admin_waiting_migrate_inbound' and admin_status:
        parts = text.split()
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            context.user_data['state'] = 'none'
            return await update.message.reply_text("❌ فرمت اشتباه. مثال: `1 613 617`", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
        panel_id, src_ib, dst_ib = (int(p) for p in parts)
        context.user_data['state'] = 'none'
        await update.message.reply_text("🔄 مهاجرت شروع شد... (بسته به تعداد کلاینت‌ها ممکن است کمی طول بکشد)")
        moved, failed, missing = await migrate_inbound_clients(panel_id, src_ib, dst_ib)
        msg = f"✅ مهاجرت تمام شد.\n🟢 منتقل‌شده: {moved}\n🔴 ناموفق: {failed}\n⚪️ بدونِ کلاینت روی مبدأ: {missing}"
        if failed:
            msg += "\n\n⚠️ ناموفق‌ها به‌صورت خودکار روی مبدأ باقی ماندند؛ لینک‌ها سالم‌اند."
        await update.message.reply_text(msg, reply_markup=await get_main_keyboard(user_id))
        return

    # ---------- بن/آزادسازی کاربر ----------
    if state == 'admin_waiting_ban_toggle' and admin_status:
        context.user_data['state'] = 'none'
        if not text.lstrip('-').isdigit():
            return await update.message.reply_text("❌ فقط آیدی عددی بفرستید.", reply_markup=await get_main_keyboard(user_id))
        uid = int(text)
        if uid == user_id or uid == SUPER_ADMIN_ID:
            return await update.message.reply_text("❌ نمی‌توانید خودتان یا مدیر کل را بن کنید.", reply_markup=await get_main_keyboard(user_id))
        if await db.is_user_banned(uid):
            await db.set_user_banned(uid, False)
            await update.message.reply_text(f"✅ کاربر {uid} **آزاد** شد.", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
        else:
            await db.set_user_banned(uid, True)
            await update.message.reply_text(f"🚫 کاربر {uid} **بن** شد. (تاریخچه و سفارش‌هایش محفوظ ماند)", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
        return

    # ---------- اجبار عضویت کانال ----------
    if state == 'admin_waiting_force_channel' and admin_status:
        context.user_data['state'] = 'none'
        if text.strip() in ('خاموش', 'off', '-'):
            await update_setting('force_channel', '')
            await update.message.reply_text("🔴 اجبار عضویت **خاموش** شد.", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
        elif re.match(r"^@?[A-Za-z0-9_]{4,}$", text.strip()):
            await update_setting('force_channel', text.strip().lstrip('@'))
            await update.message.reply_text(f"🟢 اجبار عضویت روی `{text.strip()}` **روشن** شد.\nربات باید در آن کانال ادمین باشد.", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ فرمت یوزرنیم اشتباه است. مثال: `@mychannel` یا بفرستید: خاموش", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
        return

    # بقیه هندلرهای ادمین
    if state == 'admin_waiting_del_panel' and admin_status:
        if text.isdigit():
            await db.delete_panel(int(text))
            context.user_data['state'] = 'none'
            await update.message.reply_text("✅ پنل حذف شد. (توجه: پلن‌ها و سفارش‌های متصل به این پنل باید به پنل دیگری منتقل شوند.)", reply_markup=await get_main_keyboard(user_id))
        else:
            await update.message.reply_text("❌ فقط عدد بفرستید.")
        return

    if state == 'admin_waiting_del_plan' and admin_status:
        if text.isdigit():
            async with db.db_pool.acquire() as conn: await conn.execute("DELETE FROM plans WHERE id = $1", int(text))
            context.user_data['state'] = 'none'
            await update.message.reply_text("✅ پلن حذف شد.", reply_markup=await get_main_keyboard(user_id))
            
    elif state == 'admin_waiting_vip_id' and admin_status:
        parts = text.split(maxsplit=1)
        if len(parts) >= 1 and parts[0].isdigit():
            uid = int(parts[0])
            name = parts[1] if len(parts) > 1 else 'بدون نام'
            async with db.db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, nickname, role) VALUES ($1, $2, 'vip') ON CONFLICT (user_id) DO UPDATE SET role = 'vip', nickname = $2", uid, name)
            context.user_data['state'] = 'none'
            await update.message.reply_text(
                f"✅ کاربر {name} VIP شد.\n💎 قیمت VIP برایش فعال است.",
                reply_markup=await get_main_keyboard(user_id),
            )
        else: await update.message.reply_text("❌ فرمت اشتباه! مثال: `123456789 علی`")

    elif state == 'admin_waiting_toggle_plan' and admin_status:
        if text.isdigit():
            new_state = await db.toggle_plan_active(int(text))
            context.user_data['state'] = 'none'
            if new_state is None:
                await update.message.reply_text("❌ پلنی با این آیدی یافت نشد.", reply_markup=await get_main_keyboard(user_id))
            else:
                await update.message.reply_text(
                    f"{'🟢 پلن فعال شد.' if new_state else '🔴 پلن غیرفعال شد (از منوی فروش حذف می‌شود).'}",
                    reply_markup=await get_main_keyboard(user_id),
                )
        else:
            await update.message.reply_text("❌ فقط عدد بفرستید.")

    elif state == 'admin_waiting_toggle_offer' and admin_status:
        if text.isdigit():
            new_state = await db.toggle_offer_active(int(text))
            context.user_data['state'] = 'none'
            if new_state is None:
                await update.message.reply_text("❌ آفری با این آیدی یافت نشد.", reply_markup=await get_main_keyboard(user_id))
            else:
                await update.message.reply_text(
                    f"{'🟢 آفر فعال شد.' if new_state else '🔴 آفر غیرفعال شد.'}",
                    reply_markup=await get_main_keyboard(user_id),
                )
        else:
            await update.message.reply_text("❌ فقط عدد بفرستید.")

    elif state == 'admin_waiting_reseller_id' and admin_status:
        parts = text.split(maxsplit=1)
        if parts and parts[0].isdigit():
            uid = int(parts[0])
            name = parts[1] if len(parts) > 1 else 'بدون نام'
            async with db.db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO users (user_id, nickname, role) VALUES ($1, $2, 'reseller') ON CONFLICT (user_id) DO UPDATE SET role = 'reseller', nickname = $2",
                    uid, name,
                )
            context.user_data['state'] = 'none'
            await update.message.reply_text(
                f"🤝 کاربر {name} نماینده شد.\n🤝 قیمت همکاری برایش فعال است.",
                reply_markup=await get_main_keyboard(user_id),
            )
        else:
            await update.message.reply_text("❌ فرمت اشتباه! مثال: `123456789 علی`")

    elif state == 'admin_waiting_rem_reseller' and admin_status:
        if text.isdigit():
            await db.set_user_role(int(text), pricing.NORMAL)
            context.user_data['state'] = 'none'
            await update.message.reply_text(f"🔻 کاربر {text} به سطح عادی برگشت.", reply_markup=await get_main_keyboard(user_id))
        else:
            await update.message.reply_text("❌ فقط عدد بفرستید.")

    elif state == 'admin_waiting_rem_vip' and admin_status:
        if text.isdigit():
            async with db.db_pool.acquire() as conn: await conn.execute("UPDATE users SET role = 'normal' WHERE user_id = $1", int(text))
            context.user_data['state'] = 'none'
            await update.message.reply_text(f"🔴 کاربر {text} عادی شد.", reply_markup=await get_main_keyboard(user_id))

    elif state == 'admin_waiting_card' and admin_status:
        await update_setting('card_number', text)
        context.user_data['state'] = 'none'
        await update.message.reply_text("✅ شماره کارت تغییر کرد.", reply_markup=await get_main_keyboard(user_id))

    elif state == 'admin_waiting_support' and admin_status:
        await update_setting('support_id', text)
        context.user_data['state'] = 'none'
        await update.message.reply_text("✅ آیدی پشتیبانی تغییر کرد.", reply_markup=await get_main_keyboard(user_id))

    elif state == 'superadmin_waiting_add_admin' and user_id == SUPER_ADMIN_ID:
        parts = text.split(maxsplit=1)
        if len(parts) >= 1 and parts[0].isdigit():
            uid = int(parts[0])
            name = parts[1] if len(parts) > 1 else 'بدون نام'
            async with db.db_pool.acquire() as conn:
                await conn.execute("INSERT INTO admins (user_id, name) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET name = $2", uid, name)
            context.user_data['state'] = 'none'
            await update.message.reply_text(f"✅ ادمین {name} اضافه شد.", reply_markup=await get_main_keyboard(user_id))

    elif state == 'superadmin_waiting_rem_admin' and user_id == SUPER_ADMIN_ID:
        if text.isdigit():
            async with db.db_pool.acquire() as conn: await conn.execute("DELETE FROM admins WHERE user_id = $1", int(text))
            context.user_data['state'] = 'none'
            await update.message.reply_text(f"✅ ادمین {text} حذف شد.", reply_markup=await get_main_keyboard(user_id))

    # هندلرهای کاربری
    elif state.startswith('waiting_rename_'):
        order_id = state.split('_')[2]
        if not admin_status and not await order_belongs_to(order_id, user_id):
            context.user_data['state'] = 'none'
            return await update.message.reply_text("⛔️ این سفارش متعلق به شما نیست.", reply_markup=await get_main_keyboard(user_id))
        if re.match(r"^[A-Za-z0-9_-]+$", text) and len(text) < 20:
            row, old_email = await load_order_service(order_id)
            if not row:
                context.user_data['state'] = 'none'
                return await update.message.reply_text("❌ سفارش یافت نشد.", reply_markup=await get_main_keyboard(user_id))
            if not old_email:
                context.user_data['state'] = 'none'
                return await update.message.reply_text("❌ نام فعلی این سرویس قابل تشخیص نیست؛ به پشتیبانی اطلاع دهید.", reply_markup=await get_main_keyboard(user_id))

            await update.message.reply_text("در حال اعمال تغییرات در سرور... ⏳")
            xui, cfg_ip = await get_order_xui(order_id)
            is_logged, _ = await xui.login()
            if is_logged:
                inbound_id, _port, client_dict = await xui.get_client_exact_info(old_email)
                if client_dict:
                    new_email = await unique_service_name(xui, text)
                    client_dict['email'] = new_email
                    # subId معمولاً برابر ایمیل است؛ اگر بود آن را هم هم‌نام می‌کنیم تا لینک ساب نشکند
                    if client_dict.get('subId') == old_email:
                        client_dict['subId'] = new_email
                    if await xui.update_client(inbound_id, client_key(client_dict), client_dict):
                        # لینک از روی اینباند بازسازی می‌شود (در vmess نام داخل Base64 است)
                        await refresh_order_link(order_id, xui, inbound_id, client_dict, cfg_ip)
                        await update.message.reply_text(f"✅ نام کانفیگ با موفقیت به `{new_email}` تغییر کرد!", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
                        context.user_data['state'] = 'none'
                    else: await update.message.reply_text("❌ سرور درخواست تغییر نام را رد کرد.")
                else: await update.message.reply_text("❌ کانفیگ در سرور یافت نشد.")
            else: await update.message.reply_text("❌ خطا در اتصال به پنل X-UI.")
        else: await update.message.reply_text("❌ نامعتبر! فقط حروف انگلیسی و اعداد.")

    elif state.startswith('waiting_for_nick_'):
        plan_id = int(state.split('_')[-1])
        if re.match(r"^[A-Za-z0-9_-]+$", text) and len(text) < 20:
            plan = await db.get_plan(plan_id)
            if not plan: return
            if not plan['is_active']:
                context.user_data['state'] = 'none'
                return await update.message.reply_text("⛔️ این پلن در حال حاضر غیرفعال است.", reply_markup=await get_main_keyboard(user_id))

            quote = await pricing.quote_for_user(user_id, plan, KIND_BUY, admin=is_super_admin(user_id))
            if not quote.available:
                context.user_data['state'] = 'none'
                return await update.message.reply_text(pricing.UNAVAILABLE_TEXT, reply_markup=await get_main_keyboard(user_id))
            final_name = text
            # قیمت لحظه‌ی نمایش نگه داشته می‌شود تا مبلغ تاییدشده همان چیزی باشد که کسر می‌شود
            remember_quote(context, plan_id, KIND_BUY, quote)
            body, _spec = pricing.quote_lines(plan, quote)
            kb = [[InlineKeyboardButton(f"✅ تایید و پرداخت {quote.price:,} تومان", callback_data=f"confirm_buy_{plan_id}_{final_name}")]]
            await update.message.reply_text(
                f"🏷 نام سرویس: `{final_name}`\n\n{body}\n\n💳 موجودی شما: {balance:,} تومان",
                reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown',
            )
            context.user_data['state'] = 'none'
        else: await update.message.reply_text("❌ نامعتبر! فقط از حروف انگلیسی و اعداد استفاده کنید:")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پوشش‌دهنده‌ی هندلر دکمه‌ها.

    پاسخ به query عمداً *بعد از* اجرای شاخه‌ها داده می‌شود؛ چون هر query فقط یک‌بار
    قابل پاسخ‌دهی است و پاسخ زودهنگام باعث می‌شد هشدارهای واقعی (مثل «موجودی کافی
    نیست») هرگز به کاربر نمایش داده نشوند.
    """
    query = update.callback_query
    if not _rate_ok(query.from_user.id):
        await safe_answer(query, "⏳ کمی آرام‌تر! لطفاً چند لحظه صبر کنید.")
        return
    if await _banned_guard(query, query.from_user.id):
        return
    try:
        await handle_callback(update, context)
    finally:
        # اگر شاخه‌ای خودش پاسخ نداده باشد، اسپینر لودینگ تلگرام پاک می‌شود.
        await safe_answer(query)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    admin_status = await is_admin(user_id)
    balance, nickname, role = await get_user(user_id)

    if data == 'ignore': return

    if data == 'admin_toggle_sales' and admin_status:
        current = await get_setting('sales_status')
        await update_setting('sales_status', 'closed' if current == 'open' else 'open')
        await query.edit_message_reply_markup(reply_markup=await admin_menu_markup(user_id))
    
    elif data == 'admin_manage_plans' and admin_status:
        kb = [
            [InlineKeyboardButton("➕ افزودن پلن جدید", callback_data='admin_add_plan'), InlineKeyboardButton("🗑 حذف پلن", callback_data='admin_del_plan')],
            [InlineKeyboardButton("✏️ ویرایش پلن", callback_data='admin_edit_plan')]
        ]
        async with db.db_pool.acquire() as conn: plans = await conn.fetch("SELECT * FROM plans ORDER BY id ASC")
        msg = "📋 **لیست محصولات فعلی:**\n\n"
        for p in plans: msg += f"🆔 `ID:{p['id']}` | {p['name']} | عادی: {money(p['price'])} | مدت: {p['duration_days']}روز | اینباند: {p['inbound_id']}\n"
        await query.edit_message_text(msg if plans else "محصولی یافت نشد.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data == 'admin_edit_plan' and admin_status:
        context.user_data['state'] = 'admin_waiting_edit_plan_id'
        await query.message.reply_text("آیدی (ID) پلنی که می‌خواهید ویرایش کنید را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'pe_done':
        try: await query.edit_message_text("✅ ویرایش پلن به پایان رسید.")
        except Exception: pass

    elif data.startswith('pef_') and admin_status:
        parts = data.split('_')
        field, plan_id = parts[1], parts[2]
        meta = PLAN_FIELD_MAP.get(field)
        if not meta: return
        label = meta[1]
        context.user_data['state'] = f"peset_{field}_{plan_id}"
        if field == 'panel':
            panels = await db.get_panels()
            lst = "\n".join([f"🆔 {pr['id']} - {pr['name']}" for pr in panels]) or "—"
            await query.message.reply_text(f"🖥 آیدی پنل جدید را بفرستید:\n\n{lst}", reply_markup=CANCEL_MARKUP)
        else:
            await query.message.reply_text(f"مقدار جدید «{label}» را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_list_specials' and admin_status:
        async with db.db_pool.acquire() as conn:
            vips = await conn.fetch("SELECT user_id, nickname FROM users WHERE role='vip'")
            admins = await conn.fetch("SELECT user_id, name FROM admins")
        
        msg = "💎 **لیست VIP ها:**\n"
        for v in vips: msg += f"🆔 `{v['user_id']}` - 👤 {v['nickname'] or 'بدون نام'}\n"
        msg += "\n👮‍♂️ **لیست ادمین‌ها:**\n"
        for a in admins: msg += f"🆔 `{a['user_id']}` - 👤 {a['name'] or 'بدون نام'}\n"
        msg += f"\n👑 `{SUPER_ADMIN_ID}` (مدیر کل)"
        await query.edit_message_text(msg, parse_mode='Markdown')

    elif data == 'admin_import_config' and admin_status:
        context.user_data['state'] = 'admin_waiting_import'
        msg = "🔗 **ایمپورت کانفیگ (تکی یا گروهی)**\n\nاول آیدی عددی کاربر، سپس نام کانفیگ‌ها (Email) یا **لینک‌های کامل** را با فاصله/خط‌جدید بفرستید.\nموارد تکراری به‌صورت خودکار رد می‌شوند و کانفیگ در همه‌ی پنل‌ها جستجو می‌شود.\n\nمثال:\n`123456789 ali_1 ali_2`\nیا\n`123456789 vless://...#ali_1`"
        await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_manage_panels' and admin_status:
        panels = await db.get_panels()
        msg = "🖥 **لیست پنل‌ها:**\n\n"
        if panels:
            for pr in panels:
                msg += f"🆔 `ID:{pr['id']}` | {pr['name']} | {pr['url']} | IP: {pr['config_ip']}\n"
        else:
            msg += "هنوز پنلی ثبت نشده است.\n"
        kb = [
            [InlineKeyboardButton("➕ افزودن پنل", callback_data='admin_add_panel'), InlineKeyboardButton("🗑 حذف پنل", callback_data='admin_del_panel')],
            [InlineKeyboardButton("✏️ ویرایش پنل", callback_data='admin_edit_panel'), InlineKeyboardButton("🔀 انتقال سفارش‌ها", callback_data='admin_move_panel')],
        ]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data == 'admin_edit_panel' and admin_status:
        context.user_data['state'] = 'admin_waiting_edit_panel_id'
        await query.message.reply_text("آیدی پنلی که می‌خواهید ویرایش کنید را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_move_panel' and admin_status:
        context.user_data['state'] = 'admin_waiting_move_panel'
        panels = await db.get_panels()
        lst = "\n".join([f"🆔 {pr['id']} - {pr['name']}" for pr in panels]) or "—"
        await query.message.reply_text(f"🔀 آیدی پنل **مبدأ** و **مقصد** را با فاصله بفرستید:\nمثال: `1 2`\n\n{lst}", parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data.startswith('panef_') and admin_status:
        parts = data.split('_')
        field, panel_id = parts[1], parts[2]
        meta = PANEL_FIELD_MAP.get(field)
        if not meta: return
        context.user_data['state'] = f"paneset_{field}_{panel_id}"
        await query.message.reply_text(f"مقدار جدید «{meta[1]}» را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_add_panel' and admin_status:
        context.user_data['new_panel'] = {}
        context.user_data['state'] = 'panel_add_name'
        await query.message.reply_text("🖥 یک **نام** برای این پنل وارد کنید (مثلاً: سرور آلمان):", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
        await query.delete_message()

    # ---------- بن کاربر ----------
    elif data == 'admin_ban_menu' and admin_status:
        banned = await db.list_banned_users()
        lines = "🚫 **بن / آزادسازی کاربر**\n\nآیدی عددی کاربر را بفرستید تا بن/آزادش کنید (تاگل).\n\n"
        if banned:
            lines += "کاربران بن‌شده:\n" + "\n".join(f"🚫 `{b['user_id']}` {e(b['nickname'] or '')}" for b in banned[:20])
        else:
            lines += "هیچ کاربری بن نیست."
        await query.message.reply_text(lines, parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    # ---------- مهاجرت اینباند ----------
    elif data == 'admin_migrate_menu' and admin_status:
        panels = await db.get_panels()
        msg = (
            "🔀 **مهاجرت اینباند**\n\n"
            "کلاینت‌های اینباند مبدا به مقصد منتقل می‌شوند — **با حفظ UUID و تنظیمات**،\n"
            "و سفارش‌ها و لینک‌های ذخیره‌شده خودکار بروزرسانی می‌شوند.\n\n"
            "فرمت پیام: `panel_id inbound_مبدأ inbound_مقصد`\nمثال: `1 613 617`\n\n"
            "پنل‌ها:\n" + "\n".join(f"🆔 {p['id']} - {p['name']}" for p in panels)
        )
        context.user_data['state'] = 'admin_waiting_migrate_inbound'
        await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    # ---------- اجبار عضویت کانال ----------
    elif data == 'admin_force_channel' and admin_status:
        cur = await get_setting('force_channel')
        cur_text = f"کانال فعلی: {cur}" if cur else "در حال حاضر خاموش است."
        context.user_data['state'] = 'admin_waiting_force_channel'
        await query.message.reply_text(
            f"⭐️ **اجبار عضویت کانال**\n\n{cur_text}\n\n"
            "یوزرنیم کانال را بفرستید (مثلاً `@mychannel`).\n"
            "ربات باید ادمین همان کانال باشد تا بتواند عضویت را چک کند.\n"
            "برای خاموش‌کردن بفرستید: `خاموش`",
            parse_mode='Markdown', reply_markup=CANCEL_MARKUP,
        )
        await query.delete_message()

    # ---------- پیام همگانی: تأیید نهایی / دورریز ----------
    elif data == 'broadcast_confirm' and admin_status:
        draft_msg = context.user_data.pop('broadcast_draft_msg', None)
        context.user_data['state'] = 'none'
        if not draft_msg:
            return await safe_answer(query, "پیش‌نویسی وجود ندارد؛ دوباره از «ارسال پیام 📢» شروع کنید.", alert=True)
        users = await db.all_user_ids()
        wait_msg = await query.message.reply_text(f"📢 ارسال به {len(users)} کاربر در پس‌زمینه آغاز شد...")
        # در پس‌زمینه اجرا می‌شود؛ وگرنه تا پایان ارسال، ربات به هیچ کاربر دیگری پاسخ نمی‌داد
        context.application.create_task(
            _run_broadcast(context, users, user_id, draft_msg, wait_msg)
        )

    elif data == 'broadcast_confirm_fwd' and admin_status:
        draft_msg = context.user_data.pop('broadcast_draft_msg', None)
        context.user_data['state'] = 'none'
        if not draft_msg:
            return await safe_answer(query, "پیش‌نویسی وجود ندارد؛ دوباره از «ارسال پیام 📢» شروع کنید.", alert=True)
        users = await db.all_user_ids()
        wait_msg = await query.message.reply_text(f"📢 ارسال (حالت فوروارد) به {len(users)} کاربر آغاز شد...")
        context.application.create_task(
            _run_broadcast(context, users, user_id, draft_msg, wait_msg, forward=True)
        )

    elif data == 'broadcast_cancel' and admin_status:
        context.user_data.pop('broadcast_draft_msg', None)
        context.user_data['state'] = 'none'
        try:
            await query.edit_message_text("🗑 پیام همگانی دور ریخته شد. هیچ پیامی ارسال نشد.")
        except Exception:
            await safe_answer(query, "دورریخته شد.", alert=True)

    # ---------- شارژ حساب: انتخاب روش پرداخت ----------
    elif data.startswith("stars_pay_") or data.startswith("card_pay_"):
        mode, amount_s = data.split("_", 1)[0], data.split("_")[2]
        amount = int(amount_s) if amount_s.isdigit() else 0
        if amount < 1000:
            context.user_data['state'] = 'none'
            return await safe_answer(query, "مبلغ نامعتبر؛ از «شارژ حساب 💳» دوباره شروع کنید.", alert=True)
        if mode == 'card':
            card = (await get_setting('card_number') or '').strip()
            if not card:
                context.user_data['state'] = 'none'
                return await safe_answer(query, "شماره کارت تنظیم نشده است.", alert=True)
            context.user_data['state'] = f'waiting_receipt_{amount}'
            await query.message.reply_text(
                f"💳 لطفاً مبلغ **{amount:,} تومان** را به شماره کارت زیر واریز کنید:\n\n`{card}`\n\n📸 سپس **عکس رسید واریزی** را همینجا ارسال کنید.",
                reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
            return
        # پرداخت با استارز
        rate = (await get_setting('stars_toman_rate') or '').strip()
        if not rate.isdigit() or int(rate) <= 0:
            context.user_data['state'] = 'none'
            return await safe_answer(query, "پرداخت با استارز فعال نیست.", alert=True)
        rate = int(rate)
        stars = amount // rate + (1 if amount % rate else 0)
        stars = max(1, stars)
        context.user_data['state'] = 'none'
        try:
            await context.bot.send_invoice(
                chat_id=user_id,
                title='شارژ کیف پول OverWall',
                description=f'افزودن {amount:,} تومان به کیف پول شما',
                payload=f'stars_topup:{amount}',
                currency='XTR',
                prices=[LabeledPrice(f'شارژ {amount:,} تومان', stars)],
            )
        except Exception as ex:
            logging.warning("send_invoice (XTR) ناموفق: %s", ex)
            await safe_answer(query, "خطا در ساخت فاکتور استارز؛ بعداً تلاش کنید.", alert=True)

    elif data == 'wallet_history':
        txns = await db.get_user_transactions(user_id, 15)
        if not txns:
            return await safe_answer(query, "تراکنشی ثبت نشده است.", alert=True)
        lines = ["📜 **۱۵ تراکنش اخیر:**\n"]
        for t in txns:
            sign = "➕" if t['amount'] > 0 else "➖"
            d = t['date'].strftime('%Y-%m-%d %H:%M') if t['date'] else ''
            desc = f" - {t['description']}" if t['description'] else ''
            lines.append(f"{sign} {abs(t['amount']):,} | {t['kind']}{desc} | {d}")
        try:
            await query.edit_message_text("\n".join(lines), parse_mode='Markdown')
        except Exception:
            await query.message.reply_text("\n".join(lines), parse_mode='Markdown')

    elif data == 'wallet_referral':
        count = await db.referral_count(user_id)
        rewarded = await db.referral_rewarded_count(user_id)
        cfg = await db.load_referral_config()
        username = context.bot.username
        link = f"https://t.me/{username}?start=ref_{user_id}" if username else f"کد دعوت شما: ref_{user_id}"
        if not cfg.enabled or not cfg.has_payout:
            rules = "در حال حاضر پاداش دعوت توسط مدیریت غیرفعال است؛ لینک دعوت همچنان ثبت می‌شود."
        else:
            rules = f"بعد از اولین شارژِ تاییدشده‌ی دوستتان {money(cfg.referrer_bonus)} تومان به حساب شما اضافه می‌شود."
            if cfg.invitee_bonus:
                rules += f"\nدوستتان هم {money(cfg.invitee_bonus)} تومان هدیه ورود می‌گیرد."
            if cfg.min_topup:
                rules += f"\nحداقل مبلغ شارژ برای آزاد شدن پاداش: {money(cfg.min_topup)} تومان."
        msg = (
            f"🔗 **دعوت دوستان**\n\n"
            f"{rules}\n\n"
            f"`{link}`\n\n"
            f"👥 تعداد دعوت‌های شما: {count}"
            + (f" (پاداش‌گرفته: {rewarded})" if count else "")
        )
        try:
            await query.edit_message_text(msg, parse_mode='Markdown')
        except Exception:
            await query.message.reply_text(msg, parse_mode='Markdown')

    elif data == 'wallet_giftcode':
        context.user_data['state'] = 'redeem_gift'
        await query.message.reply_text("🎟 کد هدیه را وارد کنید:", reply_markup=CANCEL_MARKUP)
        try: await query.delete_message()
        except Exception: pass

    elif data == 'admin_report' and admin_status:
        r = await db.get_sales_report()
        msg = (
            f"📊 **گزارش فروش**\n\n"
            f"🛒 کل فروش (کسر شده): {r['spent']:,} تومان\n"
            f"📅 فروش امروز: {r['today_spent']:,} تومان\n"
            f"💳 کل شارژ حساب‌ها: {r['topup']:,} تومان\n"
            f"↩️ کل برگشت وجه: {r['refunds']:,} تومان\n"
            f"📦 تعداد کل سفارش‌ها: {r['orders']:,}\n"
            f"👥 تعداد کاربران: {r['users']:,}\n"
            f"👛 مجموع موجودی کیف‌پول کاربران: {r['balances']:,} تومان"
        )
        await query.edit_message_text(msg, parse_mode='Markdown')

    elif data == 'admin_backup_menu' and admin_status:
        enabled = (await get_setting('backup_enabled')) == 'on'
        status = "🟢 روشن" if enabled else "🔴 خاموش"
        msg = f"💾 **بکاپ دیتابیس**\n\nبکاپ خودکار روزانه: {status}\n(بکاپ به همه‌ی ادمین‌ها ارسال می‌شود)"
        kb = [
            [InlineKeyboardButton("⬇️ بکاپ فوری", callback_data='admin_backup_now')],
            [InlineKeyboardButton(("🔴 خاموش کن" if enabled else "🟢 روشن کن"), callback_data='admin_backup_toggle')],
        ]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data == 'admin_backup_toggle' and admin_status:
        cur = (await get_setting('backup_enabled')) == 'on'
        await update_setting('backup_enabled', 'off' if cur else 'on')
        enabled = not cur
        status = "🟢 روشن" if enabled else "🔴 خاموش"
        msg = f"💾 **بکاپ دیتابیس**\n\nبکاپ خودکار روزانه: {status}\n(بکاپ به همه‌ی ادمین‌ها ارسال می‌شود)"
        kb = [
            [InlineKeyboardButton("⬇️ بکاپ فوری", callback_data='admin_backup_now')],
            [InlineKeyboardButton(("🔴 خاموش کن" if enabled else "🟢 روشن کن"), callback_data='admin_backup_toggle')],
        ]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data == 'admin_backup_now' and admin_status:
        await safe_answer(query, "در حال تهیه بکاپ... ⏳")
        await _send_backup_to(context, user_id)

    elif data == 'admin_gift_menu' and admin_status:
        await render_gift_menu(query)

    elif data == 'admin_gift_add' and admin_status:
        context.user_data['new_gift'] = {}
        context.user_data['state'] = 'gift_add_code'
        await query.message.reply_text("🎟 متن کد هدیه را وارد کنید (انگلیسی/عدد).\nخالی بفرستید تا کد تصادفی ساخته شود:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_gift_random' and admin_status:
        context.user_data['new_gift'] = {'code': rewards.generate_gift_code()}
        context.user_data['state'] = 'gift_add_amount'
        await query.message.reply_text(
            f"🎲 کد ساخته شد: `{context.user_data['new_gift']['code']}`\n💰 مبلغ هدیه (تومان) را بفرستید:",
            reply_markup=CANCEL_MARKUP, parse_mode='Markdown',
        )
        await query.delete_message()

    elif data == 'admin_gift_del' and admin_status:
        context.user_data['state'] = 'admin_waiting_del_gift'
        await query.message.reply_text("کدی که می‌خواهید حذف شود را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_gift_toggle' and admin_status:
        context.user_data['state'] = 'admin_waiting_toggle_gift'
        await query.message.reply_text("کدی که می‌خواهید فعال/غیرفعال شود را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_ref_menu' and admin_status:
        await render_referral_menu(query)

    elif data == 'admin_ref_toggle' and admin_status:
        cfg = await db.load_referral_config()
        await update_setting('ref_enabled', 'off' if cfg.enabled else 'on')
        await render_referral_menu(query)

    elif data == 'admin_set_refbonus' and admin_status:
        context.user_data['state'] = 'admin_waiting_refbonus'
        cur = await get_setting('ref_bonus')
        await query.message.reply_text(f"🎁 مبلغ پاداش معرف (تومان) را بفرستید:\nمقدار فعلی: {int(cur or 0):,}", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_set_invitee_bonus' and admin_status:
        context.user_data['state'] = 'admin_waiting_invitee_bonus'
        cur = await get_setting('ref_invitee_bonus')
        await query.message.reply_text(f"🎁 مبلغ هدیه دعوت‌شده (تومان) را بفرستید:\nمقدار فعلی: {int(cur or 0):,}", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_set_ref_min' and admin_status:
        context.user_data['state'] = 'admin_waiting_ref_min'
        cur = await get_setting('ref_min_topup')
        await query.message.reply_text(f"💳 حداقل مبلغ شارژ برای آزاد شدن پاداش دعوت (تومان):\nمقدار فعلی: {int(cur or 0):,}\nصفر یعنی بدون حداقل.", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_set_notify' and admin_status:
        context.user_data['state'] = 'admin_waiting_notify'
        cur = await get_setting('notify_days')
        await query.message.reply_text(f"🔔 چند روز مانده به انقضا هشدار ارسال شود؟ (فقط عدد)\nمقدار فعلی: {cur}", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_test_menu' and admin_status:
        await render_test_menu(query)

    elif data == 'admin_test_toggle' and admin_status:
        cur = (await get_setting('test_enabled')) == 'on'
        await update_setting('test_enabled', 'off' if cur else 'on')
        await render_test_menu(query)

    elif data == 'admin_test_set_gb' and admin_status:
        context.user_data['state'] = 'test_set_gb'
        await query.message.reply_text("💾 حجم اکانت تست را به گیگابایت بفرستید (فقط عدد):", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_test_set_days' and admin_status:
        context.user_data['state'] = 'test_set_days'
        await query.message.reply_text("📅 مدت اکانت تست را به روز بفرستید (فقط عدد):", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_test_set_panel' and admin_status:
        context.user_data['state'] = 'test_set_panel'
        panels = await db.get_panels()
        lst = "\n".join([f"🆔 {pr['id']} - {pr['name']}" for pr in panels]) or "—"
        await query.message.reply_text(f"🖥 آیدی پنل اکانت تست را بفرستید:\n\n{lst}", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_test_set_inbound' and admin_status:
        context.user_data['state'] = 'test_set_inbound'
        await query.message.reply_text("⚙️ آیدی اینباند اکانت تست را بفرستید (فقط عدد):", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_add_plan' and admin_status:
        context.user_data['state'] = 'plan_add_name'
        await query.message.reply_text("✨ لطفاً **نام پلن** جدید را ارسال کنید (مثلاً: یک ماهه 50 گیگ):", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_del_plan' and admin_status:
        context.user_data['state'] = 'admin_waiting_del_plan'
        await query.message.reply_text("🗑 آیدی (ID) پلنی که می‌خواهید حذف شود را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    # ---------- مدیریت قیمت‌ها ----------
    elif data == 'admin_pricing' and admin_status:
        await render_pricing_menu(query)

    elif data == 'admin_toggle_plan' and admin_status:
        context.user_data['state'] = 'admin_waiting_toggle_plan'
        await query.message.reply_text("🔁 آیدی پلنی که می‌خواهید فعال/غیرفعال شود را بفرستید:", reply_markup=CANCEL_MARKUP)

    elif data == 'admin_seed_plans' and admin_status:
        created = await db.seed_default_plans()
        await safe_answer(query, f"✅ {created} پلن پیش‌فرض اضافه شد." if created else "همه‌ی پلن‌های پیش‌فرض از قبل وجود دارند.", alert=True)
        await render_pricing_menu(query)

    # ---------- مدیریت آفرها ----------
    elif data == 'admin_offers' and admin_status:
        await render_offers_menu(query)

    elif data == 'admin_toggle_firstoffer' and admin_status:
        cur = (await get_setting('first_offer_enabled')) == 'on'
        await update_setting('first_offer_enabled', 'off' if cur else 'on')
        await render_offers_menu(query)

    elif data == 'admin_toggle_renewoffer' and admin_status:
        cur = (await get_setting('renew_offer_enabled')) == 'on'
        await update_setting('renew_offer_enabled', 'off' if cur else 'on')
        await render_offers_menu(query)

    elif data == 'admin_toggle_offer' and admin_status:
        context.user_data['state'] = 'admin_waiting_toggle_offer'
        await query.message.reply_text("🔁 آیدی آفری که می‌خواهید فعال/غیرفعال شود را بفرستید:", reply_markup=CANCEL_MARKUP)

    # ---------- گزارش مالی و نمایندگان ----------
    elif data == 'admin_finance' and admin_status:
        await render_finance_report(query)

    elif data == 'admin_resellers' and admin_status:
        await render_resellers_menu(query)

    elif data == 'admin_add_reseller' and admin_status:
        context.user_data['state'] = 'admin_waiting_reseller_id'
        await query.message.reply_text("🤝 آیدی عددی و نام نماینده را با فاصله بفرستید:\n\nمثال: `123456789 علی`", parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_rem_reseller' and admin_status:
        context.user_data['state'] = 'admin_waiting_rem_reseller'
        await query.message.reply_text("آیدی نماینده‌ای که به کاربر عادی تبدیل می‌شود را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_toggle_resbonus' and admin_status:
        cur = (await get_setting('reseller_bonus_enabled')) == 'on'
        await update_setting('reseller_bonus_enabled', 'off' if cur else 'on')
        await render_resellers_menu(query)

    elif data == 'admin_add_vip' and admin_status:
        context.user_data['state'] = 'admin_waiting_vip_id'
        await query.message.reply_text("🔢 برای افزودن VIP، آیدی عددی و یک نام دلخواه را با فاصله بفرستید:\n\nمثال: `123456789 علی`", parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_rem_vip' and admin_status:
        context.user_data['state'] = 'admin_waiting_rem_vip'
        await query.message.reply_text("آیدی حذف VIP:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_set_card' and admin_status:
        context.user_data['state'] = 'admin_waiting_card'
        await query.message.reply_text("کارت جدید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_set_support' and admin_status:
        context.user_data['state'] = 'admin_waiting_support'
        await query.message.reply_text("پشتیبانی جدید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_broadcast' and admin_status:
        context.user_data['state'] = 'admin_waiting_broadcast'
        await query.message.reply_text(
            "متن، عکس، ویس یا ویدیوی خود را ارسال کنید:\n(بعد از ارسال، پیش‌نمایش می‌بینید و با تأیید ارسال می‌شود)",
            reply_markup=CANCEL_MARKUP,
        )
        await query.delete_message()

    elif data == 'superadmin_add_admin' and user_id == SUPER_ADMIN_ID:
        context.user_data['state'] = 'superadmin_waiting_add_admin'
        await query.message.reply_text("آیدی و نام ادمین را بفرستید:\nمثال: `123456789 رضا`", parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'superadmin_rem_admin' and user_id == SUPER_ADMIN_ID:
        context.user_data['state'] = 'superadmin_waiting_rem_admin'
        await query.message.reply_text("آیدی حذف ادمین:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    # ================= هندلرهای مدیریت سفارشات =================
    elif data.startswith("toggle_status_"):
        order_id = data.split("_")[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        found = await fetch_client_for_order(query, order_id)
        if not found: return
        xui, cfg_ip, inbound_id, client_dict = found
        client_dict['enable'] = not client_dict.get('enable', True)
        if await xui.update_client(inbound_id, client_key(client_dict), client_dict):
            await render_order_details(query, order_id, "✅ وضعیت تغییر کرد!")
        else:
            await query.message.reply_text("❌ سرور درخواست تغییر وضعیت را رد کرد.")

    elif data.startswith("change_uuid_"):
        order_id = data.split("_")[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        found = await fetch_client_for_order(query, order_id)
        if not found: return
        xui, cfg_ip, inbound_id, client_dict = found
        old_key = client_key(client_dict)
        # در trojan شناسه همان password است، نه uuid
        new_key, field = new_client_key(client_dict)
        client_dict[field] = new_key
        if await xui.update_client(inbound_id, old_key, client_dict):
            await refresh_order_link(order_id, xui, inbound_id, client_dict, cfg_ip)
            await render_order_details(query, order_id, "✅ لینک اتصال و شناسه با موفقیت تغییر کرد!")
        else:
            await query.message.reply_text("❌ سرور درخواست تغییر شناسه را رد کرد.")

    elif data.startswith("rename_conf_"):
        order_id = data.split("_")[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        context.user_data['state'] = f'waiting_rename_{order_id}'
        await query.message.reply_text(
            "📝 نام جدید کانفیگ را به انگلیسی وارد کنید (فقط حروف انگلیسی، اعداد، - و _):",
            reply_markup=CANCEL_MARKUP,
        )
        try: await query.delete_message()
        except Exception: pass

    elif data.startswith("renew_menu_"):
        order_id = data.split("_")[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        # فقط پلن‌های همان پنلِ سفارش؛ وگرنه پول پلنِ یک سرور کسر و حجم روی سرور دیگری اعمال می‌شد
        order_panel_id = await db.get_order_panel_id(order_id)
        plans = await db.list_plans(panel_id=order_panel_id, only_active=True)
        if not plans:
            return await safe_answer(query, "برای این سرور پلنی جهت تمدید تعریف نشده است!", alert=True)
        quoted = await pricing.quote_plans(user_id, plans, KIND_RENEW, admin=is_super_admin(user_id))
        kb = [
            [InlineKeyboardButton(pricing.button_text(p, q), callback_data=f"confirm_renew_{order_id}_{p['id']}")]
            for p, q in quoted
            if q.available
        ]
        kb.append([InlineKeyboardButton("🔙 انصراف", callback_data=f'show_order_{order_id}')])
        special = any(q.has_discount for _p, q in quoted)
        await query.edit_message_text(
            "♻️ **تمدید سرویس**\n"
            + ("💚 قیمت ویژه‌ی تمدید برای شما اعمال شده است.\n" if special else "")
            + "با تمدید، حجم و زمان سرویس مطابق پلن انتخابی **ریست** می‌شود.\nلطفاً پلن را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown',
        )

    elif data.startswith("auto_renew_"):
        order_id = data.split("_")[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        row = await db.get_order(order_id)
        if not row: return
        if row['auto_renew']:
            # خاموش‌کردن: همیشه مستقیم و بدون پلن
            await db.set_auto_renew(order_id, False)
            return await render_order_details(query, order_id, "🤖 تمدید خودکار خاموش شد.")
        # روشن‌کردن: انتخاب پلنِ تمدیدِ خودکار (همان پنل سفارش) — ۱ روز قبل از انقضا خودکار اعمال می‌شود
        order_panel_id = await db.get_order_panel_id(order_id)
        plans = await db.list_plans(panel_id=order_panel_id, only_active=True)
        quoted = await pricing.quote_plans(user_id, plans, KIND_RENEW, admin=is_super_admin(user_id))
        kb = [
            [InlineKeyboardButton(f"🤖 {pricing.button_text(p, q)}", callback_data=f"auto_renew_plan_{order_id}_{p['id']}")]
            for p, q in quoted if q.available
        ]
        if not kb:
            return await safe_answer(query, "پلن تمدید فعالی برای این سرور نیست.", alert=True)
        kb.append([InlineKeyboardButton("❌ انصراف", callback_data=f'show_order_{order_id}')])
        await query.edit_message_text(
            "🤖 **تمدید خودکار**\n\n"
            "روز قبل از انقضا، اگر موجودی‌ات به اندازه‌ی قیمت پلن باشد، سرویس بدون دخالت تو تمدید می‌شود\n"
            "(حجم و زمان ریست می‌شود و اطلاع‌رسانی هم می‌شود).\n\n"
            "کدام پلن به‌صورت خودکار تمدید شود؟",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown',
        )

    elif data.startswith("auto_renew_plan_"):
        parts = data.split("_")
        order_id, plan_id = int(parts[3]), int(parts[4])
        if not await ensure_order_access(query, order_id, admin_status): return
        plan = await db.get_plan(plan_id)
        if not plan: return
        await db.set_auto_renew(order_id, True, plan_id=plan_id)
        await render_order_details(query, order_id, f"🤖 تمدید خودکار روی پلن «{plan['name']}» فعال شد.")

    elif data == 'usage_overview':
        # وضعیت زنده‌ی همه‌ی سرویس‌های کاربر در یک نگاه (یک درخواست آمار به‌ازای هر پنل)
        from collections import defaultdict
        orders = await db.get_orders_by_user(user_id)
        if not orders:
            return await safe_answer(query, "سفارشی ندارید.", alert=True)
        by_panel = defaultdict(list)
        for o in orders:
            by_panel[o['panel_id']].append(o)
        now_ms = int(time.time() * 1000)
        lines = ["📊 **وضعیت زنده سرویس‌های شما**\n"]
        for panel_id, group in by_panel.items():
            panel = await db.get_panel(panel_id) if panel_id else None
            if panel is None:
                lines.append("⚠️ یکی از پنل‌ها در دسترس نیست.")
                continue
            xui, _ip = build_xui(panel)
            ok, _ = await xui.login()
            if not ok:
                lines.append(f"⚠️ اتصال به «{panel['name']}» برقرار نشد.")
                continue
            stats_map = await xui.get_all_client_stats() or {}
            for o in group:
                email = order_email(o)
                st = stats_map.get((email or '').strip().lower())
                if not st:
                    lines.append(f"⚪️ `{email}` — روی سرور پیدا نشد")
                    continue
                total = st.get('total', 0)
                used = st.get('up', 0) + st.get('down', 0)
                expiry = st.get('expiryTime', 0)
                emoji = "🟢" if st.get('enable', False) else "🔴"
                if expiry and expiry < now_ms: emoji = "🟠"
                if total > 0 and used >= total: emoji = "🟠"
                pct = int(used * 100 / total) if total > 0 else 0
                exp_txt = datetime.datetime.fromtimestamp(expiry / 1000).strftime('%m-%d') if expiry > 0 else 'نامحدود'
                lines.append(
                    f"{emoji} `{email}`\n"
                    f"   📊 {format_size(used)} / {format_size(total) if total > 0 else '∞'} ({pct}٪) — 📅 {exp_txt}"
                )
        lines.append(f"\n🕐 {datetime.datetime.now().strftime('%H:%M:%S')}")
        text = "\n".join(lines)
        try:
            await query.edit_message_text(text, parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data='back_to_orders')]]))
        except Exception:
            await query.message.reply_text(text, parse_mode='Markdown')

    elif data.startswith("confirm_renew_"):
        parts = data.split("_")
        order_id = parts[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        plan_id = int(parts[3])
        plan = await db.get_plan(plan_id)
        if not plan: return
        quote = await pricing.quote_for_user(user_id, plan, KIND_RENEW, admin=is_super_admin(user_id))
        if not quote.available:
            return await safe_answer(query, pricing.UNAVAILABLE_TEXT, alert=True)
        remember_quote(context, plan_id, KIND_RENEW, quote)
        days = plan['duration_days'] or 30

        if balance < quote.price:
            kb = [[InlineKeyboardButton("🔙 بازگشت", callback_data=f'renew_menu_{order_id}')]]
            return await query.edit_message_text(
                f"❌ **موجودی حساب شما کافی نیست.**\nموجودی: {balance:,} | نیاز: {quote.price:,}",
                reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown',
            )

        price_lines = (
            f"💰 قیمت اصلی: {quote.base_price:,} تومان\n✅ قیمت تمدید: **{quote.price:,}** تومان\n"
            f"🎁 سود شما: {quote.discount:,} تومان"
            if quote.has_discount else f"💳 مبلغ کسر: **{quote.price:,} تومان**"
        )
        kb = [[InlineKeyboardButton("✅ تایید و اعمال تمدید", callback_data=f'execute_renew_{order_id}_{plan_id}')], [InlineKeyboardButton("❌ انصراف", callback_data=f'renew_menu_{order_id}')]]
        await query.edit_message_text(
            f"{quote.label + chr(10) if quote.label else ''}"
            f"آیا از اعمال پلن **{plan['name']}** روی این سرویس اطمینان دارید؟\n"
            f"💾 حجم جدید: **{plan['gb']} GB** (ریست کامل)\n"
            f"📅 مدت جدید: **{days} روز** از همین الان\n"
            f"{price_lines}",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown',
        )

    elif data.startswith("execute_renew_"):
        parts = data.split("_")
        order_id = parts[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        plan_id = int(parts[3])
        plan = await db.get_plan(plan_id)
        if not plan: return await safe_answer(query, "❌ پلن یافت نشد!", alert=True)
        # قیمتِ همان صفحه‌ی تایید ملاک است تا تغییر قیمت در این فاصله به کاربر تحمیل نشود
        quote = recall_quote(context, plan_id, KIND_RENEW) or await pricing.quote_for_user(user_id, plan, KIND_RENEW, admin=is_super_admin(user_id))
        if not quote.available:
            return await safe_answer(query, pricing.UNAVAILABLE_TEXT, alert=True)
        price = quote.price

        if context.user_data.get('processing'):
            return await safe_answer(query, "⏳ یک عملیات در حال انجام است، صبر کنید.", alert=True)
        context.user_data['processing'] = True
        try:
            row, email = await load_order_service(order_id)
            if not row:
                return await safe_answer(query, "❌ سفارش یافت نشد!", alert=True)
            if not email:
                return await safe_answer(query, "❌ نام این سرویس قابل تشخیص نیست؛ به پشتیبانی اطلاع دهید.", alert=True)

            # کسر اتمیک موجودی پیش از تماس با پنل
            if await deduct_balance(user_id, price, kind='تمدید') is None:
                return await safe_answer(query, "❌ موجودی کم است!", alert=True)

            xui, _ip = await get_order_xui(order_id)
            await query.edit_message_text("در حال انجام عملیات تمدید... ⏳")
            is_logged, _ = await xui.login()
            if not is_logged:
                await credit_balance(user_id, price, kind='برگشت وجه')
                return await render_order_details(query, order_id, "❌ خطا در اتصال به پنل! (وجه بازگشت داده شد)")

            inbound_id, _port, client_dict = await xui.get_client_exact_info(email)
            if not client_dict:
                await credit_balance(user_id, price, kind='برگشت وجه')
                return await render_order_details(query, order_id, "❌ کانفیگ در سرور یافت نشد! (وجه بازگشت داده شد)")

            # ریست کامل مطابق پلن: حجم = حجم پلن، مصرف صفر، انقضا = الان + مدت پلن
            duration_days = plan['duration_days'] or 30
            client_dict['totalGB'] = int(plan['gb']) * 1024 * 1024 * 1024
            client_dict['up'] = 0
            client_dict['down'] = 0
            client_dict['expiryTime'] = int((time.time() + (duration_days * 86400)) * 1000)
            client_dict['enable'] = True
            if await xui.update_client(inbound_id, client_key(client_dict), client_dict):
                # مصرف واقعی در جدول جدا نگه داشته می‌شود؛ updateClient آن را صفر نمی‌کند،
                # پس حتماً از endpoint اختصاصی ریست ترافیک استفاده می‌کنیم.
                await xui.reset_client_traffic(inbound_id, email)
                await db.reset_notify(order_id)
                await db.update_order_service(order_id, inbound_id=inbound_id)
                # ثبت فروش و مصرف آفر فقط بعد از موفقیت واقعی روی پنل
                await pricing.commit_quote(user_id, quote, KIND_RENEW, plan_id, order_id=int(order_id), role=role)
                context.user_data.pop('quote', None)
                logging.info("RENEW user=%s plan=%s price=%s base=%s", user_id, plan_id, quote.price, quote.base_price)
                await render_order_details(query, order_id, f"✅ تمدید شد: {plan['gb']}GB / {duration_days} روز")
            else:
                await credit_balance(user_id, price, kind='برگشت وجه')
                await render_order_details(query, order_id, "❌ خطا: سرور درخواست تمدید را رد کرد! (وجه بازگشت داده شد)")
        finally:
            context.user_data['processing'] = False

    # ================= هندلر دریافت بارکد =================
    elif data.startswith("get_conf_"):
        order_id = data.split("_")[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        row, email = await load_order_service(order_id)
        if row:
            config_link = row['config_link']
            panel = await db.get_panel(row['panel_id'])
            sub = sub_link_for(panel, email)
            # اگر پنل لینک ساب داشته باشد، آن را به‌عنوان لینک اصلی (که با تغییر UUID خراب نمی‌شود) می‌دهیم
            primary = sub or config_link
            caption = f"📥 **لینک اتصال شما:**\n\n`{primary}`"
            if sub:
                caption += f"\n\n🔗 *لینک ساب با تغییر UUID همچنان معتبر می‌ماند.*\n\nکانفیگ مستقیم:\n`{config_link}`"
            caption += f"\n\n*(برای کپی، روی لینک ضربه بزنید)*{SECURITY_WARNING}"
            encoded_url = urllib.parse.quote(primary)
            qr_api_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={encoded_url}&margin=20"
            try:
                await context.bot.send_photo(chat_id=user_id, photo=qr_api_url, caption=caption, parse_mode='Markdown')
            except Exception:
                await context.bot.send_message(chat_id=user_id, text=caption, parse_mode='Markdown')

    # بقیه هندلرهای خرید
    elif data.startswith("prebuy_"):
        if not await _join_ok(context, user_id):
            return await _send_join_prompt(query, (await get_setting('force_channel') or '').strip())
        plan_id = data.split("_")[1]
        context.user_data['state'] = f'waiting_for_nick_{plan_id}'
        await query.edit_message_text("یک نام دلخواه به انگلیسی برای کانفیگ خود وارد کنید:\n(همین نام در اپ شما نمایش داده می‌شود؛ در صورت تکراری‌بودن فقط یک شماره به انتها اضافه می‌شود)")

    elif data.startswith("confirm_buy_"):
        parts = data.split("_")
        plan_id = int(parts[2])
        final_name = "_".join(parts[3:])

        plan = await db.get_plan(plan_id)

        if not plan: return await query.edit_message_text("❌ پلن یافت نشد.")
        if not plan['is_active']:
            return await query.edit_message_text("⛔️ این پلن در حال حاضر غیرفعال است.")

        # منویی که قبل از بستن فروش باز مانده نباید همچنان خرید انجام دهد
        if await get_setting('sales_status') == 'closed':
            return await query.edit_message_text("⛔️ فروش در حال حاضر بسته است.")

        # همان قیمتی که در صفحه‌ی تایید نشان داده شد ملاک است
        quote = recall_quote(context, plan_id, KIND_BUY) or await pricing.quote_for_user(user_id, plan, KIND_BUY, admin=is_super_admin(user_id))
        if not quote.available:
            return await query.edit_message_text(pricing.UNAVAILABLE_TEXT)
        price = quote.price

        if context.user_data.get('processing'):
            return await safe_answer(query, "⏳ یک عملیات در حال انجام است، صبر کنید.", alert=True)
        context.user_data['processing'] = True
        try:
            # ابتدا موجودی به‌صورت اتمیک کسر می‌شود تا از کسر دوباره/همزمان جلوگیری شود
            if await deduct_balance(user_id, price, kind='خرید') is None:
                return await query.edit_message_text(f"❌ موجودی کافی نیست. (نیاز: {price:,})")

            await query.edit_message_text("در حال اتصال به سرور و استخراج پورت... ⏳")
            panel = await db.get_panel(plan['panel_id'])
            xui, cfg_ip = build_xui(panel)
            order_panel_id = panel['id'] if panel else None

            # جلوگیری از ساختِ کانفیگِ خراب بدون هاست/دامنه
            if not cfg_ip:
                await credit_balance(user_id, price, kind='برگشت وجه')
                return await query.edit_message_text("❌ «IP کانفیگ» این پنل تنظیم نشده است. لطفاً به ادمین اطلاع دهید. (وجه بازگشت داده شد)")

            is_logged, login_err = await xui.login()

            if not is_logged:
                await credit_balance(user_id, price, kind='برگشت وجه')  # برگشت وجه
                return await query.edit_message_text(f"❌ خطا در لاگین به پنل سنایی.\nدلیل: {login_err}")

            inbound = await xui.get_inbound(plan['inbound_id'])
            if not inbound:
                await credit_balance(user_id, price, kind='برگشت وجه')  # برگشت وجه
                return await query.edit_message_text(f"❌ خطای پنل: اینباند با آیدی {plan['inbound_id']} پیدا نشد!")

            # نام سرویس یکتا روی پنل: اسم دلخواه کاربر عیناً می‌ماند؛ فقط در
            # صورت تکرار، پسوند کوتاه خودکار می‌گیرد (مثل username)
            final_name = await unique_service_name(xui, final_name)
            config_link, error_msg = await xui.add_client(plan['inbound_id'], final_name, plan['gb'], (plan['duration_days'] or 30), cfg_ip, inbound=inbound)
            if config_link:
                # قیمت روی خود سفارش ثبت می‌شود تا تغییر قیمت‌های بعدی این سفارش را عوض نکند
                order_id = await add_order(
                    user_id, config_link, order_panel_id, email=final_name, inbound_id=plan['inbound_id'],
                    plan_id=plan_id, price=quote.price, base_price=quote.base_price,
                    discount=quote.discount, purchase_kind=KIND_BUY, user_role=role,
                )
                await pricing.commit_quote(user_id, quote, KIND_BUY, plan_id, order_id=order_id, role=role)
                context.user_data.pop('quote', None)
                logging.info("PURCHASE user=%s plan=%s price=%s base=%s offer=%s name=%s",
                             user_id, plan_id, quote.price, quote.base_price, quote.label or '-', final_name)

                # اگر پنل لینک ساب داشته باشد، آن را لینک اصلی قرار می‌دهیم
                sub = sub_link_for(panel, final_name)
                primary = sub or config_link
                caption = f"✅ خرید موفق!\n\n`{primary}`"
                if sub:
                    caption += f"\n\nکانفیگ مستقیم:\n`{config_link}`"
                if quote.has_discount:
                    caption += f"\n\n🎁 تخفیف اعمال‌شده: {quote.discount:,} تومان"
                caption += SECURITY_WARNING
                encoded_url = urllib.parse.quote(primary)
                qr_api_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={encoded_url}&margin=20"
                try:
                    await context.bot.send_photo(chat_id=user_id, photo=qr_api_url, caption=caption, parse_mode='Markdown')
                    await query.delete_message()
                except Exception:
                    await query.edit_message_text(caption, parse_mode='Markdown')

                # پورتال اشتراک: پیام جداگانه با لینک پورتال و اشتراک خام (additive)
                try:
                    sub_token = await db.ensure_sub_token(order_id)
                    sub_link = subportal.raw_url(sub_token) if sub_token else None
                    portal_link = subportal.portal_url(sub_token) if sub_token else None
                    if sub_link:
                        kb = []
                        if portal_link:
                            kb.append([InlineKeyboardButton("🌐 پورتال اشتراک", url=portal_link)])
                        portal_text = (
                            "✅ سرویس شما با موفقیت ساخته شد.\n\n"
                            f"👤 نام سرویس: `{final_name}`\n"
                            f"🛍 پلن: {plan['name']} ({plan['gb']}GB / {plan['duration_days'] or 30} روز)\n\n"
                            "🌐 پورتال اشتراک (وضعیت زنده، مصرف، انقضا):\n"
                            f"`{portal_link}`\n\n"
                            "🔗 لینک اشتراک (در کلاینت اضافه کنید):\n"
                            f"`{sub_link}`"
                        )
                        await context.bot.send_message(
                            user_id, portal_text,
                            reply_markup=InlineKeyboardMarkup(kb) if kb else None,
                            parse_mode='Markdown',
                        )
                except Exception as ex:
                    logging.warning("ارسال پیام پورتال اشتراک ناموفق بود: %s", ex)
            else:
                await credit_balance(user_id, price, kind='برگشت وجه')  # برگشت وجه چون ساخت کانفیگ ناموفق بود
                await query.edit_message_text(f"❌ سرور ساخت کانفیگ را رد کرد!\nارور: `{error_msg}`", parse_mode='Markdown')
        finally:
            context.user_data['processing'] = False

    elif data.startswith("show_order_") or data.startswith("refresh_order_"):
        order_id = data.split("_")[2]
        alert = "🔄 بروزرسانی شد" if data.startswith("refresh_order_") else None
        await render_order_details(query, order_id, alert)

    elif data.startswith("delorder_"):
        order_id = data.split("_")[1]
        if not await ensure_order_access(query, order_id, admin_status): return
        await db.delete_order(order_id)
        orders = await db.get_orders_by_user(user_id)
        if not orders:
            try: await query.edit_message_text("🗑 سرویس حذف شد. سفارش دیگری ندارید.")
            except Exception: pass
            return
        keyboard = await generate_orders_keyboard(orders, page=0, search=context.user_data.get('order_search'))
        try: await query.edit_message_text("🗑 سرویس از لیست حذف شد.\n✅ سرویس خود را انتخاب کنید:", reply_markup=keyboard)
        except Exception: pass

    elif data == 'orders_cleanup':
        orders = await db.get_orders_by_user(user_id)
        if not orders:
            return await safe_answer(query, "سفارشی ندارید.", alert=True)
        stale, unknown_panels = await find_stale_orders(orders)
        if not stale:
            note = "\n\n⚠️ برخی پنل‌ها در دسترس نبودند و بررسی نشدند." if unknown_panels else ""
            return await safe_answer(query, f"✅ همه‌ی سرویس‌های شما روی پنل موجود هستند.{note}", alert=True)
        context.user_data['cleanup_ids'] = [o['id'] for o in stale]
        names = "\n".join(f"• {order_email(o) or o['id']}" for o in stale[:15])
        more = f"\n… و {len(stale) - 15} مورد دیگر" if len(stale) > 15 else ""
        kb = [
            [InlineKeyboardButton(f"🗑 بله، {len(stale)} مورد را حذف کن", callback_data='orders_cleanup_go')],
            [InlineKeyboardButton("🔙 انصراف", callback_data='back_to_orders')],
        ]
        await query.edit_message_text(
            f"🧹 این سرویس‌ها روی پنل پیدا نشدند و فقط از **لیست شما** حذف می‌شوند:\n\n{names}{more}\n\nحذف شوند؟",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown',
        )

    elif data == 'orders_cleanup_go':
        ids = context.user_data.pop('cleanup_ids', [])
        removed = 0
        for oid in ids:
            if await order_belongs_to(oid, user_id) or admin_status:
                await db.delete_order(oid)
                removed += 1
        orders = await db.get_orders_by_user(user_id)
        if not orders:
            try: await query.edit_message_text(f"🧹 پاک‌سازی انجام شد. {removed} سرویس از لیست حذف شد.\nسفارش دیگری ندارید.")
            except Exception as ex: logging.warning("cleanup edit failed: %s", ex)
            return
        keyboard = await generate_orders_keyboard(orders, page=0, search=context.user_data.get('order_search'))
        try:
            await query.edit_message_text(f"🧹 {removed} سرویس از لیست حذف شد.\n✅ سرویس خود را انتخاب کنید:", reply_markup=keyboard)
        except Exception as ex:
            logging.warning("cleanup edit failed: %s", ex)


    elif data == 'back_to_orders':
        orders = await db.get_orders_by_user(user_id)
        keyboard = await generate_orders_keyboard(orders, page=context.user_data.get('order_page', 0), search=context.user_data.get('order_search'))
        await query.edit_message_text("✅ سرویس خود را انتخاب کنید:", reply_markup=keyboard)

    elif data.startswith('orders_page_'):
        page = int(data.split('_')[2])
        context.user_data['order_page'] = page
        orders = await db.get_orders_by_user(user_id)
        keyboard = await generate_orders_keyboard(orders, page=page, search=context.user_data.get('order_search'))
        try: await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception: pass

    elif data == 'orders_search':
        context.user_data['state'] = 'waiting_order_search'
        await query.message.reply_text("🔎 بخشی از نام سرویس را بفرستید:", reply_markup=CANCEL_MARKUP)

    elif data == 'orders_clearsearch':
        context.user_data['order_search'] = None
        context.user_data['order_page'] = 0
        orders = await db.get_orders_by_user(user_id)
        keyboard = await generate_orders_keyboard(orders, page=0, search=None)
        try: await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception: pass

    elif data.startswith("approve_") and admin_status:
        parts = data.split("_")
        uid = int(parts[1])
        amt = int(parts[2])
        receipt_id = parts[3]
        async with db.db_pool.acquire() as conn:
            res = await conn.fetchval("SELECT status FROM receipts WHERE id = $1", receipt_id)
            if not res or res != 'pending': return await query.edit_message_caption(caption="⚠️ قبلاً بررسی شده.")
            await conn.execute("UPDATE receipts SET status = 'approved' WHERE id = $1", receipt_id)
        await credit_balance(uid, amt)
        logging.info("CHARGE_APPROVED admin=%s user=%s amount=%s receipt=%s", user_id, uid, amt, receipt_id)
        # هدیه‌ی اولین شارژ نماینده (فقط یک‌بار، بالای حداقل مبلغِ تعیین‌شده)
        bonus, percent = await pricing.reseller_bonus_on_topup(uid, amt)
        if bonus > 0:
            await credit_balance(uid, bonus, kind='هدیه نمایندگی', description=f'اولین شارژ نمایندگی ({percent}٪)')
            logging.info("RESELLER_BONUS user=%s amount=%s bonus=%s", uid, amt, bonus)
        await query.edit_message_caption(caption="✅ تایید شد." + (f" (+{bonus:,} هدیه)" if bonus else ""))
        try:
            msg = f"🎉 حساب شما {amt:,} شارژ شد."
            if bonus > 0:
                msg += f"\n🎁 هدیه‌ی اولین شارژ نمایندگی: {bonus:,} تومان ({percent}٪)\n💰 مجموع اعتبار افزوده‌شده: {amt + bonus:,} تومان"
            await context.bot.send_message(chat_id=uid, text=msg)
        except Exception as ex:
            logging.warning("اطلاع شارژ به کاربر %s ناموفق بود: %s", uid, ex)
        # پاداش معرف بعد از اولین شارژِ تاییدشده‌ی واجد شرایط
        reward = await db.try_reward_referrer(uid, amt)
        if reward and reward.invitee_bonus:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"🎁 هدیه ورود با دعوت: {reward.invitee_bonus:,} تومان به حساب شما اضافه شد.",
                )
            except Exception as ex:
                logging.warning("اطلاع هدیه دعوت به کاربر %s ناموفق بود: %s", uid, ex)
        if reward and reward.referrer_bonus:
            try:
                await context.bot.send_message(
                    chat_id=reward.referrer_id,
                    text=f"🎁 یکی از دعوت‌شدگان شما شارژ کرد! {reward.referrer_bonus:,} تومان پاداش به حساب شما اضافه شد.",
                )
            except Exception as ex:
                logging.warning("اطلاع پاداش دعوت به %s ناموفق بود: %s", reward.referrer_id, ex)

    elif data.startswith("reject_") and admin_status:
        parts = data.split("_")
        uid = int(parts[1])
        receipt_id = parts[2]
        async with db.db_pool.acquire() as conn:
            res = await conn.fetchval("SELECT status FROM receipts WHERE id = $1", receipt_id)
            if not res or res != 'pending': return await query.edit_message_caption(caption="⚠️ قبلاً بررسی شده.")
            await conn.execute("UPDATE receipts SET status = 'rejected' WHERE id = $1", receipt_id)
        await query.edit_message_caption(caption="❌ رد شد.")
        try: await context.bot.send_message(chat_id=uid, text="❌ رسید شما تایید نشد.")
        except Exception as ex:
            logging.warning("اطلاع رد رسید به کاربر %s ناموفق بود: %s", uid, ex)

# ================= پرداخت با Telegram Stars =================
async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تأیید پیش‌پرداخت استارز. فقط فاکتورهای خودمان (stars_topup) قبول می‌شود."""
    q = update.pre_checkout_query
    if q.currency == 'XTR' and q.invoice_payload.startswith('stars_topup:'):
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="پرداخت نامعتبر است. دوباره تلاش کنید.")


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پرداخت موفق استارز → شارژ کیف پول به‌صورت خودکار."""
    sp = update.message.successful_payment
    user_id = update.message.from_user.id
    try:
        payload = sp.invoice_payload or ''
        if not payload.startswith('stars_topup:'):
            raise ValueError("payload ناشناس")
        toman = int(payload.split(':', 1)[1])
        if toman <= 0:
            raise ValueError("مبلغ نامعتبر")
    except (ValueError, IndexError):
        logging.error("STARS_PAY payload خراب: %r (user=%s)", getattr(sp, 'invoice_payload', None), user_id)
        await update.message.reply_text("⚠️ پرداخت انجام شد اما ثبت خودکار ناموفق بود؛ با پشتیبانی تماس بگیرید.")
        return
    await credit_balance(user_id, toman, kind='شارژ حساب', description=f"پرداخت با استارز ({sp.total_amount} ⭐️)")
    balance, _nick, _role = await get_user(user_id)
    logging.info("STARS_PAY user=%s stars=%s toman=%s charge_id=%s", user_id, sp.total_amount, toman, sp.telegram_payment_charge_id)
    await update.message.reply_text(
        f"✅ پرداخت با استارز موفق بود!\n⭐️ {sp.total_amount} استارز → 💰 {toman:,} تومان\n"
        f"موجودی جدید: {balance:,} تومان",
        reply_markup=await get_main_keyboard(user_id),
    )


async def panel_health_job(context: ContextTypes.DEFAULT_TYPE):
    """هشدار قطعی/برگشت پنل‌ها به ادمین‌ها (هر ۳۰ دقیقه؛ الگوریتم: ۲ بار پشت‌سرهم down)."""
    try:
        panels = await db.get_panels()
    except Exception:
        return
    if not panels:
        return
    try:
        import webcomponents as wc
        results = await asyncio.wait_for(wc.ping_panels(panels, timeout=6), timeout=90)
    except Exception as ex:
        logging.warning("panel_health_job ping ناموفق: %s", ex)
        return
    for panel, res in zip(panels, results):
        pid = panel['id']
        alive = res.get('state') != 'down'
        prev = _PANEL_FAIL.get(pid, 0)
        if alive:
            if prev >= 2:
                await _alert_admins(context, f"🟢 پنل «{panel['name']}» دوباره در دسترس است.")
            _PANEL_FAIL[pid] = 0
        else:
            _PANEL_FAIL[pid] = prev + 1
            if prev + 1 == 2:
                await _alert_admins(context, f"🔴 پنل «{panel['name']}» ({panel['url']}) پاسخ نمی‌دهد!\n"
                                             f"سرویس‌های این پنل ممکن است قطع باشند؛ سریع بررسی کنید.")


async def _alert_admins(context, text):
    for adm in await get_all_admins():
        if not adm:
            continue
        try:
            await context.bot.send_message(adm, text)
        except Exception:
            pass


_PANEL_FAIL = {}


async def setup_db(app: Application):
    await init_db()
    # اجرای پنل وب مدیریت (در صورت تنظیم WEB_ADMIN_PASSWORD) داخل همان event loop
    try:
        import webpanel
        app.bot_data['web_runner'] = await webpanel.start_web(app.bot)
    except Exception as e:
        logging.error("راه‌اندازی پنل وب ناموفق بود: %s", e)
    logging.info("🚀 ربات با موفقیت استارت شد...")


async def on_shutdown(app: Application):
    runner = app.bot_data.get('web_runner')
    if runner:
        try:
            await runner.cleanup()
        except Exception:
            pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """هندلر سراسری خطا تا استثناهای مدیریت‌نشده لاگ شوند به‌جای اینکه بی‌صدا گم شوند."""
    logging.error("استثنای مدیریت‌نشده هنگام پردازش آپدیت", exc_info=context.error)


async def _notify_user(context, order_row, text):
    kb = [[InlineKeyboardButton("♻️ تمدید سریع", callback_data=f"renew_menu_{order_row['id']}")]]
    try:
        await context.bot.send_message(chat_id=order_row['user_id'], text=text, reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        pass


async def make_backup_bytes():
    """با pg_dump یک بکاپ متنی از دیتابیس می‌سازد. خروجی: (data_bytes, error_str)."""
    env = os.environ.copy()
    env['PGPASSWORD'] = DB_PASS
    try:
        proc = await asyncio.create_subprocess_exec(
            'pg_dump', '-h', DB_HOST, '-p', str(DB_PORT), '-U', DB_USER, '-d', DB_NAME,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return None, (err.decode(errors='ignore')[:500] or 'pg_dump failed')
        return out, None
    except FileNotFoundError:
        return None, "ابزار pg_dump روی سرور نصب نیست (بسته‌ی postgresql-client را نصب کنید)."
    except Exception as e:
        return None, str(e)


async def _send_backup_to(context, chat_id):
    data, err = await make_backup_bytes()
    if not data:
        try: await context.bot.send_message(chat_id, f"❌ بکاپ ناموفق: {err}")
        except Exception: pass
        return False
    bio = io.BytesIO(data)
    bio.name = f"backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.sql"
    try:
        await context.bot.send_document(chat_id, document=bio, caption="💾 بکاپ دیتابیس")
        return True
    except Exception:
        return False


async def backup_job(context: ContextTypes.DEFAULT_TYPE):
    if (await get_setting('backup_enabled')) != 'on':
        return
    admins = await db.get_all_admins()
    data, err = await make_backup_bytes()
    for a in admins:
        if not a:
            continue
        if not data:
            try: await context.bot.send_message(a, f"❌ بکاپ خودکار ناموفق: {err}")
            except Exception: pass
            continue
        bio = io.BytesIO(data)
        bio.name = f"backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.sql"
        try: await context.bot.send_document(a, document=bio, caption="💾 بکاپ خودکار دیتابیس")
        except Exception: pass


async def _try_auto_renew(context, o, xui):
    """تمدید خودکار یک سفارش با پلنِ ثبت‌شده‌اش. خروجی: (موفق؟، پیام وضعیت)."""
    plan = await db.get_plan(o['auto_renew_plan_id'])
    if not plan or not plan['is_active']:
        return False, "پلن تمدید ناموجود/غیرفعال"
    quote = await pricing.quote_for_user(o['user_id'], plan, KIND_RENEW, admin=is_super_admin(o['user_id']))
    if not quote.available:
        return False, "پلن برای سطح شما موجود نیست"
    price = quote.price
    if await deduct_balance(o['user_id'], price, kind='تمدید خودکار') is None:
        return False, "موجودی ناکافی"
    try:
        _bal, _nick, role = await db.get_user(o['user_id'])
        inbound_id, _port, client_dict = await xui.get_client_exact_info(o['email'])
        if not client_dict:
            raise RuntimeError("کلاینت روی پنل پیدا نشد")
        duration_days = plan['duration_days'] or 30
        client_dict['totalGB'] = int(plan['gb']) * 1024 * 1024 * 1024
        client_dict['up'] = 0
        client_dict['down'] = 0
        client_dict['expiryTime'] = int((time.time() + (duration_days * 86400)) * 1000)
        client_dict['enable'] = True
        if not await xui.update_client(inbound_id, client_key(client_dict), client_dict):
            raise RuntimeError("سرور تمدید را رد کرد")
        await xui.reset_client_traffic(inbound_id, o['email'])
        await db.reset_notify(o['id'])
        await db.update_order_service(o['id'], inbound_id=inbound_id)
        await pricing.commit_quote(o['user_id'], quote, KIND_RENEW, plan['id'], order_id=o['id'], role=role)
        logging.info("AUTO_RENEW order=%s user=%s plan=%s price=%s", o['id'], o['user_id'], plan['id'], price)
        return True, f"✅ تمدید خودکار شد: {plan['name']} ({price:,} تومان)"
    except Exception as ex:
        if price > 0:
            await credit_balance(o['user_id'], price, kind='برگشت وجه')
        logging.warning("AUTO_RENEW order=%s ناموفق: %s", o['id'], ex)
        return False, f"خطای پنل: {ex}"


async def notify_job(context: ContextTypes.DEFAULT_TYPE):
    """یادآوری هوشمند سه‌مرحله‌ای + تمدید خودکار.

    مراحل انقضا (هر کدام فقط یک‌بار): هشدار اولیه (notify_days)، فوری (۱ روز)،
    منقضی‌شده. سرویس‌های دارای تمدید خودکار قبل از یادآوری تلاش می‌شوند.
    """
    from collections import defaultdict
    try:
        days = int(await get_setting('notify_days') or 3)
    except (TypeError, ValueError):
        days = 3
    orders = await db.get_orders_for_smart_notify()
    auto_orders = await db.get_auto_renew_orders()
    auto_by_id = {o['id']: o for o in auto_orders}
    by_panel = defaultdict(list)
    for o in orders:
        by_panel[o['panel_id']].append(o)
    # سفارش‌های تمدید خودکار که هنوز در فهرست یادآوری نیستند (مرحله‌هایشان تمام شده ولی سرویس فعال است)
    for oid, o in auto_by_id.items():
        if not any(x['id'] == oid for x in orders):
            by_panel[o['panel_id']].append(o)

    now_ms = int(time.time() * 1000)
    for panel_id, group in by_panel.items():
        panel = await db.get_panel(panel_id) if panel_id else None
        if panel is None and not PANEL_URL:
            continue
        xui, _ip = build_xui(panel)
        ok, _ = await xui.login()
        if not ok:
            continue
        stats_map = await xui.get_all_client_stats()
        if not stats_map:
            continue
        for o in group:
            email = order_email(o)
            if not email:
                continue
            st = stats_map.get(email.strip().lower())
            if not st or not st.get('enable', False):
                continue
            total = st.get('total', 0)
            used = st.get('up', 0) + st.get('down', 0)
            expiry = st.get('expiryTime', 0)
            remain_ms = (expiry - now_ms) if expiry > 0 else 0
            remain_days = remain_ms / 86400000.0 if expiry > 0 else 999

            # ---------- تمدید خودکار (۱ روز قبل از انقضا) ----------
            if o['id'] in auto_by_id:
                if remain_ms <= 86400000 and remain_ms > -3 * 86400000:
                    done, info = await _try_auto_renew(context, auto_by_id[o['id']], xui)
                    if done:
                        try:
                            await context.bot.send_message(
                                o['user_id'],
                                f"🤖 تمدید خودکار انجام شد!\n🏷 سرویس: `{email}`\n{info}",
                                parse_mode='Markdown',
                            )
                        except Exception:
                            pass
                        continue  # انقضا تازه شد؛ یادآوری لازم نیست
                    else:
                        # تمدید خودکار شکست → به‌جایش یادآوری فوری بفرست (فقط یک‌بار: مرحله ۲)
                        if not o.get('notify_1d'):
                            await _notify_user(context, o,
                                f"🤖⚠️ تمدید خودکار سرویس «{email}» انجام نشد ({info}).\n"
                                f"موجودی را شارژ یا از دکمه‌ی زیر تمدید دستی بزنید.")
                            await db.mark_smart_notify(o['id'], 'notify_1d')
                        continue

            # ---------- یادآوری سه‌مرحله‌ای ----------
            if expiry > 0:
                if remain_ms <= 0 and not o.get('notify_expired'):
                    await _notify_user(context, o,
                        f"⛔️ سرویس «{email}» منقضی شده است و قطع شده.\nهمین حالا تمدید کنید تا با همان نام و UUID فعال شود.")
                    await db.mark_smart_notify(o['id'], 'notify_expired')
                elif 0 < remain_days <= 1 and not o.get('notify_1d'):
                    await _notify_user(context, o,
                        f"🔴 سرویس «{email}» فقط {max(1, int(remain_ms // 3600000))} ساعت دیگر منقضی می‌شود!\n"
                        f"برای جلوگیری از قطعی، همین حالا تمدید کنید.")
                    await db.mark_smart_notify(o['id'], 'notify_1d')
                elif 1 < remain_days <= days and not o.get('notify_3d'):
                    await _notify_user(context, o,
                        f"⏳ سرویس «{email}» تا حدود {int(remain_days) + 1} روز دیگر منقضی می‌شود.\n"
                        f"برای جلوگیری از قطعی، همین حالا تمدید کنید.")
                    await db.mark_smart_notify(o['id'], 'notify_3d')

            # ---------- اتمام حجم (کمتر از ۱۰٪ باقی‌مانده) ----------
            if total > 0 and not o.get('lowdata_notified'):
                remain = total - used
                if remain <= total * 0.1:
                    await _notify_user(context, o, f"📉 حجم سرویس «{email}» رو به اتمام است ({format_size(max(remain, 0))} باقی‌مانده).\nبرای ادامه‌ی استفاده، سرویس را تمدید کنید.")
                    await db.mark_notified(o['id'], 'lowdata_notified')

def main():
    if not TOKEN:
        raise SystemExit("❌ متغیر محیطی BOT_TOKEN تنظیم نشده است.")
    if not DB_PASS:
        raise SystemExit("❌ متغیر محیطی DB_PASS تنظیم نشده است. مقدار آن را در فایل .env بگذارید.")

    # دو نمونه‌ی جدا: long-polling نباید اتصالی از pool درخواست‌های معمولی را اشغال کند.
    # تایم‌اوت‌ها برای شبکه‌های کند یکسان تنظیم شده‌اند (قبلاً بدون پروکسی ۵ ثانیه بود).
    def _request():
        kwargs = dict(connection_pool_size=20, connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
        if PROXY_URL:
            kwargs['proxy'] = PROXY_URL
        return HTTPXRequest(**kwargs)

    # ساخت ربات و اعمال تنظیمات
    app = (
        Application.builder().token(TOKEN)
        .request(_request()).get_updates_request(_request())
        .post_init(setup_db).post_shutdown(on_shutdown).build()
    )

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    # فقط چت خصوصی: اگر ربات به گروهی اضافه شود نباید هر پیام گروه پردازش و ثبت شود
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_all_messages))
    app.add_handler(CallbackQueryHandler(button_handler))
    # اجرای دوره‌ای هشدار انقضا/اتمام حجم (هر ۶ ساعت)
    if app.job_queue:
        app.job_queue.run_repeating(notify_job, interval=21600, first=120)
        # بکاپ خودکار دیتابیس (هر ۲۴ ساعت)
        app.job_queue.run_repeating(backup_job, interval=86400, first=300)
        # سلامت پنل‌ها: هر ۳۰ دقیقه + ۲ بار پشت‌سرهم down = هشدار به ادمین‌ها
        app.job_queue.run_repeating(panel_health_job, interval=1800, first=90)
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()