import datetime
import asyncpg

from config import (
    DB_USER, DB_PASS, DB_NAME, DB_HOST, DB_PORT, SUPER_ADMIN_ID,
    PANEL_URL, PANEL_USER, PANEL_PASS, CONFIG_IP,
)

# استخر اتصال دیتابیس؛ در init_db مقداردهی می‌شود و سایر ماژول‌ها با db.db_pool به آن دسترسی دارند.
db_pool = None


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
        await conn.execute('''CREATE TABLE IF NOT EXISTS plans (id SERIAL PRIMARY KEY, name TEXT, gb INT, price BIGINT, vip_price BIGINT, bulk_price BIGINT, vip_bulk_price BIGINT, inbound_id INT, duration_days INT DEFAULT 30)''')
        # مهاجرت برای دیتابیس‌های قدیمی که هنوز ستون مدت‌زمان را ندارند
        await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS duration_days INT DEFAULT 30")
        # جدول جدید برای قیمت‌های اختصاصی کاربران
        await conn.execute('''CREATE TABLE IF NOT EXISTS custom_prices (user_id BIGINT, plan_id INT, price BIGINT, bulk_price BIGINT, PRIMARY KEY(user_id, plan_id))''')

        # ===== پشتیبانی چند پنل =====
        await conn.execute('''CREATE TABLE IF NOT EXISTS panels (id SERIAL PRIMARY KEY, name TEXT, url TEXT, username TEXT, password TEXT, config_ip TEXT)''')
        # هر پلن و هر سفارش به یک پنل متصل می‌شود
        await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS panel_id INT")
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS panel_id INT")

        # مهاجرت: اگر هیچ پنلی ثبت نشده ولی پنل پیش‌فرض در .env وجود دارد، آن را به‌عنوان پنل اول بساز
        default_panel_id = await conn.fetchval("SELECT id FROM panels ORDER BY id ASC LIMIT 1")
        if default_panel_id is None and PANEL_URL:
            default_panel_id = await conn.fetchval(
                "INSERT INTO panels (name, url, username, password, config_ip) VALUES ($1, $2, $3, $4, $5) RETURNING id",
                "پنل پیش‌فرض", PANEL_URL, PANEL_USER, PANEL_PASS, CONFIG_IP,
            )
        # سفارش‌ها و پلن‌های قدیمیِ بدون پنل را به پنل پیش‌فرض نسبت بده
        if default_panel_id is not None:
            await conn.execute("UPDATE plans SET panel_id = $1 WHERE panel_id IS NULL", default_panel_id)
            await conn.execute("UPDATE orders SET panel_id = $1 WHERE panel_id IS NULL", default_panel_id)

        # ===== اکانت تست =====
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS got_test BOOLEAN DEFAULT FALSE")

        await conn.execute("INSERT INTO settings (key, value) VALUES ('card_number', '6274-8817-0038-7946') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('sales_status', 'open') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('support_id', '@khodehamed') ON CONFLICT DO NOTHING")
        # تنظیمات پیش‌فرض اکانت تست
        await conn.execute("INSERT INTO settings (key, value) VALUES ('test_enabled', 'off') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('test_gb', '1') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('test_days', '1') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('test_panel_id', '') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('test_inbound_id', '') ON CONFLICT DO NOTHING")


async def is_admin(user_id):
    if user_id == SUPER_ADMIN_ID:
        return True
    async with db_pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT user_id FROM admins WHERE user_id = $1", user_id))


async def get_all_admins():
    async with db_pool.acquire() as conn:
        admins = [row['user_id'] for row in await conn.fetch("SELECT user_id FROM admins")]
        admins.append(SUPER_ADMIN_ID)
        return list(set(admins))


async def get_setting(key):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)


async def update_setting(key, value):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE settings SET value = $1 WHERE key = $2", str(value), key)


async def get_user(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        row = await conn.fetchrow("SELECT balance, nickname, role, can_bulk FROM users WHERE user_id = $1", user_id)
        return row['balance'], row['nickname'], row['role'], row['can_bulk']


async def update_balance(user_id, new_balance):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = $1 WHERE user_id = $2", new_balance, user_id)


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


async def add_order(user_id, config_link, panel_id=None):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO orders (user_id, config_link, date, panel_id) VALUES ($1, $2, $3, $4)", user_id, config_link, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), panel_id)


async def get_order_by_id(order_id):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT config_link, date FROM orders WHERE id = $1", int(order_id))
        return (row['config_link'], row['date']) if row else None


async def get_order_panel_id(order_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT panel_id FROM orders WHERE id = $1", int(order_id))


# ================= پنل‌ها =================
async def get_panels():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM panels ORDER BY id ASC")


async def get_panel(panel_id):
    if panel_id is None:
        return None
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM panels WHERE id = $1", int(panel_id))


async def add_panel(name, url, username, password, config_ip):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO panels (name, url, username, password, config_ip) VALUES ($1, $2, $3, $4, $5) RETURNING id",
            name, url, username, password, config_ip,
        )


async def delete_panel(panel_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM panels WHERE id = $1", int(panel_id))


async def get_default_panel_id():
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT id FROM panels ORDER BY id ASC LIMIT 1")


# ================= اکانت تست =================
async def has_test(user_id):
    async with db_pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT got_test FROM users WHERE user_id = $1", user_id))


async def mark_test_used(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET got_test = TRUE WHERE user_id = $1", user_id)


async def order_belongs_to(order_id, user_id):
    """آیا این سفارش متعلق به همین کاربر است؟"""
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return False
    async with db_pool.acquire() as conn:
        owner = await conn.fetchval("SELECT user_id FROM orders WHERE id = $1", oid)
    return owner is not None and owner == user_id
