import time
from urllib.parse import unquote

from telegram import ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup

import db
from db import get_user, is_admin
from panel import build_xui
from config import PANEL_URL


# ================= رابط کاربری =================
async def get_main_keyboard(user_id):
    _, _, _, can_bulk = await get_user(user_id)
    is_adm = await is_admin(user_id)
    menu = []
    if can_bulk or is_adm:
        menu.append(['خرید عمده 📦', 'محصولات 🛍'])
    else:
        menu.append(['محصولات 🛍'])
    menu.append(['کیف پول من 💰', 'پشتیبانی 📞'])
    menu.append(['سفارشات من 📦', 'شارژ حساب 💳'])
    if is_adm:
        menu.append(['مدیریت ⚙️'])
    return ReplyKeyboardMarkup(menu, resize_keyboard=True)


CANCEL_MARKUP = ReplyKeyboardMarkup([['لغو ❌']], resize_keyboard=True)


async def generate_orders_keyboard(orders):
    """orders: لیستی از ردیف‌ها شامل (id, config_link, date, panel_id).
    آمار هر پنل فقط یک‌بار گرفته و بین سفارش‌های همان پنل به اشتراک گذاشته می‌شود."""
    keyboard = [[InlineKeyboardButton("وضعیت 🔎", callback_data='ignore'), InlineKeyboardButton("عنوان 📋", callback_data='ignore')]]

    recent = orders[-30:]
    # برای هر پنلِ درگیر، یک‌بار آمار همه‌ی کلاینت‌ها را می‌گیریم
    stats_by_panel = {}
    for panel_id in {o[3] for o in recent}:
        panel = await db.get_panel(panel_id) if panel_id else None
        if panel is None and not PANEL_URL:
            stats_by_panel[panel_id] = None
            continue
        xui, _ip = build_xui(panel)
        is_login, _ = await xui.login()
        stats_by_panel[panel_id] = await xui.get_all_client_stats() if is_login else None

    for order_id, link, _date, panel_id in recent:
        email = unquote(link.split("#")[-1]) if "#" in link else f"سرویس {order_id}"
        stats_map = stats_by_panel.get(panel_id)
        if stats_map is None:
            status_text = "خطا ⚠️"
        else:
            stats = stats_map.get(email.strip().lower())
            if stats:
                enable = stats.get('enable', False)
                total = stats.get('total', 0)
                used = stats.get('up', 0) + stats.get('down', 0)
                expiry = stats.get('expiryTime', 0)
                status_text = "فعال 🟢"
                if not enable:
                    status_text = "غیرفعال 🔴"
                elif expiry > 0 and expiry < int(time.time() * 1000):
                    status_text = "منقضی 🔴"
                elif total > 0 and used >= total:
                    status_text = "پایان حجم 🔴"
            else:
                status_text = "نامشخص ⚪️"

        cb_data = f"show_order_{order_id}"
        keyboard.append([InlineKeyboardButton(status_text, callback_data=cb_data), InlineKeyboardButton(email, callback_data=cb_data)])
    return InlineKeyboardMarkup(keyboard)
