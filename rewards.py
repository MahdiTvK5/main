"""موتور دعوت و کد هدیه: توابع خالصِ قابل‌تست، جدا از دیتابیس.

منطق پاداش دعوت و اعتبارسنجی کد هدیه اینجاست تا ادمین بتواند همه‌ی اعداد را از
جدول `settings` عوض کند و تست‌ها بدون PostgreSQL اجرا شوند. ذخیره‌سازی و کسر/شارژ
موجودی همچنان در `db.py` است.
"""
import datetime
import re
import secrets
import string
from dataclasses import dataclass

# مخاطب کد هدیه (زیرمجموعه‌ی مخاطب آفرها)
GIFT_AUDIENCES = {
    'all': 'همه',
    'new': 'کاربران جدید (بدون سفارش)',
    'existing': 'مشتریان فعلی',
}

# الفبای کد: بدون 0/O و 1/I تا موقع خواندن قاطی نشود
_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
_PERSIAN_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def _int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _on(value):
    return str(value or '').strip().lower() in ('on', '1', 'true', 'yes')


# ================= کد هدیه =================
def normalize_gift_code(raw):
    """کد را به حروف بزرگ انگلیسی/عدد تبدیل می‌کند؛ فاصله و خط تیره حذف می‌شود."""
    if raw is None:
        return ''
    text = str(raw).translate(_PERSIAN_DIGITS).strip().upper()
    return re.sub(r'[^A-Z0-9]', '', text)


def is_valid_gift_code(code):
    return bool(re.fullmatch(r'[A-Z0-9]{3,32}', code or ''))


def generate_gift_code(length=8):
    length = max(4, min(int(length or 8), 16))
    return ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def gift_audience_ok(audience, order_count):
    """آیا این کاربر مخاطبِ کد هست؟ order_count تعداد سفارش‌های قبلی است."""
    audience = (audience or 'all').strip().lower()
    orders = _int(order_count)
    if audience == 'new':
        return orders == 0
    if audience == 'existing':
        return orders > 0
    return True


def gift_reject_reason(row, *, now=None, order_count=0, already_used=False):
    """اگر کد قابل استفاده نباشد پیام خطا برمی‌گرداند، وگرنه None.

    `row` دیکشنری/ردیف با کلیدهای amount, max_uses, used_count, is_active, expires_at, audience.
    """
    if not row:
        return "❌ کد نامعتبر است."
    if already_used:
        return "❌ شما قبلاً این کد را استفاده کرده‌اید."
    if row.get('is_active') is False:
        return "❌ این کد غیرفعال است."
    expires = row.get('expires_at')
    if expires is not None:
        now = now or datetime.datetime.now()
        if hasattr(expires, 'tzinfo') and expires.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=expires.tzinfo)
        if now > expires:
            return "❌ مهلت این کد تمام شده است."
    used = _int(row.get('used_count'))
    max_uses = _int(row.get('max_uses'), 1)
    if max_uses > 0 and used >= max_uses:
        return "❌ ظرفیت این کد تکمیل شده است."
    if _int(row.get('amount')) <= 0:
        return "❌ این کد نامعتبر است."
    if not gift_audience_ok(row.get('audience'), order_count):
        if (row.get('audience') or '').strip().lower() == 'new':
            return "❌ این کد فقط برای کاربران جدید است."
        return "❌ این کد مخصوص مشتریان فعلی است."
    return None


# ================= دعوت =================
@dataclass
class ReferralConfig:
    """تنظیمات قابل‌مدیریت سیستم دعوت."""
    enabled: bool = True
    referrer_bonus: int = 0
    invitee_bonus: int = 0
    min_topup: int = 0

    @property
    def has_payout(self):
        return self.referrer_bonus > 0 or self.invitee_bonus > 0


@dataclass
class ReferralReward:
    referrer_id: int
    referrer_bonus: int
    invitee_bonus: int = 0


def parse_referral_config(settings):
    """settings دیکشنری key→value از جدول settings است."""
    settings = settings or {}
    enabled_raw = settings.get('ref_enabled')
    # اگر کلید هنوز ست نشده، روشن فرض می‌شود تا نصب‌های قدیمی با ref_bonus کار کنند
    enabled = True if enabled_raw is None or str(enabled_raw).strip() == '' else _on(enabled_raw)
    return ReferralConfig(
        enabled=enabled,
        referrer_bonus=max(0, _int(settings.get('ref_bonus'))),
        invitee_bonus=max(0, _int(settings.get('ref_invitee_bonus'))),
        min_topup=max(0, _int(settings.get('ref_min_topup'))),
    )


def referral_should_pay(config, *, referred_by, already_rewarded, charge_amount=0):
    """آیا این شارژ باید پاداش دعوت را آزاد کند؟

    اگر سیستم خاموش باشد یا مبلغ به حداقل نرسیده باشد False است و پرچم «پرداخت‌شده»
    نباید بالا برود تا شارژ بعدی / روشن‌کردن سیستم شانس داشته باشد.
    """
    if already_rewarded or not referred_by:
        return False
    if not config or not config.enabled or not config.has_payout:
        return False
    if _int(charge_amount) < config.min_topup:
        return False
    return True
