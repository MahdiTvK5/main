import os
import asyncio
import asyncpg
import httpx
import json
import uuid
import time
import datetime
import math
import re
import logging
import random
import io
import urllib.parse
from urllib.parse import unquote
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.request import HTTPXRequest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ================= تنظیمات ثابت =================
TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")

DB_USER = os.getenv("DB_USER", "overwall_user")
DB_PASS = os.getenv("DB_PASS", "OverWall@12345")
DB_NAME = os.getenv("DB_NAME", "overwall_db")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5432"))

try: SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", 0))
except ValueError: SUPER_ADMIN_ID = 0

PANEL_URL = os.getenv("PANEL_URL")
PANEL_USER = os.getenv("PANEL_USER")
PANEL_PASS = os.getenv("PANEL_PASS")
CONFIG_IP = os.getenv("CONFIG_IP")

SECURITY_WARNING = "\n\n⚠️ **هشدار امنیتی بسیار مهم:**\nلطفاً از ارسال لینک در پیام‌رسان‌های داخلی جداً خودداری کنید."
db_pool = None 

def format_size(size_bytes):
    if size_bytes <= 0: return "0 MB"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    return f"{round(size_bytes / p, 2)} {size_name[i]}"

def clean_num(text):
    return int(re.sub(r'\D', '', str(text)))

