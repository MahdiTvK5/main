"""موتور قیمت‌گذاری: سطح کاربر، آفرها، تمدید و پله‌های تخفیف نمایندگی.

طراحی به‌صورت دو لایه است:

* توابع خالص (`base_price_for`, `apply_offer`, `resolve_quote`, ...) که هیچ وابستگی
  به دیتابیس ندارند و کاملاً قابل تست هستند.
* توابع async (`build_buyer`, `quote_for_user`, ...) که داده را از `db` می‌گیرند و
  محاسبه را به لایه‌ی خالص می‌سپارند.

هیچ قیمتی در این ماژول hard-code نشده؛ همه‌ی اعداد از جدول `plans` و جدول `settings`
خوانده می‌شوند تا ادمین بتواند بدون تغییر کد آن‌ها را عوض کند.
"""
import datetime
import logging
from dataclasses import dataclass, field

import db

# ================= سطوح کاربر =================
NORMAL = 'normal'
VIP = 'vip'
RESELLER = 'reseller'
ROLES = (NORMAL, VIP, RESELLER)

ROLE_LABELS = {
    NORMAL: 'عادی',
    VIP: 'VIP 💎',
    RESELLER: 'نماینده 🤝',
}

# نوع خرید
KIND_BUY = 'buy'
KIND_RENEW = 'renew'
KIND_BULK = 'bulk'

AUDIENCES = {
    'all': 'همه',
    'new': 'کاربران جدید',
    'existing': 'کاربران قدیمی',
    'vip': 'کاربران VIP',
    'reseller': 'نمایندگان',
}


def normalize_role(role):
    """هر مقدار ناشناخته‌ای در ستون role به «عادی» تفسیر می‌شود."""
    role = (role or '').strip().lower()
    return role if role in ROLES else NORMAL


def role_label(role):
    return ROLE_LABELS.get(normalize_role(role), ROLE_LABELS[NORMAL])


def can_buy_bulk(role, admin_status=False):
    """خرید عمده برای ادمین، VIP و نماینده."""
    return bool(admin_status) or normalize_role(role) in (VIP, RESELLER)


def _int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _positive(value):
    """مقدار مثبت یا None (ستون‌های قیمتِ خالی/صفر یعنی «تنظیم نشده»)."""
    v = _int(value, 0)
    return v if v > 0 else None


# ================= داده‌های ورودی محاسبه =================
@dataclass
class Buyer:
    """تصویری از کاربر در لحظه‌ی قیمت‌گذاری."""
    user_id: int
    role: str = NORMAL
    admin: bool = False
    order_count: int = 0
    first_offer_used: bool = False
    reseller_bonus_used: bool = False
    monthly_topup: int = 0
    offer_uses: dict = field(default_factory=dict)

    @property
    def is_new_customer(self):
        """کاربری که هنوز هیچ سرویسی نگرفته و آفر خرید اول را مصرف نکرده است."""
        return self.order_count == 0 and not self.first_offer_used

    @property
    def is_returning(self):
        return self.order_count > 0


@dataclass
class PricingConfig:
    """تنظیمات قابل‌مدیریت (از جدول settings خوانده می‌شود)."""
    first_offer_enabled: bool = True
    renew_offer_enabled: bool = True
    reseller_bonus_enabled: bool = True
    reseller_bonus_min: int = 1_000_000
    reseller_bonus_percent: int = 10
    reseller_t2_min: int = 1_000_000
    reseller_t2_discount: int = 5
    reseller_t3_min: int = 3_000_000
    reseller_t3_discount: int = 8


@dataclass
class Quote:
    """نتیجه‌ی قیمت‌گذاری یک پلن برای یک کاربر."""
    plan_id: int
    kind: str = KIND_BUY
    base_price: int = 0        # قیمت پایه‌ی همان سطح کاربر (قبل از آفر)
    price: int = 0             # قیمت نهایی قابل پرداخت
    label: str = ''            # عنوان آفر برای نمایش
    offer_id: int = None       # آفر جدول offers (در صورت اعمال)
    first_offer: bool = False  # آیا آفر خرید اول اعمال شده
    tier_discount: int = 0     # درصد تخفیف پله‌ی نمایندگی

    @property
    def discount(self):
        return max(0, self.base_price - self.price)

    @property
    def has_discount(self):
        return self.discount > 0

    @property
    def discount_percent(self):
        if self.base_price <= 0:
            return 0
        return round(self.discount * 100 / self.base_price)


