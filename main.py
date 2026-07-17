import os
import asyncio
import uuid
import time
import datetime
import re
import io
import urllib.parse
import logging
from urllib.parse import unquote

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.request import HTTPXRequest

from config import (
    TOKEN, PROXY_URL, SUPER_ADMIN_ID, PANEL_URL, PANEL_USER, PANEL_PASS,
    CONFIG_IP, SECURITY_WARNING, format_size, clean_num,
    DB_USER, DB_PASS, DB_NAME, DB_HOST, DB_PORT,
)
import db
from db import (
    init_db, is_admin, get_all_admins, get_setting, update_setting, get_user,
    update_balance, deduct_balance, credit_balance, get_balance, add_order,
    get_order_by_id, order_belongs_to,
)
from panel import AsyncXuiAPI, build_xui, sub_link_for, build_vless_link
from keyboards import get_main_keyboard, generate_orders_keyboard, CANCEL_MARKUP


async def get_order_xui(order_id):
    """کلاینت X-UI و config_ip متناسب با پنلِ همان سفارش را برمی‌گرداند."""
    panel = await db.get_panel(await db.get_order_panel_id(order_id))
    return build_xui(panel)


def can_buy_bulk(role, admin_status):
    """خرید عمده فقط برای VIP و ادمین."""
    return admin_status or role == 'vip'


async def resolve_plan_price(user_id, plan, role, bulk=False):
    """قیمت پلن با اولویت: قیمت اختصاصی ← VIP ← عادی."""
    async with db.db_pool.acquire() as conn:
        custom = await conn.fetchrow(
            "SELECT price, bulk_price FROM custom_prices WHERE user_id=$1 AND plan_id=$2",
            user_id, plan['id'],
        )
    if bulk:
        if custom and custom['bulk_price'] is not None:
            return int(custom['bulk_price'])
        return int(plan['vip_bulk_price'] if role == 'vip' else plan['bulk_price'])
    if custom and custom['price'] is not None:
        return int(custom['price'])
    return int(plan['vip_price'] if role == 'vip' else plan['price'])


# ================= محدودیت نرخ (ضدِ اسپم) =================
from collections import defaultdict, deque

_RATE_LIMIT = 10        # حداکثر تعداد رویداد
_RATE_WINDOW = 5.0      # در این بازه (ثانیه)
_user_hits = defaultdict(deque)


def _rate_ok(user_id):
    """پنجره‌ی لغزان ساده برای جلوگیری از اسپم و فشار روی دیتابیس/پنل."""
    now = time.monotonic()
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
    ("price", "قیمت عادی", "price", True),
    ("vipprice", "قیمت VIP", "vip_price", True),
    ("bulk", "قیمت عمده", "bulk_price", True),
    ("vipbulk", "عمده VIP", "vip_bulk_price", True),
    ("inbound", "اینباند", "inbound_id", True),
    ("panel", "پنل", "panel_id", True),
]
PLAN_FIELD_MAP = {f[0]: f for f in PLAN_FIELDS}


def plan_edit_markup(plan_id):
    rows = [[InlineKeyboardButton(f"✏️ {label}", callback_data=f"pef_{key}_{plan_id}")] for key, label, _col, _isnum in PLAN_FIELDS]
    rows.append([InlineKeyboardButton("✅ پایان", callback_data="pe_done")])
    return InlineKeyboardMarkup(rows)


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


async def plan_edit_text(plan):
    panel = await db.get_panel(plan['panel_id']) if plan['panel_id'] else None
    pname = panel['name'] if panel else "—"
    return (
        f"✏️ **ویرایش پلن** (ID: `{plan['id']}`)\n\n"
        f"📋 نام: {plan['name']}\n"
        f"💾 حجم: {plan['gb']} GB\n"
        f"📅 مدت: {plan['duration_days']} روز\n"
        f"💰 قیمت عادی: {plan['price']:,}\n"
        f"💎 قیمت VIP: {plan['vip_price']:,}\n"
        f"📦 عمده: {plan['bulk_price']:,}\n"
        f"👑 عمده VIP: {plan['vip_bulk_price']:,}\n"
        f"⚙️ اینباند: {plan['inbound_id']}\n"
        f"🖥 پنل: {pname}\n\n"
        f"برای تغییر هر مورد، دکمه‌اش را بزنید."
    )


async def ensure_order_access(query, order_id, admin_status):
    """کنترل دسترسی به سفارش. ادمین‌ها به همه‌چیز دسترسی دارند؛ کاربر عادی فقط به سفارش خودش."""
    if admin_status or await order_belongs_to(order_id, query.from_user.id):
        return True
    await query.answer("⛔️ این سفارش متعلق به شما نیست.", show_alert=True)
    return False


async def render_order_details(query, order_id, alert_msg=None):
    if not await is_admin(query.from_user.id) and not await order_belongs_to(order_id, query.from_user.id):
        await query.answer("⛔️ این سفارش متعلق به شما نیست.", show_alert=True)
        return
    res = await get_order_by_id(order_id)
    if not res: 
        await query.answer("سفارش یافت نشد!", show_alert=True)
        return
        
    link, _ = res
    email = unquote(link.split("#")[-1])
    xui, _ip = await get_order_xui(order_id)
    is_logged, _ = await xui.login()
    
    if not is_logged:
        await query.answer("❌ خطا در اتصال به سرور پنل.", show_alert=True)
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
        
    msg = f"🏷 سرویس: `{email}`\n📡 وضعیت: {status_text}\n🔋 کل: {total_fmt}\n📊 مصرف: {used_fmt}\n📉 باقی‌مانده: {remain_fmt}\n📅 انقضا: {exp_str}"
    
    kb = [
        [InlineKeyboardButton("تغییر وضعیت 🔄", callback_data=f'toggle_status_{order_id}'), InlineKeyboardButton("تغییر UUID 🔑", callback_data=f'change_uuid_{order_id}')],
        [InlineKeyboardButton("تغییر نام 📝", callback_data=f'rename_conf_{order_id}'), InlineKeyboardButton("📊 بروزرسانی", callback_data=f'refresh_order_{order_id}')], 
        [InlineKeyboardButton("📥 دریافت کانفیگ و QR", callback_data=f'get_conf_{order_id}')],
        [InlineKeyboardButton("♻️ تمدید و تغییر پلن سرویس", callback_data=f'renew_menu_{order_id}')],
        [InlineKeyboardButton("🔙 بازگشت", callback_data='back_to_orders')]
    ]
    if alert_msg: await query.answer(alert_msg, show_alert=True)
    else: await query.answer()
        
    try: await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except: pass

# ================= هندلرهای اصلی =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    is_new = await db.is_new_user(user_id)
    await get_user(user_id)
    # پردازش لینک دعوت: /start ref_<referrer_id>
    if is_new and context.args:
        arg = context.args[0]
        if arg.startswith("ref_") and arg[4:].isdigit():
            referrer_id = int(arg[4:])
            await db.set_referrer(user_id, referrer_id)
    context.user_data['state'] = 'none'
    await update.message.reply_text("به ربات OverWallVpn خوش آمدید.", reply_markup=await get_main_keyboard(user_id))