# ================= توابع دیتابیس =================
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    async with db_pool.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, balance BIGINT DEFAULT 0, nickname TEXT, role TEXT DEFAULT 'normal', can_bulk BOOLEAN DEFAULT FALSE)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS orders (id SERIAL PRIMARY KEY, user_id BIGINT, config_link TEXT, date TEXT)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY, name TEXT)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, status TEXT DEFAULT 'pending')''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS plans (id SERIAL PRIMARY KEY, name TEXT, gb INT, price BIGINT, vip_price BIGINT, bulk_price BIGINT, vip_bulk_price BIGINT, inbound_id INT)''')
        # جدول جدید برای قیمت‌های اختصاصی کاربران
        await conn.execute('''CREATE TABLE IF NOT EXISTS custom_prices (user_id BIGINT, plan_id INT, price BIGINT, bulk_price BIGINT, PRIMARY KEY(user_id, plan_id))''')
        
        await conn.execute("INSERT INTO settings (key, value) VALUES ('card_number', '6274-8817-0038-7946') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('sales_status', 'open') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('support_id', '@khodehamed') ON CONFLICT DO NOTHING")

async def is_admin(user_id):
    if user_id == SUPER_ADMIN_ID: return True
    async with db_pool.acquire() as conn: return bool(await conn.fetchval("SELECT user_id FROM admins WHERE user_id = $1", user_id))

async def get_all_admins():
    async with db_pool.acquire() as conn:
        admins = [row['user_id'] for row in await conn.fetch("SELECT user_id FROM admins")]
        admins.append(SUPER_ADMIN_ID)
        return list(set(admins))

async def get_setting(key):
    async with db_pool.acquire() as conn: return await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)

async def update_setting(key, value):
    async with db_pool.acquire() as conn: await conn.execute("UPDATE settings SET value = $1 WHERE key = $2", str(value), key)

async def get_user(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        row = await conn.fetchrow("SELECT balance, nickname, role, can_bulk FROM users WHERE user_id = $1", user_id)
        return row['balance'], row['nickname'], row['role'], row['can_bulk']

async def update_balance(user_id, new_balance):
    async with db_pool.acquire() as conn: await conn.execute("UPDATE users SET balance = $1 WHERE user_id = $2", new_balance, user_id)

async def deduct_balance(user_id, amount):
    """کسر اتمیک موجودی. فقط در صورتی کم می‌کند که موجودی کافی باشد.
    خروجی: موجودی جدید در صورت موفقیت، یا None اگر موجودی کافی نبود."""
    if amount <= 0:
        return await get_balance(user_id)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        new_balance = await conn.fetchval(
            "UPDATE users SET balance = balance - $1 WHERE user_id = $2 AND balance >= $1 RETURNING balance",
            amount, user_id,
        )
        return new_balance

async def credit_balance(user_id, amount):
    """افزودن اتمیک موجودی (برای شارژ/برگشت وجه). خروجی: موجودی جدید."""
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        return await conn.fetchval(
            "UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance",
            amount, user_id,
        )

async def get_balance(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", user_id) or 0

async def add_order(user_id, config_link):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO orders (user_id, config_link, date) VALUES ($1, $2, $3)", user_id, config_link, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))

async def get_order_by_id(order_id):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT config_link, date FROM orders WHERE id = $1", int(order_id))
        return (row['config_link'], row['date']) if row else None

async def order_belongs_to(order_id, user_id):
    """آیا این سفارش متعلق به همین کاربر است؟"""
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return False
    async with db_pool.acquire() as conn:
        owner = await conn.fetchval("SELECT user_id FROM orders WHERE id = $1", oid)
    return owner is not None and owner == user_id

async def ensure_order_access(query, order_id, admin_status):
    """کنترل دسترسی به سفارش. ادمین‌ها به همه‌چیز دسترسی دارند؛ کاربر عادی فقط به سفارش خودش."""
    if admin_status or await order_belongs_to(order_id, query.from_user.id):
        return True
    await query.answer("⛔️ این سفارش متعلق به شما نیست.", show_alert=True)
    return False

# ================= پنل X-UI =================
class AsyncXuiAPI:
    def __init__(self, panel_url, username, password):
        self.url = panel_url.rstrip('/')
        self.username = username
        self.password = password
        self.cookies = None

    async def login(self):
        async with httpx.AsyncClient(verify=False, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.post(f"{self.url}/login", data={"username": self.username, "password": self.password})
                if res.status_code == 200 and res.json().get('success'):
                    self.cookies = res.cookies
                    return True, "OK"
                return False, res.text
            except Exception as e: return False, str(e)

    async def get_inbound_port(self, inbound_id):
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.get(f"{self.url}/panel/api/inbounds/list")
                if res.status_code == 200:
                    for inbound in res.json().get('obj', []):
                        if int(inbound.get('id')) == int(inbound_id): return inbound.get('port')
            except Exception as e:
                logging.warning("get_inbound_port failed: %s", e)
        return None

    async def add_client(self, inbound_id, client_email, total_gb, expire_days, limit_ip=1):
        client_uuid = str(uuid.uuid4())
        total_bytes = total_gb * 1024 * 1024 * 1024
        expire_time = int((time.time() + (expire_days * 86400)) * 1000)
        settings = {"clients": [{"id": client_uuid, "email": client_email, "enable": True, "limitIp": limit_ip, "totalGB": total_bytes, "expiryTime": expire_time, "tgId": "", "subId": ""}]}
        payload = {"id": inbound_id, "settings": json.dumps(settings)}
        
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.post(f"{self.url}/panel/api/inbounds/addClient", json=payload, headers={'Accept': 'application/json'})
                data = res.json()
                if res.status_code == 200 and data.get('success'): return client_uuid, None
                else: return None, data.get('msg', res.text)
            except Exception as e: return None, str(e)

    async def get_client_stats(self, email):
        target_email = str(email).strip().lower()
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.get(f"{self.url}/panel/api/inbounds/getClientTraffics/{email}")
                if res.status_code == 200 and res.json().get('success'): return res.json().get('obj', {})
            except Exception as e:
                logging.warning("get_client_stats failed: %s", e)
        return None

    async def get_all_client_stats(self):
        """با یک درخواست، آمار تمام کلاینت‌ها را برمی‌گرداند.
        خروجی: dict با کلید ایمیلِ lowercase و مقدارِ دیکشنری آمار (مشابه get_client_stats)."""
        result = {}
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=15.0, trust_env=False) as client:
            try:
                res = await client.get(f"{self.url}/panel/api/inbounds/list")
                if res.status_code == 200 and res.json().get('success'):
                    for inbound in res.json().get('obj', []):
                        for cs in inbound.get('clientStats', []) or []:
                            email = str(cs.get('email', '')).strip().lower()
                            if email:
                                result[email] = cs
            except Exception as e:
                logging.warning("get_all_client_stats failed: %s", e)
                return None
        return result

    async def get_client_exact_info(self, email):
        target_email = str(email).strip().lower()
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.get(f"{self.url}/panel/api/inbounds/list")
                if res.status_code == 200 and res.json().get('success'):
                    for inbound in res.json().get('obj', []):
                        settings = json.loads(inbound.get('settings', '{}'))
                        for c in settings.get('clients', []):
                            c_email = str(c.get('email', '')).strip().lower()
                            if c_email == target_email: 
                                return inbound.get('id'), inbound.get('port'), c
            except Exception as e:
                logging.warning("get_client_exact_info failed: %s", e)
        return None, None, None

    async def update_client(self, inbound_id, old_uuid, client_dict):
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [client_dict]})}
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.post(f"{self.url}/panel/api/inbounds/updateClient/{old_uuid}", json=payload, headers={'Accept': 'application/json'})
                return res.status_code == 200 and res.json().get('success', False)
            except Exception as e:
                logging.warning("update_client failed: %s", e)
                return False

# ================= رابط کاربری =================
async def get_main_keyboard(user_id):
    _, _, _, can_bulk = await get_user(user_id)
    is_adm = await is_admin(user_id)
    menu = []
    if can_bulk or is_adm: menu.append(['خرید عمده 📦', 'محصولات 🛍'])
    else: menu.append(['محصولات 🛍'])
    menu.append(['کیف پول من 💰', 'پشتیبانی 📞'])
    menu.append(['سفارشات من 📦', 'شارژ حساب 💳'])
    if is_adm: menu.append(['مدیریت ⚙️'])
    return ReplyKeyboardMarkup(menu, resize_keyboard=True)

CANCEL_MARKUP = ReplyKeyboardMarkup([['لغو ❌']], resize_keyboard=True)

async def generate_orders_keyboard(orders, xui_api):
    keyboard = [[InlineKeyboardButton("وضعیت 🔎", callback_data='ignore'), InlineKeyboardButton("عنوان 📋", callback_data='ignore')]]
    is_login, _ = await xui_api.login()

    # به‌جای یک درخواست به‌ازای هر سفارش (N+1)، یک‌بار آمار همه‌ی کلاینت‌ها گرفته می‌شود
    stats_map = await xui_api.get_all_client_stats() if is_login else None

    for order_id, link, _ in orders[-30:]:
        email = unquote(link.split("#")[-1]) if "#" in link else f"سرویس {order_id}"
        if not is_login or stats_map is None:
            status_text = "خطا ⚠️"
        else:
            stats = stats_map.get(email.strip().lower())
            if stats:
                enable = stats.get('enable', False)
                total = stats.get('total', 0)
                used = stats.get('up', 0) + stats.get('down', 0)
                expiry = stats.get('expiryTime', 0)
                status_text = "فعال 🟢"
                if not enable: status_text = "غیرفعال 🔴"
                elif expiry > 0 and expiry < int(time.time() * 1000): status_text = "منقضی 🔴"
                elif total > 0 and used >= total: status_text = "پایان حجم 🔴"
            else: status_text = "نامشخص ⚪️"

        cb_data = f"show_order_{order_id}"
        keyboard.append([InlineKeyboardButton(status_text, callback_data=cb_data), InlineKeyboardButton(email, callback_data=cb_data)])
    return InlineKeyboardMarkup(keyboard)

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
    xui = AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS)
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
    await get_user(user_id)
    context.user_data['state'] = 'none'
    await update.message.reply_text("به ربات OverWallVpn خوش آمدید.", reply_markup=await get_main_keyboard(user_id))

async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.message.from_user.id
    balance, nickname, role, can_bulk = await get_user(user_id)
    state = context.user_data.get('state', 'none')
    admin_status = await is_admin(user_id)
    
    # 📢 Broadcast
    if state == 'admin_waiting_broadcast' and admin_status:
        async with db_pool.acquire() as conn: users = [r['user_id'] for r in await conn.fetch("SELECT user_id FROM users")]
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
        async with db_pool.acquire() as conn: await conn.execute("INSERT INTO receipts (id, status) VALUES ($1, 'pending')", receipt_id)
            
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
                [InlineKeyboardButton("مجوز عمده 📦", callback_data='admin_toggle_bulk'), InlineKeyboardButton("افزودن VIP 🟢", callback_data='admin_add_vip')],
                [InlineKeyboardButton("ارسال پیام 📢", callback_data='admin_broadcast'), InlineKeyboardButton("حذف VIP 🔴", callback_data='admin_rem_vip')],
                [InlineKeyboardButton("ایمپورت کانفیگ 🔗", callback_data='admin_import_config'), InlineKeyboardButton("پشتیبانی 📞", callback_data='admin_set_support')],
                [InlineKeyboardButton(f"وضعیت فروش: {status_text}", callback_data='admin_toggle_sales')]
            ]
            if user_id == SUPER_ADMIN_ID: kb.append([InlineKeyboardButton("افزودن ادمین 👮‍♂️", callback_data='superadmin_add_admin'), InlineKeyboardButton("حذف ادمین ⛔️", callback_data='superadmin_rem_admin')])
            await update.message.reply_text("⚙️ پنل مدیریت اختصاصی:", reply_markup=InlineKeyboardMarkup(kb))
            
        elif text == 'کیف پول من 💰': await update.message.reply_text(f"موجودی فعلی: {balance:,} تومان")
        elif text == 'پشتیبانی 📞': await update.message.reply_text(f"پشتیبانی: {await get_setting('support_id')}")
        
        elif text == 'شارژ حساب 💳':
            context.user_data['state'] = 'waiting_for_amount'
            await update.message.reply_text("مبلغ را به تومان وارد کنید:", reply_markup=CANCEL_MARKUP)
            
        elif text == 'محصولات 🛍':
            if await get_setting('sales_status') == 'closed': return await update.message.reply_text("⛔️ فروش بسته است.")
            async with db_pool.acquire() as conn:
                plans = await conn.fetch("SELECT * FROM plans ORDER BY price ASC")
                customs = await conn.fetch("SELECT plan_id, price FROM custom_prices WHERE user_id = $1", user_id)
            if not plans: return await update.message.reply_text("🛒 هنوز هیچ محصولی اضافه نشده است.")
            
            custom_map = {c['plan_id']: c['price'] for c in customs}
            kb = []
            for p in plans:
                price = custom_map.get(p['id'], p['vip_price'] if role == 'vip' else p['price'])
                kb.append([InlineKeyboardButton(f"{p['name']} - {price:,} تومان", callback_data=f"prebuy_{p['id']}")])
            await update.message.reply_text("🛍 محصول مورد نظر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))

        elif text == 'خرید عمده 📦' and (can_bulk or admin_status):
            if await get_setting('sales_status') == 'closed': return await update.message.reply_text("⛔️ فروش بسته است.")
            async with db_pool.acquire() as conn:
                plans = await conn.fetch("SELECT * FROM plans ORDER BY bulk_price ASC")
                customs = await conn.fetch("SELECT plan_id, bulk_price FROM custom_prices WHERE user_id = $1", user_id)
            if not plans: return await update.message.reply_text("🛒 هیچ محصولی برای عمده موجود نیست.")
            
            custom_map = {c['plan_id']: c['bulk_price'] for c in customs}
            kb = []
            for p in plans:
                price = custom_map.get(p['id'], p['vip_bulk_price'] if role == 'vip' else p['bulk_price'])
                kb.append([InlineKeyboardButton(f"عمده {p['name']} - دونه‌ای {price:,}T", callback_data=f"bulkbuy_{p['id']}")])
            await update.message.reply_text("📦 لطفاً پلن خرید گروهی را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))

        elif text == 'سفارشات من 📦':
            async with db_pool.acquire() as conn: orders = await conn.fetch("SELECT id, config_link, date FROM orders WHERE user_id = $1", user_id)
            if not orders: return await update.message.reply_text("📦 شما سفارشی ندارید.")
            
            wait_msg = await update.message.reply_text("در حال دریافت وضعیت از سرور... ⏳")
            xui = AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS)
            keyboard = await generate_orders_keyboard(orders, xui)
            await wait_msg.edit_text(f"✅ سفارش خود را انتخاب کنید (۳۰ سرویس اخیر):", reply_markup=keyboard)
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

    # ================= ماشین وضعیت ایمپورت گروهی کانفیگ (اصلاح شده) =================
    if state == 'admin_waiting_import' and admin_status:
        parts = text.split()
        if len(parts) >= 2 and parts[0].isdigit():
            target_user_id = int(parts[0])
            config_emails = parts[1:]
            
            await update.message.reply_text(f"در حال جستجو و ایمپورت {len(config_emails)} کانفیگ در سرور... ⏳")
            xui = AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS)
            is_logged, _ = await xui.login()
            
            if is_logged:
                success_count = 0
                failed_emails = []
                await get_user(target_user_id) 
                
                for email in config_emails:
                    inbound_id, port, client_dict = await xui.get_client_exact_info(email)
                    if client_dict:
                        client_uuid = client_dict['id']
                        real_email_from_panel = client_dict.get('email', email)
                        config_link = f"vless://{client_uuid}@{CONFIG_IP}:{port}?path=%2F&security=tls&alpn=h2%2Chttp%2F1.1&encryption=none&insecure=0&fp=chrome&type=ws&allowInsecure=0&sni={CONFIG_IP}#{real_email_from_panel}"
                        await add_order(target_user_id, config_link)
                        success_count += 1
                    else:
                        failed_emails.append(email)
                        
                context.user_data['state'] = 'none'
                report = f"✅ عملیات ایمپورت پایان یافت.\n\n📦 تعداد موفق: {success_count}\n"
                if failed_emails:
                    report += f"❌ یافت نشد (دقیقاً چک کنید): {', '.join(failed_emails)}"
                await update.message.reply_text(report, reply_markup=await get_main_keyboard(user_id))
            else:
                await update.message.reply_text("❌ خطا در اتصال به پنل X-UI.", reply_markup=await get_main_keyboard(user_id))
        else:
            await update.message.reply_text("❌ فرمت اشتباه است! لطفاً اول آیدی عددی و سپس نام کانفیگ‌ها را بفرستید.")
        return

    # ================= ماشین وضعیت خرید عمده مرحله‌ای =================
    if state == 'bulk_waiting_prefix':
        context.user_data['b_prefix'] = text.strip().replace(" ", "_")
        context.user_data['state'] = 'bulk_waiting_start'
        await update.message.reply_text("🔢 شماره شروع را وارد کنید (مثلاً اگر میخواهید از ali1 شروع شود، بنویسید 1):", reply_markup=CANCEL_MARKUP)
            
    elif state == 'bulk_waiting_start':
        if text.isdigit():
            context.user_data['b_start'] = int(text)
            context.user_data['state'] = 'bulk_waiting_end'
            await update.message.reply_text("🔢 شماره پایان را وارد کنید (مثلاً اگر میخواهید تا ali10 ساخته شود، بنویسید 10):", reply_markup=CANCEL_MARKUP)
        else:
            await update.message.reply_text("❌ لطفاً فقط عدد وارد کنید.")
            
    elif state == 'bulk_waiting_end':
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

    elif state == 'bulk_waiting_postfix':
        postfix = "" if text in ['0', '-', 'ندارد', 'خیر'] else text.strip().replace(" ", "_")
        plan_id = context.user_data['b_plan']
        prefix = context.user_data['b_prefix']
        start_n = context.user_data['b_start']
        end_n = context.user_data['b_end']
        count = end_n - start_n + 1

        context.user_data['b_postfix'] = postfix
        context.user_data['b_count'] = count

        async with db_pool.acquire() as conn:
            plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", int(plan_id))
            custom = await conn.fetchrow("SELECT bulk_price FROM custom_prices WHERE user_id=$1 AND plan_id=$2", user_id, int(plan_id))
        
        unit_price = custom['bulk_price'] if custom else (plan['vip_bulk_price'] if role == 'vip' else plan['bulk_price'])
        total = unit_price * count

        kb = [[InlineKeyboardButton("✅ تایید و ساخت", callback_data="confirm_execute_bulk")]]
        msg = f"📦 **تعداد ساخت:** {count} عدد\nنمونه نام: `{prefix}{start_n}{postfix}`\nپلن: {plan['name']}\nقیمت کل: {total:,} تومان\n\nتایید می‌کنید؟"
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        context.user_data['state'] = 'none'

    # ================= ماشین وضعیت ویرایش پلن =================
    if state == 'admin_waiting_edit_plan_id' and admin_status:
        if text.isdigit():
            async with db_pool.acquire() as conn: p = await conn.fetchrow("SELECT * FROM plans WHERE id=$1", int(text))
            if not p:
                return await update.message.reply_text("❌ پلنی با این آیدی یافت نشد.")
            context.user_data['edit_plan_id'] = int(text)
            context.user_data['state'] = 'admin_waiting_edit_plan_data'
            
            old_data = f"{p['name']} | {p['gb']} | {p['price']} | {p['vip_price']} | {p['bulk_price']} | {p['vip_bulk_price']} | {p['inbound_id']}"
            await update.message.reply_text(f"✏️ لطفاً اطلاعات جدید را با فرمت زیر بفرستید:\n`نام | حجم | عادی | VIP | عمده | عمده VIP | اینباند`\n\nمقدار قبلی:\n`{old_data}`", parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
        else: await update.message.reply_text("❌ فقط عدد بفرستید.")
        return

    if state == 'admin_waiting_edit_plan_data' and admin_status:
        try:
            parts = text.split('|')
            if len(parts) != 7: raise ValueError
            name = parts[0].strip()
            nums = [clean_num(x) for x in parts[1:]]
            gb, price, vip_price, bulk_price, vip_bulk_price, inbound_id = nums
            plan_id = context.user_data['edit_plan_id']
            
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE plans SET name=$1, gb=$2, price=$3, vip_price=$4, bulk_price=$5, vip_bulk_price=$6, inbound_id=$7 WHERE id=$8", name, gb, price, vip_price, bulk_price, vip_bulk_price, inbound_id, plan_id)
            context.user_data['state'] = 'none'
            await update.message.reply_text("✅ پلن با موفقیت ویرایش شد.", reply_markup=await get_main_keyboard(user_id))
        except: await update.message.reply_text("❌ فرمت اشتباه است!")
        return

    # ================= ماشین وضعیت قیمت اختصاصی =================
    if state == 'admin_waiting_custom_price' and admin_status:
        try:
            parts = text.split('|')
            if len(parts) != 4: raise ValueError
            target_uid, plan_id, c_price, c_bulk = [clean_num(x) for x in parts]
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO custom_prices (user_id, plan_id, price, bulk_price) VALUES ($1, $2, $3, $4) ON CONFLICT (user_id, plan_id) DO UPDATE SET price=$3, bulk_price=$4", target_uid, plan_id, c_price, c_bulk)
            context.user_data['state'] = 'none'
            await update.message.reply_text("✅ قیمت اختصاصی برای این کاربر با موفقیت ثبت شد.", reply_markup=await get_main_keyboard(user_id))
        except: await update.message.reply_text("❌ فرمت اشتباه است!")
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
                async with db_pool.acquire() as conn:
                    await conn.execute("INSERT INTO plans (name, gb, price, vip_price, bulk_price, vip_bulk_price, inbound_id) VALUES ($1, $2, $3, $4, $5, $6, $7)", p['name'], p['gb'], p['price'], p['vip_price'], p['bulk_price'], p['vip_bulk_price'], inbound)
                context.user_data['state'] = 'none'
                await update.message.reply_text(f"✅ **پلن `{p['name']}` با موفقیت ذخیره شد!**", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
        except ValueError: await update.message.reply_text("❌ لطفاً فقط عدد وارد کنید!")
        return

    # بقیه هندلرهای ادمین
    if state == 'admin_waiting_del_plan' and admin_status:
        if text.isdigit():
            async with db_pool.acquire() as conn: await conn.execute("DELETE FROM plans WHERE id = $1", int(text))
            context.user_data['state'] = 'none'
            await update.message.reply_text("✅ پلن حذف شد.", reply_markup=await get_main_keyboard(user_id))
            
    elif state == 'admin_waiting_bulk_id' and admin_status:
        if text.isdigit():
            async with db_pool.acquire() as conn:
                current = await conn.fetchval("SELECT can_bulk FROM users WHERE user_id = $1", int(text))
                if current is not None:
                    await conn.execute("UPDATE users SET can_bulk = $1 WHERE user_id = $2", not current, int(text))
                    await update.message.reply_text(f"✅ دسترسی عمده تغییر کرد.", reply_markup=await get_main_keyboard(user_id))
            context.user_data['state'] = 'none'

    elif state == 'admin_waiting_vip_id' and admin_status:
        parts = text.split(maxsplit=1)
        if len(parts) >= 1 and parts[0].isdigit():
            uid = int(parts[0])
            name = parts[1] if len(parts) > 1 else 'بدون نام'
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, nickname, role) VALUES ($1, $2, 'vip') ON CONFLICT (user_id) DO UPDATE SET role = 'vip', nickname = $2", uid, name)
            context.user_data['state'] = 'none'
            await update.message.reply_text(f"✅ کاربر {name} VIP شد.", reply_markup=await get_main_keyboard(user_id))
        else: await update.message.reply_text("❌ فرمت اشتباه! مثال: `123456789 علی`")

    elif state == 'admin_waiting_rem_vip' and admin_status:
        if text.isdigit():
            async with db_pool.acquire() as conn: await conn.execute("UPDATE users SET role = 'normal' WHERE user_id = $1", int(text))
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
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO admins (user_id, name) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET name = $2", uid, name)
            context.user_data['state'] = 'none'
            await update.message.reply_text(f"✅ ادمین {name} اضافه شد.", reply_markup=await get_main_keyboard(user_id))

    elif state == 'superadmin_waiting_rem_admin' and user_id == SUPER_ADMIN_ID:
        if text.isdigit():
            async with db_pool.acquire() as conn: await conn.execute("DELETE FROM admins WHERE user_id = $1", int(text))
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
            xui = AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS)
            is_logged, _ = await xui.login()
            if is_logged:
                inbound_id, port, client_dict = await xui.get_client_exact_info(old_email)
                if client_dict:
                    new_email = f"{user_id}_{text}"
                    client_dict['email'] = new_email
                    if await xui.update_client(inbound_id, client_dict['id'], client_dict):
                        new_link = old_link.replace(f"#{old_email}", f"#{new_email}")
                        async with db_pool.acquire() as conn: await conn.execute("UPDATE orders SET config_link = $1 WHERE id = $2", new_link, int(order_id))
                        await update.message.reply_text(f"✅ نام کانفیگ با موفقیت به `{new_email}` تغییر کرد!", reply_markup=await get_main_keyboard(user_id), parse_mode='Markdown')
                        context.user_data['state'] = 'none'
                    else: await update.message.reply_text("❌ سرور درخواست تغییر نام را رد کرد.")
                else: await update.message.reply_text("❌ کانفیگ در سرور یافت نشد.")
            else: await update.message.reply_text("❌ خطا در اتصال به پنل X-UI.")
        else: await update.message.reply_text("❌ نامعتبر! فقط حروف انگلیسی و اعداد.")

    elif state.startswith('waiting_for_nick_'):
        plan_id = int(state.split('_')[-1])
        if re.match(r"^[A-Za-z0-9_-]+$", text) and len(text) < 20:
            async with db_pool.acquire() as conn:
                plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", plan_id)
                custom = await conn.fetchrow("SELECT price FROM custom_prices WHERE user_id=$1 AND plan_id=$2", user_id, plan_id)
            if not plan: return
            
            price = custom['price'] if custom else (plan['vip_price'] if role == 'vip' else plan['price'])
            final_name = f"{user_id}_{text}"
            kb = [[InlineKeyboardButton("✅ تایید نهایی و پرداخت", callback_data=f"confirm_buy_{plan_id}_{final_name}")]]
            await update.message.reply_text(f"اسم: `{final_name}`\nپلن: {plan['name']}\nقیمت: {price:,} تومان\nتایید خرید؟", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
            context.user_data['state'] = 'none'
        else: await update.message.reply_text("❌ نامعتبر! فقط از حروف انگلیسی و اعداد استفاده کنید:")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
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
            [InlineKeyboardButton("مجوز عمده 📦", callback_data='admin_toggle_bulk'), InlineKeyboardButton("افزودن VIP 🟢", callback_data='admin_add_vip')],
            [InlineKeyboardButton("ارسال پیام 📢", callback_data='admin_broadcast'), InlineKeyboardButton("حذف VIP 🔴", callback_data='admin_rem_vip')],
            [InlineKeyboardButton("ایمپورت کانفیگ 🔗", callback_data='admin_import_config'), InlineKeyboardButton("پشتیبانی 📞", callback_data='admin_set_support')],
            [InlineKeyboardButton(f"وضعیت فروش: {status_text}", callback_data='admin_toggle_sales')]
        ]
        if user_id == SUPER_ADMIN_ID: kb.append([InlineKeyboardButton("افزودن ادمین 👮‍♂️", callback_data='superadmin_add_admin'), InlineKeyboardButton("حذف ادمین ⛔️", callback_data='superadmin_rem_admin')])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == 'admin_manage_plans' and admin_status:
        kb = [
            [InlineKeyboardButton("➕ افزودن پلن جدید", callback_data='admin_add_plan'), InlineKeyboardButton("🗑 حذف پلن", callback_data='admin_del_plan')],
            [InlineKeyboardButton("✏️ ویرایش پلن", callback_data='admin_edit_plan')]
        ]
        async with db_pool.acquire() as conn: plans = await conn.fetch("SELECT * FROM plans ORDER BY id ASC")
        msg = "📋 **لیست محصولات فعلی:**\n\n"
        for p in plans: msg += f"🆔 `ID:{p['id']}` | {p['name']} | عادی: {p['price']:,} | اینباند: {p['inbound_id']}\n"
        await query.edit_message_text(msg if plans else "محصولی یافت نشد.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data == 'admin_edit_plan' and admin_status:
        context.user_data['state'] = 'admin_waiting_edit_plan_id'
        await query.message.reply_text("آیدی (ID) پلنی که می‌خواهید ویرایش کنید را بفرستید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_custom_price' and admin_status:
        context.user_data['state'] = 'admin_waiting_custom_price'
        msg = "💎 **ثبت قیمت اختصاصی برای کاربر**\n\nلطفاً اطلاعات را با فرمت زیر بفرستید:\n`آیدی کاربر | آیدی پلن | قیمت تکی | قیمت عمده`\n\nمثال:\n`123456789 | 2 | 50000 | 45000`"
        await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
        await query.delete_message()

    elif data == 'admin_list_specials' and admin_status:
        async with db_pool.acquire() as conn:
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
        msg = "🔗 **ایمپورت کانفیگ (تکی یا گروهی)**\n\nلطفاً آیدی عددی کاربر و نام کانفیگ‌ها (Email) در سرور را بفرستید.\nخط اول آیدی کاربر، و در ادامه نام کانفیگ‌ها را با فاصله یا خط جدید وارد کنید.\n\nمثال:\n`123456789 ali_1 ali_2 ali_3`"
        await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=CANCEL_MARKUP)
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
        context.user_data['state'] = 'admin_waiting_bulk_id'
        await query.message.reply_text("👤 آیدی عددی کاربر را برای فعال/غیرفعال کردن قابلیت «خرید عمده» وارد کنید:", reply_markup=CANCEL_MARKUP)
        await query.delete_message()

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
        xui = AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS)
        is_logged, _ = await xui.login()
        if is_logged:
            inbound_id, port, client_dict = await xui.get_client_exact_info(email)
            if client_dict:
                old_uuid = client_dict['id']
                client_dict['enable'] = not client_dict['enable'] 
                if await xui.update_client(inbound_id, old_uuid, client_dict):
                    await render_order_details(query, order_id, f"✅ وضعیت تغییر کرد!")
        
    elif data.startswith("change_uuid_"):
        order_id = data.split("_")[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        res = await get_order_by_id(order_id)
        if not res: return
        old_link, _ = res
        email = unquote(old_link.split("#")[-1])
        xui = AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS)
        is_logged, _ = await xui.login()
        if is_logged:
            inbound_id, port, client_dict = await xui.get_client_exact_info(email)
            if client_dict:
                old_uuid = client_dict['id']
                new_uuid = str(uuid.uuid4())
                client_dict['id'] = new_uuid
                if await xui.update_client(inbound_id, old_uuid, client_dict):
                    new_link = old_link.replace(old_uuid, new_uuid)
                    async with db_pool.acquire() as conn: await conn.execute("UPDATE orders SET config_link = $1 WHERE id = $2", new_link, int(order_id))
                    await render_order_details(query, order_id, "✅ لینک اتصال و UUID با موفقیت تغییر کرد!")

    elif data.startswith("renew_menu_"):
        order_id = data.split("_")[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        async with db_pool.acquire() as conn: plans = await conn.fetch("SELECT * FROM plans ORDER BY price ASC")
        if not plans: return await query.answer("پلنی برای تمدید وجود ندارد!", show_alert=True)
        kb = []
        for p in plans:
            price = p['vip_price'] if role == 'vip' else p['price']
            kb.append([InlineKeyboardButton(f"{p['name']} - {price:,} T", callback_data=f"confirm_renew_{order_id}_{p['id']}")])
        kb.append([InlineKeyboardButton("🔙 انصراف", callback_data=f'show_order_{order_id}')])
        await query.edit_message_text("🔄 **بخش تمدید سرویس**\nلطفاً پلن جدید را برای این کانفیگ انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data.startswith("confirm_renew_"):
        parts = data.split("_")
        order_id = parts[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        plan_id = int(parts[3])
        async with db_pool.acquire() as conn: plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", plan_id)
        if not plan: return
        price = plan['vip_price'] if role == 'vip' else plan['price']
        
        if balance < price: 
            kb = [[InlineKeyboardButton("🔙 بازگشت", callback_data=f'renew_menu_{order_id}')]]
            return await query.edit_message_text(f"❌ **موجودی حساب شما کافی نیست.**\nموجودی: {balance:,} | نیاز: {price:,}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
            
        kb = [[InlineKeyboardButton("✅ تایید و اعمال تمدید", callback_data=f'execute_renew_{order_id}_{plan_id}')], [InlineKeyboardButton("❌ انصراف", callback_data=f'renew_menu_{order_id}')]]
        await query.edit_message_text(f"آیا از اعمال پلن **{plan['name']}** روی این سرویس اطمینان دارید؟\nمبلغ کسر: **{price:,} تومان**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data.startswith("execute_renew_"):
        parts = data.split("_")
        order_id = parts[2]
        if not await ensure_order_access(query, order_id, admin_status): return
        plan_id = int(parts[3])
        async with db_pool.acquire() as conn: plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", plan_id)
        if not plan: return await query.answer("❌ پلن یافت نشد!", show_alert=True)
        price = plan['vip_price'] if role == 'vip' else plan['price']

        if context.user_data.get('processing'):
            return await query.answer("⏳ یک عملیات در حال انجام است، صبر کنید.", show_alert=True)
        context.user_data['processing'] = True
        try:
            res = await get_order_by_id(order_id)
            if not res: return
            email = unquote(res[0].split("#")[-1])

            # کسر اتمیک موجودی پیش از تماس با پنل
            if await deduct_balance(user_id, price) is None:
                return await query.answer("❌ موجودی کم است!", show_alert=True)

            xui = AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS)
            await query.edit_message_text("در حال انجام عملیات تمدید... ⏳")
            is_logged, _ = await xui.login()
            if not is_logged:
                await credit_balance(user_id, price)  # برگشت وجه
                return await render_order_details(query, order_id, "❌ خطا در اتصال به پنل!")

            stats = await xui.get_client_stats(email)
            used_bytes = stats.get('up', 0) + stats.get('down', 0) if stats else 0
            inbound_id, port, client_dict = await xui.get_client_exact_info(email)
            if not client_dict:
                await credit_balance(user_id, price)  # برگشت وجه
                return await render_order_details(query, order_id, "❌ کانفیگ در سرور یافت نشد!")

            client_dict['totalGB'] = used_bytes + (plan['gb'] * 1024 * 1024 * 1024)
            client_dict['expiryTime'] = int((time.time() + (30 * 86400)) * 1000)
            client_dict['enable'] = True
            if await xui.update_client(inbound_id, client_dict['id'], client_dict):
                await render_order_details(query, order_id, f"✅ با موفقیت تمدید شد!")
            else:
                await credit_balance(user_id, price)  # برگشت وجه
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
            encoded_url = urllib.parse.quote(config_link)
            qr_api_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={encoded_url}&margin=20"
            try:
                await context.bot.send_photo(
                    chat_id=user_id, 
                    photo=qr_api_url, 
                    caption=f"📥 **کانفیگ شما:**\n\n`{config_link}`\n\n*(برای کپی کردن، روی لینک بالا ضربه بزنید)*{SECURITY_WARNING}", 
                    parse_mode='Markdown'
                )
            except Exception:
                await context.bot.send_message(chat_id=user_id, text=f"📥 **کانفیگ شما:**\n\n`{config_link}`", parse_mode='Markdown')

    # بقیه هندلرهای خرید
    elif data.startswith("prebuy_"):
        plan_id = data.split("_")[1]
        context.user_data['state'] = f'waiting_for_nick_{plan_id}'
        await query.edit_message_text("یک نام دلخواه به انگلیسی برای کانفیگ خود وارد کنید:\n(این نام پس از آیدی شما قرار می‌گیرد)")

    elif data.startswith("confirm_buy_"):
        parts = data.split("_")
        plan_id = int(parts[2])
        final_name = "_".join(parts[3:]) 
        
        async with db_pool.acquire() as conn:
            plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", plan_id)
            custom = await conn.fetchrow("SELECT price FROM custom_prices WHERE user_id=$1 AND plan_id=$2", user_id, plan_id)
            
        if not plan: return await query.edit_message_text("❌ پلن یافت نشد.")
        
        price = custom['price'] if custom else (plan['vip_price'] if role == 'vip' else plan['price'])

        if context.user_data.get('processing'):
            return await query.answer("⏳ یک عملیات در حال انجام است، صبر کنید.", show_alert=True)
        context.user_data['processing'] = True
        try:
            # ابتدا موجودی به‌صورت اتمیک کسر می‌شود تا از کسر دوباره/همزمان جلوگیری شود
            if await deduct_balance(user_id, price) is None:
                return await query.edit_message_text(f"❌ موجودی کافی نیست. (نیاز: {price:,})")

            await query.edit_message_text("در حال اتصال به سرور و استخراج پورت... ⏳")
            xui = AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS)
            is_logged, login_err = await xui.login()

            if not is_logged:
                await credit_balance(user_id, price)  # برگشت وجه
                return await query.edit_message_text(f"❌ خطا در لاگین به پنل سنایی.\nدلیل: {login_err}")

            port = await xui.get_inbound_port(plan['inbound_id'])
            if not port:
                await credit_balance(user_id, price)  # برگشت وجه
                return await query.edit_message_text(f"❌ خطای پنل: اینباند با آیدی {plan['inbound_id']} پیدا نشد!")

            new_uuid, error_msg = await xui.add_client(plan['inbound_id'], final_name, plan['gb'], 30, 1)
            if new_uuid:
                config_link = f"vless://{new_uuid}@{CONFIG_IP}:{port}?path=%2F&security=tls&alpn=h2%2Chttp%2F1.1&encryption=none&insecure=0&fp=chrome&type=ws&allowInsecure=0&sni={CONFIG_IP}#{final_name}"
                await add_order(user_id, config_link)
                
                # ====== ارسال بارکد برای خرید تکی ======
                encoded_url = urllib.parse.quote(config_link)
                qr_api_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={encoded_url}&margin=20"
                try:
                    await context.bot.send_photo(chat_id=user_id, photo=qr_api_url, caption=f"✅ خرید موفق!\n\n`{config_link}`{SECURITY_WARNING}", parse_mode='Markdown')
                    await query.delete_message()
                except:
                    await query.edit_message_text(f"✅ خرید موفق!\n\n`{config_link}`{SECURITY_WARNING}", parse_mode='Markdown')
                # ========================================
            else:
                await credit_balance(user_id, price)  # برگشت وجه چون ساخت کانفیگ ناموفق بود
                await query.edit_message_text(f"❌ سرور ساخت کانفیگ را رد کرد!\nارور: `{error_msg}`", parse_mode='Markdown')
        finally:
            context.user_data['processing'] = False

    elif data.startswith("bulkbuy_"):
        plan_id = data.split("_")[1]
        context.user_data['b_plan'] = plan_id
        context.user_data['state'] = 'bulk_waiting_prefix'
        await query.edit_message_text("🔡 بخش اول اسم (Prefix) تمام کانفیگ‌های این سفارش را به انگلیسی وارد کنید:")

    # ================= ساخت کانفیگ عمده + فاکتور دقیق ادمین =================
    elif data == "confirm_execute_bulk":
        start_n = context.user_data.get('b_start', 1)
        end_n = context.user_data.get('b_end', 1)
        postfix = context.user_data.get('b_postfix', "")
        plan_id = context.user_data['b_plan']
        prefix = context.user_data['b_prefix']
        count = end_n - start_n + 1
        
        async with db_pool.acquire() as conn:
            plan = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", int(plan_id))
            custom = await conn.fetchrow("SELECT bulk_price FROM custom_prices WHERE user_id=$1 AND plan_id=$2", user_id, int(plan_id))
            
        unit_price = custom['bulk_price'] if custom else (plan['vip_bulk_price'] if role == 'vip' else plan['bulk_price'])
        total_price = unit_price * count

        if context.user_data.get('processing'):
            return await query.answer("⏳ یک عملیات در حال انجام است، صبر کنید.", show_alert=True)
        context.user_data['processing'] = True
        try:
            # کل مبلغ به‌صورت اتمیک رزرو می‌شود؛ مابه‌التفاوت کانفیگ‌های ناموفق بعداً برگشت می‌خورد
            if await deduct_balance(user_id, total_price) is None:
                return await query.edit_message_text("❌ موجودی کافی نیست!")

            await query.edit_message_text(f"در حال ساخت {count} کانفیگ... ⏳")
            xui = AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS)
            is_logged, login_err = await xui.login()
            if not is_logged:
                await credit_balance(user_id, total_price)  # برگشت کل وجه
                return await query.edit_message_text(f"❌ خطا در لاگین پنل: {login_err}")

            port = await xui.get_inbound_port(plan['inbound_id'])
            success_configs = []
            last_err = "نامشخص"
            for i in range(start_n, end_n + 1):
                name = f"{prefix}{i}{postfix}"
                new_uuid, err = await xui.add_client(plan['inbound_id'], name, plan['gb'], 30, 1)
                if new_uuid:
                    link = f"vless://{new_uuid}@{CONFIG_IP}:{port}?path=%2F&security=tls&alpn=h2%2Chttp%2F1.1&encryption=none&insecure=0&fp=chrome&type=ws&allowInsecure=0&sni={CONFIG_IP}#{name}"
                    success_configs.append(link)
                    await add_order(user_id, link)
                else:
                    last_err = err

            # برگشت وجهِ کانفیگ‌هایی که ساخته نشدند
            actual_deduction = unit_price * len(success_configs)
            refund = total_price - actual_deduction
            if refund > 0:
                await credit_balance(user_id, refund)

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
                user_type = "VIP 💎" if role == 'vip' else "همکار (عادی) 📦"
                
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
        await render_order_details(query, data.split("_")[2])
        
    elif data == 'back_to_orders':
        async with db_pool.acquire() as conn: orders = await conn.fetch("SELECT id, config_link, date FROM orders WHERE user_id = $1", user_id)
        xui = AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS)
        keyboard = await generate_orders_keyboard(orders, xui)
        await query.edit_message_text("✅ سرویس خود را انتخاب کنید:", reply_markup=keyboard)

    elif data.startswith("approve_") and admin_status:
        parts = data.split("_")
        uid = int(parts[1])
        amt = int(parts[2])
        receipt_id = parts[3]
        async with db_pool.acquire() as conn:
            res = await conn.fetchval("SELECT status FROM receipts WHERE id = $1", receipt_id)
            if not res or res != 'pending': return await query.edit_message_caption(caption="⚠️ قبلاً بررسی شده.")
            await conn.execute("UPDATE receipts SET status = 'approved' WHERE id = $1", receipt_id)
        await credit_balance(uid, amt)
        await query.edit_message_caption(caption="✅ تایید شد.")
        try: await context.bot.send_message(chat_id=uid, text=f"🎉 حساب شما {amt:,} شارژ شد.")
        except: pass

    elif data.startswith("reject_") and admin_status:
        parts = data.split("_")
        uid = int(parts[1])
        receipt_id = parts[2]
        async with db_pool.acquire() as conn:
            res = await conn.fetchval("SELECT status FROM receipts WHERE id = $1", receipt_id)
            if not res or res != 'pending': return await query.edit_message_caption(caption="⚠️ قبلاً بررسی شده.")
            await conn.execute("UPDATE receipts SET status = 'rejected' WHERE id = $1", receipt_id)
        await query.edit_message_caption(caption="❌ رد شد.")
        try: await context.bot.send_message(chat_id=uid, text="❌ رسید شما تایید نشد.")
        except: pass

async def setup_db(app: Application):
    await init_db()
    logging.info("🚀 ربات با موفقیت استارت شد...")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """هندلر سراسری خطا تا استثناهای مدیریت‌نشده لاگ شوند به‌جای اینکه بی‌صدا گم شوند."""
    logging.error("استثنای مدیریت‌نشده هنگام پردازش آپدیت", exc_info=context.error)

def main():
    if not TOKEN:
        raise SystemExit("❌ متغیر محیطی BOT_TOKEN تنظیم نشده است.")
    # تنظیم دقیق و استاندارد پروکسی برای python-telegram-bot
    req = HTTPXRequest(proxy=PROXY_URL, connect_timeout=30.0, read_timeout=30.0) if PROXY_URL else HTTPXRequest(connect_timeout=30.0)
    
    # ساخت ربات و اعمال تنظیمات
    app = Application.builder().token(TOKEN).request(req).post_init(setup_db).build()
    
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_all_messages))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()