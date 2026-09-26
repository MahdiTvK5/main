import datetime
import logging
import secrets

import asyncpg

from config import (
    DB_USER, DB_PASS, DB_NAME, DB_HOST, DB_PORT, SUPER_ADMIN_ID,
    PANEL_URL, PANEL_USER, PANEL_PASS, CONFIG_IP,
)
from links import email_from_link
import rewards

# استخر اتصال دیتابیس؛ در init_db مقداردهی می‌شود و سایر ماژول‌ها با db.db_pool به آن دسترسی دارند.
db_pool = None


# ================= توابع دیتابیس =================
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    async with db_pool.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, balance BIGINT DEFAULT 0, nickname TEXT, role TEXT DEFAULT 'normal')''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS orders (id SERIAL PRIMARY KEY, user_id BIGINT, config_link TEXT, date TEXT)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY, name TEXT)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, status TEXT DEFAULT 'pending')''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS plans (id SERIAL PRIMARY KEY, name TEXT, gb INT, price BIGINT, vip_price BIGINT, inbound_id INT, duration_days INT DEFAULT 30)''')
        # مهاجرت برای دیتابیس‌های قدیمی که هنوز ستون مدت‌زمان را ندارند
        await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS duration_days INT DEFAULT 30")
        # آیکون/ایموجی نمایشی ابتدای دکمه‌ی هر پلن (اختیاری)
        await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS icon TEXT DEFAULT ''")
        # ترتیب نمایش دلخواه؛ پیش‌فرض بر اساس آیدی (پلن‌های جدید پایین‌تر)
        await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS sort_order INT")

        # ===== پشتیبانی چند پنل =====
        await conn.execute('''CREATE TABLE IF NOT EXISTS panels (id SERIAL PRIMARY KEY, name TEXT, url TEXT, username TEXT, password TEXT, config_ip TEXT)''')
        # آدرس سرور اشتراک (subscription) برای ساخت لینک ساب؛ اختیاری
        await conn.execute("ALTER TABLE panels ADD COLUMN IF NOT EXISTS sub_url TEXT DEFAULT ''")
        # نوع پنل: 'xui' (پیش‌فرض) یا 'vpn-ui' (فورک Sir-MmD با API کمی متفاوت)
        await conn.execute("ALTER TABLE panels ADD COLUMN IF NOT EXISTS panel_type TEXT DEFAULT ''")
        # پنل‌های مجاز برای هر کاربر (لیست id با کاما؛ خالی = همه‌ی پنل‌ها — دیفالت)
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS allowed_panels TEXT DEFAULT ''")
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

        # ===== شناسه‌ی سرویس روی سفارش =====
        # نام سرویس (email) و اینباند دیگر از روی متنِ لینک حدس زده نمی‌شوند، چون
        # لینک vmess فرگمنت # ندارد و همه‌چیز داخل Base64 است.
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS email TEXT")
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS inbound_id INT")
        await _backfill_order_emails(conn)

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
        # ===== تمدید خودکار (opt-in هر کاربر برای هر سفارش) =====
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS auto_renew BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS auto_renew_plan_id INT")
        # ستون‌های مرحله‌بندی یادآوری: هر مرحله فقط یک‌بار
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS notify_3d BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS notify_1d BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS notify_expired BOOLEAN DEFAULT FALSE")
        # ===== بن کاربر (مسدودسازی بدون حذف تاریخچه) =====
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_banned ON users(is_banned)")
        # ===== اجبار عضویت کانال =====
        await conn.execute("INSERT INTO settings (key, value) VALUES ('force_channel', '') ON CONFLICT DO NOTHING")
        # ===== پرداخت با Telegram Stars (نرخ: تومان به ازای هر استارز؛ خالی = خاموش) =====
        await conn.execute("INSERT INTO settings (key, value) VALUES ('stars_toman_rate', '') ON CONFLICT DO NOTHING")
        # ===== بکاپ خودکار =====
        await conn.execute("INSERT INTO settings (key, value) VALUES ('backup_enabled', 'off') ON CONFLICT DO NOTHING")

        # شماره کارت عمداً خالی است تا اطلاعات بانکیِ واقعی داخل سورس نباشد؛
        # از منوی مدیریت یا پنل وب تنظیم می‌شود.
        await conn.execute("INSERT INTO settings (key, value) VALUES ('card_number', '') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('sales_status', 'open') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('support_id', '@khodehamed') ON CONFLICT DO NOTHING")
        # تنظیمات پیش‌فرض اکانت تست
        await conn.execute("INSERT INTO settings (key, value) VALUES ('test_enabled', 'off') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('test_gb', '1') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('test_days', '1') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('test_panel_id', '') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('test_inbound_id', '') ON CONFLICT DO NOTHING")

        # ===== سطح‌بندی قیمت، آفرها و فروش (فاز ۲۴) =====
        await _init_pricing(conn)

        # ===== دعوت و کد هدیه (فاز ۲۵) =====
        await _init_rewards(conn)

        # ===== ایندکس‌ها =====
        # این کوئری‌ها در هر «سفارشات من»، هر گزارش و هر هشدار انقضا اجرا می‌شوند.
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_panel ON orders(panel_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_email ON orders(LOWER(email))")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, id DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_kind ON transactions(kind)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_ref ON users(referred_by)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_panel ON plans(panel_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(created_at)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_user ON sales(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")


# پلن‌های پیش‌فرضِ فروشگاه: (حجم، روز، همکاری، VIP، عادی، تمدید، خرید اول، آیکون)
DEFAULT_PLANS = [
    (10, 30, 80_000, 95_000, 110_000, 95_000, 79_000, '🟦'),
    (20, 30, 110_000, 130_000, 150_000, 130_000, 99_000, '🟦'),
    (30, 30, 140_000, 165_000, 190_000, 169_000, 129_000, '🟦'),
    (40, 30, 170_000, 195_000, 220_000, 199_000, 159_000, '🟦'),
    (50, 30, 200_000, 230_000, 260_000, 239_000, 189_000, '🟦'),
    (80, 60, 290_000, 325_000, 360_000, 329_000, 279_000, '🟪'),
    (120, 90, 400_000, 450_000, 500_000, 459_000, 389_000, '🟨'),
]

PRICING_SETTINGS = {
    'first_offer_enabled': 'on',      # آفر خرید اول
    'renew_offer_enabled': 'on',      # قیمت ویژه‌ی تمدید
    'reseller_bonus_enabled': 'on',   # هدیه‌ی اولین شارژ نماینده
    'reseller_bonus_min': '1000000',  # حداقل مبلغ شارژ برای دریافت هدیه
    'reseller_bonus_percent': '10',   # درصد هدیه
    'reseller_t2_min': '1000000',     # آستانه‌ی سطح ۲ (شارژ ۳۰ روز اخیر)
    'reseller_t2_discount': '5',      # درصد تخفیف اضافه‌ی سطح ۲
    'reseller_t3_min': '3000000',     # آستانه‌ی سطح VIP نماینده
    'reseller_t3_discount': '8',      # درصد تخفیف اضافه‌ی سطح VIP نماینده
}

REFERRAL_SETTINGS = {
    'ref_enabled': 'on',              # سیستم دعوت
    'ref_invitee_bonus': '0',         # هدیه به دعوت‌شده هنگام اولین شارژ واجد شرایط
    'ref_min_topup': '0',             # حداقل مبلغ شارژ برای آزاد شدن پاداش (۰ = بدون حداقل)
}


async def _init_pricing(conn):
    """مهاجرت‌های سیستم قیمت‌گذاری/آفر/گزارش.

    تغییرات idempotent هستند و سرویس‌ها و سفارش‌های فعلی دست نمی‌خورند. تنها استثنا
    حذف ستون‌های «خرید عمده» است (پایین همین تابع) که با حذف کامل آن قابلیت انجام
    می‌شود و برگشت‌ناپذیر است.
    """
    # --- سطوح قیمت روی پلن ---
    await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS reseller_price BIGINT")
    await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS renew_price BIGINT")
    await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS first_price BIGINT")

    # --- قیمت تمدید جدا برای هر سطح (عادی/VIP/همکاری) ---
    await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS renew_normal_price BIGINT")
    await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS renew_vip_price BIGINT")
    await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS renew_reseller_price BIGINT")
    # انتقالِ یک‌باره‌ی «تمدید» مشترک قبلی: برای هر سطح دقیقاً همان مبلغ مؤثری که
    # تا امروز محاسبه می‌شد (کمترینِ قیمت همان سطح و تمدید) ذخیره می‌شود؛ هیچ عدد
    # جدیدی ساخته نمی‌شود و ردیف‌های دست‌کاری‌شده دوباره بازنویسی نمی‌شوند.
    await conn.execute("""
        UPDATE plans
        SET renew_normal_price = LEAST(COALESCE(price, 0), renew_price),
            renew_vip_price = LEAST(COALESCE(vip_price, price, 0), renew_price),
            renew_reseller_price = LEAST(COALESCE(reseller_price, vip_price, price, 0), renew_price)
        WHERE COALESCE(renew_price, 0) > 0
          AND renew_normal_price IS NULL AND renew_vip_price IS NULL AND renew_reseller_price IS NULL
    """)
    await conn.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE")
    await conn.execute("UPDATE plans SET is_active = TRUE WHERE is_active IS NULL")

    # --- وضعیت آفرها روی کاربر ---
    await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_offer_used BOOLEAN DEFAULT FALSE")
    await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reseller_bonus_used BOOLEAN DEFAULT FALSE")
    # مشتریان فعلی که قبلاً خرید کرده‌اند نباید مشمول «آفر خرید اول» شوند
    await conn.execute(
        """UPDATE users SET first_offer_used = TRUE
           WHERE first_offer_used = FALSE AND user_id IN (SELECT DISTINCT user_id FROM orders)"""
    )

    # --- قیمتِ منجمدشده روی سفارش (تغییر قیمت‌های بعدی نباید سفارش قبلی را عوض کند) ---
    await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS plan_id INT")
    await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS price BIGINT")
    await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS base_price BIGINT")
    await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS discount BIGINT DEFAULT 0")
    await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS purchase_kind TEXT")
    await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS user_role TEXT")

    # --- آفرهای قابل مدیریت ---
    await conn.execute('''CREATE TABLE IF NOT EXISTS offers (
        id SERIAL PRIMARY KEY,
        name TEXT,
        kind TEXT DEFAULT 'percent',      -- percent | fixed
        value BIGINT DEFAULT 0,           -- درصد یا مبلغ ثابت
        audience TEXT DEFAULT 'all',      -- all | new | existing | vip | reseller
        plan_id INT,                      -- خالی یعنی همه‌ی پلن‌ها
        starts_at TIMESTAMP,
        ends_at TIMESTAMP,
        max_uses INT DEFAULT 0,           -- صفر یعنی نامحدود
        per_user_limit INT DEFAULT 1,
        used_count INT DEFAULT 0,
        is_active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT NOW()
    )''')
    await conn.execute('''CREATE TABLE IF NOT EXISTS offer_redemptions (
        id SERIAL PRIMARY KEY,
        offer_id INT,
        user_id BIGINT,
        created_at TIMESTAMP DEFAULT NOW()
    )''')
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_offer_redemptions ON offer_redemptions(offer_id, user_id)")

    # --- دفتر فروش برای گزارش مالی (تمدید سفارش جدید نمی‌سازد، پس جدا ثبت می‌شود) ---
    await conn.execute('''CREATE TABLE IF NOT EXISTS sales (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        order_id INT,
        plan_id INT,
        kind TEXT,                        -- buy | renew
        user_role TEXT,
        base_price BIGINT DEFAULT 0,
        price BIGINT DEFAULT 0,
        discount BIGINT DEFAULT 0,
        offer_id INT,
        first_offer BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT NOW()
    )''')

    # --- حذف «خرید عمده» (برگشت‌ناپذیر) ---
    # این قابلیت کامل حذف شده؛ ستون‌های مربوطه هم برداشته می‌شوند تا شِمای دیتابیس با
    # کد هم‌خوان بماند. ردیف‌های تاریخی sales.kind='bulk' و orders.purchase_kind='bulk'
    # عمداً دست نخورده می‌مانند تا گزارش مالی گذشته درست بماند.
    await conn.execute("ALTER TABLE plans DROP COLUMN IF EXISTS bulk_price")
    await conn.execute("ALTER TABLE plans DROP COLUMN IF EXISTS vip_bulk_price")
    await conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS can_bulk")

    # قیمت اختصاصی کاربر کامل حذف شده؛ جدولش هم برداشته می‌شود تا شِما با کد هم‌خوان بماند.
    await conn.execute("DROP TABLE IF EXISTS custom_prices")

    # --- پورتال اشتراک: توکن امنِ هر سرویس (ضد IDOR؛ هیچ شناسه‌ی دیتابیسی در URL نیست) ---
    await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS sub_token TEXT")
    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_sub_token ON orders(sub_token)")
    # backfill سفارش‌های قدیمی که توکن ندارند تا پورتال برای همه‌ی سرویس‌ها کار کند
    rows = await conn.fetch("SELECT id FROM orders WHERE sub_token IS NULL OR sub_token = ''")
    for r in rows:
        await conn.execute("UPDATE orders SET sub_token = $1 WHERE id = $2", make_sub_token(), r['id'])
    if rows:
        logging.info("توکن پورتال اشتراک برای %s سفارش قدیمی ساخته شد.", len(rows))

    # --- تنظیمات پیش‌فرض ---
    for key, value in PRICING_SETTINGS.items():
        await conn.execute("INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT DO NOTHING", key, value)

    # --- پلن‌های پیش‌فرض فقط روی نصب تازه (اگر هیچ پلنی وجود ندارد) ---
    if not await conn.fetchval("SELECT 1 FROM plans LIMIT 1"):
        panel_id = await conn.fetchval("SELECT id FROM panels ORDER BY id ASC LIMIT 1")
        await _insert_default_plans(conn, panel_id)
        logging.info("پلن‌های پیش‌فرض قیمت‌گذاری ساخته شدند (%s مورد).", len(DEFAULT_PLANS))


async def _init_rewards(conn):
    """مهاجرت‌های سیستم دعوت و کد هدیه؛ افزایشی و idempotent."""
    await conn.execute("ALTER TABLE gift_codes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP")
    await conn.execute("ALTER TABLE gift_codes ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE")
    await conn.execute("ALTER TABLE gift_codes ADD COLUMN IF NOT EXISTS note TEXT DEFAULT ''")
    await conn.execute("ALTER TABLE gift_codes ADD COLUMN IF NOT EXISTS audience TEXT DEFAULT 'all'")
    await conn.execute("ALTER TABLE gift_codes ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")
    await conn.execute("UPDATE gift_codes SET is_active = TRUE WHERE is_active IS NULL")
    await conn.execute("UPDATE gift_codes SET audience = 'all' WHERE audience IS NULL OR audience = ''")
    await conn.execute("UPDATE gift_codes SET created_at = NOW() WHERE created_at IS NULL")
    await conn.execute("ALTER TABLE gift_redemptions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")
    await conn.execute("ALTER TABLE gift_redemptions ADD COLUMN IF NOT EXISTS amount BIGINT")
    await conn.execute("UPDATE gift_redemptions SET created_at = NOW() WHERE created_at IS NULL")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_gift_redemptions_user ON gift_redemptions(user_id)")
    for key, value in REFERRAL_SETTINGS.items():
        await conn.execute("INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT DO NOTHING", key, value)


async def _insert_default_plans(conn, panel_id, inbound_id=1):
    """درج پلن‌های پیش‌فرض. پلن‌هایی که هم‌نامشان وجود دارد رد می‌شوند."""
    created = 0
    for gb, days, reseller, vip, normal, renew, first, icon in DEFAULT_PLANS:
        name = f"{gb}GB - {days} روزه"
        if await conn.fetchval("SELECT 1 FROM plans WHERE name = $1", name):
            continue
        # همان نگاشت مهاجرت: تمدید هر سطح = کمترینِ قیمت همان سطح و قیمت تمدید
        rn = min(normal, renew) if renew else None
        rv = min(vip, renew) if renew else None
        rr = min(reseller, renew) if renew else None
        await conn.execute(
            """INSERT INTO plans (name, gb, duration_days, price, vip_price, reseller_price,
                                  renew_price, first_price,
                                  renew_normal_price, renew_vip_price, renew_reseller_price,
                                  inbound_id, panel_id, icon, sort_order, is_active)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,TRUE)""",
            name, gb, days, normal, vip, reseller, renew, first,
            rn, rv, rr,
            inbound_id, panel_id, icon, gb,
        )
        created += 1
    return created


async def seed_default_plans(inbound_id=1, panel_id=None):
    """درج پلن‌های پیش‌فرض از پنل مدیریت (بدون دست‌زدن به پلن‌های موجود)."""
    async with db_pool.acquire() as conn:
        if panel_id is None:
            panel_id = await conn.fetchval("SELECT id FROM panels ORDER BY id ASC LIMIT 1")
        return await _insert_default_plans(conn, panel_id, inbound_id)


async def _backfill_order_emails(conn):
    """برای سفارش‌های قدیمی، نام سرویس را یک‌بار از لینک استخراج و ذخیره می‌کند."""
    rows = await conn.fetch("SELECT id, config_link FROM orders WHERE email IS NULL OR email = ''")
    if not rows:
        return
    filled = 0
    for r in rows:
        email = email_from_link(r['config_link'])
        if email:
            await conn.execute("UPDATE orders SET email = $1 WHERE id = $2", email, r['id'])
            filled += 1
    logging.info("backfill: نام سرویس برای %s سفارش از %s مورد پر شد.", filled, len(rows))


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
    """ذخیره‌ی تنظیم. اگر کلید وجود نداشته باشد ساخته می‌شود (قبلاً بی‌صدا گم می‌شد)."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2",
            key, str(value),
        )


async def get_user(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        row = await conn.fetchrow("SELECT balance, nickname, role FROM users WHERE user_id = $1", user_id)
        return row['balance'], row['nickname'], row['role']


async def update_balance(user_id, new_balance):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = $1 WHERE user_id = $2", new_balance, user_id)


async def get_user_row(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT user_id, balance, nickname, role FROM users WHERE user_id = $1", user_id)


def _parse_allowed_panels(val):
    """رشته‌ی ذخیره‌شده (مثل '1,3') را به مجموعه تبدیل می‌کند؛ None یعنی همه‌ی پنل‌ها."""
    if val is None or not str(val).strip():
        return None
    try:
        ids = {int(x) for x in str(val).split(',') if x.strip().isdigit()}
    except Exception:
        return None
    # لیست نامعتبر/خالی‌یِ صریح به «همه» برنمی‌گردد؛ '' و None یعنی همه، لیست عددی یعنی همان‌ها
    return ids if ids else None


def _allowed_panels_value(panel_ids):
    """مجموعه/لیست را به رشته‌ی ذخیره‌سازی تبدیل می‌کند؛ خالی یعنی بدون محدودیت."""
    if not panel_ids:
        return ''
    try:
        return ','.join(str(int(p)) for p in sorted(panel_ids))
    except (TypeError, ValueError):
        return ''


async def get_user_allowed_panels(user_id):
    """مجموعه‌ی id پنل‌های مجاز کاربر؛ None یعنی همه‌ی پنل‌ها (محدودیتی ثبت نشده)."""
    async with db_pool.acquire() as conn:
        val = await conn.fetchval("SELECT COALESCE(allowed_panels, '') FROM users WHERE user_id = $1", int(user_id))
    return _parse_allowed_panels(val)


async def set_user_allowed_panels(user_id, panel_ids):
    """ثبت پنل‌های مجاز کاربر. panel_ids خالی/None یعنی همه‌ی پنل‌ها (بدون محدودیت)."""
    val = _allowed_panels_value(panel_ids)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, allowed_panels) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET allowed_panels = $2",
            int(user_id), val,
        )


async def save_user(user_id, nickname, role, balance):
    """ساخت یا ویرایش کاربر (upsert) از پنل وب."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (user_id, nickname, role, balance) VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id) DO UPDATE SET nickname = $2, role = $3, balance = $4""",
            user_id, nickname, role, balance,
        )


async def delete_user(user_id):
    """حذف کاربر همراه داده‌های وابسته.

    تراکنش‌ها عمداً نگه داشته می‌شوند چون سند مالی هستند و گزارش فروش را تغییر
    نمی‌دهند. خروجی: تعداد سفارش‌هایی که از لیست پاک شد.
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            status = await conn.execute("DELETE FROM orders WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM gift_redemptions WHERE user_id = $1", user_id)
            await conn.execute("UPDATE users SET referred_by = NULL WHERE referred_by = $1", user_id)
            await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0


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
    if amount <= 0:
        return await get_balance(user_id)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        nb = await conn.fetchval(
            "UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance",
            amount, user_id,
        )
        await conn.execute("INSERT INTO transactions (user_id, amount, kind, description) VALUES ($1, $2, $3, $4)", user_id, amount, kind, description)
        return nb


async def list_users(search=None, limit=100, offset=0):
    async with db_pool.acquire() as conn:
        if search:
            like = f"%{search}%"
            return await conn.fetch(
                "SELECT user_id, balance, nickname, role FROM users WHERE CAST(user_id AS TEXT) LIKE $1 OR nickname ILIKE $1 ORDER BY user_id DESC LIMIT $2 OFFSET $3",
                like, limit, offset,
            )
        return await conn.fetch("SELECT user_id, balance, nickname, role FROM users ORDER BY user_id DESC LIMIT $1 OFFSET $2", limit, offset)


async def count_users(search=None):
    async with db_pool.acquire() as conn:
        if search:
            like = f"%{search}%"
            return int(await conn.fetchval("SELECT COUNT(*) FROM users WHERE CAST(user_id AS TEXT) LIKE $1 OR nickname ILIKE $1", like) or 0)
        return int(await conn.fetchval("SELECT COUNT(*) FROM users") or 0)


async def list_recent_orders(limit=50, search=None, offset=0):
    async with db_pool.acquire() as conn:
        cols = """SELECT o.id, o.user_id, o.config_link, o.email, o.date, o.panel_id,
                         o.plan_id, o.price, p.name AS plan_name"""
        if search:
            like = f"%{search}%"
            return await conn.fetch(
                cols + " FROM orders o LEFT JOIN plans p ON p.id = o.plan_id "
                "WHERE o.email ILIKE $1 OR o.config_link ILIKE $1 OR CAST(o.user_id AS TEXT) LIKE $1 "
                "ORDER BY o.id DESC LIMIT $2 OFFSET $3", like, limit, offset)
        return await conn.fetch(
            cols + " FROM orders o LEFT JOIN plans p ON p.id = o.plan_id "
            "ORDER BY o.id DESC LIMIT $1 OFFSET $2", limit, offset)


async def count_orders(search=None):
    async with db_pool.acquire() as conn:
        if search:
            like = f"%{search}%"
            return int(await conn.fetchval("SELECT COUNT(*) FROM orders WHERE email ILIKE $1 OR config_link ILIKE $1 OR CAST(user_id AS TEXT) LIKE $1", like) or 0)
        return int(await conn.fetchval("SELECT COUNT(*) FROM orders") or 0)


async def get_orders_by_user(user_id):
    """سفارش‌های یک کاربر؛ همان ستون‌هایی که کیبورد سفارش‌ها و پنل وب لازم دارند."""
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, config_link, email, date, panel_id, inbound_id FROM orders WHERE user_id = $1 ORDER BY id DESC",
            user_id,
        )


async def find_order_by_email(email):
    """اولین سفارشِ ثبت‌شده با این email (هر کاربری). برای جلوگیری از ایمپورت تکراری."""
    email = str(email or '').strip()
    if not email:
        return None
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, user_id FROM orders WHERE lower(email) = lower($1) ORDER BY id ASC LIMIT 1",
            email,
        )


async def list_recent_transactions(limit=50, offset=0):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, amount, kind, description, date FROM transactions ORDER BY id DESC LIMIT $1 OFFSET $2", limit, offset)


async def count_transactions():
    async with db_pool.acquire() as conn:
        return int(await conn.fetchval("SELECT COUNT(*) FROM transactions") or 0)


async def list_vips(role='vip'):
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT user_id, nickname, balance FROM users WHERE role = $1 ORDER BY user_id DESC", role,
        )


async def list_admin_rows():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, name FROM admins ORDER BY user_id")


async def get_user_detail(user_id):
    """ردیف کامل کاربر همراه فیلدهای اکانت تست، دعوت و بن (برای صفحه‌ی جزئیات در پنل وب)."""
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT user_id, balance, nickname, role, got_test, referred_by, ref_rewarded, "
            "COALESCE(is_banned, FALSE) AS is_banned FROM users WHERE user_id = $1",
            user_id,
        )


async def all_user_ids():
    async with db_pool.acquire() as conn:
        return [r['user_id'] for r in await conn.fetch("SELECT user_id FROM users")]


async def set_user_role(user_id, role):
    """نقش کاربر را تنظیم می‌کند (normal/vip) و در صورت نبود، کاربر را می‌سازد."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, role) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET role = $2",
            user_id, role,
        )


PLAN_ORDER = "ORDER BY COALESCE(sort_order, id) ASC, id ASC"


async def list_plans(panel_id=None, only_active=False, allowed_panels=None):
    """لیست پلن‌ها به ترتیب نمایش.

    panel_id: فقط پلن‌های همان پنل. only_active: فقط پلن‌های فعال (برای منوی فروش).
    allowed_panels: مجموعه‌ی id پنل‌های مجاز کاربر (None = همه)؛ پلن‌های سایر پنل‌ها
    حذف می‌شوند تا کاربر فقط بتواند از پنل‌های تعیین‌شده برایش خرید کند.
    """
    where, args = [], []
    if panel_id is not None:
        args.append(int(panel_id))
        where.append(f"panel_id = ${len(args)}")
    if only_active:
        where.append("COALESCE(is_active, TRUE) = TRUE")
    if allowed_panels is not None:  # مجموعه‌ی خالی یعنی هیچ پنلی مجاز نیست
        args.append([int(p) for p in allowed_panels])
        where.append(f"panel_id = ANY(${len(args)})")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    async with db_pool.acquire() as conn:
        return await conn.fetch(f"SELECT * FROM plans {clause} {PLAN_ORDER}", *args)


PLAN_PRICE_COLUMNS = {
    'gb', 'duration_days', 'price', 'vip_price', 'reseller_price',
    'renew_price', 'first_price',
    'renew_normal_price', 'renew_vip_price', 'renew_reseller_price',
    'name', 'icon', 'inbound_id', 'panel_id', 'sort_order', 'is_active',
}


async def update_plan_field(plan_id, col, value):
    """به‌روزرسانی یک فیلد پلن. نام ستون از لیست ثابت بالا می‌آید."""
    if col not in PLAN_PRICE_COLUMNS:
        return False
    async with db_pool.acquire() as conn:
        await conn.execute(f"UPDATE plans SET {col} = $1 WHERE id = $2", value, int(plan_id))
    return True


async def toggle_plan_active(plan_id):
    """فعال/غیرفعال کردن پلن. خروجی: وضعیت جدید."""
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "UPDATE plans SET is_active = NOT COALESCE(is_active, TRUE) WHERE id = $1 RETURNING is_active",
            int(plan_id),
        )


async def get_plan(plan_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM plans WHERE id = $1", int(plan_id))


async def create_plan(name, gb, duration_days, price, vip_price, inbound_id, panel_id,
                      icon='', reseller_price=None, renew_price=None, first_price=None, is_active=True,
                      renew_normal_price=None, renew_vip_price=None, renew_reseller_price=None):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO plans (name, gb, duration_days, price, vip_price,
                                  inbound_id, panel_id, icon, reseller_price, renew_price, first_price,
                                  renew_normal_price, renew_vip_price, renew_reseller_price, is_active)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING id""",
            name, gb, duration_days, price, vip_price, inbound_id, panel_id,
            icon or '', reseller_price, renew_price, first_price,
            renew_normal_price, renew_vip_price, renew_reseller_price, bool(is_active),
        )


async def update_plan(plan_id, name, gb, duration_days, price, vip_price, inbound_id, panel_id,
                      icon='', reseller_price=None, renew_price=None, first_price=None, is_active=True,
                      renew_normal_price=None, renew_vip_price=None, renew_reseller_price=None):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE plans SET name=$2, gb=$3, duration_days=$4, price=$5, vip_price=$6,
                   inbound_id=$7, panel_id=$8, icon=$9,
                   reseller_price=$10, renew_price=$11, first_price=$12,
                   renew_normal_price=$13, renew_vip_price=$14, renew_reseller_price=$15, is_active=$16
               WHERE id=$1""",
            int(plan_id), name, gb, duration_days, price, vip_price, inbound_id, panel_id,
            icon or '', reseller_price, renew_price, first_price,
            renew_normal_price, renew_vip_price, renew_reseller_price, bool(is_active),
        )


async def delete_plan(plan_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM plans WHERE id = $1", int(plan_id))


async def update_panel(panel_id, name, url, username, password, config_ip, sub_url, panel_type=None):
    async with db_pool.acquire() as conn:
        if panel_type is None:
            # سازگاری با فراخوانی‌های قدیمی: نوع فعلی حفظ می‌شود
            await conn.execute(
                "UPDATE panels SET name=$2, url=$3, username=$4, password=$5, config_ip=$6, sub_url=$7 WHERE id=$1",
                int(panel_id), name, url, username, password, config_ip, sub_url,
            )
        else:
            await conn.execute(
                "UPDATE panels SET name=$2, url=$3, username=$4, password=$5, config_ip=$6, sub_url=$7, panel_type=$8 WHERE id=$1",
                int(panel_id), name, url, username, password, config_ip, sub_url, panel_type,
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


async def add_order(user_id, config_link, panel_id=None, email=None, inbound_id=None,
                    plan_id=None, price=None, base_price=None, discount=0,
                    purchase_kind=None, user_role=None):
    """ثبت سفارش.

    نام سرویس و اینباند صریحاً ذخیره می‌شوند تا بعداً از لینک حدس زده نشوند، و قیمتِ
    پرداخت‌شده روی خود سفارش می‌نشیند تا تغییر قیمت‌های بعدی سفارش‌های قبلی را عوض نکند.
    خروجی: شناسه‌ی سفارش.
    """
    if not email:
        email = email_from_link(config_link)
    sub_token = secrets.token_urlsafe(24)
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO orders (user_id, config_link, date, panel_id, email, inbound_id,
                                   plan_id, price, base_price, discount, purchase_kind, user_role, sub_token)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING id""",
            user_id, config_link, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), panel_id, email, inbound_id,
            plan_id, price, base_price, int(discount or 0), purchase_kind, user_role, sub_token,
        )


def make_sub_token():
    """توکن تصادفی و URL-safe برای شناسایی اشتراک (منبع واحد با subportal)."""
    return secrets.token_urlsafe(24)


async def ensure_sub_token(order_id):
    """توکن پورتال اشتراکِ سفارش را برمی‌گرداند؛ اگر نبود می‌سازد (اتمیک)."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT sub_token FROM orders WHERE id = $1", int(order_id))
        if not row:
            return None
        if row['sub_token']:
            return row['sub_token']
        token = make_sub_token()
        try:
            await conn.execute("UPDATE orders SET sub_token = $1 WHERE id = $2", token, int(order_id))
            return token
        except asyncpg.UniqueViolationError:
            # مسابقه‌ی هم‌زمانی: توکنی که تازه ساخته شده را برمی‌گردانیم
            row = await conn.fetchrow("SELECT sub_token FROM orders WHERE id = $1", int(order_id))
            return row['sub_token'] if row else None


async def get_order_by_token(token):
    """سفارش بر اساس توکن پورتال. توکن نامعتبر → None (بدون هیچ نشت اطلاعات)."""
    if not token or not isinstance(token, str) or len(token) > 128:
        return None
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            """SELECT id, user_id, config_link, email, date, panel_id, inbound_id,
                      plan_id, purchase_kind, user_role
               FROM orders WHERE sub_token = $1""",
            token,
        )


async def delete_order(order_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM orders WHERE id = $1", int(order_id))


async def get_order_by_id(order_id):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT config_link, date FROM orders WHERE id = $1", int(order_id))
        return (row['config_link'], row['date']) if row else None


async def get_order(order_id):
    """ردیف کاملِ سفارش (شامل نام سرویس، اینباند و تمدید خودکار)؛ مبنای همه‌ی عملیات روی سرویس."""
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT o.id, o.user_id, o.config_link, o.email, o.date, o.panel_id, o.inbound_id, "
            "o.auto_renew, o.auto_renew_plan_id, p.name AS auto_plan_label "
            "FROM orders o LEFT JOIN plans p ON p.id = o.auto_renew_plan_id WHERE o.id = $1",
            int(order_id),
        )


async def update_order_service(order_id, config_link=None, email=None, inbound_id=None):
    """به‌روزرسانی لینک/نام/اینباند سفارش بعد از تغییرات روی پنل."""
    sets, args = [], []
    for col, val in (("config_link", config_link), ("email", email), ("inbound_id", inbound_id)):
        if val is not None:
            args.append(val)
            sets.append(f"{col} = ${len(args)}")
    if not sets:
        return
    args.append(int(order_id))
    async with db_pool.acquire() as conn:
        await conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = ${len(args)}", *args)


async def get_order_panel_id(order_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT panel_id FROM orders WHERE id = $1", int(order_id))


async def get_orders_for_notify():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT id, user_id, config_link, email, panel_id, expiry_notified, lowdata_notified FROM orders")


async def mark_notified(order_id, field):
    if field not in ('expiry_notified', 'lowdata_notified'):
        return
    async with db_pool.acquire() as conn:
        await conn.execute(f"UPDATE orders SET {field} = TRUE WHERE id = $1", int(order_id))


async def reset_notify(order_id):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET expiry_notified = FALSE, lowdata_notified = FALSE, "
            "notify_3d = FALSE, notify_1d = FALSE, notify_expired = FALSE WHERE id = $1", int(order_id))


# ================= تمدید خودکار =================
async def set_auto_renew(order_id, enabled, plan_id=None):
    """فعال/غیرفعال‌کردن تمدید خودکار یک سفارش. plan_id = پلن پیش‌فرض تمدید."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET auto_renew = $2, auto_renew_plan_id = $3 WHERE id = $1",
            int(order_id), bool(enabled), int(plan_id) if plan_id else None,
        )


async def get_auto_renew_orders():
    """سفارش‌هایی که تمدید خودکار دارند (برای Job دوره‌ای)."""
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            """SELECT o.id, o.user_id, o.email, o.panel_id, o.inbound_id, o.auto_renew_plan_id,
                      p.id AS plan_id, p.name AS plan_name, p.gb, p.duration_days
               FROM orders o LEFT JOIN plans p ON p.id = o.auto_renew_plan_id
               WHERE o.auto_renew = TRUE"""
        )


# ================= یادآوری هوشمند سه‌مرحله‌ای =================
async def get_orders_for_smart_notify():
    """سفارش‌هایی که هنوز هر سه مرحله‌ی یادآوری برایشان تمام نشده."""
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, user_id, config_link, email, panel_id, "
            "expiry_notified, lowdata_notified, notify_3d, notify_1d, notify_expired "
            "FROM orders WHERE notify_expired = FALSE OR notify_3d = FALSE OR notify_1d = FALSE "
            "OR expiry_notified = FALSE"
        )


async def mark_smart_notify(order_id, field):
    """علامت‌زدن مرحله‌ی یادآوری. هر مرحله فقط یک‌بار."""
    if field not in ('notify_3d', 'notify_1d', 'notify_expired', 'expiry_notified', 'lowdata_notified'):
        return
    async with db_pool.acquire() as conn:
        await conn.execute(f"UPDATE orders SET {field} = TRUE WHERE id = $1", int(order_id))


# ================= بن کاربر =================
async def set_user_banned(user_id, banned):
    """بن/آزادسازی. اگر ردیف کاربر نباشد ساخته می‌شود تا بنِ هر آیدی کار کند."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (user_id, is_banned) VALUES ($1, $2)
               ON CONFLICT (user_id) DO UPDATE SET is_banned = $2""",
            int(user_id), bool(banned),
        )


async def is_user_banned(user_id):
    async with db_pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT is_banned FROM users WHERE user_id = $1", int(user_id)))


async def list_banned_users():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, nickname FROM users WHERE is_banned = TRUE ORDER BY user_id DESC")


# ================= خروجی CSV =================
async def list_sales_export(limit=20000):
    """همه‌ی فروش‌ها برای خروجی CSV."""
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, user_id, order_id, plan_id, kind, user_role, base_price, price, discount, created_at "
            "FROM sales ORDER BY id DESC LIMIT $1", limit)


async def list_users_export():
    """همه‌ی کاربران برای خروجی CSV."""
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT user_id, nickname, role, balance, COALESCE(is_banned, FALSE) AS is_banned, referred_by "
            "FROM users ORDER BY user_id")


# ================= قیمت‌گذاری، آفر و فروش =================
async def get_pricing_profile(user_id):
    """اطلاعات لازم برای قیمت‌گذاریِ یک کاربر، با یک رفت‌وبرگشت به دیتابیس.

    خروجی: dict شامل نقش، وضعیت آفر خرید اول، تعداد خریدهای قبلی و شارژ ۳۰ روز اخیر.
    """
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        row = await conn.fetchrow(
            """SELECT u.role, u.balance, u.nickname,
                      COALESCE(u.first_offer_used, FALSE) AS first_offer_used,
                      COALESCE(u.reseller_bonus_used, FALSE) AS reseller_bonus_used,
                      (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.user_id) AS order_count,
                      (SELECT COALESCE(SUM(t.amount), 0) FROM transactions t
                         WHERE t.user_id = u.user_id AND t.amount > 0
                           AND t.kind IN ('شارژ حساب', 'کد هدیه')
                           AND t.date >= NOW() - INTERVAL '30 days') AS monthly_topup
               FROM users u WHERE u.user_id = $1""",
            user_id,
        )
    return dict(row) if row else {}


async def mark_first_offer_used(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET first_offer_used = TRUE WHERE user_id = $1", user_id)


async def claim_reseller_bonus(user_id):
    """پرچم «هدیه‌ی اولین شارژ نماینده» را اتمیک برمی‌دارد.

    خروجی True یعنی این فراخوانی صاحب هدیه است؛ فراخوانی‌های بعدی False می‌گیرند.
    """
    async with db_pool.acquire() as conn:
        return bool(await conn.fetchval(
            """UPDATE users SET reseller_bonus_used = TRUE
               WHERE user_id = $1 AND COALESCE(reseller_bonus_used, FALSE) = FALSE
               RETURNING TRUE""",
            user_id,
        ))


async def count_users_by_role():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT COALESCE(role, 'normal') AS role, COUNT(*) AS n FROM users GROUP BY 1")
    return {r['role']: int(r['n']) for r in rows}


# ---------- آفرها ----------
async def list_offers(active_only=False):
    async with db_pool.acquire() as conn:
        if active_only:
            return await conn.fetch(
                """SELECT * FROM offers
                   WHERE COALESCE(is_active, FALSE) = TRUE
                     AND (starts_at IS NULL OR starts_at <= NOW())
                     AND (ends_at IS NULL OR ends_at >= NOW())
                     AND (max_uses = 0 OR COALESCE(used_count, 0) < max_uses)
                   ORDER BY id DESC"""
            )
        return await conn.fetch("SELECT * FROM offers ORDER BY id DESC")


async def get_offer(offer_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM offers WHERE id = $1", int(offer_id))


async def save_offer(offer_id, name, kind, value, audience, plan_id, starts_at, ends_at,
                     max_uses, per_user_limit, is_active):
    async with db_pool.acquire() as conn:
        if offer_id:
            await conn.execute(
                """UPDATE offers SET name=$2, kind=$3, value=$4, audience=$5, plan_id=$6,
                       starts_at=$7, ends_at=$8, max_uses=$9, per_user_limit=$10, is_active=$11
                   WHERE id=$1""",
                int(offer_id), name, kind, value, audience, plan_id, starts_at, ends_at,
                max_uses, per_user_limit, bool(is_active),
            )
            return int(offer_id)
        return await conn.fetchval(
            """INSERT INTO offers (name, kind, value, audience, plan_id, starts_at, ends_at,
                                   max_uses, per_user_limit, is_active)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
            name, kind, value, audience, plan_id, starts_at, ends_at, max_uses, per_user_limit, bool(is_active),
        )


async def delete_offer(offer_id):
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM offer_redemptions WHERE offer_id = $1", int(offer_id))
            await conn.execute("DELETE FROM offers WHERE id = $1", int(offer_id))


async def toggle_offer_active(offer_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "UPDATE offers SET is_active = NOT COALESCE(is_active, FALSE) WHERE id = $1 RETURNING is_active",
            int(offer_id),
        )


async def offer_uses_by_user(user_id):
    """تعداد دفعاتی که این کاربر از هر آفر استفاده کرده: {offer_id: count}."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT offer_id, COUNT(*) AS n FROM offer_redemptions WHERE user_id = $1 GROUP BY 1", user_id,
        )
    return {r['offer_id']: int(r['n']) for r in rows}


async def redeem_offer(offer_id, user_id, per_user_limit=1, max_uses=0):
    """ثبت اتمیک استفاده از آفر با رعایت سقف کل و سقف هر کاربر.

    خروجی: True اگر ثبت شد. تصمیم‌گیری داخل یک تراکنش انجام می‌شود تا دو درخواست
    هم‌زمان نتوانند از سقف عبور کنند.
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT max_uses, per_user_limit, used_count FROM offers WHERE id = $1 FOR UPDATE", int(offer_id))
            if not row:
                return False
            total_limit = row['max_uses'] if row['max_uses'] is not None else max_uses
            user_limit = row['per_user_limit'] if row['per_user_limit'] is not None else per_user_limit
            if total_limit and int(row['used_count'] or 0) >= int(total_limit):
                return False
            if user_limit:
                used = await conn.fetchval(
                    "SELECT COUNT(*) FROM offer_redemptions WHERE offer_id = $1 AND user_id = $2", int(offer_id), user_id,
                )
                if int(used or 0) >= int(user_limit):
                    return False
            await conn.execute("INSERT INTO offer_redemptions (offer_id, user_id) VALUES ($1, $2)", int(offer_id), user_id)
            await conn.execute("UPDATE offers SET used_count = COALESCE(used_count, 0) + 1 WHERE id = $1", int(offer_id))
            return True


# ---------- دفتر فروش ----------
async def record_sale(user_id, kind, user_role, base_price, price, discount,
                      plan_id=None, order_id=None, offer_id=None, first_offer=False):
    """ثبت یک فروش برای گزارش مالی (تمدید سفارش جدید نمی‌سازد، پس اینجا ثبت می‌شود)."""
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO sales (user_id, order_id, plan_id, kind, user_role, base_price, price, discount, offer_id, first_offer)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
            user_id, order_id, plan_id, kind, user_role,
            int(base_price or 0), int(price or 0), int(discount or 0), offer_id, bool(first_offer),
        )


async def get_financial_report():
    """گزارش مالی کامل: تفکیک کاربران، فروش دوره‌ای، تفکیک نقش، آفرها و تخفیف‌ها."""
    async with db_pool.acquire() as conn:
        roles = await conn.fetch("SELECT COALESCE(role,'normal') AS role, COUNT(*) AS n FROM users GROUP BY 1")
        periods = await conn.fetchrow(
            """SELECT
                 COALESCE(SUM(price) FILTER (WHERE created_at::date = CURRENT_DATE), 0) AS today,
                 COALESCE(SUM(price) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days'), 0) AS week,
                 COALESCE(SUM(price) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days'), 0) AS month,
                 COALESCE(SUM(price), 0) AS total,
                 COALESCE(SUM(discount), 0) AS discount,
                 COUNT(*) FILTER (WHERE kind = 'renew') AS renewals,
                 COUNT(*) FILTER (WHERE first_offer) AS first_offers,
                 COUNT(*) AS sales_count
               FROM sales"""
        )
        by_role = await conn.fetch(
            "SELECT COALESCE(user_role,'normal') AS role, COALESCE(SUM(price),0) AS total, COUNT(*) AS n FROM sales GROUP BY 1"
        )
        refunds = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kind = 'برگشت وجه'")
        topup = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kind = 'شارژ حساب'")
        bonus = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kind = 'هدیه نمایندگی'")
        gifts = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kind = 'کد هدیه'")
        referrals = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kind IN ('پاداش دعوت', 'هدیه دعوت')"
        )

    users_by_role = {r['role']: int(r['n']) for r in roles}
    sales_by_role = {r['role']: {'total': int(r['total']), 'count': int(r['n'])} for r in by_role}
    total = int(periods['total'])
    discount = int(periods['discount'])
    gifts = int(gifts or 0)
    referrals = int(referrals or 0)
    bonus = int(bonus or 0)
    refunds = int(refunds or 0)
    return {
        'users': users_by_role,
        'today': int(periods['today']),
        'week': int(periods['week']),
        'month': int(periods['month']),
        'total': total,
        'discount': discount,
        'renewals': int(periods['renewals']),
        'first_offers': int(periods['first_offers']),
        'sales_count': int(periods['sales_count']),
        'by_role': sales_by_role,
        'refunds': refunds,
        'topup': int(topup or 0),
        'bonus': bonus,
        'gifts': gifts,
        'referrals': referrals,
        # درآمد خالص: فروش ثبت‌شده منهای برگشت وجه و اعتبارهای هدیه‌ای
        'net': total - refunds - bonus - gifts - referrals,
    }


async def get_revenue_series(days=30):
    """سری زمانی درآمد برای نمودار داشبورد.

    خروجی: (لیست فروش ۳۰ روز اخیر قدیم→جدید با صفر برای روزهای بدون فروش،
    جمع دوره‌ی قبلی برای مقایسه‌ی رشد). روز صفر یعنی فروش نداشته، نه داده‌ی گم‌شده.
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT created_at::date AS d, COALESCE(SUM(price),0) AS total
               FROM sales
               WHERE created_at >= CURRENT_DATE - make_interval(days => $1 * 2)
               GROUP BY 1""",
            days,
        )
        users_total = await conn.fetchval("SELECT COUNT(*) FROM users")
        orders_total = await conn.fetchval("SELECT COUNT(*) FROM orders")
        users_active = await conn.fetchval(
            "SELECT COUNT(DISTINCT user_id) FROM orders WHERE date >= to_char(CURRENT_DATE - 30, 'YYYY-MM-DD')"
        )
    by_day = {r['d']: int(r['total']) for r in rows}
    today = datetime.date.today()
    series, prev_total = [], 0
    for i in range(days - 1, -1, -1):
        day = today - datetime.timedelta(days=i)
        series.append(by_day.get(day, 0))
    for i in range(days, days * 2):
        prev_total += by_day.get(today - datetime.timedelta(days=i), 0)
    return {
        'series': series,
        'prev_total': prev_total,
        'cur_total': sum(series),
        'users_total': int(users_total or 0),
        'users_active': int(users_active or 0),
        'orders_total': int(orders_total or 0),
    }


async def get_dashboard_health():
    """شمارنده‌های سلامت داشبورد: فروش ۲۴ ساعت، سفارش‌های امروز و تمدیدها."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT
                 COUNT(*) FILTER (WHERE created_at::date = CURRENT_DATE) AS orders_today,
                 COUNT(*) FILTER (WHERE kind='renew' AND created_at::date = CURRENT_DATE) AS renewals_today,
                 (SELECT COUNT(*) FROM users) AS users_total
               FROM sales"""
        )
    return dict(
        orders_today=int(row['orders_today']),
        renewals_today=int(row['renewals_today']),
        users_total=int(row['users_total']),
    )


# ================= پنل‌ها =================
async def get_panels():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM panels ORDER BY id ASC")


async def get_panel(panel_id):
    if panel_id is None:
        return None
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM panels WHERE id = $1", int(panel_id))


async def add_panel(name, url, username, password, config_ip, sub_url='', panel_type=''):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO panels (name, url, username, password, config_ip, sub_url, panel_type) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
            name, url, username, password, config_ip, sub_url, panel_type,
        )


async def delete_panel(panel_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM panels WHERE id = $1", int(panel_id))


PANEL_COLUMNS = {'name', 'url', 'username', 'password', 'config_ip', 'sub_url', 'panel_type'}


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


async def load_referral_config():
    keys = ('ref_enabled', 'ref_bonus', 'ref_invitee_bonus', 'ref_min_topup')
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM settings WHERE key = ANY($1::text[])", list(keys))
    return rewards.parse_referral_config({r['key']: r['value'] for r in rows})


async def set_referrer(user_id, referrer_id):
    """معرف را فقط اگر وجود داشته باشد، خودش نباشد و قبلاً ثبت نشده باشد ذخیره می‌کند.
    خروجی True یعنی ثبت شد."""
    try:
        referrer_id = int(referrer_id)
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False
    if referrer_id == user_id:
        return False
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", referrer_id)
        if not exists:
            return False
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        status = await conn.execute(
            "UPDATE users SET referred_by = $1 WHERE user_id = $2 AND referred_by IS NULL",
            referrer_id, user_id,
        )
    try:
        return int(status.split()[-1]) == 1
    except (ValueError, IndexError, AttributeError):
        return False


async def referral_count(user_id):
    async with db_pool.acquire() as conn:
        return int(await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by = $1", user_id) or 0)


async def referral_rewarded_count(user_id):
    async with db_pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referred_by = $1 AND COALESCE(ref_rewarded, FALSE)",
            user_id,
        ) or 0)


async def get_referral_stats():
    """آمار کلی دعوت برای پنل وب و منوی ادمین."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT
                 COUNT(*) FILTER (WHERE referred_by IS NOT NULL) AS invited,
                 COUNT(*) FILTER (WHERE COALESCE(ref_rewarded, FALSE)) AS rewarded,
                 COUNT(*) FILTER (WHERE referred_by IS NOT NULL AND NOT COALESCE(ref_rewarded, FALSE)) AS pending
               FROM users"""
        )
        paid = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kind IN ('پاداش دعوت', 'هدیه دعوت')"
        )
    return {
        'invited': int(row['invited'] or 0),
        'rewarded': int(row['rewarded'] or 0),
        'pending': int(row['pending'] or 0),
        'paid': int(paid or 0),
    }


async def list_referrals(search=None, limit=50, offset=0):
    async with db_pool.acquire() as conn:
        if search:
            like = f"%{search}%"
            return await conn.fetch(
                """SELECT u.user_id, u.nickname, u.referred_by, u.ref_rewarded, u.balance,
                          r.nickname AS referrer_nick
                   FROM users u
                   LEFT JOIN users r ON r.user_id = u.referred_by
                   WHERE u.referred_by IS NOT NULL
                     AND (CAST(u.user_id AS TEXT) LIKE $1
                          OR CAST(u.referred_by AS TEXT) LIKE $1
                          OR u.nickname ILIKE $1)
                   ORDER BY u.user_id DESC LIMIT $2 OFFSET $3""",
                like, limit, offset,
            )
        return await conn.fetch(
            """SELECT u.user_id, u.nickname, u.referred_by, u.ref_rewarded, u.balance,
                      r.nickname AS referrer_nick
               FROM users u
               LEFT JOIN users r ON r.user_id = u.referred_by
               WHERE u.referred_by IS NOT NULL
               ORDER BY u.user_id DESC LIMIT $1 OFFSET $2""",
            limit, offset,
        )


async def count_referrals(search=None):
    async with db_pool.acquire() as conn:
        if search:
            like = f"%{search}%"
            return int(await conn.fetchval(
                """SELECT COUNT(*) FROM users u
                   WHERE u.referred_by IS NOT NULL
                     AND (CAST(u.user_id AS TEXT) LIKE $1
                          OR CAST(u.referred_by AS TEXT) LIKE $1
                          OR u.nickname ILIKE $1)""",
                like,
            ) or 0)
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL"
        ) or 0)


async def try_reward_referrer(user_id, charge_amount=0):
    """هنگام اولین شارژِ واجد شرایط، یک‌بار به معرف (و در صورت تنظیم به دعوت‌شده) پاداش می‌دهد.

    اگر سیستم خاموش باشد یا مبلغ به حداقل نرسیده باشد پرچم بالا نمی‌رود.
    خروجی: `rewards.ReferralReward` یا None.
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT referred_by, ref_rewarded FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if not row:
                return None
            setting_rows = await conn.fetch(
                "SELECT key, value FROM settings WHERE key = ANY($1::text[])",
                ['ref_enabled', 'ref_bonus', 'ref_invitee_bonus', 'ref_min_topup'],
            )
            cfg = rewards.parse_referral_config({r['key']: r['value'] for r in setting_rows})
            if not rewards.referral_should_pay(
                cfg,
                referred_by=row['referred_by'],
                already_rewarded=bool(row['ref_rewarded']),
                charge_amount=charge_amount,
            ):
                return None
            referrer = row['referred_by']
            await conn.execute("UPDATE users SET ref_rewarded = TRUE WHERE user_id = $1", user_id)
            if cfg.referrer_bonus > 0:
                await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", referrer)
                await conn.execute(
                    "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                    cfg.referrer_bonus, referrer,
                )
                await conn.execute(
                    "INSERT INTO transactions (user_id, amount, kind, description) VALUES ($1, $2, $3, $4)",
                    referrer, cfg.referrer_bonus, 'پاداش دعوت', f'دعوت کاربر {user_id}',
                )
            if cfg.invitee_bonus > 0:
                await conn.execute(
                    "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                    cfg.invitee_bonus, user_id,
                )
                await conn.execute(
                    "INSERT INTO transactions (user_id, amount, kind, description) VALUES ($1, $2, $3, $4)",
                    user_id, cfg.invitee_bonus, 'هدیه دعوت', f'ورود با دعوت {referrer}',
                )
            return rewards.ReferralReward(referrer, cfg.referrer_bonus, cfg.invitee_bonus)


# ================= کدهای هدیه =================
def _gift_select_cols():
    return ("code, amount, max_uses, used_count, expires_at, is_active, note, audience, created_at")


async def _find_gift_code(conn, code):
    """ردیف کد را با تطبیق بدون حساسیت به حروف پیدا می‌کند."""
    normalized = rewards.normalize_gift_code(code)
    if not normalized:
        return None
    return await conn.fetchrow(
        f"SELECT {_gift_select_cols()} FROM gift_codes WHERE UPPER(code) = $1",
        normalized,
    )


async def add_gift_code(code, amount, max_uses, expires_at=None, is_active=True, note='', audience='all'):
    """ساخت یا به‌روزرسانی کد هدیه. خروجی کد نرمال‌شده، یا None اگر نامعتبر باشد."""
    normalized = rewards.normalize_gift_code(code) or rewards.generate_gift_code()
    if not rewards.is_valid_gift_code(normalized):
        return None
    amount = int(amount)
    max_uses = max(1, int(max_uses or 1))
    audience = audience if audience in rewards.GIFT_AUDIENCES else 'all'
    async with db_pool.acquire() as conn:
        existing = await conn.fetchval("SELECT code FROM gift_codes WHERE UPPER(code) = $1", normalized)
        key = existing or normalized
        await conn.execute(
            """INSERT INTO gift_codes (code, amount, max_uses, expires_at, is_active, note, audience)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (code) DO UPDATE SET
                 amount = $2, max_uses = $3, expires_at = $4, is_active = $5, note = $6, audience = $7""",
            key, amount, max_uses, expires_at, bool(is_active), note or '', audience,
        )
    return key


async def delete_gift_code(code):
    normalized = rewards.normalize_gift_code(code)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT code FROM gift_codes WHERE UPPER(code) = $1", normalized)
            if not row:
                return False
            await conn.execute("DELETE FROM gift_redemptions WHERE code = $1", row['code'])
            await conn.execute("DELETE FROM gift_codes WHERE code = $1", row['code'])
            return True


async def list_gift_codes():
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            f"SELECT {_gift_select_cols()} FROM gift_codes ORDER BY created_at DESC NULLS LAST, code"
        )


async def get_gift_code(code):
    async with db_pool.acquire() as conn:
        return await _find_gift_code(conn, code)


async def toggle_gift_code_active(code):
    normalized = rewards.normalize_gift_code(code)
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            """UPDATE gift_codes SET is_active = NOT COALESCE(is_active, FALSE)
               WHERE UPPER(code) = $1 RETURNING is_active""",
            normalized,
        )


async def list_gift_redemptions(code):
    normalized = rewards.normalize_gift_code(code)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT code FROM gift_codes WHERE UPPER(code) = $1", normalized)
        if not row:
            return []
        return await conn.fetch(
            """SELECT r.user_id, r.amount, r.created_at, u.nickname
               FROM gift_redemptions r
               LEFT JOIN users u ON u.user_id = r.user_id
               WHERE r.code = $1
               ORDER BY r.created_at DESC NULLS LAST""",
            row['code'],
        )


async def redeem_gift_code(user_id, code):
    """اعمال اتمیک کد هدیه. خروجی: (ok, message)."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            gc = await _find_gift_code(conn, code)
            if not gc:
                return False, "❌ کد نامعتبر است."
            # قفل ردیف برای جلوگیری از استفاده هم‌زمان بیش از سقف
            gc = await conn.fetchrow(
                f"SELECT {_gift_select_cols()} FROM gift_codes WHERE code = $1 FOR UPDATE",
                gc['code'],
            )
            order_count = int(await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE user_id = $1", user_id,
            ) or 0)
            already = await conn.fetchval(
                "SELECT 1 FROM gift_redemptions WHERE code = $1 AND user_id = $2",
                gc['code'], user_id,
            )
            reason = rewards.gift_reject_reason(
                dict(gc), order_count=order_count, already_used=bool(already),
            )
            if reason:
                return False, reason
            await conn.execute(
                "INSERT INTO gift_redemptions (code, user_id, amount) VALUES ($1, $2, $3)",
                gc['code'], user_id, gc['amount'],
            )
            await conn.execute(
                "UPDATE gift_codes SET used_count = used_count + 1 WHERE code = $1",
                gc['code'],
            )
            await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
            await conn.execute(
                "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                gc['amount'], user_id,
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, kind, description) VALUES ($1, $2, $3, $4)",
                user_id, gc['amount'], 'کد هدیه', gc['code'],
            )
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