# ================= لایه‌ی خالص =================
def base_price_for(plan, role, kind=KIND_BUY, config=None, custom_price=None):
    """قیمت پایه‌ی یک پلن برای یک سطح کاربر (قبل از هر آفری).

    ترتیب اولویت: قیمت اختصاصی کاربر ← قیمت سطح (نماینده/VIP/عادی) با fallback به
    سطح بالاتر. قیمت تمدید و آفر خرید اول اینجا اعمال نمی‌شوند؛ آن‌ها در
    `resolve_quote` روی همین پایه می‌نشینند تا «قیمت اصلی» برای کاربر قابل نمایش بماند.
    """
    role = normalize_role(role)

    if custom_price is not None:
        base = _int(custom_price)
    else:
        normal = _positive(plan['price']) or 0
        vip = _positive(_plan_get(plan, 'vip_price')) or normal
        reseller = _positive(_plan_get(plan, 'reseller_price')) or vip
        base = {NORMAL: normal, VIP: vip, RESELLER: reseller}[role]

    if kind == KIND_BULK:
        bulk = _positive(_plan_get(plan, 'vip_bulk_price'))
        if bulk:
            base = bulk if custom_price is None else min(base, bulk)

    return max(0, _int(base))


def _plan_get(plan, key, default=None):
    """خواندن امن یک ستون از Record/dict پلن (ستون‌های جدید ممکن است نباشند)."""
    try:
        value = plan[key]
    except (KeyError, TypeError, IndexError):
        return default
    return default if value is None else value


def reseller_tier(monthly_topup, config):
    """پله‌ی نمایندگی بر اساس مجموع شارژ ۳۰ روز اخیر.

    خروجی: (شماره‌ی پله، درصد تخفیف، عنوان).
    """
    topup = _int(monthly_topup)
    if config.reseller_t3_min and topup >= config.reseller_t3_min:
        return 3, _int(config.reseller_t3_discount), 'نماینده VIP'
    if config.reseller_t2_min and topup >= config.reseller_t2_min:
        return 2, _int(config.reseller_t2_discount), 'نماینده سطح ۲'
    return 1, 0, 'نماینده سطح ۱'


def offer_matches(offer, buyer, plan_id, now=None):
    """آیا این آفر برای این کاربر و این پلن قابل اعمال است؟"""
    if not offer.get('is_active', True):
        return False
    now = now or datetime.datetime.now()
    starts, ends = offer.get('starts_at'), offer.get('ends_at')
    if starts and now < starts:
        return False
    if ends and now > ends:
        return False
    max_uses = _int(offer.get('max_uses'))
    if max_uses and _int(offer.get('used_count')) >= max_uses:
        return False
    per_user = _int(offer.get('per_user_limit'), 1)
    if per_user and _int(buyer.offer_uses.get(offer.get('id')), 0) >= per_user:
        return False
    plan_filter = offer.get('plan_id')
    if plan_filter and _int(plan_filter) != _int(plan_id):
        return False

    audience = (offer.get('audience') or 'all').lower()
    role = normalize_role(buyer.role)
    if audience == 'new' and not buyer.is_new_customer:
        return False
    if audience == 'existing' and not buyer.is_returning:
        return False
    if audience == 'vip' and role != VIP:
        return False
    if audience == 'reseller' and role != RESELLER:
        return False
    return True


