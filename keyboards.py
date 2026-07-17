import time
from urllib.parse import unquote

from telegram import ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup

import db
from db import get_user, is_admin, get_setting, has_test
from panel import build_xui
from config import PANEL_URL


# ================= رابط کاربری =================
async def get_main_keyboard(user_id):
    _, _, role, _ = await get_user(user_id)
    is_adm = await is_admin(user_id)
    menu = []
    # خرید عمده فقط برای VIP و ادمین (نه کاربران عادی با can_bulk)
    if is_adm or role == 'vip':
        menu.append(['خرید عمده 📦', 'محصولات 🛍'])
    else:
        menu.append(['محصولات 🛍'])
    menu.append(['کیف پول من 💰', 'پشتیبانی 📞'])
    menu.append(['سفارشات من 📦', 'شارژ حساب 💳'])
    # اکانت تست رایگان: فقط وقتی ادمین فعالش کرده و کاربر قبلاً نگرفته
    if (await get_setting('test_enabled')) == 'on' and not is_adm and not await has_test(user_id):
        menu.append(['🎁 اکانت تست رایگان'])
    if is_adm:
        menu.append(['مدیریت ⚙️'])
    return ReplyKeyboardMarkup(menu, resize_keyboard=True)


CANCEL_MARKUP = ReplyKeyboardMarkup([['لغو ❌']], resize_keyboard=True)


async def generate_orders_keyboard(orders, page=0, page_size=8, search=None):
    """orders: لیستی از ردیف‌ها شامل (id, config_link, date, panel_id).
    با صفحه‌بندی و جستجو. آمار هر پنل فقط یک‌بار برای آیتم‌های همان صفحه گرفته می‌شود."""
    keyboard = [[InlineKeyboardButton("وضعیت 🔎", callback_data='ignore'), InlineKeyboardButton("عنوان 📋", callback_data='ignore')]]

    def _email_of(link, oid):
        return unquote(link.split("#")[-1]) if "#" in link else f"سرویس {oid}"

    # جدیدترین‌ها اول
    items = list(orders)[::-1]
    if search:
        s = search.strip().lower()
        items = [o for o in items if s in _email_of(o[1], o[0]).lower()]

    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    chunk = items[start:start + page_size]

    # برای هر پنلِ درگیر در این صفحه، یک‌بار آمار همه‌ی کلاینت‌ها را می‌گیریم
    stats_by_panel = {}
    for panel_id in {o[3] for o in chunk}:
        panel = await db.get_panel(panel_id) if panel_id else None
        if panel is None and not PANEL_URL:
            stats_by_panel[panel_id] = None
            continue
        xui, _ip = build_xui(panel)
        is_login, _ = await xui.login()
        stats_by_panel[panel_id] = await xui.get_all_client_stats() if is_login else None

    for order_id, link, _date, panel_id in chunk:
        email = _email_of(link, order_id)
        stats_map = stats_by_panel.get(panel_id)
        if stats_map is None:
            status_text = "خطا ⚠️"
        else:
            stats = stats_map.get(email.strip().lower())
            if stats:
                enable = stats.get('enable', False)
                tot = stats.get('total', 0)
                used = stats.get('up', 0) + stats.get('down', 0)
                expiry = stats.get('expiryTime', 0)
                status_text = "فعال 🟢"
                if not enable:
                    status_text = "غیرفعال 🔴"
                elif expiry > 0 and expiry < int(time.time() * 1000):
                    status_text = "منقضی 🔴"
                elif tot > 0 and used >= tot:
                    status_text = "پایان حجم 🔴"
            else:
                status_text = "نامشخص ⚪️"

        cb_data = f"show_order_{order_id}"
        keyboard.append([InlineKeyboardButton(status_text, callback_data=cb_data), InlineKeyboardButton(email, callback_data=cb_data)])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"orders_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data='ignore'))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"orders_page_{page + 1}"))
    keyboard.append(nav)
    bottom = [InlineKeyboardButton("🔎 جستجو", callback_data='orders_search')]
    if search:
        bottom.append(InlineKeyboardButton("❌ حذف جستجو", callback_data='orders_clearsearch'))
    keyboard.append(bottom)
    return InlineKeyboardMarkup(keyboard)
