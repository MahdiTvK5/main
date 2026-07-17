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
        # آدرس سرور اشتراک (subscription) برای ساخت لینک ساب؛ اختیاری
        await conn.execute("ALTER TABLE panels ADD COLUMN IF NOT EXISTS sub_url TEXT DEFAULT ''")
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

        # ===== تاریخچه تراکنش‌ها =====
        await conn.execute('''CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            amount BIGINT,
            kind TEXT,
            description TEXT,
            date TIMESTAMP DEFAULT NOW()
        )''')

        # ===== سیستم دعوت (Referral) =====
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_rewarded BOOLEAN DEFAULT FALSE")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('ref_bonus', '0') ON CONFLICT DO NOTHING")

        # ===== کدهای هدیه =====
        await conn.execute('''CREATE TABLE IF NOT EXISTS gift_codes (code TEXT PRIMARY KEY, amount BIGINT, max_uses INT DEFAULT 1, used_count INT DEFAULT 0)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS gift_redemptions (code TEXT, user_id BIGINT, PRIMARY KEY (code, user_id))''')

        # ===== هشدار انقضا/اتمام حجم =====
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS expiry_notified BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS lowdata_notified BOOLEAN DEFAULT FALSE")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('notify_days', '3') ON CONFLICT DO NOTHING")
        # ===== بکاپ خودکار =====
        await conn.execute("INSERT INTO settings (key, value) VALUES ('backup_enabled', 'off') ON CONFLICT DO NOTHING")

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


async def get_user_row(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT user_id, balance, nickname, role, can_bulk FROM users WHERE user_id = $1", user_id)


async def save_user(user_id, nickname, role, can_bulk, balance):
    """ساخت یا ویرایش کاربر (upsert) از پنل وب."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (user_id, nickname, role, can_bulk, balance) VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (user_id) DO UPDATE SET nickname = $2, role = $3, can_bulk = $4, balance = $5""",
            user_id, nickname, role, can_bulk, balance,
        )


async def delete_user(user_id):
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM custom_prices WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)


async def deduct_balance(user_id, amount, kind='خرید', description=''):
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
        if new_balance is not None:
            await conn.execute("INSERT INTO transactions (user_id, amount, kind, description) VALUES ($1, $2, $3, $4)", user_id, -amount, kind, description)
        return new_balance


async def credit_balance(user_id, amount, kind='شارژ حساب', description=''):
    """افزودن اتمیک موجودی (برای شارژ/برگشت وجه). خروجی: موجودی جدید."""
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        nb = await conn.fetchval(
            "UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance",
            amount, user_id,
        )
        await conn.execute("INSERT INTO transactions (user_id, amount, kind, description) VALUES ($1, $2, $3, $4)", user_id, amount, kind, description)
        return nb


async def list_users(search=None, limit=100):
    async with db_pool.acquire() as conn:
        if search:
            like = f"%{search}%"
            return await conn.fetch(
                "SELECT user_id, balance, nickname, role, can_bulk FROM users WHERE CAST(user_id AS TEXT) LIKE $1 OR nickname ILIKE $1 ORDER BY user_id DESC LIMIT $2",
                like, limit,
            )
        return await conn.fetch("SELECT user_id, balance, nickname, role, can_bulk FROM users ORDER BY user_id DESC LIMIT $1", limit)


async def list_recent_orders(limit=50, search=None):
    async with db_pool.acquire() as conn:
        if search:
            like = f"%{search}%"
            return await conn.fetch("SELECT id, user_id, config_link, date, panel_id FROM orders WHERE config_link ILIKE $1 OR CAST(user_id AS TEXT) LIKE $1 ORDER BY id DESC LIMIT $2", like, limit)
        return await conn.fetch("SELECT id, user_id, config_link, date, panel_id FROM orders ORDER BY id DESC LIMIT $1", limit)


async def list_recent_transactions(limit=50):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, amount, kind, description, date FROM transactions ORDER BY id DESC LIMIT $1", limit)


async def list_plans():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM plans ORDER BY id ASC")