def apply_offer(base, offer):
    """قیمت بعد از اعمال یک آفر (درصدی یا مبلغ ثابت)."""
    value = _int(offer.get('value'))
    if (offer.get('kind') or 'percent').lower() == 'fixed':
        return max(0, value)
    return max(0, base - (base * max(0, min(100, value)) // 100))


def resolve_quote(plan, buyer, kind=KIND_BUY, config=None, custom_price=None, offers=(), now=None):
    """قیمت نهایی یک پلن برای یک کاربر.

    قاعده: تخفیف‌ها روی هم سوار نمی‌شوند؛ همه‌ی قیمت‌های ممکن (سطح کاربر، آفر خرید
    اول، قیمت تمدید، پله‌ی نمایندگی و آفرهای فعال) کنار هم گذاشته می‌شوند و
    **ارزان‌ترین** برنده است. این هم برای کاربر قابل فهم است و هم جلوی تخفیفِ
    تصادفیِ روی‌هم‌رفته (مثلاً پله‌ی نمایندگی روی قیمت آفر خرید اول) را می‌گیرد.
    """
    config = config or PricingConfig()
    role = normalize_role(buyer.role)
    plan_id = _int(_plan_get(plan, 'id'))
    base = base_price_for(plan, role, kind, config, custom_price)

    # هر گزینه: (قیمت، برچسب، آفر خرید اول؟، آیدی آفر، درصد پله)
    candidates = [(base, '', False, None, 0)]

    if (kind == KIND_BUY and config.first_offer_enabled and buyer.is_new_customer
            and custom_price is None):
        first = _positive(_plan_get(plan, 'first_price'))
        if first:
            candidates.append((first, '🔥 آفر خرید اول', True, None, 0))

    if kind == KIND_RENEW and config.renew_offer_enabled and custom_price is None:
        renew = _positive(_plan_get(plan, 'renew_price'))
        if renew:
            candidates.append((renew, '♻️ قیمت ویژه‌ی تمدید', False, None, 0))

    if role == RESELLER:
        _tier, percent, tier_title = reseller_tier(buyer.monthly_topup, config)
        if percent > 0:
            tier_price = max(0, base - (base * percent // 100))
            candidates.append((tier_price, f'🤝 {tier_title} ({percent}٪)', False, None, percent))

    for offer in offers:
        if not offer_matches(offer, buyer, plan_id, now):
            continue
        candidates.append((
            apply_offer(base, offer), f"🎯 {offer.get('name') or 'آفر ویژه'}", False, offer.get('id'), 0,
        ))

    price, label, first_offer, offer_id, tier_discount = min(candidates, key=lambda c: c[0])
    return Quote(
        plan_id=plan_id, kind=kind, base_price=base, price=max(0, price),
        label=label if price < base else '', offer_id=offer_id,
        first_offer=first_offer, tier_discount=tier_discount,
    )


def reseller_bonus_for(amount, buyer, config):
    """اعتبار هدیه‌ی اولین شارژ نماینده. خروجی: مبلغ هدیه (۰ یعنی بدون هدیه)."""
    if not config.reseller_bonus_enabled:
        return 0
    if normalize_role(buyer.role) != RESELLER:
        return 0
    if buyer.reseller_bonus_used:
        return 0
    if _int(amount) < _int(config.reseller_bonus_min):
        return 0
    return _int(amount) * _int(config.reseller_bonus_percent) // 100


# ================= نمایش =================
def money(value):
    return f"{_int(value):,}"


def plan_title(plan):
    days = _int(_plan_get(plan, 'duration_days'), 30) or 30
    icon = str(_plan_get(plan, 'icon', '') or '').strip()
    name = _plan_get(plan, 'name', '') or ''
    return f"{icon + ' ' if icon else ''}{name}".strip(), f"{_plan_get(plan, 'gb', 0)}GB / {days} روز"


def button_text(plan, quote):
    """متن دکمه‌ی پلن در منوی خرید (قیمت آفر با علامت 🔥 مشخص می‌شود)."""
    title, spec = plan_title(plan)
    if quote.has_discount:
        return f"{title} | {spec} - {money(quote.price)} 🔥"
    return f"{title} | {spec} - {money(quote.price)} تومان"


def quote_lines(plan, quote):
    """متن چندخطی نمایش قیمت برای صفحه‌ی تأیید خرید."""
    title, spec = plan_title(plan)
    days = _int(_plan_get(plan, 'duration_days'), 30) or 30
    lines = []
    if quote.label:
        lines.append(f"**{quote.label}**\n")
    lines.append(f"🛍 {title}")
    lines.append(f"💾 حجم: {_plan_get(plan, 'gb', 0)}GB")
    lines.append(f"📅 مدت: {days} روز")
    if quote.has_discount:
        # حالت Markdown تلگرام خط‌خورده ندارد، پس قیمت اصلی ساده نوشته می‌شود
        lines.append(f"💰 قیمت اصلی: {money(quote.base_price)} تومان")
        lines.append(f"✅ قیمت ویژه: **{money(quote.price)}** تومان")
        lines.append(f"🎁 سود شما: {money(quote.discount)} تومان ({quote.discount_percent}٪)")
    else:
        lines.append(f"💰 قیمت: **{money(quote.price)}** تومان")
    return "\n".join(lines), spec


# ================= لایه‌ی متصل به دیتابیس =================
async def load_config():
    """تنظیمات قیمت‌گذاری را از جدول settings می‌خواند."""
    cfg = PricingConfig()
    try:
        values = {}
        async with db.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM settings WHERE key = ANY($1::text[])",
                list(db.PRICING_SETTINGS.keys()),
            )
        for r in rows:
            values[r['key']] = r['value']
    except Exception as ex:  # دیتابیس در دسترس نیست → مقادیر پیش‌فرض
        logging.warning("خواندن تنظیمات قیمت‌گذاری ناموفق بود: %s", ex)
        return cfg

    def flag(key, default):
        return (values.get(key) or ('on' if default else 'off')) == 'on'

    cfg.first_offer_enabled = flag('first_offer_enabled', True)
    cfg.renew_offer_enabled = flag('renew_offer_enabled', True)
    cfg.reseller_bonus_enabled = flag('reseller_bonus_enabled', True)
    cfg.reseller_bonus_min = _int(values.get('reseller_bonus_min'), cfg.reseller_bonus_min)
    cfg.reseller_bonus_percent = _int(values.get('reseller_bonus_percent'), cfg.reseller_bonus_percent)
    cfg.reseller_t2_min = _int(values.get('reseller_t2_min'), cfg.reseller_t2_min)
    cfg.reseller_t2_discount = _int(values.get('reseller_t2_discount'), cfg.reseller_t2_discount)
    cfg.reseller_t3_min = _int(values.get('reseller_t3_min'), cfg.reseller_t3_min)
    cfg.reseller_t3_discount = _int(values.get('reseller_t3_discount'), cfg.reseller_t3_discount)
    return cfg


async def build_buyer(user_id, admin=False):
    """تصویر کاربر برای قیمت‌گذاری (نقش، سابقه‌ی خرید، شارژ ماهانه، مصرف آفرها)."""
    profile = await db.get_pricing_profile(user_id)
    buyer = Buyer(
        user_id=user_id,
        role=normalize_role(profile.get('role')),
        admin=admin,
        order_count=_int(profile.get('order_count')),
        first_offer_used=bool(profile.get('first_offer_used')),
        monthly_topup=_int(profile.get('monthly_topup')),
    )
    buyer.reseller_bonus_used = bool(profile.get('reseller_bonus_used'))
    buyer.offer_uses = await db.offer_uses_by_user(user_id)
    return buyer


async def custom_price_for(user_id, plan_id, bulk=False):
    """قیمت اختصاصیِ ثبت‌شده برای این کاربر و پلن (اگر وجود داشته باشد)."""
    async with db.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT price, bulk_price FROM custom_prices WHERE user_id=$1 AND plan_id=$2",
            user_id, _int(plan_id),
        )
    if not row:
        return None
    value = row['bulk_price'] if bulk else row['price']
    return None if value is None else _int(value)


async def quote_for_user(user_id, plan, kind=KIND_BUY, buyer=None, config=None, offers=None, admin=False):
    """قیمت نهایی یک پلن برای یک کاربر (نسخه‌ی متصل به دیتابیس)."""
    buyer = buyer or await build_buyer(user_id, admin=admin)
    config = config or await load_config()
    if offers is None:
        offers = [dict(o) for o in await db.list_offers(active_only=True)]
    custom = await custom_price_for(user_id, _plan_get(plan, 'id'), bulk=(kind == KIND_BULK))
    return resolve_quote(plan, buyer, kind=kind, config=config, custom_price=custom, offers=offers)


async def quote_plans(user_id, plans, kind=KIND_BUY, admin=False):
    """قیمت‌گذاری یک لیست پلن با حداقل رفت‌وبرگشت به دیتابیس.

    خروجی: لیستی از (plan, quote) به همان ترتیب ورودی.
    """
    buyer = await build_buyer(user_id, admin=admin)
    config = await load_config()
    offers = [dict(o) for o in await db.list_offers(active_only=True)]
    result = []
    for plan in plans:
        quote = await quote_for_user(
            user_id, plan, kind=kind, buyer=buyer, config=config, offers=offers, admin=admin,
        )
        result.append((plan, quote))
    return result


async def commit_quote(user_id, quote, kind, plan_id, order_id=None, role=NORMAL):
    """ثبت اثرات یک خریدِ انجام‌شده: مصرف آفرها و درج در دفتر فروش.

    فقط بعد از موفق‌بودن خرید صدا زده می‌شود تا آفری بی‌دلیل سوخته نشود.
    """
    if quote.first_offer:
        await db.mark_first_offer_used(user_id)
    if quote.offer_id:
        await db.redeem_offer(quote.offer_id, user_id)
    await db.record_sale(
        user_id=user_id, kind=kind, user_role=role,
        base_price=quote.base_price, price=quote.price, discount=quote.discount,
        plan_id=plan_id, order_id=order_id, offer_id=quote.offer_id, first_offer=quote.first_offer,
    )


async def reseller_bonus_on_topup(user_id, amount, config=None):
    """هدیه‌ی اولین شارژ نماینده. خروجی: (مبلغ هدیه، درصد) یا (۰، ۰).

    برداشتن پرچم به‌صورت اتمیک انجام می‌شود تا هدیه دوبار داده نشود.
    """
    config = config or await load_config()
    if not config.reseller_bonus_enabled:
        return 0, 0
    profile = await db.get_pricing_profile(user_id)
    if normalize_role(profile.get('role')) != RESELLER:
        return 0, 0
    if bool(profile.get('reseller_bonus_used')):
        return 0, 0
    if _int(amount) < _int(config.reseller_bonus_min):
        return 0, 0
    if not await db.claim_reseller_bonus(user_id):
        return 0, 0
    bonus = _int(amount) * _int(config.reseller_bonus_percent) // 100
    return bonus, _int(config.reseller_bonus_percent)
