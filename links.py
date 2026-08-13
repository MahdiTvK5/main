"""توابع خالصِ کار با لینک اشتراک و شناسه‌ی کلاینت.

این ماژول عمداً هیچ وابستگی‌ای به دیتابیس، شبکه یا تلگرام ندارد تا هم قابل تست باشد
و هم بتوان از همه‌جا (ربات، پنل وب، لایه‌ی دیتابیس) بدون خطر import چرخه‌ای صدایش زد.
"""
import base64
import json
from urllib.parse import unquote


def email_from_link(link):
    """نام سرویس (email) را از لینک اشتراک بیرون می‌کشد.

    در vless/trojan نام سرویس در فرگمنتِ بعد از # است، اما در vmess کل تنظیمات
    (از جمله نام، در فیلد ps) داخل Base64 قرار دارد و هیچ فرگمنتی وجود ندارد.
    اگر نام قابل استخراج نباشد رشته‌ی خالی برمی‌گردد تا صدازننده تصمیم بگیرد.
    """
    if not link:
        return ""
    link = str(link).strip()
    if link.startswith("vmess://"):
        payload = link[len("vmess://"):].split("#")[0].strip()
        try:
            payload += "=" * (-len(payload) % 4)
            conf = json.loads(base64.b64decode(payload).decode("utf-8", "ignore"))
            return str(conf.get("ps") or "").strip()
        except Exception:
            return ""
    if "#" in link:
        return unquote(link.split("#")[-1]).strip()
    return ""


def _field(row, key):
    """خواندن امنِ یک فیلد از Record دیتابیس یا dict (بدون خطا اگر وجود نداشت)."""
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return None


def order_email(row):
    """نام سرویسِ یک سفارش.

    اولویت با ستون email در دیتابیس است؛ برای رکوردهای قدیمی که این ستون را ندارند
    یا خالی است، از خود لینک استخراج می‌شود.
    """
    email = _field(row, "email")
    if email and str(email).strip():
        return str(email).strip()
    return email_from_link(_field(row, "config_link"))


def client_key(client):
    """شناسه‌ای که X-UI برای updateClient لازم دارد.

    vless و vmess با uuid (فیلد id) شناخته می‌شوند و trojan با password.
    """
    if not client:
        return ""
    return client.get("id") or client.get("password") or ""


def new_client_key(client):
    """کلید جدید برای «تغییر UUID»، متناسب با پروتکلِ همان کلاینت.

    خروجی: (کلید جدید، نام فیلدی که باید به‌روزرسانی شود).
    """
    import uuid as _uuid
    if client and client.get("id"):
        return str(_uuid.uuid4()), "id"
    if client and client.get("password"):
        return _uuid.uuid4().hex, "password"
    return str(_uuid.uuid4()), "id"