async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.message.from_user.id
    if not _rate_ok(user_id):
        return  # ضدِ اسپم: پیام‌های اضافی بی‌صدا نادیده گرفته می‌شوند
    balance, nickname, role, can_bulk = await get_user(user_id)
    state = context.user_data.get('state', 'none')
    admin_status = await is_admin(user_id)
    
    # 📢 Broadcast
    if state == 'admin_waiting_broadcast' and admin_status:
        async with db.db_pool.acquire() as conn: users = [r['user_id'] for r in await conn.fetch("SELECT user_id FROM users")]
        success = 0
        wait_msg = await update.message.reply_text("در حال ارسال پیام همگانی... ⏳")
        for u in users:
            try: 
                await context.bot.copy_message(chat_id=u, from_chat_id=user_id, message_id=update.message.message_id)
                success += 1
            except: pass
        context.user_data['state'] = 'none'
        await wait_msg.edit_text(f"✅ پیام با موفقیت به {success} کاربر ارسال شد.")
        return

    # 📸 Receipts
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
            except: pass
                
        context.user_data['state'] = 'none'
        msg = "✅ رسید در صف بررسی قرار گرفت." if sent else "⚠️ سیستم نتوانست رسید را بفرستد."
        await update.message.reply_text(msg, reply_markup=await get_main_keyboard(user_id))
        return

    text = update.message.text
    if not text: return

    if text == 'لغو ❌':
        context.user_data['state'] = 'none'
        await update.message.reply_text("عملیات لغو شد.", reply_markup=await get_main_keyboard(user_id))
        return

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
            port = await xui.get_inbound_port(int(inbound_setting))
            if not port:
                return await update.message.reply_text("❌ اینباند اکانت تست پیدا نشد.", reply_markup=await get_main_keyboard(user_id))
            test_name = f"{user_id}_test_{str(uuid.uuid4())[:4]}"
            new_uuid, err = await xui.add_client(int(inbound_setting), test_name, test_gb, test_days, 1)
            if not new_uuid:
                return await update.message.reply_text(f"❌ ساخت اکانت تست ناموفق بود.\n{err}", reply_markup=await get_main_keyboard(user_id))
            config_link = build_vless_link(new_uuid, cfg_ip, port, test_name)
            await add_order(user_id, config_link, opid)
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

    MAIN_BUTTONS = ['خرید عمده 📦', 'محصولات 🛍', 'کیف پول من 💰', 'پشتیبانی 📞', 'سفارشات من 📦', 'شارژ حساب 💳', 'مدیریت ⚙️']
    if text in MAIN_BUTTONS:
        context.user_data['state'] = 'none'
            
        if text == 'مدیریت ⚙️' and admin_status:
            status = await get_setting('sales_status')
            status_text = "🟢 باز" if status == 'open' else "🔴 بسته"
            kb = [
                [InlineKeyboardButton("محصولات و پلن‌ها 🛒", callback_data='admin_manage_plans')],
                [InlineKeyboardButton("لیست VIP و ادمین‌ها 📋", callback_data='admin_list_specials')],
                [InlineKeyboardButton("قیمت اختصاصی کاربر 💎", callback_data='admin_custom_price'), InlineKeyboardButton("کارت 💳", callback_data='admin_set_card')],
                [InlineKeyboardButton("افزودن VIP 🟢", callback_data='admin_add_vip'), InlineKeyboardButton("حذف VIP 🔴", callback_data='admin_rem_vip')],
                [InlineKeyboardButton("ارسال پیام 📢", callback_data='admin_broadcast')],
                [InlineKeyboardButton("ایمپورت کانفیگ 🔗", callback_data='admin_import_config'), InlineKeyboardButton("پشتیبانی 📞", callback_data='admin_set_support')],
                [InlineKeyboardButton("مدیریت پنل‌ها 🖥", callback_data='admin_manage_panels'), InlineKeyboardButton("اکانت تست 🎁", callback_data='admin_test_menu')],
                [InlineKeyboardButton("📊 گزارش فروش", callback_data='admin_report'), InlineKeyboardButton("🔔 هشدار انقضا", callback_data='admin_set_notify')],
                [InlineKeyboardButton("🎟 کدهای هدیه", callback_data='admin_gift_menu'), InlineKeyboardButton("🎁 پاداش دعوت", callback_data='admin_set_refbonus')],
                [InlineKeyboardButton("💾 بکاپ دیتابیس", callback_data='admin_backup_menu')],
                [InlineKeyboardButton(f"وضعیت فروش: {status_text}", callback_data='admin_toggle_sales')]
            ]
            if user_id == SUPER_ADMIN_ID: kb.append([InlineKeyboardButton("افزودن ادمین 👮‍♂️", callback_data='superadmin_add_admin'), InlineKeyboardButton("حذف ادمین ⛔️", callback_data='superadmin_rem_admin')])
            await update.message.reply_text("⚙️ پنل مدیریت اختصاصی:\n(خرید عمده فقط برای کاربران VIP فعال است)", reply_markup=InlineKeyboardMarkup(kb))
            
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
            async with db.db_pool.acquire() as conn:
                plans = await conn.fetch("SELECT * FROM plans ORDER BY price ASC")
            if not plans: return await update.message.reply_text("🛒 هنوز هیچ محصولی اضافه نشده است.")
            kb = []
            for p in plans:
                price = await resolve_plan_price(user_id, p, role, bulk=False)
                days = p['duration_days'] or 30
                kb.append([InlineKeyboardButton(f"{p['name']} | {p['gb']}GB / {days}روز - {price:,} تومان", callback_data=f"prebuy_{p['id']}")])
            await update.message.reply_text("🛍 محصول مورد نظر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))

        elif text == 'خرید عمده 📦':
            if not can_buy_bulk(role, admin_status):
                return await update.message.reply_text("❌ خرید عمده فقط برای کاربران VIP فعال است.")
            if await get_setting('sales_status') == 'closed': return await update.message.reply_text("⛔️ فروش بسته است.")
            async with db.db_pool.acquire() as conn:
                plans = await conn.fetch("SELECT * FROM plans ORDER BY bulk_price ASC")
            if not plans: return await update.message.reply_text("🛒 هیچ محصولی برای عمده موجود نیست.")
            kb = []
            for p in plans:
                price = await resolve_plan_price(user_id, p, role, bulk=True)
                days = p['duration_days'] or 30
                kb.append([InlineKeyboardButton(f"عمده {p['name']} | {p['gb']}GB / {days}روز - دونه‌ای {price:,}T", callback_data=f"bulkbuy_{p['id']}")])
            await update.message.reply_text("📦 لطفاً پلن خرید گروهی را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))

        elif text == 'سفارشات من 📦':
            async with db.db_pool.acquire() as conn: orders = await conn.fetch("SELECT id, config_link, date, panel_id FROM orders WHERE user_id = $1", user_id)
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
        async with db.db_pool.acquire() as conn: orders = await conn.fetch("SELECT id, config_link, date, panel_id FROM orders WHERE user_id = $1", user_id)
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
            context.user_data['state'] = f'waiting_receipt_{amount}'
            card = await get_setting('card_number')
            await update.message.reply_text(f"💳 لطفاً مبلغ **{amount:,} تومان** را به شماره کارت زیر واریز کنید:\n\n`{card}`\n\n📸 سپس **عکس رسید واریزی** را همینجا ارسال کنید.", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
        except ValueError:
            await update.message.reply_text("❌ لطفاً مبلغ را فقط به صورت عدد (بدون حروف) وارد کنید:")
        return

    # ================= ماشین وضعیت ایمپورت گروهی کانفیگ =================
    if state == 'admin_waiting_import' and admin_status:
        parts = text.split()
        if len(parts) >= 2 and parts[0].isdigit():
            target_user_id = int(parts[0])
            # هر توکن می‌تواند نام کانفیگ (email) یا یک لینک کامل vless://...#email باشد
            wanted = []
            for tok in parts[1:]:
                em = unquote(tok.split("#")[-1]).strip() if "#" in tok else tok.strip()
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
                async with db.db_pool.acquire() as conn:
                    rows = await conn.fetch("SELECT config_link FROM orders WHERE user_id = $1", target_user_id)
                existing_emails = set()
                for r in rows:
                    l = r['config_link']
                    if "#" in l:
                        existing_emails.add(unquote(l.split("#")[-1]).strip().lower())

                success_count = 0
                skipped = []
                failed_emails = []
                per_panel = {}

                for email in wanted:
                    if email.lower() in existing_emails:
                        skipped.append(email)
                        continue
                    found = False
                    for prow, x, ip, ok in panel_clients:
                        if not ok:
                            continue
                        inbound_id, port, client_dict = await x.get_client_exact_info(email)
                        if client_dict:
                            client_uuid = client_dict['id']
                            real_email_from_panel = client_dict.get('email', email)
                            pid = prow['id'] if prow else None
                            pname = prow['name'] if prow else "پیش‌فرض"
                            config_link = build_vless_link(client_uuid, ip, port, real_email_from_panel)
                            await add_order(target_user_id, config_link, pid)
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
                    report += f"↩️ تکراری (رد شد): {len(skipped)}\n"
                if failed_emails:
                    report += f"❌ یافت نشد: {', '.join(failed_emails)}"
                await update.message.reply_text(report, reply_markup=await get_main_keyboard(user_id))
        else:
            await update.message.reply_text("❌ فرمت اشتباه! اول آیدی عددی کاربر، سپس نام کانفیگ‌ها یا لینک‌های کامل.")
        return

    # ================= ماشین وضعیت خرید عمده مرحله‌ای =================
    if state.startswith('bulk_waiting_'):
        if not can_buy_bulk(role, admin_status):
            context.user_data['state'] = 'none'
            return await update.message.reply_text("❌ خرید عمده فقط برای کاربران VIP فعال است.", reply_markup=await get_main_keyboard(user_id))

    if state == 'bulk_waiting_prefix':
        context.user_data['b_prefix'] = text.strip().replace(" ", "_")
        context.user_data['state'] = 'bulk_waiting_start'
        await update.message.reply_text("🔢 شماره شروع را وارد کنید (مثلاً اگر میخواهید از ali1 شروع شود، بنویسید 1):", reply_markup=CANCEL_MARKUP)
        return

    if state == 'bulk_waiting_start':
        if text.isdigit():
            context.user_data['b_start'] = int(text)
            context.user_data['state'] = 'bulk_waiting_end'
            await update.message.reply_text("🔢 شماره پایان را وارد کنید (مثلاً اگر میخواهید تا ali10 ساخته شود، بنویسید 10):", reply_markup=CANCEL_MARKUP)
        else:
            await update.message.reply_text("❌ لطفاً فقط عدد وارد کنید.")
        return

    if state == 'bulk_waiting_end':
        if text.isdigit():
            end_n = int(text)
            start_n = context.user_data['b_start']
            if end_n < start_n:
                return await update.message.reply_text("❌ شماره پایان نمی‌تواند از شماره شروع کوچکتر باشد.")
            if (end_n - start_n) >= 100:
                return await update.message.reply_text("❌ مجاز به ساخت حداکثر 100 کانفیگ در هر بار هستید.")

            context.user_data['b_end'] = end_n
            context.user_data['state'] = 'bulk_waiting_postfix'
            await update.message.reply_text("🔡 پسوند (Postfix) را وارد کنید:\nاگر پسوند نمی‌خواهید کلمه `خیر` یا عدد `0` را بفرستید.", reply_markup=CANCEL_MARKUP)
        else:
            await update.message.reply_text("❌ لطفاً فقط عدد وارد کنید.")
        return

    if state == 'bulk_waiting_postfix':
        postfix = "" if text in ['0', '-', 'ندارد', 'خیر'] else text.strip().replace(" ", "_")
        plan_id = context.user_data['b_plan']
        prefix = context.user_data['b_prefix']
        start_n = context.user_data['b_start']
        end_n = context.user_data['b_end']
        count = end_n - start_n + 1

        context.user_data['b_postfix'] = postfix
        context.user_data['b_count'] = count

        async with db.db_pool.acquire() as conn:
            plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", int(plan_id))
        if not plan:
            context.user_data['state'] = 'none'
            return await update.message.reply_text("❌ پلن یافت نشد.", reply_markup=await get_main_keyboard(user_id))

        unit_price = await resolve_plan_price(user_id, plan, role, bulk=True)
        total = unit_price * count
        days = plan['duration_days'] or 30

        kb = [[InlineKeyboardButton("✅ تایید و ساخت", callback_data="confirm_execute_bulk")]]
        msg = (
            f"📦 **تعداد ساخت:** {count} عدد\n"
            f"نمونه نام: `{prefix}{start_n}{postfix}`\n"
            f"پلن: {plan['name']} ({plan['gb']}GB / {days}روز)\n"
            f"قیمت واحد: {unit_price:,} | قیمت کل: {total:,} تومان\n\nتایید می‌کنید؟"
        )
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        context.user_data['state'] = 'none'
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

    # ================= ماشین وضعیت قیمت اختصاصی (ویزارد) =================
    if state == 'cp_uid' and admin_status:
        if not text.isdigit():
            return await update.message.reply_text("❌ فقط آیدی عددی کاربر را بفرستید.")
        context.user_data['cp_uid'] = int(text)
        async with db.db_pool.acquire() as conn: plans = await conn.fetch("SELECT id, name, price, bulk_price FROM plans ORDER BY id ASC")
        if not plans:
            context.user_data['state'] = 'none'
            return await update.message.reply_text("❌ هیچ پلنی وجود ندارد.", reply_markup=await get_main_keyboard(user_id))
        kb = [[InlineKeyboardButton(f"{p['name']} (پیش‌فرض {p['price']:,} / عمده {p['bulk_price']:,})", callback_data=f"cpplan_{p['id']}")] for p in plans]
        context.user_data['state'] = 'cp_choose'
        await update.message.reply_text("کدام پلن؟", reply_markup=InlineKeyboardMarkup(kb))
        return

    if state == 'cp_single' and admin_status:
        try:
            context.user_data['cp_single'] = clean_num(text)
        except ValueError:
            return await update.message.reply_text("❌ فقط عدد بفرستید.")
        context.user_data['state'] = 'cp_bulk'
        await update.message.reply_text("💰 قیمت اختصاصی **عمده** (دونه‌ای) را بفرستید:", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
        return

    if state == 'cp_bulk' and admin_status:
        try:
            c_bulk = clean_num(text)
        except ValueError:
            return await update.message.reply_text("❌ فقط عدد بفرستید.")
        uid = context.user_data.get('cp_uid')
        plan_id = context.user_data.get('cp_plan')
        c_price = context.user_data.get('cp_single')
        if uid is None or plan_id is None or c_price is None:
            context.user_data['state'] = 'none'
            return await update.message.reply_text("❌ اطلاعات ناقص است، دوباره از منو شروع کنید.", reply_markup=await get_main_keyboard(user_id))
        async with db.db_pool.acquire() as conn:
            await conn.execute("INSERT INTO custom_prices (user_id, plan_id, price, bulk_price) VALUES ($1, $2, $3, $4) ON CONFLICT (user_id, plan_id) DO UPDATE SET price=$3, bulk_price=$4", uid, plan_id, c_price, c_bulk)
        context.user_data['state'] = 'none'
        await update.message.reply_text(f"✅ قیمت اختصاصی برای کاربر `{uid}` ثبت شد.\nتکی: {c_price:,} | عمده: {c_bulk:,}", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
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
                context.user_data['state'] = 'plan_add_bulkprice'
                await update.message.reply_text("📦 **قیمت عمده (برای کاربر عادی)** دونه‌ای چند تومان؟", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
            elif step == 'bulkprice':
                context.user_data['new_plan']['bulk_price'] = clean_num(text)
                context.user_data['state'] = 'plan_add_vipbulkprice'
                await update.message.reply_text("👑 **قیمت عمده VIP** دونه‌ای چند تومان؟", reply_markup=CANCEL_MARKUP, parse_mode='Markdown')
            elif step == 'vipbulkprice':
                context.user_data['new_plan']['vip_bulk_price'] = clean_num(text)
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
                        await conn.execute("INSERT INTO plans (name, gb, price, vip_price, bulk_price, vip_bulk_price, inbound_id, duration_days, panel_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)", p['name'], p['gb'], p['price'], p['vip_price'], p['bulk_price'], p['vip_bulk_price'], inbound, p.get('duration_days', 30), chosen_pid)
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
                    await conn.execute("INSERT INTO plans (name, gb, price, vip_price, bulk_price, vip_bulk_price, inbound_id, duration_days, panel_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)", p['name'], p['gb'], p['price'], p['vip_price'], p['bulk_price'], p['vip_bulk_price'], p['inbound_id'], p.get('duration_days', 30), chosen_pid)
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
            g['code'] = text.strip()
            context.user_data['state'] = 'gift_add_amount'
            await update.message.reply_text("💰 مبلغ هدیه (تومان) را بفرستید:", reply_markup=CANCEL_MARKUP)
        elif step == 'amount':
            try:
                g['amount'] = clean_num(text)
            except ValueError:
                return await update.message.reply_text("❌ فقط عدد بفرستید.")
            context.user_data['state'] = 'gift_add_uses'
            await update.message.reply_text("🔢 حداکثر تعداد دفعات قابل‌استفاده را بفرستید:", reply_markup=CANCEL_MARKUP)
        elif step == 'uses':
            try:
                max_uses = max(1, clean_num(text))
            except ValueError:
                return await update.message.reply_text("❌ فقط عدد بفرستید.")
            await db.add_gift_code(g['code'], g['amount'], max_uses)
            context.user_data['state'] = 'none'
            await update.message.reply_text(f"✅ کد هدیه `{g['code']}` ساخته شد.", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
        return

    if state == 'admin_waiting_del_gift' and admin_status:
        await db.delete_gift_code(text.strip())
        context.user_data['state'] = 'none'
        await update.message.reply_text("✅ کد حذف شد (در صورت وجود).", reply_markup=await get_main_keyboard(user_id))
        return

    if state == 'admin_waiting_refbonus' and admin_status:
        try:
            await update_setting('ref_bonus', clean_num(text))
        except ValueError:
            return await update.message.reply_text("❌ فقط عدد بفرستید.")
        context.user_data['state'] = 'none'
        await update.message.reply_text("✅ پاداش دعوت تنظیم شد.", reply_markup=await get_main_keyboard(user_id))
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
                f"✅ کاربر {name} VIP شد.\n📦 دسترسی خرید عمده به‌صورت خودکار فعال است.",
                reply_markup=await get_main_keyboard(user_id),
            )
        else: await update.message.reply_text("❌ فرمت اشتباه! مثال: `123456789 علی`")

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
            res = await get_order_by_id(order_id)
            if not res: return
            old_link, _ = res
            old_email = unquote(old_link.split("#")[-1])

            await update.message.reply_text("در حال اعمال تغییرات در سرور... ⏳")
            xui, _ip = await get_order_xui(order_id)
            is_logged, _ = await xui.login()
            if is_logged:
                inbound_id, port, client_dict = await xui.get_client_exact_info(old_email)
                if client_dict:
                    new_email = f"{user_id}_{text}"
                    client_dict['email'] = new_email
                    if await xui.update_client(inbound_id, client_dict['id'], client_dict):
                        new_link = old_link.replace(f"#{old_email}", f"#{new_email}")
                        async with db.db_pool.acquire() as conn: await conn.execute("UPDATE orders SET config_link = $1 WHERE id = $2", new_link, int(order_id))
                        await update.message.reply_text(f"✅ نام کانفیگ با موفقیت به `{new_email}` تغییر کرد!", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
                        context.user_data['state'] = 'none'
                    else: await update.message.reply_text("❌ سرور درخواست تغییر نام را رد کرد.")
                else: await update.message.reply_text("❌ کانفیگ در سرور یافت نشد.")
            else: await update.message.reply_text("❌ خطا در اتصال به پنل X-UI.")
        else: await update.message.reply_text("❌ نامعتبر! فقط حروف انگلیسی و اعداد.")

    elif state.startswith('waiting_for_nick_'):
        plan_id = int(state.split('_')[-1])
        if re.match(r"^[A-Za-z0-9_-]+$", text) and len(text) < 20:
            async with db.db_pool.acquire() as conn:
                plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", plan_id)
            if not plan: return

            price = await resolve_plan_price(user_id, plan, role, bulk=False)
            days = plan['duration_days'] or 30
            final_name = f"{user_id}_{text}"
            kb = [[InlineKeyboardButton("✅ تایید نهایی و پرداخت", callback_data=f"confirm_buy_{plan_id}_{final_name}")]]
            await update.message.reply_text(
                f"اسم: `{final_name}`\nپلن: {plan['name']} ({plan['gb']}GB / {days}روز)\nقیمت: {price:,} تومان\nتایید خرید؟",
                reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown',
            )
            context.user_data['state'] = 'none'
        else: await update.message.reply_text("❌ نامعتبر! فقط از حروف انگلیسی و اعداد استفاده کنید:")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not _rate_ok(user_id):
        try: await query.answer("⏳ کمی آرام‌تر! لطفاً چند لحظه صبر کنید.", show_alert=False)
        except Exception: pass
        return
    await query.answer()
    data = query.data
    admin_status = await is_admin(user_id)
    balance, nickname, role, can_bulk = await get_user(user_id)
    
    if data == 'ignore': return

    if data == 'admin_toggle_sales' and admin_status:
        current = await get_setting('sales_status')
        new_status = 'closed' if current == 'open' else 'open'
        await update_setting('sales_status', new_status)
        status_text = "🟢 باز" if new_status == 'open' else "🔴 بسته"
        kb = [
            [InlineKeyboardButton("محصولات و پلن‌ها 🛒", callback_data='admin_manage_plans')],
            [InlineKeyboardButton("لیست VIP و ادمین‌ها 📋", callback_data='admin_list_specials')],
            [InlineKeyboardButton("قیمت اختصاصی کاربر 💎", callback_data='admin_custom_price'), InlineKeyboardButton("کارت 💳", callback_data='admin_set_card')],
            [InlineKeyboardButton("افزودن VIP 🟢", callback_data='admin_add_vip'), InlineKeyboardButton("حذف VIP 🔴", callback_data='admin_rem_vip')],
            [InlineKeyboardButton("ارسال پیام 📢", callback_data='admin_broadcast')],
            [InlineKeyboardButton("ایمپورت کانفیگ 🔗", callback_data='admin_import_config'), InlineKeyboardButton("پشتیبانی 📞", callback_data='admin_set_support')],
            [InlineKeyboardButton("مدیریت پنل‌ها 🖥", callback_data='admin_manage_panels'), InlineKeyboardButton("اکانت تست 🎁", callback_data='admin_test_menu')],
            [InlineKeyboardButton("📊 گزارش فروش", callback_data='admin_report'), InlineKeyboardButton("🔔 هشدار انقضا", callback_data='admin_set_notify')],
            [InlineKeyboardButton("🎟 کدهای هدیه", callback_data='admin_gift_menu'), InlineKeyboardButton("🎁 پاداش دعوت", callback_data='admin_set_refbonus')],
            [InlineKeyboardButton("💾 بکاپ دیتابیس", callback_data='admin_backup_menu')],
            [InlineKeyboardButton(f"وضعیت فروش: {status_text}", callback_data='admin_toggle_sales')]
        ]
        if user_id == SUPER_ADMIN_ID: kb.append([InlineKeyboardButton("افزودن ادمین 👮‍♂️", callback_data='superadmin_add_admin'), InlineKeyboardButton("حذف ادمین ⛔️", callback_data='superadmin_rem_admin')])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == 'admin_manage_plans' and admin_status:
        kb = [
            [InlineKeyboardButton("➕ افزودن پلن جدید", callback_data='admin_add_plan'), InlineKeyboardButton("🗑 حذف پلن", callback_data='admin_del_plan')],
            [InlineKeyboardButton("✏️ ویرایش پلن", callback_data='admin_edit_plan')]
        ]
        async with db.db_pool.acquire() as conn: plans = await conn.fetch("SELECT * FROM plans ORDER BY id ASC")
        msg = "📋 **لیست محصولات فعلی:**\n\n"
        for p in plans: msg += f"🆔 `ID:{p['id']}` | {p['name']} | عادی: {p['price']:,} | مدت: {p['duration_days']}روز | اینباند: {p['inbound_id']}\n"
        await query.edit_message_text(msg if plans else "محصولی یافت نشد.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data == 'admin_edit_plan' and admin_status:
        context.user_data['state'] = 'admin_waiting_edit_plan_id'
        await query.message.reply_text("آیدی (ID) پلنی که می‌خواهید ویرایش کنید را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_custom_price' and admin_status:
        context.user_data['state'] = 'cp_uid'
        await query.message.reply_text("💎 **قیمت اختصاصی کاربر**\n\nآیدی عددی کاربر را بفرستید:", parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data.startswith('cpplan_') and admin_status:
        context.user_data['cp_plan'] = int(data.split('_')[1])
        context.user_data['state'] = 'cp_single'
        await query.message.reply_text("💰 قیمت اختصاصی **تکی** را بفرستید:", parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'pe_done':
        try: await query.edit_message_text("✅ ویرایش پلن به پایان رسید.")
        except: pass

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

    elif data == 'admin_del_panel' and admin_status:
        context.user_data['state'] = 'admin_waiting_del_panel'
        await query.message.reply_text("🗑 آیدی (ID) پنلی که می‌خواهید حذف شود را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'wallet_history':
        txns = await db.get_user_transactions(user_id, 15)
        if not txns:
            return await query.answer("تراکنشی ثبت نشده است.", show_alert=True)
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
        bonus = await get_setting('ref_bonus')
        username = context.bot.username
        link = f"https://t.me/{username}?start=ref_{user_id}" if username else f"کد دعوت شما: ref_{user_id}"
        msg = (
            f"🔗 **دعوت دوستان**\n\n"
            f"با ارسال لینک زیر برای دوستانتان، بعد از اولین شارژِ آن‌ها {int(bonus or 0):,} تومان هدیه می‌گیرید:\n\n"
            f"`{link}`\n\n"
            f"👥 تعداد دعوت‌های شما: {count}"
        )
        try:
            await query.edit_message_text(msg, parse_mode='Markdown')
        except Exception:
            await query.message.reply_text(msg, parse_mode='Markdown')

    elif data == 'wallet_giftcode':
        context.user_data['state'] = 'redeem_gift'
        await query.message.reply_text("🎟 کد هدیه را وارد کنید:", reply_markup=CANCEL_MARKUP)
        try: await query.delete_message()
        except: pass

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
        await query.answer("در حال تهیه بکاپ... ⏳")
        await _send_backup_to(context, user_id)

    elif data == 'admin_gift_menu' and admin_status:
        codes = await db.list_gift_codes()
        msg = "🎟 **کدهای هدیه:**\n\n"
        if codes:
            for c in codes:
                msg += f"`{c['code']}` | {c['amount']:,} تومان | استفاده: {c['used_count']}/{c['max_uses']}\n"
        else:
            msg += "هیچ کدی ثبت نشده است.\n"
        kb = [[InlineKeyboardButton("➕ افزودن کد", callback_data='admin_gift_add'), InlineKeyboardButton("🗑 حذف کد", callback_data='admin_gift_del')]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data == 'admin_gift_add' and admin_status:
        context.user_data['new_gift'] = {}
        context.user_data['state'] = 'gift_add_code'
        await query.message.reply_text("🎟 متن کد هدیه را وارد کنید (انگلیسی/عدد):", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_gift_del' and admin_status:
        context.user_data['state'] = 'admin_waiting_del_gift'
        await query.message.reply_text("کدی که می‌خواهید حذف شود را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_set_refbonus' and admin_status:
        context.user_data['state'] = 'admin_waiting_refbonus'
        cur = await get_setting('ref_bonus')
        await query.message.reply_text(f"🎁 مبلغ پاداش دعوت (تومان) را بفرستید:\nمقدار فعلی: {int(cur or 0):,}", reply_markup=CANCEL_MARKUP)
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

    elif data == 'admin_toggle_bulk' and admin_status:
        await query.answer("خرید عمده فقط با نقش VIP فعال است. کاربر را VIP کنید.", show_alert=True)

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
        await query.message.reply_text("متن، عکس، ویس یا ویدیوی خود را ارسال کنید:", reply_markup=CANCEL_MARKUP)
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
        res = await get_order_by_id(order_id)
        if not res: return
        email = unquote(res[0].split("#")[-1])
        xui, _ip = await get_order_xui(order_id)
        is_logged, login_err = await xui.login()
        if not is_logged:
            return await query.answer(f"❌ اتصال به پنل ناموفق بود.\n{str(login_err)[:150]}", show_alert=True)
        inbound_id, port, client_dict = await xui.get_client_exact_info(email)
        if not client_dict:
            return await query.answer("❌ کانفیگ در پنل یافت نشد (شاید حذف شده).", show_alert=True)
        old_uuid = client_dict['id']
        client_dict['enable'] = not client_dict['enable']
        if await xui.update_client(inbound_id, old_uuid, client_dict):
            await render_order_details(query, order_id, "✅ وضعیت تغییر کرد!")
        else:
            await query.answer("❌ سرور درخواست تغییر وضعیت را رد کرد.", show_alert=True)

    elif data.startswith("change_uuid_"):
        order_id = data.split("_")[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        res = await get_order_by_id(order_id)
        if not res: return
        old_link, _ = res
        email = unquote(old_link.split("#")[-1])
        xui, _ip = await get_order_xui(order_id)
        is_logged, login_err = await xui.login()
        if not is_logged:
            return await query.answer(f"❌ اتصال به پنل ناموفق بود.\n{str(login_err)[:150]}", show_alert=True)
        inbound_id, port, client_dict = await xui.get_client_exact_info(email)
        if not client_dict:
            return await query.answer("❌ کانفیگ در پنل یافت نشد (شاید حذف شده).", show_alert=True)
        old_uuid = client_dict['id']
        new_uuid = str(uuid.uuid4())
        client_dict['id'] = new_uuid
        if await xui.update_client(inbound_id, old_uuid, client_dict):
            new_link = old_link.replace(old_uuid, new_uuid)
            async with db.db_pool.acquire() as conn: await conn.execute("UPDATE orders SET config_link = $1 WHERE id = $2", new_link, int(order_id))
            await render_order_details(query, order_id, "✅ لینک اتصال و UUID با موفقیت تغییر کرد!")
        else:
            await query.answer("❌ سرور درخواست تغییر UUID را رد کرد.", show_alert=True)

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
        async with db.db_pool.acquire() as conn: plans = await conn.fetch("SELECT * FROM plans ORDER BY price ASC")
        if not plans: return await query.answer("پلنی برای تمدید وجود ندارد!", show_alert=True)
        kb = []
        for p in plans:
            price = await resolve_plan_price(user_id, p, role, bulk=False)
            days = p['duration_days'] or 30
            kb.append([InlineKeyboardButton(f"{p['name']} | {p['gb']}GB / {days}روز - {price:,} T", callback_data=f"confirm_renew_{order_id}_{p['id']}")])
        kb.append([InlineKeyboardButton("🔙 انصراف", callback_data=f'show_order_{order_id}')])
        await query.edit_message_text(
            "🔄 **بخش تمدید سرویس**\nبا تمدید، حجم و زمان سرویس مطابق پلن انتخابی **ریست** می‌شود.\nلطفاً پلن را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown',
        )

    elif data.startswith("confirm_renew_"):
        parts = data.split("_")
        order_id = parts[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        plan_id = int(parts[3])
        async with db.db_pool.acquire() as conn: plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", plan_id)
        if not plan: return
        price = await resolve_plan_price(user_id, plan, role, bulk=False)
        days = plan['duration_days'] or 30

        if balance < price:
            kb = [[InlineKeyboardButton("🔙 بازگشت", callback_data=f'renew_menu_{order_id}')]]
            return await query.edit_message_text(f"❌ **موجودی حساب شما کافی نیست.**\nموجودی: {balance:,} | نیاز: {price:,}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

        kb = [[InlineKeyboardButton("✅ تایید و اعمال تمدید", callback_data=f'execute_renew_{order_id}_{plan_id}')], [InlineKeyboardButton("❌ انصراف", callback_data=f'renew_menu_{order_id}')]]
        await query.edit_message_text(
            f"آیا از اعمال پلن **{plan['name']}** روی این سرویس اطمینان دارید؟\n"
            f"💾 حجم جدید: **{plan['gb']} GB** (ریست کامل)\n"
            f"📅 مدت جدید: **{days} روز** از همین الان\n"
            f"💳 مبلغ کسر: **{price:,} تومان**",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown',
        )

    elif data.startswith("execute_renew_"):
        parts = data.split("_")
        order_id = parts[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        plan_id = int(parts[3])
        async with db.db_pool.acquire() as conn: plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", plan_id)
        if not plan: return await query.answer("❌ پلن یافت نشد!", show_alert=True)
        price = await resolve_plan_price(user_id, plan, role, bulk=False)

        if context.user_data.get('processing'):
            return await query.answer("⏳ یک عملیات در حال انجام است، صبر کنید.", show_alert=True)
        context.user_data['processing'] = True
        try:
            res = await get_order_by_id(order_id)
            if not res: return
            email = unquote(res[0].split("#")[-1])

            # کسر اتمیک موجودی پیش از تماس با پنل
            if await deduct_balance(user_id, price, kind='تمدید') is None:
                return await query.answer("❌ موجودی کم است!", show_alert=True)

            xui, _ip = await get_order_xui(order_id)
            await query.edit_message_text("در حال انجام عملیات تمدید... ⏳")
            is_logged, _ = await xui.login()
            if not is_logged:
                await credit_balance(user_id, price, kind='برگشت وجه')
                return await render_order_details(query, order_id, "❌ خطا در اتصال به پنل!")

            inbound_id, port, client_dict = await xui.get_client_exact_info(email)
            if not client_dict:
                await credit_balance(user_id, price, kind='برگشت وجه')
                return await render_order_details(query, order_id, "❌ کانفیگ در سرور یافت نشد!")

            # ریست کامل مطابق پلن: حجم = حجم پلن، مصرف صفر، انقضا = الان + مدت پلن
            duration_days = plan['duration_days'] or 30
            client_dict['totalGB'] = int(plan['gb']) * 1024 * 1024 * 1024
            client_dict['up'] = 0
            client_dict['down'] = 0
            client_dict['expiryTime'] = int((time.time() + (duration_days * 86400)) * 1000)
            client_dict['enable'] = True
            if await xui.update_client(inbound_id, client_dict['id'], client_dict):
                # مصرف واقعی در جدول جدا نگه داشته می‌شود؛ updateClient آن را صفر نمی‌کند،
                # پس حتماً از endpoint اختصاصی ریست ترافیک استفاده می‌کنیم.
                await xui.reset_client_traffic(inbound_id, email)
                await db.reset_notify(order_id)
                await render_order_details(query, order_id, f"✅ تمدید شد: {plan['gb']}GB / {duration_days} روز")
            else:
                await credit_balance(user_id, price, kind='برگشت وجه')
                await render_order_details(query, order_id, "❌ خطا: سرور درخواست تمدید را رد کرد!")
        finally:
            context.user_data['processing'] = False

    # ================= هندلر دریافت بارکد =================
    elif data.startswith("get_conf_"):
        order_id = data.split("_")[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        res = await get_order_by_id(order_id)
        if res:
            config_link = res[0]
            email = unquote(config_link.split("#")[-1]) if "#" in config_link else ""
            panel = await db.get_panel(await db.get_order_panel_id(order_id))
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
        plan_id = data.split("_")[1]
        context.user_data['state'] = f'waiting_for_nick_{plan_id}'
        await query.edit_message_text("یک نام دلخواه به انگلیسی برای کانفیگ خود وارد کنید:\n(این نام پس از آیدی شما قرار می‌گیرد)")

    elif data.startswith("confirm_buy_"):
        parts = data.split("_")
        plan_id = int(parts[2])
        final_name = "_".join(parts[3:])

        async with db.db_pool.acquire() as conn:
            plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", plan_id)

        if not plan: return await query.edit_message_text("❌ پلن یافت نشد.")

        price = await resolve_plan_price(user_id, plan, role, bulk=False)

        if context.user_data.get('processing'):
            return await query.answer("⏳ یک عملیات در حال انجام است، صبر کنید.", show_alert=True)
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

            port = await xui.get_inbound_port(plan['inbound_id'])
            if not port:
                await credit_balance(user_id, price, kind='برگشت وجه')  # برگشت وجه
                return await query.edit_message_text(f"❌ خطای پنل: اینباند با آیدی {plan['inbound_id']} پیدا نشد!")

            new_uuid, error_msg = await xui.add_client(plan['inbound_id'], final_name, plan['gb'], (plan['duration_days'] or 30), 1)
            if new_uuid:
                config_link = build_vless_link(new_uuid, cfg_ip, port, final_name)
                await add_order(user_id, config_link, order_panel_id)
                logging.info("PURCHASE user=%s plan=%s price=%s name=%s", user_id, plan_id, price, final_name)

                # اگر پنل لینک ساب داشته باشد، آن را لینک اصلی قرار می‌دهیم
                sub = sub_link_for(panel, final_name)
                primary = sub or config_link
                caption = f"✅ خرید موفق!\n\n`{primary}`"
                if sub:
                    caption += f"\n\nکانفیگ مستقیم:\n`{config_link}`"
                caption += SECURITY_WARNING
                encoded_url = urllib.parse.quote(primary)
                qr_api_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={encoded_url}&margin=20"
                try:
                    await context.bot.send_photo(chat_id=user_id, photo=qr_api_url, caption=caption, parse_mode='Markdown')
                    await query.delete_message()
                except:
                    await query.edit_message_text(caption, parse_mode='Markdown')
            else:
                await credit_balance(user_id, price, kind='برگشت وجه')  # برگشت وجه چون ساخت کانفیگ ناموفق بود
                await query.edit_message_text(f"❌ سرور ساخت کانفیگ را رد کرد!\nارور: `{error_msg}`", parse_mode='Markdown')
        finally:
            context.user_data['processing'] = False

    elif data.startswith("bulkbuy_"):
        if not can_buy_bulk(role, admin_status):
            return await query.answer("⛔️ خرید عمده فقط برای کاربران VIP فعال است.", show_alert=True)
        plan_id = data.split("_")[1]
        context.user_data['b_plan'] = plan_id
        context.user_data['state'] = 'bulk_waiting_prefix'
        await query.edit_message_text("🔡 بخش اول اسم (Prefix) تمام کانفیگ‌های این سفارش را به انگلیسی وارد کنید:")

    # ================= ساخت کانفیگ عمده + فاکتور دقیق ادمین =================
    elif data == "confirm_execute_bulk":
        if not can_buy_bulk(role, admin_status):
            return await query.answer("⛔️ خرید عمده فقط برای کاربران VIP فعال است.", show_alert=True)
        start_n = context.user_data.get('b_start', 1)
        end_n = context.user_data.get('b_end', 1)
        postfix = context.user_data.get('b_postfix', "")
        plan_id = context.user_data.get('b_plan')
        prefix = context.user_data.get('b_prefix')
        if not plan_id or prefix is None:
            return await query.edit_message_text("❌ اطلاعات خرید عمده ناقص است. دوباره از منو شروع کنید.")
        count = end_n - start_n + 1

        async with db.db_pool.acquire() as conn:
            plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", int(plan_id))
        if not plan:
            return await query.edit_message_text("❌ پلن یافت نشد.")

        unit_price = await resolve_plan_price(user_id, plan, role, bulk=True)
        total_price = unit_price * count

        if context.user_data.get('processing'):
            return await query.answer("⏳ یک عملیات در حال انجام است، صبر کنید.", show_alert=True)
        context.user_data['processing'] = True
        try:
            # کل مبلغ به‌صورت اتمیک رزرو می‌شود؛ مابه‌التفاوت کانفیگ‌های ناموفق بعداً برگشت می‌خورد
            if await deduct_balance(user_id, total_price, kind='خرید عمده') is None:
                return await query.edit_message_text("❌ موجودی کافی نیست!")

            await query.edit_message_text(f"در حال ساخت {count} کانفیگ... ⏳")
            panel = await db.get_panel(plan['panel_id'])
            xui, cfg_ip = build_xui(panel)
            order_panel_id = panel['id'] if panel else None

            # جلوگیری از ساختِ کانفیگِ خراب بدون هاست/دامنه
            if not cfg_ip:
                await credit_balance(user_id, total_price, kind='برگشت وجه')
                return await query.edit_message_text("❌ «IP کانفیگ» این پنل تنظیم نشده است. لطفاً به ادمین اطلاع دهید. (وجه بازگشت داده شد)")

            is_logged, login_err = await xui.login()
            if not is_logged:
                await credit_balance(user_id, total_price, kind='برگشت وجه')  # برگشت کل وجه
                return await query.edit_message_text(f"❌ خطا در لاگین پنل: {login_err}")

            port = await xui.get_inbound_port(plan['inbound_id'])
            success_configs = []
            last_err = "نامشخص"
            for i in range(start_n, end_n + 1):
                name = f"{prefix}{i}{postfix}"
                new_uuid, err = await xui.add_client(plan['inbound_id'], name, plan['gb'], (plan['duration_days'] or 30), 1)
                if new_uuid:
                    link = build_vless_link(new_uuid, cfg_ip, port, name)
                    success_configs.append(link)
                    await add_order(user_id, link, order_panel_id)
                else:
                    last_err = err

            # برگشت وجهِ کانفیگ‌هایی که ساخته نشدند
            actual_deduction = unit_price * len(success_configs)
            refund = total_price - actual_deduction
            if refund > 0:
                await credit_balance(user_id, refund, kind='برگشت وجه', description='کانفیگ‌های ناموفق عمده')

            if success_configs:
                configs_text = "\n\n".join(success_configs)
                file_in_ram = io.BytesIO(configs_text.encode('utf-8'))
                file_in_ram.name = f"Configs_Bulk_{prefix}.txt"
                
                await context.bot.send_document(
                    chat_id=user_id, 
                    document=file_in_ram, 
                    caption=f"✅ عملیات موفق!\n📦 تعداد ساخته شده: {len(success_configs)}\n💰 مبلغ کسر شده: {actual_deduction:,} تومان"
                )
                await query.delete_message()
                
                # --- سیستم فاکتور دقیق برای مدیریت (هر کسی خرید عمده زد) ---
                admins = await get_all_admins()
                u_nick = nickname if nickname else "بدون نام"
                user_type = "ادمین 👑" if admin_status else ("VIP 💎" if role == 'vip' else "عادی")
                
                invoice_text = (
                    f"🧾 **فاکتور فروش عمده**\n\n"
                    f"👤 **خریدار:** `{user_id}` ( {u_nick} )\n"
                    f"🔰 **سطح کاربری:** {user_type}\n"
                    f"🛍 **محصول:** {plan['name']}\n"
                    f"📦 **تعداد موفق:** {len(success_configs)} از {count}\n"
                    f"💵 **قیمت واحد:** {unit_price:,} تومان\n"
                    f"💳 **جمع کل کسر شده:** {actual_deduction:,} تومان\n"
                    f"📅 **تاریخ:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
                )
                
                for adm in admins:
                    if adm != 0:
                        try: await context.bot.send_message(chat_id=adm, text=invoice_text, parse_mode='Markdown')
                        except: pass
                # -------------------------------------------------------------
            else: 
                await query.edit_message_text(f"❌ سرور پنل سنایی اجازه ساخت هیچ کانفیگی را نداد!\n\nدلیل خطای پنل: `{last_err}`\n\n⚠️ راهنمایی: احتمالاً کانفیگی با این اسم از قبل در پنل وجود دارد.", parse_mode='Markdown')
        finally:
            context.user_data['processing'] = False

    elif data.startswith("show_order_") or data.startswith("refresh_order_"):
        order_id = data.split("_")[2]
        alert = "🔄 بروزرسانی شد" if data.startswith("refresh_order_") else None
        await render_order_details(query, order_id, alert)
        
    elif data == 'back_to_orders':
        async with db.db_pool.acquire() as conn: orders = await conn.fetch("SELECT id, config_link, date, panel_id FROM orders WHERE user_id = $1", user_id)
        keyboard = await generate_orders_keyboard(orders, page=context.user_data.get('order_page', 0), search=context.user_data.get('order_search'))
        await query.edit_message_text("✅ سرویس خود را انتخاب کنید:", reply_markup=keyboard)

    elif data.startswith('orders_page_'):
        page = int(data.split('_')[2])
        context.user_data['order_page'] = page
        async with db.db_pool.acquire() as conn: orders = await conn.fetch("SELECT id, config_link, date, panel_id FROM orders WHERE user_id = $1", user_id)
        keyboard = await generate_orders_keyboard(orders, page=page, search=context.user_data.get('order_search'))
        try: await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception: pass

    elif data == 'orders_search':
        context.user_data['state'] = 'waiting_order_search'
        await query.message.reply_text("🔎 بخشی از نام سرویس را بفرستید:", reply_markup=CANCEL_MARKUP)

    elif data == 'orders_clearsearch':
        context.user_data['order_search'] = None
        context.user_data['order_page'] = 0
        async with db.db_pool.acquire() as conn: orders = await conn.fetch("SELECT id, config_link, date, panel_id FROM orders WHERE user_id = $1", user_id)
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
        await query.edit_message_caption(caption="✅ تایید شد.")
        try: await context.bot.send_message(chat_id=uid, text=f"🎉 حساب شما {amt:,} شارژ شد.")
        except: pass
        # پاداش معرف بعد از اولین شارژِ تاییدشده
        reward = await db.try_reward_referrer(uid)
        if reward:
            referrer_id, bonus = reward
            try: await context.bot.send_message(chat_id=referrer_id, text=f"🎁 یکی از دعوت‌شدگان شما شارژ کرد! {bonus:,} تومان پاداش به حساب شما اضافه شد.")
            except: pass

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
        except: pass

async def setup_db(app: Application):
    await init_db()
    # اجرای پنل وب مدیریت (در صورت تنظیم WEB_ADMIN_PASSWORD) داخل همان event loop
    try:
        import webpanel
        app.bot_data['web_runner'] = await webpanel.start_web()
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


async def notify_job(context: ContextTypes.DEFAULT_TYPE):
    """به‌صورت دوره‌ای سرویس‌های نزدیک به انقضا یا اتمام حجم را پیدا و به کاربر اطلاع می‌دهد."""
    from collections import defaultdict
    try:
        days = int(await get_setting('notify_days') or 3)
    except (TypeError, ValueError):
        days = 3
    orders = await db.get_orders_for_notify()
    by_panel = defaultdict(list)
    for o in orders:
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
            link = o['config_link']
            email = unquote(link.split("#")[-1]) if "#" in link else ""
            st = stats_map.get(email.strip().lower())
            if not st or not st.get('enable', False):
                continue
            total = st.get('total', 0)
            used = st.get('up', 0) + st.get('down', 0)
            expiry = st.get('expiryTime', 0)
            # نزدیک انقضا
            if expiry > 0 and not o['expiry_notified']:
                remain_days = (expiry - now_ms) / 86400000.0
                if 0 < remain_days <= days:
                    await _notify_user(context, o, f"⏳ سرویس «{email}» تا حدود {int(remain_days) + 1} روز دیگر منقضی می‌شود.\nبرای جلوگیری از قطعی، همین حالا تمدید کنید.")
                    await db.mark_notified(o['id'], 'expiry_notified')
            # اتمام حجم (کمتر از ۱۰٪ باقی‌مانده)
            if total > 0 and not o['lowdata_notified']:
                remain = total - used
                if remain <= total * 0.1:
                    await _notify_user(context, o, f"📉 حجم سرویس «{email}» رو به اتمام است ({format_size(max(remain, 0))} باقی‌مانده).\nبرای ادامه‌ی استفاده، سرویس را تمدید کنید.")
                    await db.mark_notified(o['id'], 'lowdata_notified')

def main():
    if not TOKEN:
        raise SystemExit("❌ متغیر محیطی BOT_TOKEN تنظیم نشده است.")
    # تنظیم درخواست با pool بزرگ‌تر و درخواست اختصاصی getUpdates (پایداری بیشتر روی سرور ایران)
    req = HTTPXRequest(connection_pool_size=20, proxy=PROXY_URL, connect_timeout=30.0, read_timeout=30.0) if PROXY_URL else HTTPXRequest(connection_pool_size=20, connect_timeout=30.0)

    # ساخت ربات و اعمال تنظیمات
    app = Application.builder().token(TOKEN).request(req).get_updates_request(req).post_init(setup_db).post_shutdown(on_shutdown).build()
    
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_all_messages))
    app.add_handler(CallbackQueryHandler(button_handler))
    # اجرای دوره‌ای هشدار انقضا/اتمام حجم (هر ۶ ساعت)
    if app.job_queue:
        app.job_queue.run_repeating(notify_job, interval=21600, first=120)
        # بکاپ خودکار دیتابیس (هر ۲۴ ساعت)
        app.job_queue.run_repeating(backup_job, interval=86400, first=300)
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()