async def get_plan(plan_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM plans WHERE id = $1", int(plan_id))


async def create_plan(name, gb, duration_days, price, vip_price, bulk_price, vip_bulk_price, inbound_id, panel_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO plans (name, gb, duration_days, price, vip_price, bulk_price, vip_bulk_price, inbound_id, panel_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id""",
            name, gb, duration_days, price, vip_price, bulk_price, vip_bulk_price, inbound_id, panel_id,
        )


async def update_plan(plan_id, name, gb, duration_days, price, vip_price, bulk_price, vip_bulk_price, inbound_id, panel_id):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE plans SET name=$2, gb=$3, duration_days=$4, price=$5, vip_price=$6, bulk_price=$7, vip_bulk_price=$8, inbound_id=$9, panel_id=$10 WHERE id=$1""",
            int(plan_id), name, gb, duration_days, price, vip_price, bulk_price, vip_bulk_price, inbound_id, panel_id,
        )


async def delete_plan(plan_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM plans WHERE id = $1", int(plan_id))


async def update_panel(panel_id, name, url, username, password, config_ip, sub_url):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE panels SET name=$2, url=$3, username=$4, password=$5, config_ip=$6, sub_url=$7 WHERE id=$1",
            int(panel_id), name, url, username, password, config_ip, sub_url,
        )


async def get_user_transactions(user_id, limit=15):
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT amount, kind, description, date FROM transactions WHERE user_id = $1 ORDER BY id DESC LIMIT $2",
            user_id, limit,
        )


async def get_sales_report():
    async with db_pool.acquire() as conn:
        topup = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kind = 'شارژ حساب'")
        spent = await conn.fetchval("SELECT COALESCE(-SUM(amount), 0) FROM transactions WHERE amount < 0")
        today_spent = await conn.fetchval("SELECT COALESCE(-SUM(amount), 0) FROM transactions WHERE amount < 0 AND date::date = CURRENT_DATE")
        refunds = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kind = 'برگشت وجه'")
        orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
        users = await conn.fetchval("SELECT COUNT(*) FROM users")
        balances = await conn.fetchval("SELECT COALESCE(SUM(balance), 0) FROM users")
    return dict(topup=int(topup), spent=int(spent), today_spent=int(today_spent), refunds=int(refunds), orders=int(orders), users=int(users), balances=int(balances))


async def get_balance(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", user_id) or 0


async def add_order(user_id, config_link, panel_id=None):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO orders (user_id, config_link, date, panel_id) VALUES ($1, $2, $3, $4)", user_id, config_link, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), panel_id)


async def delete_order(order_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM orders WHERE id = $1", int(order_id))


async def get_order_by_id(order_id):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT config_link, date FROM orders WHERE id = $1", int(order_id))
        return (row['config_link'], row['date']) if row else None


async def get_order_panel_id(order_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT panel_id FROM orders WHERE id = $1", int(order_id))


async def get_orders_for_notify():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT id, user_id, config_link, panel_id, expiry_notified, lowdata_notified FROM orders")


async def mark_notified(order_id, field):
    if field not in ('expiry_notified', 'lowdata_notified'):
        return
    async with db_pool.acquire() as conn:
        await conn.execute(f"UPDATE orders SET {field} = TRUE WHERE id = $1", int(order_id))


async def reset_notify(order_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE orders SET expiry_notified = FALSE, lowdata_notified = FALSE WHERE id = $1", int(order_id))


# ================= پنل‌ها =================
async def get_panels():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM panels ORDER BY id ASC")


async def get_panel(panel_id):
    if panel_id is None:
        return None
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM panels WHERE id = $1", int(panel_id))


async def add_panel(name, url, username, password, config_ip, sub_url=''):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO panels (name, url, username, password, config_ip, sub_url) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            name, url, username, password, config_ip, sub_url,
        )


async def delete_panel(panel_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM panels WHERE id = $1", int(panel_id))


PANEL_COLUMNS = {'name', 'url', 'username', 'password', 'config_ip', 'sub_url'}


async def update_panel_field(panel_id, col, value):
    if col not in PANEL_COLUMNS:
        return
    async with db_pool.acquire() as conn:
        await conn.execute(f"UPDATE panels SET {col} = $1 WHERE id = $2", value, int(panel_id))


async def move_panel_assets(src_panel_id, dst_panel_id):
    """همه‌ی پلن‌ها و سفارش‌های پنل مبدأ را به پنل مقصد منتقل می‌کند.
    خروجی: (تعداد پلن‌ها، تعداد سفارش‌ها)."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            p_status = await conn.execute("UPDATE plans SET panel_id = $1 WHERE panel_id = $2", dst_panel_id, src_panel_id)
            o_status = await conn.execute("UPDATE orders SET panel_id = $1 WHERE panel_id = $2", dst_panel_id, src_panel_id)

    def _count(status):
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError, AttributeError):
            return 0

    return _count(p_status), _count(o_status)


async def get_default_panel_id():
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT id FROM panels ORDER BY id ASC LIMIT 1")


# ================= سیستم دعوت =================
async def is_new_user(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", user_id) is None


async def set_referrer(user_id, referrer_id):
    """معرف را فقط در صورتی ثبت می‌کند که قبلاً ثبت نشده باشد و خودِ کاربر نباشد."""
    if referrer_id == user_id:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        await conn.execute("UPDATE users SET referred_by = $1 WHERE user_id = $2 AND referred_by IS NULL", referrer_id, user_id)


async def referral_count(user_id):
    async with db_pool.acquire() as conn:
        return int(await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by = $1", user_id) or 0)


async def try_reward_referrer(user_id):
    """هنگام اولین شارژِ تاییدشده‌ی کاربر، یک‌بار به معرف پاداش می‌دهد.
    خروجی: (referrer_id, bonus) یا None."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT referred_by, ref_rewarded FROM users WHERE user_id = $1 FOR UPDATE", user_id)
            if not row or not row['referred_by'] or row['ref_rewarded']:
                return None
            try:
                bonus = int(await conn.fetchval("SELECT value FROM settings WHERE key = 'ref_bonus'") or 0)
            except (TypeError, ValueError):
                bonus = 0
            referrer = row['referred_by']
            await conn.execute("UPDATE users SET ref_rewarded = TRUE WHERE user_id = $1", user_id)
            if bonus <= 0:
                return None
            await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", referrer)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", bonus, referrer)
            await conn.execute("INSERT INTO transactions (user_id, amount, kind, description) VALUES ($1, $2, $3, $4)", referrer, bonus, 'پاداش دعوت', f'دعوت کاربر {user_id}')
            return referrer, bonus


# ================= کدهای هدیه =================
async def add_gift_code(code, amount, max_uses):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO gift_codes (code, amount, max_uses) VALUES ($1, $2, $3) ON CONFLICT (code) DO UPDATE SET amount = $2, max_uses = $3",
            code, amount, max_uses,
        )


async def delete_gift_code(code):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM gift_codes WHERE code = $1", code)


async def list_gift_codes():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT code, amount, max_uses, used_count FROM gift_codes ORDER BY code")


async def redeem_gift_code(user_id, code):
    """اعمال اتمیک کد هدیه. خروجی: (ok, message)."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            gc = await conn.fetchrow("SELECT amount, max_uses, used_count FROM gift_codes WHERE code = $1 FOR UPDATE", code)
            if not gc:
                return False, "❌ کد نامعتبر است."
            if gc['used_count'] >= gc['max_uses']:
                return False, "❌ ظرفیت این کد تکمیل شده است."
            already = await conn.fetchval("SELECT 1 FROM gift_redemptions WHERE code = $1 AND user_id = $2", code, user_id)
            if already:
                return False, "❌ شما قبلاً این کد را استفاده کرده‌اید."
            await conn.execute("INSERT INTO gift_redemptions (code, user_id) VALUES ($1, $2)", code, user_id)
            await conn.execute("UPDATE gift_codes SET used_count = used_count + 1 WHERE code = $1", code)
            await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", gc['amount'], user_id)
            await conn.execute("INSERT INTO transactions (user_id, amount, kind, description) VALUES ($1, $2, $3, $4)", user_id, gc['amount'], 'کد هدیه', code)
            return True, f"✅ کد اعمال شد. {gc['amount']:,} تومان به حساب شما اضافه شد."


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
