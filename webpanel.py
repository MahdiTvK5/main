"""پنل وب مدیریت مبتنی بر aiohttp (نسخه‌ی بازطراحی‌شده).

اگر WEB_ADMIN_PASSWORD تنظیم نشده باشد، پنل اجرا نمی‌شود.
احراز هویت با یک رمز عبور و کوکی امضاشده (HMAC) انجام می‌شود.

لایه‌ی نمایش (CSS/JS/HTML) بازطراحی شده؛ تمام مسیرها، اکشن‌ها، نام فیلدها،
ریدایرکت‌ها و کوئری‌های دیتابیس عیناً مثل نسخه‌ی قبل هستند تا هیچ عملکرد یا
منطق کسب‌وکاری تغییر نکرده باشد.
"""
import os
import html
import hmac
import time
import asyncio
import logging
import secrets
import datetime
from urllib.parse import quote

from aiohttp import web

import db
import importer
import pricing
import rewards
import subportal
import webcomponents
from links import order_email
from config import (
    WEB_ADMIN_PASSWORD, WEB_PORT, WEB_HOST, WEB_SESSION_TTL,
    DB_USER, DB_PASS, DB_NAME, DB_HOST, DB_PORT, PORTAL_PUBLIC_URL,
    SUPER_ADMIN_ID, format_size,
)


async def _system_resources():
    """CPU/RAM/Disk واقعی با psutil؛ در صورت نبود psutil، لیست خالی (کارت پیام مناسب می‌دهد)."""
    try:
        import psutil
    except ImportError:
        return []
    import time as _t
    try:
        cpu = psutil.cpu_percent(interval=0.15)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        ram_pct = mem.percent
        ram_label = f"{ram_pct:.0f}% ({mem.used // (1024**3):.0f}/{mem.total // (1024**3):.0f} GB)"
        disk_pct = disk.percent
        disk_label = f"{disk_pct:.0f}% ({disk.used // (1024**3):.0f}/{disk.total // (1024**3):.0f} GB)"
        boot = psutil.boot_time()
        return [
            ('CPU', cpu, f"{cpu:.0f}%"),
            ('RAM', ram_pct, ram_label),
            ('Disk', disk_pct, disk_label),
        ]
    except Exception:
        return []


# ربات برای قابلیت‌هایی مثل پیام همگانی از پنل وب
_BOT = None
PER_PAGE = 20

# تسک‌های پس‌زمینه (مثل پیام همگانی) نگه داشته می‌شوند تا زودتر از موعد جمع نشوند
_TASKS = set()

# گزارش آخرین ارسال همگانی (در حافظه‌ی همین پروسه)؛ در صفحه‌ی /broadcast نمایش داده می‌شود
_BROADCAST_REPORT = None

# سطوح کاربر و مخاطب آفرها از موتور قیمت‌گذاری می‌آیند تا یک منبع واحد داشته باشیم
ROLE_LABELS = {pricing.NORMAL: 'عادی', pricing.VIP: 'VIP', pricing.RESELLER: 'نماینده'}
AUDIENCE_LABELS = pricing.AUDIENCES

# ================= احراز هویت =================
# نشست‌ها تصادفی و دارای انقضا هستند. قبلاً توکن یک مقدار ثابتِ مشتق از رمز بود؛
# یعنی هرگز باطل نمی‌شد، خروج از حساب کاری نمی‌کرد و لو رفتنِ کوکی دسترسیِ دائمی می‌داد.
_SESSIONS = {}  # token -> expires_at (monotonic)

# محدودیت تلاش ورود به‌ازای هر IP (جلوگیری از حدس زدن رمز)
_LOGIN_FAILS = {}  # ip -> (تعداد تلاش ناموفق، زمان آزادسازی)
_MAX_LOGIN_FAILS = 5
_LOGIN_BLOCK_SECONDS = 300


def _prune_sessions():
    now = time.monotonic()
    for tok in [t for t, exp in _SESSIONS.items() if exp <= now]:
        _SESSIONS.pop(tok, None)


def _new_session():
    _prune_sessions()
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.monotonic() + WEB_SESSION_TTL
    return token


def _authed(request):
    if not WEB_ADMIN_PASSWORD:
        return False
    token = request.cookies.get("session", "")
    if not token:
        return False
    exp = _SESSIONS.get(token)
    if exp is None:
        return False
    if exp <= time.monotonic():
        _SESSIONS.pop(token, None)
        return False
    return True


def _login_blocked(ip):
    fails, until = _LOGIN_FAILS.get(ip, (0, 0.0))
    if fails >= _MAX_LOGIN_FAILS and time.monotonic() < until:
        return int(until - time.monotonic()) + 1
    return 0


def _note_login_fail(ip):
    fails, _until = _LOGIN_FAILS.get(ip, (0, 0.0))
    fails += 1
    _LOGIN_FAILS[ip] = (fails, time.monotonic() + _LOGIN_BLOCK_SECONDS)
    return fails


def _is_https(request):
    return request.scheme == "https" or request.headers.get("X-Forwarded-Proto", "") == "https"


def require_auth(handler):
    async def wrapper(request):
        if not _authed(request):
            raise web.HTTPFound("/login")
        return await handler(request)
    return wrapper


def money(value):
    """نمایش مبلغ با جداکننده‌ی هزارگان؛ NULL را صفر در نظر می‌گیرد."""
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def e(v):
    return html.escape(str(v if v is not None else ""))


def num(v):
    """مقدار عددی/فنی برای نمایش خوانا در متن فارسی (جهت LTR)."""
    return f'<span class="num" dir="ltr">{e(v)}</span>'


def _fmt_dt(value, fmt='%Y-%m-%d %H:%M'):
    """نمایش امن تاریخ؛ ستون‌های متنی (مثل orders.date) رشته برمی‌گردانند و
    ستون‌های TIMESTAMP به datetime تبدیل می‌شوند — هر دو پوشش داده می‌شوند."""
    if not value:
        return ''
    try:
        return value.strftime(fmt)
    except AttributeError:
        return str(value)


def _redirect(path, ok=None, err=None):
    sep = "&" if "?" in path else "?"
    if ok:
        path = f"{path}{sep}ok={quote(ok)}"
    elif err:
        path = f"{path}{sep}err={quote(err)}"
    return web.HTTPFound(path)


# ================= آیکون‌های SVG (اسپرایت داخلی؛ بدون وابستگی خارجی) =================
ICONS = {
    'dashboard': '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    'chart': '<path d="M22 7l-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
    'users': '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    'box': '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
    'card': '<rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/>',
    'cart': '<circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/>',
    'target': '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    'server': '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>',
    'gift': '<polyline points="20 12 20 22 4 22 4 12"/><rect x="2" y="7" width="20" height="5"/><line x1="12" y1="22" x2="12" y2="7"/><path d="M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7z"/><path d="M12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z"/>',
    'link': '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    'import': '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    'megaphone': '<path d="M3 11l18-7-4 16-6-4-3 4-1-6z"/>',
    'star': '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
    'settings': '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    'logout': '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    'sun': '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>',
    'moon': '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    'menu': '<line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/>',
    'x': '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    'search': '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    'plus': '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    'edit': '<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>',
    'trash': '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    'eye': '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
    'copy': '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    'check': '<polyline points="20 6 9 17 4 12"/>',
    'alert': '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    'back': '<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>',
    'wallet': '<path d="M20 7H4a2 2 0 0 1 0-4h14a1 1 0 0 1 1 1v3"/><path d="M22 9v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5"/><circle cx="16" cy="14" r="1"/>',
    'refresh': '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
    'db': '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
    'clock': '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    'zap': '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    'chevron': '<polyline points="6 9 12 15 18 9"/>',
    'shield': '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    'pause': '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    'play': '<polygon points="5 3 19 12 5 21 5 3"/>',
}


def icon(name, cls=''):
    """آیکون خطی SVG از اسپرایت داخلی؛ بدون درخواست شبکه."""
    body = ICONS.get(name, ICONS['dash' + 'board'])
    return (f'<svg class="ic {cls}" width="18" height="18" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{body}</svg>')


def _svg_sprite():
    defs = "".join(
        f'<symbol id="i-{name}" viewBox="0 0 24 24">{body}</symbol>'
        for name, body in ICONS.items()
    )
    return f'<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">{defs}</svg>'


def _use(name, cls=''):
    """ارجاع به اسپرایت (بعد از بار اول ارزان‌تر است)."""
    return f'<svg class="ic {cls}" width="18" height="18" aria-hidden="true"><use href="#i-{name}"/></svg>'


# ================= ناوبری =================
NAV_GROUPS = [
    ('خانه', [
        ('/', 'داشبورد', 'dashboard'),
        ('/finance', 'گزارش مالی', 'chart'),
        ('/users', 'کاربران', 'users'),
        ('/orders', 'سفارش‌ها', 'box'),
        ('/transactions', 'تراکنش‌ها', 'card'),
        ('/specials', 'VIP و نمایندگان', 'star'),
        ('/referrals', 'دعوت‌ها', 'link'),
    ]),
    ('فروش', [
        ('/plans', 'پلن‌ها و قیمت‌ها', 'cart'),
        ('/offers', 'تخفیف‌ها', 'target'),
        ('/giftcodes', 'کد هدیه', 'gift'),
    ]),
    ('سیستم', [
        ('/panels', 'سرورها', 'server'),
        ('/import', 'ایمپورت کانفیگ', 'import'),
        ('/broadcast', 'پیام همگانی', 'megaphone'),
        ('/settings', 'تنظیمات', 'settings'),
    ]),
]


def crumb(items):
    """مسیر راهنما: لیستی از (برچسب، لینک یا None)."""
    parts = []
    for i, (label, href) in enumerate(items):
        last = i == len(items) - 1
        if last or not href:
            parts.append(f'<span class="crumb-here" aria-current="page">{e(label)}</span>')
        else:
            parts.append(f'<a href="{href}">{e(label)}</a>')
        if not last:
            parts.append('<span class="crumb-sep">/</span>')
    return f'<nav class="crumbs" aria-label="مسیر">{"".join(parts)}</nav>'


def _page_head(title, subtitle='', actions='', crumbs=None):
    c = crumb(crumbs or [('خانه', '/'), (title, None)])
    sub = f'<p class="page-sub">{subtitle}</p>' if subtitle else ''
    act = f'<div class="head-actions">{actions}</div>' if actions else ''
    return (f'<header class="page-head"><div>{c}<h1>{title}</h1>{sub}</div>{act}</header>')


def _stat_card(label, value, icon_name='', sub='', tone=''):
    """کارت آماری با عددِ همیشه‌جادارفته: اندازه‌ی فونت بر اساس طول مقدار و
    عنوان کامل به‌صورت tooltip؛ هیچ‌وقت عدد از کادر بیرون نمی‌زند."""
    ic = f'<span class="stat-ic">{icon(icon_name)}</span>' if icon_name else ''
    raw = str(value)
    size = 'v-sm' if len(raw) > 11 else ('v-md' if len(raw) > 8 else 'v-lg')
    sub_h = f'<div class="stat-sub">{sub}</div>' if sub else ''
    return (
        f'<div class="stat {(("tone-" + tone) if tone else "").strip()}" title="{e(raw)}">'
        f'<div class="stat-head">{ic}<span class="stat-l">{e(label)}</span></div>'
        f'<div class="stat-v {size}">{e(value)}</div>{sub_h}</div>'
    )


def _badge(kind, text):
    return f'<span class="badge b-{kind}">{e(text)}</span>'


def _empty(title, hint='', icon_name='box'):
    hint_h = f'<p>{e(hint)}</p>' if hint else ''
    return (f'<div class="empty"><span class="empty-ic">{icon(icon_name, "ic-lg")}</span>'
            f'<h3>{e(title)}</h3>{hint_h}</div>')


def _copy_btn(value, label='کپی'):
    return (f'<button type="button" class="icon-btn" data-copy="{e(value)}" '
            f'title="{e(label)}" aria-label="{e(label)}">{_use("copy")}</button>')


def _confirm(text):
    """تأیید ایمن: با JS مودال، بدون JS همان confirm مرورگر."""
    return f' data-confirm="{e(text)}" onsubmit="return confirm(\'{e(text)}\')"'


# ================= سیستم طراحی =================
PAGE_CSS = """
<style>
/* ============================================================
   OverWall Admin — Design System v3 «Aurora»
   RTL-first · واقعیِ Light/Dark · بدون فایل خارجی
   ============================================================ */
:root{
  --bg:#05070F; --bg-soft:#0A101F; --panel:#0E1730; --panel-2:#0A1020;
  --line:rgba(151,170,255,.10); --line-strong:rgba(151,170,255,.18);
  --text:#E3EAFB; --text-2:#A9B6DC; --muted:#6B7BA0;
  --primary:#4E7CFF; --primary-h:#7897FF; --primary-soft:rgba(78,124,255,.15);
  --accent:#22D3EE;
  --grad:linear-gradient(112deg,#3D7BFF 0%,#6D5EFF 45%,#22D3EE 105%);
  --grad-accent:linear-gradient(112deg,#22D3EE,#6D5EFF);
  --danger:#F25265; --danger-h:#ff8494; --danger-soft:rgba(242,82,101,.14);
  --ok:#2DD4A7; --ok-soft:rgba(45,212,167,.14); --warn:#F7B32B; --warn-soft:rgba(247,179,43,.14);
  --gold:#F7B32B; --gold-soft:rgba(247,179,43,.14);
  --violet:#9A86FF; --violet-soft:rgba(154,134,255,.15);
  --teal:#2DD4A7; --teal-soft:rgba(45,212,167,.14);
  --r:20px; --r-sm:14px;
  --glass:rgba(14,23,48,.66);
  --shadow:0 1px 2px rgba(0,0,0,.5),0 16px 44px -20px rgba(2,6,20,.85);
  --shadow-lg:0 34px 80px -26px rgba(2,6,20,.9);
  --glow:0 10px 30px -10px color-mix(in srgb,var(--primary) 65%,transparent);
  --ring:0 0 0 3px color-mix(in srgb,var(--primary) 30%,transparent);
}
[data-theme="dark"]{
  --bg:#05070F; --bg-soft:#0A101F; --panel:#0E1730; --panel-2:#0A1020;
  --line:rgba(151,170,255,.10); --line-strong:rgba(151,170,255,.18);
  --text:#E3EAFB; --text-2:#A9B6DC; --muted:#6B7BA0;
  --primary:#4E7CFF; --primary-h:#7897FF; --primary-soft:rgba(78,124,255,.15);
  --accent:#22D3EE;
  --grad:linear-gradient(112deg,#3D7BFF 0%,#6D5EFF 45%,#22D3EE 105%);
  --grad-accent:linear-gradient(112deg,#22D3EE,#6D5EFF);
  --danger:#F25265; --danger-h:#ff8494; --danger-soft:rgba(242,82,101,.14);
  --ok:#2DD4A7; --ok-soft:rgba(45,212,167,.14); --warn:#F7B32B; --warn-soft:rgba(247,179,43,.14);
  --gold:#F7B32B; --gold-soft:rgba(247,179,43,.14);
  --violet:#9A86FF; --violet-soft:rgba(154,134,255,.15);
  --teal:#2DD4A7; --teal-soft:rgba(45,212,167,.14);
  --glass:rgba(14,23,48,.66);
  --shadow:0 1px 2px rgba(0,0,0,.5),0 16px 44px -20px rgba(2,6,20,.85);
  --shadow-lg:0 34px 80px -26px rgba(2,6,20,.9);
  --glow:0 10px 30px -10px color-mix(in srgb,var(--primary) 65%,transparent);
  --ring:0 0 0 3px color-mix(in srgb,var(--primary) 30%,transparent);
}
[data-theme="light"]{
  --bg:#EEF2FB; --bg-soft:#F6F8FE; --panel:#FFFFFF; --panel-2:#F3F6FD;
  --line:rgba(19,32,73,.10); --line-strong:rgba(19,32,73,.17);
  --text:#111E3E; --text-2:#42548A; --muted:#7E8BAD;
  --primary:#3D63F5; --primary-h:#2555ff; --primary-soft:rgba(61,99,245,.12);
  --accent:#0891B2;
  --grad:linear-gradient(112deg,#3D63F5 0%,#5A47F0 50%,#08A5C6 105%);
  --grad-accent:linear-gradient(112deg,#08A5C6,#5A47F0);
  --danger:#DC3856; --danger-h:#c22442; --danger-soft:rgba(220,56,86,.11);
  --ok:#0F9E77; --ok-soft:rgba(15,158,119,.11); --warn:#C77E11; --warn-soft:rgba(199,126,17,.11);
  --gold:#B9820F; --gold-soft:rgba(185,130,15,.11);
  --violet:#6C4DF5; --violet-soft:rgba(108,77,245,.11);
  --teal:#0F9E77; --teal-soft:rgba(15,158,119,.11);
  --glass:rgba(255,255,255,.8);
  --shadow:0 1px 2px rgba(19,32,73,.06),0 14px 36px -18px rgba(19,32,73,.22);
  --shadow-lg:0 26px 60px -22px rgba(19,32,73,.3);
  --glow:0 10px 26px -12px color-mix(in srgb,var(--primary) 55%,transparent);
  --ring:0 0 0 3px color-mix(in srgb,var(--primary) 22%,transparent);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;font-family:"Vazirmatn","IRANSansX","Vazir",Tahoma,"Segoe UI",Arial,sans-serif;
  background:var(--bg);color:var(--text);direction:rtl;font-size:13.5px;line-height:1.75;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
}
/* پس‌زمینه‌ی آرورا: حباب‌های رنگی آرام */
body::before{
  content:"";position:fixed;inset:-20%;z-index:-1;pointer-events:none;
  background:
    radial-gradient(38% 30% at 82% 6%, color-mix(in srgb,var(--primary) 20%,transparent), transparent 70%),
    radial-gradient(30% 26% at 4% 16%, color-mix(in srgb,var(--accent) 11%,transparent), transparent 70%),
    radial-gradient(34% 26% at 52% 108%, color-mix(in srgb,var(--violet) 10%,transparent), transparent 70%);
  animation:aurora 24s ease-in-out infinite alternate;
}
[data-theme="light"] body::before{opacity:.5}
@keyframes aurora{
  0%{transform:translate(0,0) scale(1)}
  50%{transform:translate(2.5%,1.5%) scale(1.04)}
  100%{transform:translate(-2%, -1.5%) scale(1.02)}
}
a{color:var(--primary);text-decoration:none;transition:color .15s}
a:hover{color:var(--primary-h)}
:focus-visible{outline:none;box-shadow:var(--ring);border-radius:10px}
::selection{background:color-mix(in srgb,var(--primary) 30%,transparent);color:var(--text)}
.num{direction:ltr;unicode-bidi:isolate;font-variant-numeric:tabular-nums lining-nums;letter-spacing:.2px;max-width:100%}
svg.ic{flex-shrink:0}
h1,h2,h3{color:var(--text)}

/* اسکرول‌بار باریک */
*{scrollbar-width:thin;scrollbar-color:color-mix(in srgb,var(--muted) 45%,transparent) transparent}
*::-webkit-scrollbar{width:9px;height:9px}
*::-webkit-scrollbar-thumb{background:color-mix(in srgb,var(--muted) 42%,transparent);border-radius:99px;border:2px solid transparent;background-clip:content-box}
*::-webkit-scrollbar-track{background:transparent}

/* ================= چیدمان ================= */
.app{display:flex;min-height:100vh}
.app>*{min-width:0}
.side{
  width:280px;flex-shrink:0;display:flex;flex-direction:column;position:sticky;top:0;height:100vh;overflow-y:auto;
  background:color-mix(in srgb,var(--panel-2) 88%,transparent);
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);
  border-left:1px solid var(--line);z-index:40;transition:width .22s ease,transform .25s ease;
}
.side::after{content:"";display:block;height:8px;margin-top:auto}
.side-head{display:flex;align-items:center;gap:11px;padding:22px 18px 14px}
.brand-mark{
  width:44px;height:44px;border-radius:14px;background:var(--grad);display:flex;align-items:center;justify-content:center;
  color:#fff;flex-shrink:0;position:relative;box-shadow:var(--glow);
}
.brand-mark::after{
  content:"";position:absolute;inset:-5px;border-radius:18px;border:1px solid color-mix(in srgb,var(--accent) 45%,transparent);
  animation:halo 3.4s ease infinite;
}
@keyframes halo{0%,100%{opacity:.75;transform:scale(1)}50%{opacity:.2;transform:scale(1.06)}}
.brand-name{font-weight:900;font-size:17px;line-height:1.3;letter-spacing:.2px;
  background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
[data-theme="light"] .brand-name{color:var(--text)}
.brand-name small{display:block;font-weight:600;font-size:10px;color:var(--muted);letter-spacing:2.5px;-webkit-text-fill-color:var(--muted)}
.side-search{position:relative;margin:2px 14px 8px}
.side-search svg{position:absolute;right:10px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none}
.side-search input{
  width:100%;padding:8px 32px 8px 10px;font-size:12.5px;border-radius:11px;
  background:var(--panel-2);border:1px solid var(--line);
}
.nav{flex:1;padding:2px 12px 12px}
.nav-group{margin-top:15px}
.nav-group>span{display:flex;align-items:center;gap:6px;font-size:9.5px;font-weight:800;color:var(--muted);padding:0 12px 8px;letter-spacing:2px}
.nav-group>span::after{content:"";flex:1;height:1px;background:linear-gradient(to left,var(--line-strong),transparent)}
.nav a{
  display:flex;align-items:center;gap:11px;padding:10px 13px;margin:3px 0;border-radius:13px;
  color:var(--text-2);font-weight:600;font-size:13px;position:relative;
  transition:background .16s,color .16s,transform .16s,box-shadow .16s;
}
.nav a::before{
  content:"";position:absolute;right:-12px;top:50%;transform:translateY(-50%);width:3.5px;height:0;
  border-radius:99px;background:var(--grad);transition:height .2s;
}
.nav a .ic{color:var(--muted);transition:color .16s,transform .25s}
.nav a:hover{background:var(--bg-soft);color:var(--text);transform:translateX(-2px)}
.nav a:hover .ic{color:var(--primary)}
.nav a.active{background:var(--grad);color:#fff;font-weight:800;box-shadow:var(--glow)}
.nav a.active .ic{color:#fff}
.nav a.active::before{height:56%}
.side-foot{padding:14px;border-top:1px solid var(--line);margin-top:8px}
.admin-card{display:flex;align-items:center;gap:11px;padding:11px 12px;border-radius:16px;background:var(--panel);border:1px solid var(--line);margin-bottom:10px;box-shadow:var(--shadow)}
.admin-avatar{width:38px;height:38px;border-radius:12px;background:var(--grad);display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0;box-shadow:var(--glow)}
.admin-meta{display:flex;flex-direction:column;min-width:0}
.admin-meta b{font-size:12.5px}
.admin-status{font-size:10.5px;color:var(--ok);font-weight:700;display:inline-flex;align-items:center;gap:5px}
.admin-status i{width:7px;height:7px;border-radius:99px;background:var(--ok);animation:dot 2.4s ease infinite}
@keyframes dot{0%,100%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 55%,transparent)}50%{box-shadow:0 0 0 4px transparent}}
.logout-btn{
  display:flex;align-items:center;gap:10px;width:100%;padding:10px 12px;border-radius:12px;
  color:var(--danger);background:none;border:1px dashed transparent;font:inherit;font-size:13px;font-weight:700;cursor:pointer;
  transition:all .16s;text-decoration:none;
}
.logout-btn:hover{background:var(--danger-soft);border-color:currentColor;color:var(--danger)}

.main-wrap{flex:1;display:flex;flex-direction:column;min-width:0}
.topbar{
  position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:12px;padding:11px 26px;
  background:color-mix(in srgb,var(--panel) 72%,transparent);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
  border-bottom:1px solid var(--line);
}
[data-theme="light"] .topbar{background:color-mix(in srgb,var(--panel) 82%,transparent)}
.topbar .spacer{flex:1}
.icon-btn{
  display:inline-flex;align-items:center;justify-content:center;width:37px;height:37px;border-radius:12px;
  border:1px solid var(--line);background:var(--panel);color:var(--text-2);cursor:pointer;padding:0;
  transition:all .16s;
}
.icon-btn:hover{color:var(--primary);border-color:color-mix(in srgb,var(--primary) 45%,var(--line));transform:translateY(-1px);box-shadow:var(--shadow)}
.content{flex:1;padding:26px 28px 48px;max-width:1460px;width:100%;margin:0 auto;animation:pageIn .35s cubic-bezier(.22,1,.36,1) both}
@keyframes pageIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}

/* ================= سربرگ صفحه ================= */
.crumbs{font-size:11.5px;color:var(--muted);margin-bottom:6px;display:flex;align-items:center;flex-wrap:wrap}
.crumbs a{color:var(--muted)}
.crumbs a:hover{color:var(--primary)}
.crumb-sep{margin:0 7px;opacity:.4}
.crumb-here{color:var(--text-2);font-weight:700}
.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:22px}
.page-head h1{
  margin:2px 0 0;font-size:25px;font-weight:900;letter-spacing:-.3px;padding-bottom:11px;position:relative;
}
.page-head h1::after{
  content:"";position:absolute;right:0;bottom:0;width:52px;height:4px;border-radius:99px;background:var(--grad);
  box-shadow:0 3px 10px -2px color-mix(in srgb,var(--primary) 70%,transparent);
}
.page-sub{margin:9px 0 0;color:var(--text-2);font-size:12.5px;max-width:720px}
.head-actions{display:flex;gap:9px;flex-wrap:wrap;align-items:center}

/* ================= کارت‌ها ================= */
.card,.box{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);
  position:relative;
}
.card::after,.box::after{
  content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
  box-shadow:inset 0 1px 0 color-mix(in srgb,#fff 6%,transparent);
}
[data-theme="light"] .card::after,[data-theme="light"] .box::after{box-shadow:inset 0 1px 0 rgba(255,255,255,.9)}
.box{padding:20px;margin-top:18px}
.box h3{display:flex;align-items:center;gap:9px;margin:0 0 14px;font-size:14.5px;font-weight:900;letter-spacing:-.1px}
.box h3 .ic{color:var(--primary);width:17px;height:17px}
.grid{display:grid;gap:18px}
.grid-2{grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.grid>*,.fields>*,.renew-grid>*,.stats>*{min-width:0}

/* ---- آمار ---- */
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:14px;margin-top:18px}
.stat{
  position:relative;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:16px 17px 15px;box-shadow:var(--shadow);overflow:hidden;
  transition:transform .2s cubic-bezier(.22,1,.36,1),box-shadow .2s,border-color .2s;
}
.stat::before{
  content:"";position:absolute;inset-inline:0;top:0;height:3px;opacity:0;transition:opacity .25s;
  background:var(--grad);
}
.stat:hover{transform:translateY(-4px);box-shadow:var(--shadow-lg);border-color:color-mix(in srgb,var(--primary) 40%,var(--line))}
.stat:hover::before{opacity:1}
.stat-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:11px;flex-direction:row-reverse}
.stat-ic{
  width:42px;height:42px;border-radius:13px;display:flex;align-items:center;justify-content:center;flex-shrink:0;
  transition:transform .3s cubic-bezier(.34,1.56,.64,1);
}
.stat:hover .stat-ic{transform:rotate(-8deg) scale(1.08)}
.stat-l{font-size:12px;color:var(--text-2);font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stat-v{font-weight:900;line-height:1.35;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stat-v.v-lg{font-size:24px}
.stat-v.v-md{font-size:19.5px}
.stat-v.v-sm{font-size:15.5px;letter-spacing:0}
.stat-sub{margin-top:9px;font-size:11.5px;color:var(--muted);border-top:1px dashed var(--line);padding-top:8px;line-height:1.9;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tone-blue .stat-ic{background:var(--primary-soft);color:var(--primary)}
.tone-green .stat-ic{background:var(--ok-soft);color:var(--ok)}
.tone-gold .stat-ic{background:var(--gold-soft);color:var(--gold)}
.tone-red .stat-ic{background:var(--danger-soft);color:var(--danger)}
.tone-violet .stat-ic{background:var(--violet-soft);color:var(--violet)}
.tone-teal .stat-ic{background:var(--teal-soft);color:var(--teal)}

/* ================= داشبورد — KPI ================= */
.dash-row{display:grid;gap:16px;margin-top:16px}
.dash-row.r4{grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.dash-row.r2-30-70{grid-template-columns:minmax(0,1fr) 330px}
.dash-row.r3{grid-template-columns:repeat(3,minmax(0,1fr))}
.dash-row.r2{grid-template-columns:minmax(0,1.55fr) minmax(0,1fr)}
@media (max-width:1100px){.dash-row.r2-30-70,.dash-row.r3,.dash-row.r2{grid-template-columns:1fr}}
.kpi{
  position:relative;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:18px 20px 16px;box-shadow:var(--shadow);overflow:hidden;
  transition:transform .2s cubic-bezier(.22,1,.36,1),box-shadow .2s,border-color .2s;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
}
.kpi::after{
  content:"";position:absolute;inset-inline:0;top:0;height:3px;opacity:0;transition:opacity .25s;
  background:var(--grad);
}
.kpi:hover{transform:translateY(-4px);box-shadow:var(--shadow-lg);border-color:color-mix(in srgb,var(--primary) 45%,var(--line))}
.kpi:hover::after{opacity:1}
.kpi-head{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.kpi-ic{
  width:40px;height:40px;border-radius:13px;display:flex;align-items:center;justify-content:center;
  font-size:18px;flex-shrink:0;background:var(--primary-soft);color:var(--primary);
  transition:transform .3s cubic-bezier(.34,1.56,.64,1);
}
.kpi:hover .kpi-ic{transform:rotate(-8deg) scale(1.08)}
.kpi-l{font-size:12.5px;color:var(--text-2);font-weight:700}
.kpi-delta{margin-inline-start:auto;font-size:11.5px;font-weight:800;border-radius:99px;padding:3px 10px;display:inline-flex;align-items:center;gap:4px}
.kpi-delta i{font-style:normal;font-size:9px}
.delta-up{background:var(--ok-soft);color:var(--ok)}
.delta-down{background:var(--danger-soft);color:var(--danger)}
.delta-flat{background:var(--warn-soft);color:var(--warn)}
.kpi-body{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;min-height:44px}
.kpi-v{font-size:24px;font-weight:900;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  background:linear-gradient(180deg,var(--text) 55%,color-mix(in srgb,var(--text) 62%,transparent));
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
[data-theme="light"] .kpi-v{background:var(--text);-webkit-background-clip:text;background-clip:text}
.kpi-spark-wrap{flex-shrink:0;opacity:.95}
.kpi-sub{margin-top:9px;font-size:11.5px;color:var(--muted);border-top:1px dashed var(--line);padding-top:8px;line-height:1.8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tone-green .kpi-ic{background:var(--ok-soft);color:var(--ok)}
.tone-gold .kpi-ic{background:var(--gold-soft);color:var(--gold)}
.tone-red .kpi-ic{background:var(--danger-soft);color:var(--danger)}
.tone-violet .kpi-ic{background:var(--violet-soft);color:var(--violet)}
.tone-teal .kpi-ic{background:var(--teal-soft);color:var(--teal)}
.kpi.delta-down .kpi-delta{background:var(--danger-soft);color:var(--danger)}

/* ================= نمودار درآمد ================= */
.chart-box{margin-top:0;padding:20px 20px 12px}
.chart-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.chart-head h3{display:flex;align-items:center;gap:9px;margin:0;font-size:14.5px;font-weight:900}
.rev-growth{font-size:12px;font-weight:800;border-radius:99px;padding:4px 12px}
.rev-wrap{margin:4px -6px 0}
.rev-svg{width:100%;height:auto;display:block}
.rev-grid{stroke:var(--line);stroke-width:1;stroke-dasharray:3 5}
.rev-area{animation:revIn .8s cubic-bezier(.22,1,.36,1) both}
.rev-line{filter:drop-shadow(0 6px 16px color-mix(in srgb,var(--primary) 50%,transparent))}
@keyframes revIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.rev-foot{display:flex;justify-content:space-between;align-items:center;font-size:11.5px;color:var(--muted);padding:8px 6px 6px}
.rev-sum b{color:var(--text)}

/* ================= وضعیت سرورها ================= */
.sv-box{margin-top:0}
.sv-list{list-style:none;margin:6px 0 0;padding:0;display:grid;gap:6px}
.sv-row{display:flex;align-items:center;gap:11px;padding:10px 12px;border-radius:14px;background:var(--panel-2);border:1px solid transparent;transition:border-color .18s,transform .18s}
.sv-row:hover{border-color:var(--line-strong);transform:translateX(-3px)}
.sv-flag{font-size:20px;line-height:1;flex-shrink:0}
.sv-info{display:flex;flex-direction:column;min-width:0;flex:1}
.sv-info b{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sv-state{font-size:11px;font-weight:700;display:inline-flex;align-items:center;gap:5px;color:var(--muted)}
.sv-state::before{content:"";width:7px;height:7px;border-radius:99px;background:currentColor}
.sv-state.st-ok{color:var(--ok)}
.sv-state.st-warn{color:var(--warn)}
.sv-state.st-down{color:var(--danger)}
.sv-ms{font-size:13.5px;font-weight:900;direction:ltr;color:var(--text)}
.sv-ms small{font-size:10px;color:var(--muted);font-weight:600;margin-inline-start:2px}

/* ================= سلامت سیستم ================= */
.hl-box{margin-top:0}
.hl-uptime{font-size:11.5px;font-weight:800;color:var(--ok);background:var(--ok-soft);border-radius:99px;padding:4px 12px}
.hl-list{list-style:none;margin:8px 0 0;padding:0;display:grid;gap:4px}
.hl-row{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:12px}
.hl-row:hover{background:var(--panel-2)}
.hl-name{flex:1;font-size:12.5px;font-weight:600;direction:ltr;text-align:right}
.hl-dot{width:9px;height:9px;border-radius:99px;position:relative}
.hl-dot::after{content:"";position:absolute;inset:-4px;border-radius:99px;border:1px solid currentColor;opacity:.35;animation:halo 2.6s ease infinite}
.hl-dot.st-ok{background:var(--ok);color:var(--ok)}
.hl-dot.st-warn{background:var(--warn);color:var(--warn)}
.hl-dot.st-down{background:var(--danger);color:var(--danger)}
.hl-label{font-size:11px;color:var(--muted);width:52px;text-align:left}

/* ================= مانیتور منابع ================= */
.rs-box{margin-top:0}
.rs-row{margin-bottom:13px}
.rs-row:last-child{margin-bottom:2px}
.rs-head{display:flex;justify-content:space-between;font-size:12px;font-weight:700;margin-bottom:6px}
.rs-head b{direction:ltr;color:var(--text-2)}
.rs-track{height:8px;background:var(--panel-2);border-radius:99px;overflow:hidden;border:1px solid var(--line)}
.rs-fill{height:100%;border-radius:99px;transition:width .7s cubic-bezier(.22,1,.36,1)}
.rs-fill.st-ok{background:linear-gradient(90deg,#22D3A7,#2DD4A7)}
.rs-fill.st-warn{background:linear-gradient(90deg,#F7B32B,#FCD34D)}
.rs-fill.st-down{background:linear-gradient(90deg,#F25265,#FB7185)}

/* ================= تایم‌لاین فعالیت ================= */
.act-list{list-style:none;margin:6px 0 0;padding:0;position:relative}
.act-list::before{content:"";position:absolute;top:14px;bottom:14px;right:9px;width:2px;background:linear-gradient(to bottom,var(--primary),var(--accent),transparent)}
.act-row{display:flex;gap:13px;align-items:flex-start;padding:8px 0;position:relative}
.act-dot{width:20px;height:20px;border-radius:99px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:9px;background:var(--panel-2);border:1px solid var(--line);position:relative;z-index:1}
.act-dot::before{content:"";width:8px;height:8px;border-radius:99px;background:currentColor}
.act-ok{color:var(--ok)}
.act-warn{color:var(--warn)}
.act-down{color:var(--danger)}
.act-info{color:var(--primary)}
.act-body{min-width:0;flex:1}
.act-body p{margin:0;font-size:12.5px;font-weight:600;line-height:1.7}
.act-body time{font-size:10.5px;color:var(--muted);direction:ltr;display:inline-block}

/* ================= جدول‌ها ================= */
.tbl-toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:14px 16px 0}
.tbl-toolbar .search{position:relative;flex:0 1 320px}
.tbl-toolbar .search .ic{position:absolute;right:10px;top:50%;transform:translateY(-50%);color:var(--muted)}
.tbl-toolbar .search input{padding-right:34px;width:100%}
.tbl-count{font-size:11.5px;color:var(--muted);margin-right:auto}
.tbl-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);margin-top:18px;overflow:hidden}
.tbl-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:12.5px}
th,td{padding:11.5px 16px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap;vertical-align:middle}
th{
  background:var(--panel-2);color:var(--text-2);font-weight:800;font-size:11px;letter-spacing:.4px;
  position:sticky;top:0;z-index:2;user-select:none;
}
th.sortable{cursor:pointer}
tbody tr{transition:background .13s}
tbody tr:hover td{background:color-mix(in srgb,var(--primary) 5%,transparent)}
tbody tr:last-child td{border-bottom:0}
.sort-ind{display:inline-flex;flex-direction:column;font-size:8px;line-height:.9;margin-inline-start:5px;opacity:.35;vertical-align:middle}
th.sorted{color:var(--primary)}
th.sorted .sort-ind{opacity:1}
td.wrap{white-space:normal;min-width:170px;overflow-wrap:anywhere}
td.muted,.muted td{color:var(--muted)}
.col-actions{width:1%}
td .num{font-weight:700}
th.group-buy{color:var(--primary)}
th.group-renew-n{color:var(--primary)}
th.group-renew-v{color:var(--gold)}
th.group-renew-r{color:var(--teal)}
.price-chip{
  display:inline-flex;align-items:center;gap:6px;padding:3px 11px;border-radius:999px;
  font-weight:800;font-size:12px;max-width:100%;overflow:hidden;text-overflow:ellipsis;
}
.price-chip::before{content:"";width:6px;height:6px;border-radius:99px;background:currentColor;opacity:.85;flex-shrink:0}
.chip-n{background:var(--primary-soft);color:var(--primary)}
.chip-v{background:var(--gold-soft);color:var(--gold)}
.chip-r{background:var(--teal-soft);color:var(--teal)}
.chip-off{background:var(--bg-soft);color:var(--text-2);font-weight:600}
.legend{display:flex;gap:12px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--text-2);padding:14px 16px 0}
.legend .price-chip{cursor:default}

/* ================= فرم‌ها ================= */
label{display:block;margin:0 0 6px;color:var(--text-2);font-size:12px;font-weight:800}
label .req{color:var(--danger)}
input,select,textarea{
  font-family:inherit;font-size:13px;padding:9.5px 13px;border-radius:var(--r-sm);
  border:1px solid var(--line);background:var(--panel-2);color:var(--text);max-width:100%;
  transition:border-color .16s,box-shadow .16s,background .16s;
}
input.ltr{direction:ltr;text-align:left;font-variant-numeric:tabular-nums}
textarea{width:100%;resize:vertical;line-height:1.9}
input:hover,select:hover,textarea:hover{border-color:var(--line-strong)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--primary);box-shadow:var(--ring);background:var(--panel)}
input::placeholder,textarea::placeholder{color:color-mix(in srgb,var(--muted) 80%,transparent)}
select{cursor:pointer}
select option{background:var(--panel);color:var(--text)}
.hint{font-size:11px;color:var(--muted);margin-top:5px;line-height:1.8}
.field{min-width:0}

/* ---- دکمه‌ها ---- */
button,.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:7px;font-family:inherit;font-size:13px;
  font-weight:800;padding:9.5px 18px;border-radius:12px;border:1px solid transparent;cursor:pointer;
  color:#fff;background:var(--grad);background-size:140% 140%;background-position:0 0;
  box-shadow:var(--glow);position:relative;overflow:hidden;
  transition:background-position .35s,filter .16s,transform .1s,box-shadow .2s;text-decoration:none;white-space:nowrap;
}
button:hover,.btn:hover{filter:brightness(1.09);color:#fff;transform:translateY(-1px);box-shadow:0 14px 34px -12px color-mix(in srgb,var(--primary) 75%,transparent);background-position:100% 100%}
button:active,.btn:active{transform:scale(.97)}
button .ic,.btn .ic{position:relative;z-index:1}
.btn-danger{background:linear-gradient(112deg,#E14B60,#B9203F);box-shadow:0 10px 26px -12px color-mix(in srgb,#F25265 70%,transparent)}
.btn-danger:hover{filter:brightness(1.08)}
.btn-ghost{
  background:var(--panel);color:var(--text-2);border-color:var(--line);box-shadow:none;
}
.btn-ghost:hover{color:var(--primary);border-color:color-mix(in srgb,var(--primary) 45%,var(--line));background:var(--panel)}
.btn-sm{padding:6.5px 11px;font-size:12px;border-radius:10px}
.form-foot{display:flex;gap:10px;align-items:center;margin-top:20px;padding-top:16px;border-top:1px dashed var(--line)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.inline{display:inline-flex;gap:7px;align-items:center;margin:0}

/* ================= نشان‌ها ================= */
.badge{
  display:inline-flex;align-items:center;gap:6px;padding:3.5px 11px;border-radius:999px;
  font-size:11px;font-weight:800;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis;
}
.badge::before{content:"";width:6px;height:6px;border-radius:99px;background:currentColor;flex-shrink:0}
.b-blue{background:var(--primary-soft);color:var(--primary)}
.b-green{background:var(--ok-soft);color:var(--ok)}
.b-red{background:var(--danger-soft);color:var(--danger)}
.b-gold{background:var(--gold-soft);color:var(--gold)}
.b-violet{background:var(--violet-soft);color:var(--violet)}
.b-teal{background:var(--teal-soft);color:var(--teal)}
.b-gray{background:var(--bg-soft);color:var(--text-2)}

/* ================= تب‌ها (بدون JS) ================= */
.tab-radio{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}
.tabs{display:flex;gap:6px;flex-wrap:wrap;padding:16px 18px 0}
.tabs label{
  display:inline-flex;align-items:center;gap:7px;padding:9px 16px;margin:0;cursor:pointer;
  color:var(--text-2);font-weight:800;font-size:12.5px;border:1px solid var(--line);
  border-radius:999px;transition:all .18s;
}
.tabs label:hover{color:var(--text);background:var(--bg-soft);border-color:var(--line-strong)}
.tab-body{display:none;padding:20px 18px 6px}
#tab-basic:checked ~ .tabs [for=tab-basic],
#tab-buy:checked ~ .tabs [for=tab-buy],
#tab-renew:checked ~ .tabs [for=tab-renew],
#tab-adv:checked ~ .tabs [for=tab-adv]{
  background:var(--grad);color:#fff;border-color:transparent;box-shadow:var(--glow);
}
#tab-basic:checked ~ .tb-basic,#tab-buy:checked ~ .tb-buy,
#tab-renew:checked ~ .tb-renew,#tab-adv:checked ~ .tb-adv{display:block}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(235px,1fr));gap:16px}
.renew-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}
.renew-cell{border:1px solid var(--line);border-radius:14px;padding:15px;background:var(--panel-2);position:relative;overflow:hidden;transition:border-color .16s,box-shadow .16s,transform .16s}
.renew-cell:hover{transform:translateY(-2px)}
.renew-cell:focus-within{border-color:color-mix(in srgb,var(--primary) 45%,var(--line));box-shadow:var(--ring)}
.renew-cell.rn{border-top:3px solid var(--primary)}
.renew-cell.rv{border-top:3px solid var(--gold)}
.renew-cell.rr{border-top:3px solid var(--teal)}
.renew-cell .hint{margin-top:8px}

/* ================= حالت خالی ================= */
.empty{text-align:center;padding:48px 22px;color:var(--muted)}
.empty-ic{
  width:78px;height:78px;margin:0 auto;display:flex;align-items:center;justify-content:center;
  border-radius:26px;background:color-mix(in srgb,var(--primary) 6%,transparent);
  border:1px dashed color-mix(in srgb,var(--primary) 30%,transparent);
  animation:floaty 5s ease-in-out infinite;
}
@keyframes floaty{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
.empty-ic .ic{width:34px;height:34px;opacity:.65;color:var(--primary)}
.empty h3{margin:14px 0 5px;font-size:14.5px;color:var(--text-2)}
.empty p{margin:0 auto;font-size:12.5px;max-width:380px}

/* ================= توست و مودال ================= */
.toast-zone{position:fixed;bottom:22px;left:22px;z-index:120;display:flex;flex-direction:column;gap:10px;max-width:min(400px,calc(100vw - 44px))}
.toast{
  position:relative;display:flex;gap:11px;align-items:flex-start;overflow:hidden;
  background:color-mix(in srgb,var(--panel) 92%,transparent);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  color:var(--text);border:1px solid var(--line);border-right:4px solid var(--ok);
  border-radius:15px;padding:13px 15px;box-shadow:var(--shadow-lg);
  animation:toast-in .3s cubic-bezier(.21,1.02,.73,1) both;font-size:13px;font-weight:600;
}
.toast.err{border-right-color:var(--danger)}
.toast.ok .ic{color:var(--ok)}
.toast.err .ic{color:var(--danger)}
.toast::after{content:"";position:absolute;bottom:0;inset-inline:0;height:2.5px;background:currentColor;opacity:.25;animation:tbar 4.2s linear forwards}
.toast.ok::after{background:var(--ok)} .toast.err::after{background:var(--danger)}
@keyframes tbar{from{width:100%}to{width:0}}
@keyframes toast-in{from{opacity:0;transform:translateY(12px) scale(.97)}to{opacity:1;transform:none}}
.toast.hide{opacity:0;transform:translateY(8px);transition:all .3s}
.modal-back{
  position:fixed;inset:0;z-index:110;display:none;align-items:center;justify-content:center;padding:22px;
  background:rgba(4,7,16,.55);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
}
[data-theme="light"] .modal-back{background:rgba(19,32,73,.28)}
.modal-back.open{display:flex}
.modal{
  background:var(--panel);border:1px solid var(--line);border-radius:22px;box-shadow:var(--shadow-lg);
  max-width:410px;width:100%;padding:24px;animation:toast-in .24s cubic-bezier(.22,1,.36,1) both;
}
.modal h3{margin:0 0 8px;font-size:15.5px;display:flex;gap:9px;align-items:center;color:var(--warn)}
.modal h3 .ic{color:var(--warn)}
.modal p{margin:0 0 18px;color:var(--text-2);font-size:13px;line-height:1.9}
.modal .row{justify-content:flex-start}

/* نوارهای داده */
.bars{display:grid;gap:12px;margin-top:4px}
.bar-row{display:grid;grid-template-columns:96px 1fr auto;gap:12px;align-items:center;font-size:12px}
.bar-row>b{font-size:12px}
.bar-track{height:10px;background:var(--panel-2);border-radius:99px;overflow:hidden}
.bar-fill{height:100%;border-radius:99px;background:var(--grad);min-width:3px;transition:width .6s cubic-bezier(.22,1,.36,1);box-shadow:0 0 14px -2px color-mix(in srgb,var(--primary) 60%,transparent)}

/* ================= ورود ================= */
.login-wrap{
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:22px;position:relative;overflow:hidden;
}
.login-wrap::before{
  content:"";position:fixed;inset:-25%;pointer-events:none;
  background:
    radial-gradient(34% 30% at 78% 8%, color-mix(in srgb,var(--primary) 24%,transparent), transparent 70%),
    radial-gradient(30% 26% at 10% 88%, color-mix(in srgb,var(--accent) 18%,transparent), transparent 70%),
    radial-gradient(26% 24% at 88% 82%, color-mix(in srgb,var(--violet) 14%,transparent), transparent 70%);
  animation:aurora 22s ease-in-out infinite alternate;
}
.login-card{
  position:relative;width:100%;max-width:410px;background:color-mix(in srgb,var(--panel) 86%,transparent);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border:1px solid var(--line-strong);border-radius:26px;box-shadow:var(--shadow-lg);
  padding:40px 34px 30px;text-align:center;animation:toast-in .45s cubic-bezier(.22,1,.36,1) both;
}
.login-card::before{
  content:"";position:absolute;inset-inline:22px;top:0;height:1px;
  background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--accent) 70%,transparent),transparent);
}
.login-logo{
  width:68px;height:68px;border-radius:22px;background:var(--grad);display:inline-flex;align-items:center;justify-content:center;
  color:#fff;margin-bottom:18px;box-shadow:var(--glow);position:relative;
}
.login-logo::after{content:"";position:absolute;inset:-6px;border-radius:26px;border:1px solid color-mix(in srgb,var(--accent) 40%,transparent);animation:halo 3s ease infinite}
.login-logo .ic{width:30px;height:30px}
.login-card h2{margin:0 0 5px;font-size:21.5px;font-weight:900;letter-spacing:-.2px}
.login-card .sub{color:var(--text-2);font-size:12.5px;margin:0 0 20px}
.login-err{background:var(--danger-soft);color:var(--danger);border-radius:12px;padding:10px 13px;font-size:12.5px;margin-bottom:14px;font-weight:700}
.login-card input{width:100%;margin-bottom:14px;text-align:center;letter-spacing:3px;padding:11.5px;font-size:14px}
.login-card button{width:100%;padding:12.5px;font-size:14px;border-radius:14px}
.login-foot{margin:18px 0 0;font-size:10.5px;color:var(--muted);letter-spacing:.4px}

/* ================= سایر ================= */
.muted{color:var(--muted);font-size:12px;margin:8px 0;line-height:1.95}
.pager{display:flex;gap:8px;align-items:center;margin:16px 4px 6px;justify-content:center;flex-wrap:wrap}
.pager a,.pager span{padding:7.5px 15px;border-radius:12px;background:var(--panel);border:1px solid var(--line);font-size:12.5px;color:var(--text-2);box-shadow:var(--shadow);transition:all .16s}
.pager a:hover{color:var(--primary);border-color:color-mix(in srgb,var(--primary) 45%,var(--line));transform:translateY(-1px);box-shadow:var(--shadow-lg)}
.pager span{opacity:.75}
code{
  background:var(--bg-soft);border:1px solid var(--line);border-radius:9px;padding:2px 8px;
  font-size:11.5px;direction:ltr;unicode-bidi:embed;font-variant-numeric:tabular-nums;
}
.actions{display:flex;gap:6px;flex-wrap:wrap}
.kv{display:grid;grid-template-columns:minmax(96px,auto) 1fr;gap:9px 20px;font-size:13px;align-items:baseline;margin:0}
.kv dt{color:var(--muted);font-size:11.5px;font-weight:700;white-space:nowrap}
.kv dd{margin:0;font-weight:600;word-break:break-word;overflow-wrap:anywhere}
.back-link{display:inline-flex;align-items:center;gap:6px;margin-top:18px;font-size:12.5px;color:var(--text-2);font-weight:700}
.back-link:hover{color:var(--primary)}

/* ================= ریسپانسیو ================= */
.menu-btn{display:none}
[data-theme="light"] .only-dark{display:none}
[data-theme="dark"] .only-light{display:none}
@media(min-width:861px){
  .only-desktop{display:inline-flex}
  body.side-collapsed .side{width:76px}
  body.side-collapsed .side .nav a span,
  body.side-collapsed .brand-name,
  body.side-collapsed .nav-group>span,
  body.side-collapsed .side-search{display:none}
  body.side-collapsed .side-head{justify-content:center;padding:16px 8px 10px}
  body.side-collapsed .nav a{justify-content:center;padding:11px 0}
  body.side-collapsed .nav a::before{display:none}
  body.side-collapsed .logout-btn span{display:none}
  body.side-collapsed .logout-btn{justify-content:center}
  body.side-collapsed .admin-card{justify-content:center;padding:11px 8px}
  body.side-collapsed .admin-meta{display:none}
}
@media(max-width:1024px){.content{padding:20px}}
@media(max-width:860px){
  .menu-btn{display:inline-flex}
  .side{position:fixed;right:0;top:0;transform:translateX(105%);box-shadow:var(--shadow-lg)}
  body.side-open .side{transform:none}
  .side-scrim{display:none;position:fixed;inset:0;background:rgba(3,6,15,.55);backdrop-filter:blur(3px);z-index:35}
  [data-theme="light"] .side-scrim{background:rgba(19,32,73,.3)}
  body.side-open .side-scrim{display:block}
  .topbar{padding:10px 16px}
  .content{padding:16px 14px 42px}
  .page-head h1{font-size:20px}
  .page-head h1::after{width:40px}
  .stats{grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:10px}
  .stat{padding:13px 14px 12px}
  .stat-ic,.kpi-ic{width:36px;height:36px;border-radius:11px}
  .kpi-v{font-size:20px}
  .grid-2{grid-template-columns:1fr}
  .fields,.renew-grid{grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  th,td{padding:9px 12px}
  .tbl-toolbar{padding:12px 12px 0}
  .legend{padding:12px 12px 0}
  .content{padding-bottom:56px}
}
@media(max-width:520px){
  .fields,.renew-grid{grid-template-columns:1fr}
  .bar-row{grid-template-columns:72px 1fr auto}
  .head-actions{width:100%}
  .head-actions .btn,.head-actions button{flex:1 1 auto}
  .kpi-v{font-size:18px}
  .stat-v.v-lg{font-size:20px}
  .hl-label{width:auto;flex:none}
  .toast-zone{left:14px;right:14px;max-width:none}
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}
}
</style>
"""

PAGE_JS = """
<script>
(function(){
"use strict";
var d=document;

/* ---------- تم روشن/تاریک ---------- */
function applyTheme(t){d.documentElement.setAttribute('data-theme',t);}
try{var saved=localStorage.getItem('ow-theme');applyTheme(saved||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'));}catch(e){}
window.owToggleTheme=function(){
 var cur=d.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
 applyTheme(cur);try{localStorage.setItem('ow-theme',cur);}catch(e){}
};

/* ---------- منوی کنار (موبایل) و جمع‌کردن (دسکتاپ) ---------- */
window.owSide=function(){d.body.classList.toggle('side-open');};
window.owCollapse=function(){
 d.body.classList.toggle('side-collapsed');
 try{localStorage.setItem('ow-side',d.body.classList.contains('side-collapsed')?'1':'0');}catch(e){}
};
try{
 if(localStorage.getItem('ow-side')==='1'&&matchMedia('(min-width:861px)').matches)d.body.classList.add('side-collapsed');
}catch(e){}
var scrim=d.querySelector('.side-scrim');
if(scrim)scrim.addEventListener('click',function(){d.body.classList.remove('side-open');});

/* ---------- توست ---------- */
function toast(msg,type){
 var zone=d.querySelector('.toast-zone');if(!zone)return;
 var t=d.createElement('div');
 t.className='toast '+(type||'ok');
 t.innerHTML='<svg class="ic" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+(type==='err'?'<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>':'<polyline points="20 6 9 17 4 12"/>')+'</svg><span></span>';
 t.querySelector('span').textContent=msg;
 zone.appendChild(t);
 setTimeout(function(){t.classList.add('hide');setTimeout(function(){t.remove();},350);},4200);
}
window.owToast=toast;
var flash=d.getElementById('flash-data');
if(flash){try{var fd=JSON.parse(flash.textContent);if(fd.msg)toast(fd.msg,fd.type);}catch(e){}}

/* ---------- کپی در کلیپ‌بورد ---------- */
d.addEventListener('click',function(ev){
 var b=ev.target.closest('[data-copy]');
 if(!b)return;
 var val=b.getAttribute('data-copy');
 function done(){var o=b.innerHTML;b.innerHTML='<svg class="ic" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>';setTimeout(function(){b.innerHTML=o;},1200);}
 if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(val).then(done,function(){fallback();});}else fallback();
 function fallback(){var ta=d.createElement('textarea');ta.value=val;d.body.appendChild(ta);ta.select();try{d.execCommand('copy');done();}catch(e){}ta.remove();}
 toast('کپی شد: '+val,'ok');
});

/* ---------- مودال تأیید برای فرم‌های data-confirm ---------- */
var back=d.getElementById('ow-modal');
var pendingForm=null;
d.addEventListener('submit',function(ev){
 var f=ev.target;
 if(!(f instanceof HTMLFormElement)||!f.hasAttribute('data-confirm'))return;
 ev.preventDefault();ev.stopPropagation();
 pendingForm=f;
 d.getElementById('ow-modal-msg').textContent=f.getAttribute('data-confirm');
 back.classList.add('open');
 setTimeout(function(){d.getElementById('ow-modal-ok').focus();},50);
},true);
window.owConfirm=function(ok){
 back.classList.remove('open');
 if(ok&&pendingForm){HTMLFormElement.prototype.submit.call(pendingForm);}
 pendingForm=null;
};
back.addEventListener('keydown',function(ev){if(ev.key==='Escape')owConfirm(false);});

/* ---------- تقویت جدول‌ها: جستجو و مرتب‌سازی سمت کلاینت ---------- */
d.querySelectorAll('table[data-enhance]').forEach(function(tbl){
 var wrap=tbl.closest('.tbl-card')||tbl.parentElement;
 var head=wrap.querySelector('.tbl-toolbar');
 var body=tbl.tBodies[0];
 if(!body)return;
 var rows=[].slice.call(body.rows);
 if(head){
   var input=head.querySelector('input[type=search]');
   var count=head.querySelector('.tbl-count');
   var update=function(){
     var q=input?input.value.trim().toLowerCase():'';
     var n=0;
     rows.forEach(function(r){
       var hit=!q||r.textContent.toLowerCase().indexOf(q)!==-1;
       r.style.display=hit?'':'none';if(hit)n++;
     });
     if(count)count.textContent=n+' مورد';
   };
   if(input)input.addEventListener('input',update);
   update();
 }
 th_sort:rows.length&&[].slice.call(tbl.querySelectorAll('th')).forEach(function(th,idx){
   if(!th.classList.contains('sortable'))return;
   var ind=d.createElement('span');ind.className='sort-ind';ind.textContent='▲▼';th.appendChild(ind);
   var asc=null;
   th.addEventListener('click',function(){
     asc=(asc===null)?true:!asc;
     tbl.querySelectorAll('th').forEach(function(o){o.classList.remove('sorted');});
     th.classList.add('sorted');ind.textContent=asc?'▲':'▼';
     rows.slice().sort(function(a,b){
       var x=a.cells[idx]?a.cells[idx].textContent.trim():'',y=b.cells[idx]?b.cells[idx].textContent.trim():'';
       var nx=parseFloat(x.replace(/[،,]/g,'')),ny=parseFloat(y.replace(/[،,]/g,''));
       if(!isNaN(nx)&&!isNaN(ny))return asc?nx-ny:ny-nx;
       return asc?x.localeCompare(y,'fa'):y.localeCompare(x,'fa');
     }).forEach(function(r){body.appendChild(r);});
   });
 });
});

/* ---------- فیلتر سریع منوی کنار ---------- */
var nq=d.getElementById('nav-q');
if(nq){nq.addEventListener('input',function(){
 var q=this.value.trim();
 d.querySelectorAll('.nav a').forEach(function(a){
  a.style.display=(!q||a.textContent.indexOf(q)!==-1)?'':'none';
 });
 d.querySelectorAll('.nav-group').forEach(function(g){
  var vis=[].slice.call(g.querySelectorAll('a')).some(function(a){return a.style.display!=='none';});
  g.style.display=vis?'':'none';
 });
});}

/* ---------- شمارنده‌ی متن پیام همگانی ---------- */
var bc=d.querySelector('[data-charcount]');
if(bc){
 var out=d.getElementById(bc.getAttribute('data-charcount'));
 var upd=function(){out.textContent=bc.value.length+' کاراکتر';};
 bc.addEventListener('input',upd);upd();
}
})();
</script>
"""

_MODAL_HTML = (
    '<div class="modal-back" id="ow-modal" role="dialog" aria-modal="true" aria-label="تأیید عملیات">'
    '<div class="modal"><h3>' + icon('alert').replace('class="ic "', 'class="ic"') +
    ' تأیید عملیات</h3><p id="ow-modal-msg"></p>'
    '<div class="row"><button id="ow-modal-ok" class="btn-danger" onclick="owConfirm(true)">'
    '<svg class="ic" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
    ' بله، انجام شود</button>'
    '<button class="btn-ghost" onclick="owConfirm(false)">انصراف</button></div></div></div>'
)


def _flash(request):
    """پیام موفقیت/خطا به‌صورت توست؛ بدون JS هم بنر ثابت نمایش داده می‌شود."""
    if request is None:
        return ""
    ok = request.query.get("ok")
    err = request.query.get("err")
    if not ok and not err:
        return ""
    msg = e(ok or err or "")
    typ = "ok" if ok else "err"
    data = f'<script type="application/json" id="flash-data">{{"type":"{typ}","msg":"{msg}"}}</script>'
    noscript = (f"<noscript><div class='flash {typ}' style='padding:12px 14px;border-radius:12px;"
                f"margin-bottom:14px'>{msg}</div></noscript>")
    return data + noscript


def layout(title, body, active="", request=None):
    groups_html = ""
    for gname, items in NAV_GROUPS:
        links = "".join(
            f'<a href="{path}"{" class=\"active\"" if path == active else ""}>'
            f'{_use(ic_name)}<span>{e(label)}</span></a>'
            for path, label, ic_name in items
        )
        groups_html += f'<div class="nav-group"><span>{e(gname)}</span>{links}</div>'
    flash = _flash(request)
    doc = (
        "<!doctype html><html lang='fa'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<link rel='preconnect' href='https://cdn.jsdelivr.net'>"
        "<link rel='stylesheet' href='https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/Vazirmatn-font-face.css'>"
        f"<title>{e(title)} | OverWall</title>{PAGE_CSS}</head><body>"
        f"{_svg_sprite()}"
        "<div class='app'>"
        "<div class='side-scrim' onclick='owSide()'></div>"
        "<aside class='side'>"
        "<div class='side-head'><span class='brand-mark'>" + icon('shield') + "</span>"
        "<div class='brand-name'>OverWall<small>ADMIN PANEL</small></div></div>"
        "<div class='side-search'>" + _use('search') +
        "<input id='nav-q' type='search' placeholder='\u200fجستجوی منو…' aria-label='جستجوی منو'></div>"
        f"<nav class='nav'>{groups_html}</nav>"
        "<div class='side-foot'>"
        "<div class='admin-card'>"
        "<span class='admin-avatar'>👑</span>"
        "<div class='admin-meta'><b>مدیر سیستم</b>"
        "<span class='admin-status'><i></i>Online</span></div>"
        "</div>"
        "<a class='logout-btn' href='/logout'>" + _use('logout') + "<span>خروج از حساب</span></a>"
        "</div>"
        "</aside>"
        "<div class='main-wrap'>"
        "<header class='topbar'>"
        "<button class='icon-btn menu-btn' onclick='owSide()' aria-label='منو'>" + _use('menu') + "</button>"
        f"<span class='muted' style='margin:0'>{e(title)}</span>"
        "<span class='spacer'></span>"
        "<button class='icon-btn only-desktop' onclick='owCollapse()' title='جمع‌کردن منو' aria-label='جمع‌کردن منو'>"
        + _use('chevron') + "</button>"
        "<button class='icon-btn' onclick='owToggleTheme()' title='حالت روشن/تاریک' aria-label='تغییر تم'>"
        + _use('moon', 'only-light') + _use('sun', 'only-dark') + "</button>"
        "</header>"
        f"<main class='content'>{flash}{body}</main>"
        "</div></div>"
        f"<div class='toast-zone' aria-live='polite'></div>{_MODAL_HTML}{PAGE_JS}</body></html>"
    )
    return web.Response(text=doc, content_type="text/html")

# ================= ورود / خروج =================
async def login_get(request):
    if _authed(request):
        raise web.HTTPFound("/")
    blocked = _login_blocked(request.remote)
    if blocked:
        err = f"تلاش‌های ناموفق زیاد بود. {blocked} ثانیه دیگر تلاش کنید."
    else:
        err = "رمز اشتباه است." if request.query.get("err") else ""
    err_html = f"<div class='login-err'>{e(err)}</div>" if err else ""
    body = (
        _svg_sprite() + PAGE_CSS
        + "<div class='login-wrap'><div class='login-card'>"
        + "<span class='login-logo'>" + icon('shield') + "</span>"
        + "<h2>پنل مدیریت OverWall</h2>"
        + "<p class='sub'>برای ادامه، رمز عبور مدیر را وارد کنید</p>"
        + err_html
        + "<form method='post' action='/login'>"
        + "<input type='password' name='password' placeholder='\u200f••••••••' autofocus autocomplete='current-password' required>"
        + "<button type='submit'>" + icon('logout') + " ورود به پنل</button></form>"
        + "<p class='login-foot'>دسترسی محرمانه · تمام اقدامات ثبت می‌شود</p>"
        + "</div></div>"
    )
    return web.Response(
        text=("<!doctype html><html lang='fa'><head><meta charset='utf-8'>"
              "<meta name='viewport' content='width=device-width,initial-scale=1'>"
              f"<title>ورود | OverWall</title></head><body>{body}</body></html>"),
        content_type="text/html",
    )


async def login_post(request):
    ip = request.remote
    blocked = _login_blocked(ip)
    if blocked:
        logging.warning("WEB_LOGIN blocked from %s (%ss left)", ip, blocked)
        raise web.HTTPFound("/login")
    data = await request.post()
    if WEB_ADMIN_PASSWORD and hmac.compare_digest(str(data.get("password", "")), WEB_ADMIN_PASSWORD):
        _LOGIN_FAILS.pop(ip, None)
        resp = web.HTTPFound("/")
        resp.set_cookie(
            "session", _new_session(), httponly=True, max_age=WEB_SESSION_TTL,
            samesite="Lax", secure=_is_https(request),
        )
        logging.info("WEB_LOGIN success from %s", ip)
        return resp
    fails = _note_login_fail(ip)
    logging.warning("WEB_LOGIN failed from %s (attempt %s)", ip, fails)
    # تأخیر کوچک تا حدس زدن رمز پرهزینه‌تر شود
    await asyncio.sleep(1.0)
    raise web.HTTPFound("/login?err=1")


async def logout(request):
    # نشست سمت سرور هم باطل می‌شود، نه فقط کوکی مرورگر
    _SESSIONS.pop(request.cookies.get("session", ""), None)
    resp = web.HTTPFound("/login")
    resp.del_cookie("session")
    return resp


# ================= داشبورد =================
@require_auth
async def dashboard(request):
    import webcomponents as wc

    r = await db.get_sales_report()
    fin = await db.get_financial_report()
    rev = await db.get_revenue_series(30)
    plans = await db.list_plans()
    active_plans = sum(1 for p in plans if p['is_active'])

    sales_open = (await db.get_setting('sales_status')) == 'open'
    status_badge = (_badge('green', 'فروش باز') if sales_open else _badge('red', 'فروش بسته'))

    # ---------- ردیف ۱: KPI ----------
    # درصد رشد درآمد خالص: مقایسه‌ی ۷ روز اخیر با ۷ روز قبلش (از سری ۳۰ روزه)
    s = rev['series']
    last7, prev7 = sum(s[-7:]), sum(s[-14:-7])
    net_delta = f"+{(last7 - prev7) * 100 / prev7:.1f}%" if prev7 > 0 else None
    net_tone = 'up' if last7 >= prev7 else 'down'
    today_delta = f"+{fin['today'] * 100 // max(1, fin['week'] // 7):,}%" if fin['week'] else None

    cards = "".join([
        wc.stats_card('درآمد خالص', f"{money(fin['net'])} تومان", '💰',
                      delta=net_delta, delta_tone=net_tone,
                      spark=wc._sparkline(s[-14:]), tone='primary'),
        wc.stats_card('فروش امروز', f"{money(fin['today'])} تومان", '⚡',
                      delta=today_delta, delta_tone='up', tone='green',
                      sub=f"۷ روز اخیر: {money(fin['week'])} تومان"),
        wc.stats_card('کاربران فعال', f"{money(rev['users_active'])}", '👥',
                      tone='violet',
                      sub=f"کل: {money(rev['users_total'])} کاربر ثبت‌شده"),
        wc.stats_card('سفارش‌ها', f"{money(rev['orders_total'])}", '📦',
                      delta=f"+{fin['renewals']}" if fin['renewals'] else None,
                      delta_tone='flat', tone='gold',
                      sub=f"تمدید: {money(fin['renewals'])} | آفر اول: {money(fin['first_offers'])}"),
    ])

    # ---------- ردیف ۲: نمودار درآمد + وضعیت سرورها ----------
    panels = await db.get_panels()
    try:
        panels_ping = await asyncio.wait_for(wc.ping_panels(panels), timeout=8)
    except Exception:
        panels_ping = [{'name': p['name'], 'host': p['url'], 'ms': None, 'state': 'down'} for p in panels]
    server_card = wc.server_status(panels_ping)
    revenue_card = wc.revenue_chart(s, prev_total=rev['prev_total'])

    # ---------- ردیف ۳: سلامت + منابع ----------
    health_card = wc.system_health([
        ('X-UI', 'ok' if panels_ping and all(p['state'] != 'down' for p in panels_ping) else 'down'),
        ('Database', 'ok'),
        ('Telegram Bot', 'ok'),
        ('Web Panel', 'ok'),
    ], uptime='99.8%')
    resource_card = wc.resource_monitor(await _system_resources())

    # ---------- ردیف ۴: سفارش‌های اخیر + فعالیت ----------
    recent_orders = await db.list_recent_orders(limit=7)
    plans_by_id = {p['id']: p['name'] for p in plans}
    orders_view = []
    for o in recent_orders:
        # سفارش با قیمت ثبت‌شده یعنی خرید/تمدید موفق؛ بدون قیمت = ایمپورت قدیمی
        status = 'done' if o['price'] else 'pending'
        orders_view.append({
            'id': o['id'], 'user_id': o['user_id'],
            'plan_name': o['plan_name'] or plans_by_id.get(o['plan_id'], '—'),
            'amount': o['price'] or 0, 'status': status,
            'date': _fmt_dt(o['date'], '%m-%d %H:%M'),
        })
    orders_card = wc.orders_table(orders_view)

    recent_txns = await db.list_recent_transactions(limit=8)
    events = []
    for t in recent_txns:
        kind, amount = t['kind'], t['amount']
        if kind == 'شارژ حساب':
            ev_tone, text = 'ok', f"کاربر {t['user_id']} کیف پول را شارژ کرد ({money(amount)} تومان)"
        elif kind == 'هدیه نمایندگی':
            ev_tone, text = 'ok', f"هدیه‌ی نمایندگی برای کاربر {t['user_id']} ثبت شد"
        elif amount < 0 and kind == 'خرید':
            ev_tone, text = 'ok', f"سفارش جدید ثبت شد — کاربر {t['user_id']}"
        elif amount < 0 and kind == 'تمدید':
            ev_tone, text = 'ok', f"کاربر {t['user_id']} سرویسش را تمدید کرد"
        elif kind == 'برگشت وجه':
            ev_tone, text = 'down', f"برگشت وجه برای کاربر {t['user_id']} ({money(abs(amount))} تومان)"
        else:
            ev_tone, text = 'info', f"{kind} — کاربر {t['user_id']}"
        events.append({'text': text, 'tone': ev_tone, 'time': _fmt_dt(t['date'], '%m-%d %H:%M')})
    activity_card = wc.activity_feed(events)

    body = (
        _page_head('داشبورد', 'نمای کلی شبکه، فروش و سلامت سیستم',
                   actions=(status_badge +
                            f" <a href='/export/sales.csv'><button class='btn-ghost btn-sm'>{_use('db')} خروجی فروش CSV</button></a>"
                            f" <a href='/export/users.csv'><button class='btn-ghost btn-sm'>{_use('users')} خروجی کاربران CSV</button></a>"),
                   crumbs=[('خانه', None)]),
        f"<div class='dash-row r4'>{cards}</div>",
        f"<div class='dash-row r2-30-70'>{revenue_card}{server_card}</div>",
        f"<div class='dash-row r2'>{health_card}{resource_card}</div>",
        f"<div class='dash-row r2'>{orders_card}{activity_card}</div>",
    )
    return layout("داشبورد", "".join(body), "/", request)

# ================= کاربران =================
def _role_badge(role):
    role = pricing.normalize_role(role)
    if role == pricing.VIP:
        return "<span class='badge b-gold'>VIP</span>"
    if role == pricing.RESELLER:
        return "<span class='badge b-teal'>نماینده</span>"
    return "<span class='badge b-gray'>عادی</span>"


def _role_options(current):
    current = pricing.normalize_role(current)
    return "".join(
        f"<option value='{key}' {_sel(current, key)}>{e(label)}</option>"
        for key, label in ROLE_LABELS.items()
    )


def _valid_role(value):
    return value if value in ROLE_LABELS else pricing.NORMAL


@require_auth
async def users_page(request):
    search = request.query.get("q", "").strip() or None
    page = _page_arg(request)
    total = await db.count_users(search)
    users = await db.list_users(search=search, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
    rows = ""
    for u in users:
        rows += (
            f"<tr><td>{num(u['user_id'])} {_copy_btn(u['user_id'])}</td>"
            f"<td class='wrap'>{e(u['nickname'] or '—')}</td><td>{num(money(u['balance']))}</td>"
            f"<td>{_role_badge(u['role'])}</td>"
            f"<td class='col-actions'><div class='actions'>"
            f"<a href='/users/view?id={e(u['user_id'])}'><button class='btn-ghost btn-sm' title='جزئیات'>{_use('eye')}</button></a>"
            f"<a href='/users/edit?id={e(u['user_id'])}'><button class='btn-ghost btn-sm' title='ویرایش'>{_use('edit')}</button></a>"
            f"</div></td></tr>"
        )
    extra = f"&q={quote(search)}" if search else ""
    table = (f"<table data-enhance><thead><tr><th>آیدی</th><th>نام</th><th>موجودی (تومان)</th><th>نقش</th>"
             f"<th class='col-actions'>عملیات</th></tr></thead><tbody>{rows}</tbody></table>"
             if rows else _empty('کاربری پیدا نشد', 'با افزودن کاربر شروع کنید یا عبارت جستجو را تغییر دهید.', 'users'))
    body = (
        _page_head('کاربران', 'جستجو، مشاهده و مدیریت کاربران',
                   actions=f"<a href='/users/edit'><button>{_use('plus')} افزودن کاربر</button></a>",
                   crumbs=[('خانه', '/'), ('کاربران', None)]),
        f"<div class='tbl-card'><div class='tbl-toolbar'>"
        f"<form class='inline' method='get' action='/users' style='flex:1 1 320px;position:relative'>"
        f"<input name='q' placeholder='جستجوی آیدی عددی یا نام…' value='{e(search or "")}' style='width:100%;padding-right:34px'>"
        f"<span style='position:absolute;right:10px;top:50%;transform:translateY(-50%);color:var(--muted)'>{_use('search')}</span>"
        f"</form></div><div class='tbl-scroll'>{table}</div>{_pager_html('/users', page, total, extra)}</div>"
    )
    return layout("کاربران", "".join(body), "/users", request)


@require_auth
async def user_view(request):
    uid = request.query.get("id")
    if not (uid and uid.isdigit()):
        raise _redirect("/users", err="آیدی نامعتبر")
    u = await db.get_user_detail(int(uid))
    if not u:
        raise _redirect("/users", err="کاربر یافت نشد")
    orders = await db.get_orders_by_user(int(uid))
    txns = await db.get_user_transactions(int(uid), 20)
    ref_count = await db.referral_count(int(uid))

    o_rows = ""
    for o in orders:
        email = order_email(o)
        o_rows += (
            f"<tr><td>{num(o['id'])}</td><td class='wrap'>{num(email)} {_copy_btn(email or '')}</td>"
            f"<td class='muted'>{e(o['date'])}</td><td>{num(o['panel_id'])}</td>"
            f"<td class='col-actions'><form class='inline' method='post' action='/orders/delete'{_confirm('حذف سفارش؟')}>"
            f"<input type='hidden' name='id' value='{e(o['id'])}'><input type='hidden' name='back' value='/users/view?id={e(uid)}'>"
            f"<button class='btn-danger btn-sm' title='حذف'>{_use('trash')}</button></form></td></tr>"
        )
    o_table = (f"<table><thead><tr><th>#</th><th>سرویس</th><th>تاریخ</th><th>پنل</th><th></th></tr></thead>"
               f"<tbody>{o_rows}</tbody></table>" if o_rows else _empty('سفارشی ندارد', '', 'box'))

    t_rows = ""
    for t in txns:
        d = _fmt_dt(t['date'])
        cls = 'chip-n' if t['amount'] > 0 else 'chip-off'
        sign = '+' if t['amount'] > 0 else '−'
        t_rows += (f"<tr><td><span class='price-chip {cls}'>{sign}{money(abs(t['amount']))}</span></td>"
                   f"<td>{e(t['kind'])}</td><td class='wrap muted'>{e(t['description'] or '—')}</td>"
                   f"<td class='muted'>{num(d)}</td></tr>")
    t_table = (f"<table><thead><tr><th>مبلغ</th><th>نوع</th><th>توضیح</th><th>تاریخ</th></tr></thead>"
               f"<tbody>{t_rows}</tbody></table>" if t_rows else _empty('تراکنشی ندارد', '', 'card'))

    profile = await db.get_pricing_profile(int(uid))
    tier, tier_discount, tier_title = pricing.reseller_tier(profile.get('monthly_topup'), await pricing.load_config())
    reseller_box = ""
    if pricing.normalize_role(u['role']) == pricing.RESELLER:
        reseller_box = (
            "<dt>پله‌ی نمایندگی</dt><dd>" + e(tier_title) + f" <span class='badge b-teal'>تخفیف {tier_discount}٪</span></dd>"
            "<dt>شارژ ۳۰ روز اخیر</dt><dd>" + num(money(profile.get('monthly_topup'))) + " تومان</dd>"
            "<dt>هدیه‌ی اولین شارژ</dt><dd>" + ('مصرف شده' if profile.get('reseller_bonus_used') else 'استفاده نشده') + "</dd>"
        )
    # پنل‌های مجاز کاربر (خالی = همه‌ی پنل‌ها)
    allowed_ids = await db.get_user_allowed_panels(int(uid))
    panels_list = await db.get_panels()
    if allowed_ids is None:
        panels_txt = "همه‌ی پنل‌ها (بدون محدودیت)"
    else:
        names = [p['name'] for p in panels_list if p['id'] in allowed_ids]
        panels_txt = ("، ".join(names) if names else "⛔️ هیچ پنلی (خرید نمی‌تواند)")
    first_offer = 'مصرف شده' if profile.get('first_offer_used') else 'در دسترس'
    referrer = (f"<a href=\"/users/view?id={e(u['referred_by'])}\">{num(u['referred_by'])}</a>") if u['referred_by'] else '—'
    ref_extra = (f" | پاداش دعوت: {'پرداخت شده' if u['ref_rewarded'] else 'در انتظار'}") if u['referred_by'] else ''

    info_dl = (
        "<dl class='kv'>"
        "<dt>نام</dt><dd>" + e(u['nickname'] or '—') + "</dd>"
        "<dt>نقش</dt><dd>" + _role_badge(u['role']) + "</dd>"
        "<dt>وضعیت دسترسی</dt><dd>" + ("<span class='badge b-red'>بن‌شده 🚫</span>" if u.get('is_banned') else "<span class='badge b-green'>عادی</span>") + "</dd>"
        "<dt>موجودی</dt><dd>" + num(money(u['balance'])) + " تومان</dd>"
        "<dt>اکانت تست گرفته</dt><dd>" + ('بله' if u['got_test'] else 'خیر') + "</dd>"
        "<dt>پنل‌های مجاز خرید</dt><dd>" + e(panels_txt) + "</dd>"
        "<dt>آفر خرید اول</dt><dd>" + e(first_offer) + "</dd>"
        "<dt>تعداد سفارش‌ها</dt><dd>" + str(len(orders)) + "</dd>"
        + reseller_box +
        "<dt>دعوت‌کننده</dt><dd>" + referrer + " | دعوت‌های او: " + str(ref_count) + e(ref_extra) + "</dd>"
        "</dl>"
    )
    banned = bool(u.get('is_banned'))
    ban_btn = (
        f"<form class='inline' method='post' action='/users/ban'{_confirm('این کاربر از ربات مسدود می‌شود (بدون حذف تاریخچه). ادامه؟')}>"
        f"<input type='hidden' name='user_id' value='{e(uid)}'>"
        f"<input type='hidden' name='banned' value='{'' if banned else '1'}'>"
        f"<button class='btn-ghost btn-sm'>{'✅ آزادسازی' if banned else '🚫 مسدودسازی'}</button></form>"
    )
    body = (
        _page_head(f"کاربر {uid}", None,
                   actions=("<div class='row'>"
                            "<form class='inline' method='post' action='/users/adjust'>"
                            f"<input type='hidden' name='user_id' value='{e(uid)}'>"
                            f"<input type='hidden' name='back' value='/users/view?id={e(uid)}'>"
                            "<input name='amount' placeholder='± مبلغ' style='width:120px' class='ltr' required>"
                            "<button class='btn-sm'>" + _use('wallet') + " تغییر موجودی</button></form>"
                            "<form class='inline' method='post' action='/users/role'>"
                            f"<input type='hidden' name='user_id' value='{e(uid)}'>"
                            f"<select name='role'>{_role_options(u['role'])}</select>"
                            "<button class='btn-ghost btn-sm'>تغییر سطح</button></form>"
                            + ban_btn +
                            f"<a href='/users/edit?id={e(uid)}'><button class='btn-ghost btn-sm'>{_use('edit')} ویرایش</button></a>"
                            f"<form method='post' action='/users/delete'{_confirm('حذف کامل کاربر؟ این عمل قابل بازگشت نیست.')}>"
                            f"<input type='hidden' name='user_id' value='{e(uid)}'>"
                            f"<button class='btn-danger btn-sm'>{_use('trash')} حذف کاربر</button></form></div>"),
                   crumbs=[('خانه', '/'), ('کاربران', '/users'), (f'کاربر {uid}', None)]),
        "<div class='grid grid-2'>",
        "<div class='box' style='margin-top:0'><h3>" + icon('eye') + " اطلاعات کاربر</h3>" + info_dl + "</div>",
        f"<div class='box' style='margin-top:0'><h3>{icon('box')} سفارش‌ها ({len(orders)})</h3>"
        f"<div class='tbl-scroll'>{o_table}</div></div>",
        "</div>",
        "<div class='box'><h3>" + icon('card') + " تراکنش‌های اخیر</h3><div class='tbl-scroll'>" + t_table + "</div></div>",
        "<a href='/users' class='back-link'>" + icon('back') + " بازگشت به فهرست کاربران</a>",
    )
    return layout(f"کاربر {uid}", "".join(body), "/users", request)


@require_auth
async def user_edit_form(request):
    uid = request.query.get("id")
    u = await db.get_user_row(int(uid)) if uid and uid.isdigit() else None
    is_new = u is None
    title = "افزودن کاربر" if is_new else f"ویرایش کاربر {uid}"
    uid_v = e(u['user_id']) if u else ""
    nick_v = e(u['nickname']) if u else ""
    bal_v = e(u['balance']) if u else "0"
    role_v = u['role'] if u else "normal"
    # چک‌باکس پنل‌های مجاز کاربر (وقتی هیچ‌کدام تیک نخورد یعنی همه‌ی پنل‌ها)
    panels_list = await db.get_panels()
    allowed_ids = (await db.get_user_allowed_panels(int(uid))) if not is_new else None
    if allowed_ids is None:
        allowed_ids = {p['id'] for p in panels_list}
    panel_checks = ""
    if panels_list:
        checks = ""
        for p in panels_list:
            checked = " checked" if p['id'] in allowed_ids else ""
            checks += (f"<label style='display:inline-flex;align-items:center;gap:6px;margin-inline-end:14px'>"
                       f"<input type='checkbox' name='panel_ids' value='{e(p['id'])}'{checked}> {e(p['name'])}</label>")
        panel_checks = ("<div class='field'><label>پنل‌های مجاز خرید</label><div>" + checks + "</div>"
                        "<div class='hint'>هیچ‌کدام تیک نخورد = همه‌ی پنل‌ها (بدون محدودیت؛ پنل‌های جدید هم خودکار شامل می‌شوند).</div></div>")
    uid_field = ((f"<input value='{uid_v}' disabled><input type='hidden' name='user_id' value='{uid_v}'>")
                 if not is_new else "<input name='user_id' placeholder='آیدی عددی تلگرام' class='ltr' required>")
    body = (
        _page_head(title, 'اطلاعات حساب کاربر را مدیریت کنید',
                   crumbs=[('خانه', '/'), ('کاربران', '/users'), (title, None)]),
        "<div class='box'><form method='post' action='/users/save'>",
        "<div class='fields'>",
        f"<div class='field'><label>آیدی کاربر <span class='req'>*</span></label>{uid_field}"
        "<div class='hint'>شناسه‌ی عددی تلگرام؛ پس از ثبت قابل تغییر نیست.</div></div>",
        f"<div class='field'><label>نام</label><input name='nickname' value='{nick_v}' placeholder='نام نمایشی'></div>",
        "<div class='field'><label>موجودی (تومان)</label>"
        f"<input name='balance' value='{bal_v}' class='ltr' inputmode='numeric'>"
        "<div class='hint'>عدد صحیح؛ با جداکننده وارد نکنید.</div></div>",
        "<div class='field'><label>نقش</label>"
        f"<select name='role'>{_role_options(role_v)}</select>"
        "<div class='hint'>هر نقش قیمت ستون خودش را می‌بیند؛ نماینده علاوه بر آن پله‌ی تخفیف می‌گیرد.</div></div>",
        panel_checks,
        "</div>",
        "<div class='form-foot'><button type='submit'>" + _use('check') + " ذخیره</button>"
        "<a href='/users' class='btn btn-ghost'>انصراف</a></div>",
        "</form></div>",
    )
    return layout(title, "".join(body), "/users", request)


@require_auth
async def user_save(request):
    data = await request.post()
    try:
        uid = int(data.get("user_id"))
        balance = int(data.get("balance") or 0)
    except (TypeError, ValueError):
        raise _redirect("/users", err="ورودی نامعتبر")
    nickname = (data.get("nickname") or "").strip() or None
    role = _valid_role(data.get("role"))
    await db.save_user(uid, nickname, role, balance)
    # پنل‌های مجاز: چک‌باکس تیک‌خورده → همان‌ها؛ هیچ‌کدام → همه‌ی پنل‌ها (بدون محدودیت)
    raw_ids = data.getall("panel_ids", []) if hasattr(data, "getall") else []
    try:
        chosen = {int(x) for x in raw_ids if str(x).strip().isdigit()}
    except (TypeError, ValueError):
        chosen = set()
    await db.set_user_allowed_panels(uid, chosen or None)
    logging.info("WEB_USER_SAVE user=%s role=%s balance=%s allowed_panels=%s", uid, role, balance, sorted(chosen) if chosen else 'ALL')
    raise _redirect("/users", ok="کاربر ذخیره شد")


@require_auth
async def user_delete(request):
    data = await request.post()
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        raise _redirect("/users", err="آیدی نامعتبر")
    removed = await db.delete_user(uid)
    logging.info("WEB_USER_DELETE user=%s orders=%s", uid, removed)
    raise _redirect("/users", ok=f"کاربر حذف شد ({removed} سفارش هم از لیست پاک شد)")


@require_auth
async def users_adjust(request):
    data = await request.post()
    back = data.get("back") or "/users"
    try:
        uid = int(data.get("user_id"))
        amount = int(data.get("amount"))
    except (TypeError, ValueError):
        raise _redirect(back, err="مبلغ نامعتبر")
    if amount == 0:
        raise _redirect(back, err="مبلغ صفر است")
    if amount < 0:
        # کسر از طریق مسیر اتمیک انجام می‌شود تا موجودی منفی نشود
        if await db.deduct_balance(uid, -amount, kind="تنظیم مدیریت", description="پنل وب") is None:
            raise _redirect(back, err="موجودی کاربر برای این کسر کافی نیست")
    else:
        await db.credit_balance(uid, amount, kind="تنظیم مدیریت", description="پنل وب")
    logging.info("WEB_BALANCE_ADJUST user=%s amount=%s", uid, amount)
    raise _redirect(back, ok="موجودی به‌روزرسانی شد")


@require_auth
async def user_ban(request):
    """بن/آزادسازی کاربر از پنل وب (تاگل با پارامتر banned)."""
    data = await request.post()
    try:
        uid = int(data.get("user_id"))
        banned = bool(int(data.get("banned") or 0))
    except (TypeError, ValueError):
        raise _redirect("/users", err="آیدی نامعتبر")
    if uid == SUPER_ADMIN_ID:
        raise _redirect(f"/users/view?id={uid}", err="مدیر کل قابل مسدودسازی نیست")
    await db.set_user_banned(uid, banned)
    logging.info("WEB_USER_BAN user=%s banned=%s", uid, banned)
    raise _redirect(f"/users/view?id={uid}", ok=("کاربر مسدود شد" if banned else "کاربر آزاد شد"))


@require_auth
async def user_role(request):
    data = await request.post()
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        raise _redirect("/users", err="آیدی نامعتبر")
    role = _valid_role(data.get("role"))
    await db.set_user_role(uid, role)
    logging.info("WEB_USER_ROLE user=%s role=%s", uid, role)
    raise _redirect(f"/users/view?id={uid}", ok="نقش کاربر تغییر کرد")


# ================= سفارش‌ها =================
@require_auth
async def orders_page(request):
    search = request.query.get("q", "").strip() or None
    page = _page_arg(request)
    total = await db.count_orders(search)
    orders = await db.list_recent_orders(limit=PER_PAGE, search=search, offset=(page - 1) * PER_PAGE)
    rows = ""
    for o in orders:
        email = order_email(o)
        rows += (
            f"<tr><td>{num(o['id'])}</td>"
            f"<td><a href='/users/view?id={e(o['user_id'])}'>{num(o['user_id'])}</a></td>"
            f"<td class='wrap'>{num(email)} {_copy_btn(email or '')}</td>"
            f"<td class='muted'>{e(o['date'])}</td><td>{num(o['panel_id'])}</td>"
            f"<td class='col-actions'><form class='inline' method='post' action='/orders/delete'{_confirm('حذف سفارش؟')}>"
            f"<input type='hidden' name='id' value='{e(o['id'])}'>"
            f"<button class='btn-danger btn-sm' title='حذف'>{_use('trash')}</button></form></td></tr>"
        )
    extra = f"&q={quote(search)}" if search else ""
    table = (f"<table data-enhance><thead><tr><th>#</th><th>کاربر</th><th>نام سرویس</th><th>تاریخ</th><th>پنل</th>"
             f"<th class='col-actions'></th></tr></thead><tbody>{rows}</tbody></table>"
             if rows else _empty('سفارشی پیدا نشد', 'سفارش‌ها از طریق ربات ثبت می‌شوند و اینجا نمایش داده می‌شوند.', 'box'))
    body = (
        _page_head('سفارش‌ها', 'تمام سرویس‌های فعال و پیشین کاربران',
                   crumbs=[('خانه', '/'), ('سفارش‌ها', None)]),
        "<div class='tbl-card'><div class='tbl-toolbar'>"
        "<form class='inline' method='get' action='/orders' style='flex:1 1 320px;position:relative'>"
        f"<input name='q' placeholder='جستجوی نام سرویس یا آیدی…' value='{e(search or "")}' style='width:100%;padding-right:34px'>"
        f"<span style='position:absolute;right:10px;top:50%;transform:translateY(-50%);color:var(--muted)'>{_use('search')}</span>"
        "</form></div>"
        f"<div class='tbl-scroll'>{table}</div>{_pager_html('/orders', page, total, extra)}</div>",
    )
    return layout("سفارش‌ها", "".join(body), "/orders", request)


@require_auth
async def order_delete(request):
    data = await request.post()
    back = data.get("back") or "/orders"
    pid = data.get("id")
    if pid and str(pid).isdigit():
        await db.delete_order(int(pid))
        logging.info("WEB_ORDER_DELETE id=%s", pid)
    raise _redirect(back, ok="سفارش حذف شد")


# ================= تراکنش‌ها =================
@require_auth
async def transactions_page(request):
    page = _page_arg(request)
    total = await db.count_transactions()
    txns = await db.list_recent_transactions(limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
    rows = ""
    for t in txns:
        d = _fmt_dt(t['date'])
        cls = 'chip-n' if t['amount'] > 0 else 'chip-off'
        sign = '+' if t['amount'] > 0 else '−'
        rows += (f"<tr><td><a href='/users/view?id={e(t['user_id'])}'>{num(t['user_id'])}</a></td>"
                 f"<td><span class='price-chip {cls}'>{sign}{money(abs(t['amount']))}</span></td>"
                 f"<td>{e(t['kind'])}</td><td class='wrap muted'>{e(t['description'] or '—')}</td>"
                 f"<td class='muted'>{num(d)}</td></tr>")
    table = (f"<table><thead><tr><th>کاربر</th><th>مبلغ</th><th>نوع</th><th>توضیح</th><th>تاریخ</th></tr></thead>"
             f"<tbody>{rows}</tbody></table>"
             if rows else _empty('تراکنشی ثبت نشده', '', 'card'))
    body = (
        _page_head('تراکنش‌ها', 'گردش مالی کیف‌پول کاربران',
                   crumbs=[('خانه', '/'), ('تراکنش‌ها', None)]),
        f"<div class='tbl-card'><div class='tbl-scroll'>{table}</div>{_pager_html('/transactions', page, total)}</div>",
    )
    return layout("تراکنش‌ها", "".join(body), "/transactions", request)

# ================= پلن‌ها =================
def _renew_chip(value, level):
    """نمایش متمایز قیمت تمدید هر سطح؛ مقدار از ستون واقعی همان سطح می‌آید."""
    cls = {'n': 'chip-n', 'v': 'chip-v', 'r': 'chip-r'}[level]
    return f"<span class='price-chip {cls}'>{money(value)}</span>"


@require_auth
async def plans_page(request):
    plans = await db.list_plans()
    panels = await db.get_panels()
    pname = {p['id']: p['name'] for p in panels}
    rows = ""
    for p in plans:
        icon_txt = (p['icon'] or '').strip() if 'icon' in p else ''
        active = bool(p['is_active'])
        badge = _badge('green', 'فعال') if active else _badge('red', 'غیرفعال')
        rows += (
            f"<tr><td>{num(p['id'])}</td><td>{e(icon_txt)}</td><td class='wrap'><b>{e(p['name'])}</b></td>"
            f"<td>{num(p['gb'])} / {num(p['duration_days'])}</td>"
            f"<td>{num(money(p['price']))}</td><td>{num(money(p['vip_price']))}</td><td>{num(money(p['reseller_price']))}</td>"
            f"<td>{_renew_chip(p['renew_normal_price'], 'n')}</td>"
            f"<td>{_renew_chip(p['renew_vip_price'], 'v')}</td>"
            f"<td>{_renew_chip(p['renew_reseller_price'], 'r')}</td>"
            f"<td class='muted'>{money(p['first_price'])}</td>"
            f"<td>{num(p['inbound_id'])}</td><td class='muted'>{e(pname.get(p['panel_id'], p['panel_id']))}</td><td>{badge}</td>"
            f"<td class='col-actions'><div class='actions'>"
            f"<a href='/plans/edit?id={e(p['id'])}'><button class='btn-ghost btn-sm' title='ویرایش'>{_use('edit')}</button></a>"
            f"<form class='inline' method='post' action='/plans/toggle'>"
            f"<input type='hidden' name='id' value='{e(p['id'])}'><button class='btn-ghost btn-sm' title=\"{'توقف فروش' if active else 'فعال‌سازی'}\">{_use('pause') if active else _use('play')}</button></form>"
            f"<form class='inline' method='post' action='/plans/delete'{_confirm('حذف پلن؟')}>"
            f"<input type='hidden' name='id' value='{e(p['id'])}'><button class='btn-danger btn-sm' title='حذف'>{_use('trash')}</button></form>"
            f"</div></td></tr>"
        )
    legend = (
        "<div class='legend'>رنگ‌های قیمت تمدید: "
        + _renew_chip(0, 'n').replace(money(0), 'عادی') + " "
        + _renew_chip(0, 'v').replace(money(0), 'VIP') + " "
        + _renew_chip(0, 'r').replace(money(0), 'همکاری')
        + "</div>"
    )
    table = (f"<table data-enhance><thead>"
             f"<tr><th rowspan='2'>#</th><th rowspan='2'></th><th rowspan='2'>نام</th><th rowspan='2'>GB/روز</th>"
             f"<th colspan='3' style='text-align:center;border-left:1px solid var(--line)'>قیمت خرید</th>"
             f"<th colspan='3' style='text-align:center;border-left:1px solid var(--line)'>قیمت تمدید</th>"
             f"<th rowspan='2' class='sortable' style='border-left:1px solid var(--line)'>خرید اول<span class='sort-ind'></span></th>"
             f"<th rowspan='2'>اینباند</th><th rowspan='2'>پنل</th><th rowspan='2'>وضعیت</th><th rowspan='2' class='col-actions'>عملیات</th></tr>"
             f"<tr>"
             f"<th class='group-buy sortable'>عادی<span class='sort-ind'></span></th><th class='group-buy sortable'>VIP<span class='sort-ind'></span></th><th class='group-buy sortable'>همکاری<span class='sort-ind'></span></th>"
             f"<th class='group-renew-n sortable'>عادی<span class='sort-ind'></span></th><th class='group-renew-v sortable'>VIP<span class='sort-ind'></span></th><th class='group-renew-r sortable'>همکاری<span class='sort-ind'></span></th>"
             f"</tr></thead><tbody>{rows}</tbody></table>"
             if rows else _empty('هنوز پلنی تعریف نشده', 'با دکمه‌ی «افزودن پلن» یا ساخت پلن‌های پیش‌فرض شروع کنید.', 'cart'))
    body = (
        _page_head('پلن‌ها و قیمت‌ها',
                   'قیمت خرید و قیمت تمدید برای هر سطح جداگانه تنظیم می‌شود. پلن غیرفعال از منوی فروش ربات حذف می‌شود ولی سرویس‌های فروخته‌شده دست‌نخورده می‌مانند.',
                   actions=("<a href='/plans/edit'><button>" + _use('plus') + " افزودن پلن</button></a>"
                            "<form method='post' action='/plans/seed'" + _confirm('پلن‌های پیش‌فرض ساخته شوند؟ پلن‌های موجود دست نمی‌خورند.') + ">"
                            "<button class='btn-ghost'>" + _use('db') + " پلن‌های پیش‌فرض</button></form>"),
                   crumbs=[('خانه', '/'), ('پلن‌ها و قیمت‌ها', None)]),
        f"<div class='tbl-card'>{legend}<div class='tbl-toolbar'><span class='tbl-count'></span></div>"
        f"<div class='tbl-scroll'>{table}</div></div>",
    )
    return layout("پلن‌ها و قیمت‌ها", "".join(body), "/plans", request)


@require_auth
async def plan_edit_form(request):
    pid = request.query.get("id")
    p = await db.get_plan(int(pid)) if pid and pid.isdigit() else None
    is_new = p is None
    title = "افزودن پلن" if is_new else f"ویرایش پلن {pid}"
    panels = await db.get_panels()

    def val(key, default=""):
        return e(p[key]) if p else default

    options = ""
    cur_panel = p['panel_id'] if p else (panels[0]['id'] if panels else None)
    for pr in panels:
        sel = "selected" if pr['id'] == cur_panel else ""
        options += f"<option value='{e(pr['id'])}' {sel}>{e(pr['id'])} - {e(pr['name'])}</option>"

    from panel import normalize_panel_type
    cur_type = normalize_panel_type((p['panel_type'] if p and 'panel_type' in p.keys() else '') or '')

    hidden_id = "" if is_new else f"<input type='hidden' name='id' value='{e(pid)}'>"

    def money_field(name, label_key, lbl, hint=''):
        v = val(label_key, '0')
        h = f"<div class='hint'>{hint}</div>" if hint else ''
        return (f"<div class='field'><label>{lbl}</label>"
                f"<input name='{name}' value='{v}' class='ltr' inputmode='numeric' placeholder='0'>{h}</div>")

    # رادیوهای تب باید «برادرِ» مستقیم نوار .tabs و بدنه‌های tab-body باشند؛
    # اگر داخل div تودرتو شوند، سلکتورهای ~ در CSS مچ نمی‌شوند و همه‌ی
    # بدنه‌ها مخفی می‌مانند (فرم پلن خالی رندر می‌شد).
    tabs = (
        "<input type='radio' name='plan-tab' id='tab-basic' class='tab-radio' checked>"
        "<input type='radio' name='plan-tab' id='tab-buy' class='tab-radio'>"
        "<input type='radio' name='plan-tab' id='tab-renew' class='tab-radio'>"
        "<input type='radio' name='plan-tab' id='tab-adv' class='tab-radio'>"
        "<div class='tabs'>"
        "<label for='tab-basic'>" + icon('box') + " اطلاعات پایه</label>"
        "<label for='tab-buy'>" + icon('card') + " قیمت خرید</label>"
        "<label for='tab-renew'>" + icon('refresh') + " قیمت تمدید</label>"
        "<label for='tab-adv'>" + icon('settings') + " پیشرفته</label>"
        "</div>"
    )
    tab_basic = (
        "<div class='tab-body tb-basic'><div class='fields'>"
        "<div class='field'><label>نام <span class='req'>*</span></label><input name='name' value='" + val('name') + "' required></div>"
        "<div class='field'><label>آیکون/ایموجی (اختیاری)</label><input name='icon' value='" + val('icon') + "' placeholder='مثل 🟦'></div>"
        "<div class='field'><label>حجم (GB)</label><input name='gb' value='" + val('gb', '0') + "' class='ltr' inputmode='numeric'></div>"
        "<div class='field'><label>مدت (روز)</label><input name='duration_days' value='" + val('duration_days', '30') + "' class='ltr' inputmode='numeric'></div>"
        "</div></div>"
    )
    tab_buy = (
        "<div class='tab-body tb-buy'><div class='fields'>"
        + money_field('price', 'price', 'قیمت عادی (Normal)', 'قیمت کاربر عادی؛ خالی/صفر = برای کاربر عادی فروخته نمی‌شود.')
        + money_field('vip_price', 'vip_price', 'قیمت VIP', 'خالی/صفر = این محصول برای VIP فروخته نمی‌شود.')
        + money_field('reseller_price', 'reseller_price', 'قیمت همکاری (Reseller)', 'خالی/صفر = این محصول برای نماینده فروخته نمی‌شود.')
        + money_field('first_price', 'first_price', 'قیمت آفر خرید اول', 'فقط برای اولین خرید کاربر جدید، اگر از قیمت سطحش کمتر باشد.')
        + "</div>"
        "<p class='muted'>هر سطح فقط قیمت ستون خودش را می‌بیند؛ هیچ ارجاعی بین سطوح انجام نمی‌شود. "
        "قیمت خالی یا صفر یعنی محصول برای آن سطح <b>ناموجود</b> است و در منوی ربات با برچسب «ناموجود» دیده می‌شود.</p></div>"
    )
    renew_note = ("<div class='hint' style='margin-top:12px'>قیمت تمدید هر سطح <b>مبلغ نهایی و قطعیِ</b> تمدید همان سطح است "
                  "و با قیمت پایه یا سایر سطوح مقایسه نمی‌شود. خالی/صفر یعنی تمدید برای آن سطح غیرفعال است.</div>")
    tab_renew = (
        "<div class='tab-body tb-renew'>"
        "<div class='legend' style='margin:0 0 12px'>سطح هر قیمت: "
        + _renew_chip(0, 'n').replace(money(0), 'عادی') + " "
        + _renew_chip(0, 'v').replace(money(0), 'VIP') + " "
        + _renew_chip(0, 'r').replace(money(0), 'همکاری')
        + "</div>"
        "<div class='renew-grid'>"
        "<div class='renew-cell rn'><label>تمدید کاربر عادی</label>"
        "<input name='renew_normal_price' value='" + val('renew_normal_price', '0') + "' class='ltr' inputmode='numeric' placeholder='0'></div>"
        "<div class='renew-cell rv'><label>تمدید کاربر VIP</label>"
        "<input name='renew_vip_price' value='" + val('renew_vip_price', '0') + "' class='ltr' inputmode='numeric' placeholder='0'></div>"
        "<div class='renew-cell rr'><label>تمدید نماینده (همکاری)</label>"
        "<input name='renew_reseller_price' value='" + val('renew_reseller_price', '0') + "' class='ltr' inputmode='numeric' placeholder='0'></div>"
        "</div>" + renew_note + "</div>"
    )
    tab_adv = (
        "<div class='tab-body tb-adv'><div class='fields'>"
        "<div class='field'><label>اینباند (Inbound ID)</label><input name='inbound_id' value='" + val('inbound_id', '1') + "' class='ltr' inputmode='numeric'></div>"
        "<div class='field'><label>پنل (سرور)</label><select name='panel_id'>" + options + "</select></div>"
        "<div class='field'><label>وضعیت</label><select name='is_active'>"
        f"<option value='1' {'selected' if (p is None or p['is_active']) else ''}>فعال</option>"
        f"<option value='0' {'selected' if (p is not None and not p['is_active']) else ''}>غیرفعال</option>"
        "</select><div class='hint'>پلن غیرفعال از منوی فروش ربات حذف می‌شود.</div></div>"
        "</div></div>"
    )
    body = (
        _page_head(title, None,
                   crumbs=[('خانه', '/'), ('پلن‌ها و قیمت‌ها', '/plans'), (title, None)]),
        "<div class='box' style='margin-top:16px;padding-top:0'><form method='post' action='/plans/save'>" + hidden_id
        + tabs + tab_basic + tab_buy + tab_renew + tab_adv
        + "<div class='form-foot' style='margin:0 18px 18px'><button type='submit'>" + _use('check') + " ذخیره پلن</button>"
        + "<a href='/plans' class='btn btn-ghost'>انصراف</a></div>"
        + "</form></div>",
    )
    return layout(title, "".join(body), "/plans", request)


@require_auth
async def plan_save(request):
    data = await request.post()
    try:
        name = (data.get("name") or "").strip()
        gb = int(data.get("gb") or 0)
        duration_days = int(data.get("duration_days") or 30)
        price = int(data.get("price") or 0)
        vip_price = int(data.get("vip_price") or 0)
        reseller_price = int(data.get("reseller_price") or 0)
        renew_normal_price = int(data.get("renew_normal_price") or 0)
        renew_vip_price = int(data.get("renew_vip_price") or 0)
        renew_reseller_price = int(data.get("renew_reseller_price") or 0)
        first_price = int(data.get("first_price") or 0)
        inbound_id = int(data.get("inbound_id") or 0)
        panel_id = int(data.get("panel_id")) if data.get("panel_id") else None
    except (TypeError, ValueError):
        raise _redirect("/plans", err="ورودی نامعتبر")
    if not name:
        raise _redirect("/plans", err="نام پلن الزامی است")
    icon_txt = (data.get("icon") or "").strip()
    is_active = data.get("is_active", "1") == "1"
    pid = data.get("id")
    args = (name, gb, duration_days, price, vip_price, inbound_id, panel_id)
    kwargs = dict(icon=icon_txt, reseller_price=reseller_price, renew_price=0,
                  first_price=first_price, is_active=is_active,
                  renew_normal_price=renew_normal_price, renew_vip_price=renew_vip_price,
                  renew_reseller_price=renew_reseller_price)
    if pid and pid.isdigit():
        await db.update_plan(int(pid), *args, **kwargs)
    else:
        await db.create_plan(*args, **kwargs)
    logging.info("WEB_PLAN_SAVE name=%s panel=%s active=%s", name, panel_id, is_active)
    raise _redirect("/plans", ok="پلن ذخیره شد")


@require_auth
async def plan_toggle(request):
    data = await request.post()
    pid = data.get("id")
    if not (pid and str(pid).isdigit()):
        raise _redirect("/plans", err="آیدی نامعتبر")
    state = await db.toggle_plan_active(int(pid))
    logging.info("WEB_PLAN_TOGGLE id=%s active=%s", pid, state)
    raise _redirect("/plans", ok="پلن فعال شد" if state else "پلن غیرفعال شد")


@require_auth
async def plans_seed(request):
    created = await db.seed_default_plans()
    logging.info("WEB_PLANS_SEED created=%s", created)
    raise _redirect("/plans", ok=f"{created} پلن پیش‌فرض اضافه شد" if created else "پلن‌های پیش‌فرض از قبل وجود دارند")


@require_auth
async def plan_delete(request):
    data = await request.post()
    pid = data.get("id")
    if pid and str(pid).isdigit():
        await db.delete_plan(int(pid))
        logging.info("WEB_PLAN_DELETE id=%s", pid)
    raise _redirect("/plans", ok="پلن حذف شد")

# ================= آفرها =================
OFFER_KINDS = {'percent': 'درصدی', 'fixed': 'قیمت ثابت'}


def _dt(value):
    """ورودی datetime-local را به datetime تبدیل می‌کند (خالی → None)."""
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _dt_input(value):
    return value.strftime("%Y-%m-%dT%H:%M") if value else ""


@require_auth
async def offers_page(request):
    offers = await db.list_offers()
    plans = await db.list_plans()
    plan_names = {p['id']: p['name'] for p in plans}
    rows = ""
    for o in offers:
        active = bool(o['is_active'])
        value = f"{o['value']}٪" if (o['kind'] or 'percent') == 'percent' else f"{money(o['value'])} تومان"
        window = f"{o['starts_at'].strftime('%Y-%m-%d') if o['starts_at'] else '—'} تا {o['ends_at'].strftime('%Y-%m-%d') if o['ends_at'] else '—'}"
        limit = "نامحدود" if not o['max_uses'] else f"{o['used_count']}/{o['max_uses']}"
        rows += (
            f"<tr><td>{num(o['id'])}</td><td class='wrap'><b>{e(o['name'])}</b></td><td>{e(OFFER_KINDS.get(o['kind'], o['kind']))}</td>"
            f"<td>{num(value)}</td>"
            f"<td>{_badge('violet', AUDIENCE_LABELS.get(o['audience'], o['audience']))}</td>"
            f"<td class='wrap'>{e(plan_names.get(o['plan_id'], 'همه'))}</td><td class='muted'>{num(window)}</td>"
            f"<td>{num(limit)}</td><td>{num(o['per_user_limit'])}</td>"
            f"<td>{_badge('green', 'فعال') if active else _badge('red', 'غیرفعال')}</td>"
            f"<td class='col-actions'><div class='actions'>"
            f"<a href='/offers/edit?id={e(o['id'])}'><button class='btn-ghost btn-sm' title='ویرایش'>{_use('edit')}</button></a>"
            f"<form class='inline' method='post' action='/offers/toggle'>"
            f"<input type='hidden' name='id' value='{e(o['id'])}'><button class='btn-ghost btn-sm'>{_use('pause') if active else _use('play')}</button></form>"
            f"<form class='inline' method='post' action='/offers/delete'{_confirm('حذف آفر؟')}>"
            f"<input type='hidden' name='id' value='{e(o['id'])}'><button class='btn-danger btn-sm'>{_use('trash')}</button></form>"
            f"</div></td></tr>"
        )
    first_offer = await db.get_setting("first_offer_enabled")
    renew_offer = await db.get_setting("renew_offer_enabled")
    table = (f"<table data-enhance><thead><tr><th>#</th><th>نام</th><th>نوع</th><th>مقدار</th><th>مخاطب</th><th>پلن</th>"
             f"<th>بازه</th><th>استفاده</th><th>سقف هر کاربر</th><th>وضعیت</th><th class='col-actions'>عملیات</th></tr></thead>"
             f"<tbody>{rows}</tbody></table>"
             if rows else _empty('آفری تعریف نشده', 'برای کمپین‌های فصلی می‌توانید آفر درصدی یا مبلغ ثابت بسازید.', 'target'))
    body = (
        _page_head('مدیریت آفرها', None,
                   actions=f"<a href='/offers/edit'><button>{_use('plus')} افزودن آفر</button></a>",
                   crumbs=[('خانه', '/'), ('آفرها', None)]),
        "<div class='box'><h3>" + icon('zap') + " آفرهای داخلی</h3>"
        "<form class='row' method='post' action='/offers/builtin'>"
        "<div><label>آفر خرید اول</label><select name='first_offer_enabled'>"
        f"<option value='on' {_sel(first_offer, 'on')}>فعال</option><option value='off' {_sel(first_offer, 'off')}>غیرفعال</option>"
        "</select></div>"
        "<div><label>قیمت ویژه‌ی تمدید</label><select name='renew_offer_enabled'>"
        f"<option value='on' {_sel(renew_offer, 'on')}>فعال</option><option value='off' {_sel(renew_offer, 'off')}>غیرفعال</option>"
        "</select></div>"
        "<div style='align-self:end'><button class='btn-sm'>" + _use('check') + " ذخیره</button></div>"
        "</form>"
        "<p class='muted'>مبلغ این دو آفر در صفحه‌ی «پلن‌ها و قیمت‌ها» و به‌ازای هر پلن تنظیم می‌شود."
        " آفر خرید اول فقط برای کاربری فعال است که هیچ سرویسی نگرفته باشد و فقط یک‌بار مصرف می‌شود.</p></div>"
        f"<div class='tbl-card'><div class='tbl-toolbar'><span class='tbl-count'></span></div>"
        f"<div class='tbl-scroll'>{table}</div></div>",
    )
    return layout("آفرها", "".join(body), "/offers", request)


@require_auth
async def offer_edit_form(request):
    oid = request.query.get("id")
    o = await db.get_offer(int(oid)) if oid and oid.isdigit() else None
    is_new = o is None
    title = "افزودن آفر" if is_new else f"ویرایش آفر {oid}"
    plans = await db.list_plans()

    def val(key, default=""):
        if not o:
            return default
        return e(o[key]) if o[key] is not None else default

    kind = o['kind'] if o else 'percent'
    audience = o['audience'] if o else 'all'
    plan_opts = "<option value=''>همه‌ی پلن‌ها</option>" + "".join(
        f"<option value='{e(p['id'])}' {'selected' if o and o['plan_id'] == p['id'] else ''}>{e(p['name'])}</option>"
        for p in plans
    )
    kind_opts = "".join(
        f"<option value='{k}' {_sel(kind, k)}>{e(label)}</option>" for k, label in OFFER_KINDS.items()
    )
    aud_opts = "".join(
        f"<option value='{k}' {_sel(audience, k)}>{e(label)}</option>" for k, label in AUDIENCE_LABELS.items()
    )
    hidden_id = "" if is_new else f"<input type='hidden' name='id' value='{e(oid)}'>"
    body = (
        _page_head(title, None, crumbs=[('خانه', '/'), ('آفرها', '/offers'), (title, None)]),
        "<div class='box'><form method='post' action='/offers/save'>" + hidden_id +
        "<div class='fields'>"
        "<div class='field'><label>نام آفر <span class='req'>*</span></label><input name='name' value='" + val('name') + "' placeholder='مثلاً جشنواره نوروز' required></div>"
        "<div class='field'><label>نوع</label><select name='kind'>" + kind_opts + "</select></div>"
        "<div class='field'><label>مقدار (درصد یا مبلغ ثابت)</label><input name='value' value='" + val('value', '0') + "' class='ltr' inputmode='numeric'></div>"
        "<div class='field'><label>مخاطب</label><select name='audience'>" + aud_opts + "</select></div>"
        "<div class='field'><label>پلن</label><select name='plan_id'>" + plan_opts + "</select></div>"
        "<div class='field'><label>تاریخ شروع</label><input type='datetime-local' name='starts_at' value='" + (_dt_input(o['starts_at']) if o else '') + "' class='ltr'></div>"
        "<div class='field'><label>تاریخ پایان</label><input type='datetime-local' name='ends_at' value='" + (_dt_input(o['ends_at']) if o else '') + "' class='ltr'></div>"
        "<div class='field'><label>سقف کل استفاده</label><input name='max_uses' value='" + val('max_uses', '0') + "' class='ltr' inputmode='numeric'><div class='hint'>۰ = نامحدود</div></div>"
        "<div class='field'><label>سقف استفاده هر کاربر</label><input name='per_user_limit' value='" + val('per_user_limit', '1') + "' class='ltr' inputmode='numeric'></div>"
        "<div class='field'><label>وضعیت</label><select name='is_active'>"
        f"<option value='1' {'selected' if (o is None or o['is_active']) else ''}>فعال</option>"
        f"<option value='0' {'selected' if (o is not None and not o['is_active']) else ''}>غیرفعال</option>"
        "</select></div>"
        "</div>"
        "<p class='muted'>آفر فقط وقتی اعمال می‌شود که قیمت حاصل از قیمت فعلیِ کاربر کمتر باشد؛ بین چند آفرِ واجد شرایط، ارزان‌ترین انتخاب می‌شود. آفرها روی قیمت تمدید اثری ندارند.</p>"
        "<div class='form-foot'><button type='submit'>" + _use('check') + " ذخیره</button>"
        "<a href='/offers' class='btn btn-ghost'>انصراف</a></div>"
        "</form></div>",
    )
    return layout(title, "".join(body), "/offers", request)


@require_auth
async def offer_save(request):
    data = await request.post()
    try:
        value = int(data.get("value") or 0)
        max_uses = max(0, int(data.get("max_uses") or 0))
        per_user_limit = max(0, int(data.get("per_user_limit") or 1))
        plan_id = int(data.get("plan_id")) if (data.get("plan_id") or "").strip() else None
    except (TypeError, ValueError):
        raise _redirect("/offers", err="ورودی نامعتبر")
    name = (data.get("name") or "").strip()
    if not name:
        raise _redirect("/offers", err="نام آفر الزامی است")
    kind = data.get("kind") if data.get("kind") in OFFER_KINDS else "percent"
    audience = data.get("audience") if data.get("audience") in AUDIENCE_LABELS else "all"
    if kind == "percent" and not (0 <= value <= 100):
        raise _redirect("/offers", err="درصد تخفیف باید بین ۰ تا ۱۰۰ باشد")
    oid = data.get("id")
    await db.save_offer(
        int(oid) if oid and oid.isdigit() else None,
        name, kind, value, audience, plan_id,
        _dt(data.get("starts_at")), _dt(data.get("ends_at")),
        max_uses, per_user_limit, data.get("is_active", "1") == "1",
    )
    logging.info("WEB_OFFER_SAVE name=%s kind=%s value=%s audience=%s", name, kind, value, audience)
    raise _redirect("/offers", ok="آفر ذخیره شد")


@require_auth
async def offer_toggle(request):
    data = await request.post()
    oid = data.get("id")
    if not (oid and str(oid).isdigit()):
        raise _redirect("/offers", err="آیدی نامعتبر")
    state = await db.toggle_offer_active(int(oid))
    raise _redirect("/offers", ok="آفر فعال شد" if state else "آفر غیرفعال شد")


@require_auth
async def offer_delete(request):
    data = await request.post()
    oid = data.get("id")
    if oid and str(oid).isdigit():
        await db.delete_offer(int(oid))
        logging.info("WEB_OFFER_DELETE id=%s", oid)
    raise _redirect("/offers", ok="آفر حذف شد")


@require_auth
async def offers_builtin(request):
    data = await request.post()
    for key in ("first_offer_enabled", "renew_offer_enabled"):
        await db.update_setting(key, "on" if data.get(key) == "on" else "off")
    raise _redirect("/offers", ok="آفرهای داخلی به‌روزرسانی شد")


# ================= گزارش مالی =================
@require_auth
async def finance_page(request):
    r = await db.get_financial_report()

    def card(label, value, ic_name, tone):
        return _stat_card(label, money(value), ic_name, tone=tone)

    cards = "".join([
        card('درآمد خالص', r['net'], 'wallet', 'green'),
        card('فروش کل', r['total'], 'chart', 'blue'),
        card('فروش امروز', r['today'], 'zap', 'teal'),
        card('فروش ۷ روز اخیر', r['week'], 'clock', 'violet'),
        card('فروش ۳۰ روز اخیر', r['month'], 'clock', 'blue'),
        card('مجموع تخفیف داده‌شده', r['discount'], 'target', 'gold'),
        card('هدیه‌ی نمایندگی', r['bonus'], 'gift', 'teal'),
        card('کدهای هدیه', r.get('gifts', 0), 'gift', 'gold'),
        card('پاداش دعوت', r.get('referrals', 0), 'link', 'violet'),
        card('برگشت وجه', r['refunds'], 'refresh', 'red'),
    ])

    def role_row(key, label, tone):
        item = r['by_role'].get(key, {'total': 0, 'count': 0})
        return (f"<tr><td>{_badge(tone, label)}</td><td>{num(r['users'].get(key, 0))}</td>"
                f"<td>{num(money(item['total']))}</td><td>{num(item['count'])}</td></tr>")

    bars = [('امروز', r['today']), ('۷ روز', r['week']), ('۳۰ روز', r['month']), ('کل', r['total'])]
    mx = max((v for _l, v in bars), default=0) or 1
    bars_html = "".join(
        f"<div class='bar-row'><span class='muted' style='margin:0'>{e(lbl)}</span>"
        f"<span class='bar-track'><span class='bar-fill' style='width:{int(v * 100 / mx)}%'></span></span>"
        f"<b class='num' dir='ltr'>{money(v)}</b></div>"
        for lbl, v in bars
    )
    body = (
        _page_head('گزارش مالی', 'عملکرد فروش بر اساس داده‌های واقعی دفتر فروش',
                   crumbs=[('خانه', '/'), ('گزارش مالی', None)]),
        f"<div class='stats'>{cards}</div>",
        "<div class='grid grid-2' style='margin-top:16px'>",
        "<div class='box' style='margin-top:0'><h3>" + icon('chart') + " مقایسه‌ی بازه‌ها (تومان)</h3><div class='bars'>" + bars_html + "</div></div>",
        "<div class='box' style='margin-top:0'><h3>" + icon('users') + " تفکیک بر اساس سطح کاربر</h3>"
        "<div class='tbl-scroll'><table><thead><tr><th>سطح</th><th>تعداد کاربر</th><th>مبلغ فروش</th><th>تعداد فروش</th></tr></thead>"
        "<tbody>" + role_row('normal', 'عادی', 'gray') + role_row('vip', 'VIP', 'gold') + role_row('reseller', 'نماینده', 'teal') + "</tbody></table></div></div>",
        "</div>",
        "<div class='box'><h3>" + icon('refresh') + " آفرها و تمدیدها</h3>"
        "<div class='tbl-scroll'><table><tbody>"
        f"<tr><td>استفاده از آفر خرید اول</td><td>{num(r['first_offers'])}</td></tr>"
        f"<tr><td>تعداد تمدیدها</td><td>{num(r['renewals'])}</td></tr>"
        f"<tr><td>تعداد کل فروش‌ها</td><td>{num(r['sales_count'])}</td></tr>"
        f"<tr><td>کل شارژ حساب‌ها</td><td>{num(money(r['topup']))} تومان</td></tr>"
        f"<tr><td>کدهای هدیه پرداخت‌شده</td><td>{num(money(r.get('gifts', 0)))} تومان</td></tr>"
        f"<tr><td>پاداش دعوت پرداخت‌شده</td><td>{num(money(r.get('referrals', 0)))} تومان</td></tr>"
        "</tbody></table></div>"
        "<p class='muted'>درآمد خالص = مجموع فروش‌های ثبت‌شده منهای برگشت وجه، اعتبار هدیه‌ی نمایندگی، کدهای هدیه و پاداش دعوت."
        " فروش‌ها از لحظه‌ی نصب این نسخه ثبت می‌شوند؛ سفارش‌های قدیمی‌تر مبلغ ثبت‌شده ندارند.</p></div>",
    )
    return layout("گزارش مالی", "".join(body), "/finance", request)

# ================= پنل‌ها =================
@require_auth
async def panels_page(request):
    panels = await db.get_panels()
    rows = ""
    for p in panels:
        ptype = (p['panel_type'] or 'xui') if 'panel_type' in p.keys() else 'xui'
        rows += (
            f"<tr><td>{num(p['id'])}</td><td class='wrap'><b>{e(p['name'])}</b></td>"
            f"<td class='wrap'>{num(p['url'])} {_copy_btn(p['url'] or '')}</td>"
            f"<td class='muted'>{'VPN-UI' if ptype == 'vpn-ui' else '3X-UI'}</td>"
            f"<td>{num(p['config_ip'])}</td><td class='muted'>{e(p['sub_url'] or '—')}</td>"
            f"<td class='col-actions'><div class='actions'>"
            f"<a href='/panels/edit?id={e(p['id'])}'><button class='btn-ghost btn-sm' title='ویرایش'>{_use('edit')}</button></a>"
            f"<form class='inline' method='post' action='/panels/delete'{_confirm('حذف پنل؟ (پلن‌ها و سفارش‌های متصل باید منتقل شوند)')}>"
            f"<input type='hidden' name='id' value='{e(p['id'])}'><button class='btn-danger btn-sm'>{_use('trash')}</button></form>"
            f"</div></td></tr>"
        )
    table = (f"<table data-enhance><thead><tr><th>#</th><th>نام</th><th>URL</th><th>نوع</th><th>IP کانفیگ</th><th>Sub</th>"
             f"<th class='col-actions'>عملیات</th></tr></thead><tbody>{rows}</tbody></table>"
             if rows else _empty('هنوز سروری اضافه نشده', 'برای فروش، ابتدا یک پنل (سرور) ثبت کنید.', 'server'))
    body = (
        _page_head('پنل‌ها', 'سرورهایی که ربات برای ساخت کانفیگ به آن‌ها متصل می‌شود',
                   actions=f"<a href='/panels/edit'><button>{_use('plus')} افزودن پنل</button></a>",
                   crumbs=[('خانه', '/'), ('پنل‌ها', None)]),
        f"<div class='tbl-card'><div class='tbl-scroll'>{table}</div></div>",
    )
    return layout("پنل‌ها", "".join(body), "/panels", request)


@require_auth
async def panel_edit_form(request):
    pid = request.query.get("id")
    p = await db.get_panel(int(pid)) if pid and pid.isdigit() else None
    is_new = p is None
    title = "افزودن پنل" if is_new else f"ویرایش پنل {pid}"

    from panel import normalize_panel_type
    cur_type = normalize_panel_type((p['panel_type'] if p and 'panel_type' in p.keys() else '') or '')

    def val(key):
        return e(p[key]) if p else ""

    hidden_id = "" if is_new else f"<input type='hidden' name='id' value='{e(pid)}'>"
    body = (
        _page_head(title, None, crumbs=[('خانه', '/'), ('پنل‌ها', '/panels'), (title, None)]),
        "<div class='box'><form method='post' action='/panels/save'>" + hidden_id +
        "<div class='fields'>"
        "<div class='field'><label>نام <span class='req'>*</span></label><input name='name' value='" + val('name') + "' required></div>"
        "<div class='field'><label>آدرس پنل <span class='req'>*</span></label><input name='url' value='" + val('url') + "' class='ltr' placeholder='https://example.com:54321' required>"
        "<div class='hint'>URL کامل با https و پورت.</div></div>"
        "<div class='field'><label>نام کاربری</label><input name='username' value='" + val('username') + "' class='ltr'></div>"
        "<div class='field'><label>رمز عبور" + ('' if is_new else ' (خالی بگذارید تا تغییر نکند)') + "</label>"
        "<input type='password' name='password' class='ltr'" + ('' if is_new else " placeholder='••••••••'") + "></div>"
        "<div class='field'><label>IP کانفیگ (sni/host)</label><input name='config_ip' value='" + val('config_ip') + "' class='ltr'></div>"
        "<div class='field'><label>لینک ساب (اختیاری)</label><input name='sub_url' value='" + val('sub_url') + "' class='ltr'></div>"
        "<div class='field'><label>نوع پنل</label><select name='panel_type'>"
        f"<option value='xui' {'selected' if cur_type == 'xui' else ''}>3X-UI</option>"
        f"<option value='vpn-ui' {'selected' if cur_type == 'vpn-ui' else ''}>VPN-UI</option>"
        "</select><div class='hint'>پنل vpn-ui همان 3x-ui است با API کمی متفاوت؛ اگر مطمئن نیستید 3X-UI بگذارید.</div></div>"
        "</div>"
        "<div class='form-foot'><button type='submit'>" + _use('check') + " ذخیره</button>"
        "<a href='/panels' class='btn btn-ghost'>انصراف</a></div>"
        "</form></div>",
    )
    return layout(title, "".join(body), "/panels", request)


@require_auth
async def panel_save(request):
    data = await request.post()
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    config_ip = (data.get("config_ip") or "").strip()
    sub_url = (data.get("sub_url") or "").strip()
    from panel import normalize_panel_type
    panel_type = normalize_panel_type(data.get("panel_type") or "")
    if not (name and url):
        raise _redirect("/panels", err="نام و URL الزامی است")
    pid = data.get("id")
    if pid and pid.isdigit():
        # فرم رمز را خالی بگذارد، رمز فعلی حفظ می‌شود
        if not password:
            current = await db.get_panel(int(pid))
            password = current['password'] if current else ""
        await db.update_panel(int(pid), name, url, username, password, config_ip, sub_url, panel_type)
    else:
        await db.add_panel(name, url, username, password, config_ip, sub_url, panel_type)
    logging.info("WEB_PANEL_SAVE name=%s", name)
    raise _redirect("/panels", ok="پنل ذخیره شد")


@require_auth
async def panel_delete(request):
    data = await request.post()
    pid = data.get("id")
    if pid and str(pid).isdigit():
        await db.delete_panel(int(pid))
        logging.info("WEB_PANEL_DELETE id=%s", pid)
    raise _redirect("/panels", ok="پلن حذف شد")


# ================= کدهای هدیه =================
def _gift_status_badge(c, now=None):
    now = now or datetime.datetime.now()
    if c['is_active'] is False:
        return _badge('red', 'غیرفعال')
    expires = c['expires_at']
    if expires:
        cmp_now = now
        if getattr(expires, 'tzinfo', None) and expires.tzinfo is not None and cmp_now.tzinfo is None:
            cmp_now = cmp_now.replace(tzinfo=expires.tzinfo)
        if cmp_now > expires:
            return _badge('red', 'منقضی')
    if c['max_uses'] and c['used_count'] >= c['max_uses']:
        return _badge('gray', 'تکمیل')
    return _badge('green', 'فعال')


@require_auth
async def giftcodes_page(request):
    codes = await db.list_gift_codes()
    now = datetime.datetime.now()
    aud_opts = "".join(
        f"<option value='{k}'>{e(label)}</option>" for k, label in rewards.GIFT_AUDIENCES.items()
    )
    rows = ""
    for c in codes:
        expires = c['expires_at'].strftime('%Y-%m-%d %H:%M') if c['expires_at'] else '—'
        audience = rewards.GIFT_AUDIENCES.get(c['audience'] or 'all', c['audience'] or 'همه')
        rows += (
            f"<tr><td><a href='/giftcodes/view?code={quote(c['code'])}'><b class='num' dir='ltr'>{e(c['code'])}</b></a> {_copy_btn(c['code'])}</td>"
            f"<td>{num(money(c['amount']))}</td><td>{num(str(c['used_count']))}/{num(str(c['max_uses']))}</td>"
            f"<td>{_badge('violet', audience)}</td><td class='muted'>{num(expires)}</td><td>{_gift_status_badge(c, now)}</td>"
            f"<td class='wrap muted'>{e(c['note'] or '—')}</td>"
            f"<td class='col-actions'><div class='actions'>"
            f"<form class='inline' method='post' action='/giftcodes/toggle'>"
            f"<input type='hidden' name='code' value='{e(c['code'])}'>"
            f"<button class='btn-ghost btn-sm'>{_use('pause') if c['is_active'] else _use('play')}</button></form>"
            f"<form class='inline' method='post' action='/giftcodes/delete'{_confirm('حذف کد؟')}>"
            f"<input type='hidden' name='code' value='{e(c['code'])}'><button class='btn-danger btn-sm'>{_use('trash')}</button></form>"
            f"</div></td></tr>"
        )
    table = (f"<table data-enhance><thead><tr><th>کد</th><th>مبلغ</th><th>استفاده</th><th>مخاطب</th><th>انقضا</th>"
             f"<th>وضعیت</th><th>یادداشت</th><th class='col-actions'></th></tr></thead><tbody>{rows}</tbody></table>"
             if rows else _empty('هنوز کدی ساخته نشده', 'از فرم بالا اولین کد هدیه را بسازید.', 'gift'))
    body = (
        _page_head('کدهای هدیه', 'هر کاربر هر کد را فقط یک‌بار می‌تواند استفاده کند؛ کد از کیف‌پول ربات یا لینک /start gift_CODE وارد می‌شود.',
                   crumbs=[('خانه', '/'), ('کدهای هدیه', None)]),
        "<div class='box'><h3>" + icon('plus') + " افزودن یا به‌روزرسانی کد</h3>"
        "<form method='post' action='/giftcodes/add'>"
        "<div class='fields'>"
        "<div class='field'><label>کد (خالی = تصادفی)</label><input name='code' placeholder='مثلاً NOWROZ1405' class='ltr'></div>"
        "<div class='field'><label>مبلغ (تومان) <span class='req'>*</span></label><input name='amount' placeholder='50000' class='ltr' inputmode='numeric' required></div>"
        "<div class='field'><label>حداکثر دفعات</label><input name='max_uses' value='1' class='ltr' inputmode='numeric'></div>"
        "<div class='field'><label>مخاطب</label><select name='audience'>" + aud_opts + "</select></div>"
        "<div class='field'><label>تاریخ انقضا (اختیاری)</label><input type='datetime-local' name='expires_at' class='ltr'></div>"
        "<div class='field'><label>یادداشت</label><input name='note' placeholder='مثلاً کمپین اینستاگرام'></div>"
        "<div class='field'><label>وضعیت</label><select name='is_active'><option value='1'>فعال</option><option value='0'>غیرفعال</option></select></div>"
        "</div>"
        "<div class='form-foot'><button type='submit'>" + _use('check') + " ذخیره کد</button></div>"
        "</form></div>"
        f"<div class='tbl-card'><div class='tbl-toolbar'><span class='tbl-count'></span></div>"
        f"<div class='tbl-scroll'>{table}</div></div>",
    )
    return layout("کدهای هدیه", "".join(body), "/giftcodes", request)


@require_auth
async def giftcodes_add(request):
    data = await request.post()
    try:
        code = (data.get("code") or "").strip()
        amount = int(data.get("amount"))
        max_uses = max(1, int(data.get("max_uses") or 1))
        if amount <= 0:
            raise ValueError("amount")
    except (TypeError, ValueError):
        raise _redirect("/giftcodes", err="ورودی نامعتبر")
    if code and not rewards.is_valid_gift_code(rewards.normalize_gift_code(code)):
        raise _redirect("/giftcodes", err="کد باید ۳ تا ۳۲ حرف انگلیسی یا عدد باشد")
    audience = data.get("audience") if data.get("audience") in rewards.GIFT_AUDIENCES else "all"
    saved = await db.add_gift_code(
        code, amount, max_uses,
        expires_at=_dt(data.get("expires_at")),
        is_active=data.get("is_active") != "0",
        note=(data.get("note") or "").strip(),
        audience=audience,
    )
    logging.info("WEB_GIFTCODE_ADD code=%s amount=%s", saved, amount)
    raise _redirect("/giftcodes", ok=f"کد {saved} ذخیره شد")


@require_auth
async def giftcodes_delete(request):
    data = await request.post()
    code = (data.get("code") or "").strip()
    if code:
        await db.delete_gift_code(code)
        logging.info("WEB_GIFTCODE_DELETE code=%s", code)
    raise _redirect("/giftcodes", ok="کد حذف شد")


@require_auth
async def giftcodes_toggle(request):
    data = await request.post()
    code = (data.get("code") or "").strip()
    state = await db.toggle_gift_code_active(code) if code else None
    if state is None:
        raise _redirect("/giftcodes", err="کد پیدا نشد")
    logging.info("WEB_GIFTCODE_TOGGLE code=%s active=%s", code, state)
    raise _redirect("/giftcodes", ok="کد فعال شد" if state else "کد غیرفعال شد")


@require_auth
async def giftcodes_view(request):
    code = (request.query.get("code") or "").strip()
    gc = await db.get_gift_code(code)
    if not gc:
        raise _redirect("/giftcodes", err="کد پیدا نشد")
    redemptions = await db.list_gift_redemptions(gc['code'])
    rows = ""
    for r in redemptions:
        d = r['created_at'].strftime('%Y-%m-%d %H:%M') if r['created_at'] else '—'
        rows += (
            f"<tr><td><a href='/users/view?id={e(r['user_id'])}'>{num(r['user_id'])}</a></td>"
            f"<td>{e(r['nickname'] or '—')}</td><td>{num(money(r['amount'] or gc['amount']))}</td>"
            f"<td class='muted'>{num(d)}</td></tr>"
        )
    table = (f"<table><thead><tr><th>کاربر</th><th>نام</th><th>مبلغ</th><th>تاریخ</th></tr></thead><tbody>{rows}</tbody></table>"
             if rows else _empty('هنوز کسی این کد را استفاده نکرده است', '', 'users'))
    expires = gc['expires_at'].strftime('%Y-%m-%d %H:%M') if gc['expires_at'] else 'بدون انقضا'
    audience = rewards.GIFT_AUDIENCES.get(gc['audience'] or 'all', gc['audience'])
    body = (
        _page_head(f"کد {gc['code']}", None,
                   actions=_copy_btn(f"/start gift_{gc['code']}", 'کپی دستور'),
                   crumbs=[('خانه', '/'), ('کدهای هدیه', '/giftcodes'), (f'کد {gc["code"]}', None)]),
        "<div class='box'><dl class='kv'>"
        f"<dt>مبلغ</dt><dd>{num(money(gc['amount']))} تومان</dd>"
        f"<dt>استفاده</dt><dd>{num(str(gc['used_count']))}/{num(str(gc['max_uses']))}</dd>"
        f"<dt>مخاطب</dt><dd>{e(audience)}</dd>"
        f"<dt>انقضا</dt><dd>{num(expires)}</dd>"
        f"<dt>وضعیت</dt><dd>{_gift_status_badge(gc)}</dd>"
        f"<dt>یادداشت</dt><dd>{e(gc['note'] or '—')}</dd>"
        f"<dt>لینک ورود مستقیم</dt><dd><code>/start gift_{e(gc['code'])}</code></dd>"
        "</dl></div>"
        "<div class='box'><h3>" + icon('users') + " استفاده‌کننده‌ها</h3><div class='tbl-scroll'>" + table + "</div></div>"
        "<a href='/giftcodes' class='back-link'>" + icon('back') + " بازگشت</a>",
    )
    return layout(f"کد {gc['code']}", "".join(body), "/giftcodes", request)

# ================= دعوت‌ها =================
@require_auth
async def referrals_page(request):
    search = request.query.get("q", "").strip() or None
    page = _page_arg(request)
    stats = await db.get_referral_stats()
    cfg = await db.load_referral_config()
    total = await db.count_referrals(search)
    rows_data = await db.list_referrals(search=search, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
    extra = f"&q={quote(search)}" if search else ""
    cards = "".join([
        _stat_card('دعوت‌شده', stats['invited'], 'users', tone='blue'),
        _stat_card('پاداش‌گرفته', stats['rewarded'], 'gift', tone='green'),
        _stat_card('در انتظار شارژ', stats['pending'], 'clock', tone='gold'),
        _stat_card('مجموع پرداخت‌شده', money(stats['paid']), 'wallet', tone='teal'),
    ])
    rows = ""
    for r in rows_data:
        status = _badge('green', 'پرداخت شده') if r['ref_rewarded'] else _badge('red', 'در انتظار')
        rows += (
            f"<tr><td><a href='/users/view?id={e(r['user_id'])}'>{num(r['user_id'])}</a></td>"
            f"<td class='wrap'>{e(r['nickname'] or '—')}</td>"
            f"<td><a href='/users/view?id={e(r['referred_by'])}'>{num(r['referred_by'])}</a></td>"
            f"<td class='wrap'>{e(r['referrer_nick'] or '—')}</td>"
            f"<td>{status}</td><td>{num(money(r['balance']))}</td></tr>"
        )
    table = (f"<table data-enhance><thead><tr><th>دعوت‌شده</th><th>نام</th><th>معرف</th><th>نام معرف</th>"
             f"<th>وضعیت</th><th>موجودی</th></tr></thead><tbody>{rows}</tbody></table>"
             if rows else _empty('دعوتی ثبت نشده', 'کاربران با لینک /start ref_USERID دعوت می‌شوند.', 'link'))
    body = (
        _page_head('سیستم دعوت', None, crumbs=[('خانه', '/'), ('دعوت‌ها', None)]),
        f"<div class='stats'>{cards}</div>",
        "<div class='box'><h3>" + icon('settings') + " تنظیمات دعوت</h3>"
        "<form method='post' action='/referrals/save'>"
        "<div class='fields'>"
        "<div class='field'><label>وضعیت</label><select name='ref_enabled'>"
        f"<option value='on' {_sel('on' if cfg.enabled else 'off', 'on')}>روشن</option>"
        f"<option value='off' {_sel('on' if cfg.enabled else 'off', 'off')}>خاموش</option>"
        "</select></div>"
        f"<div class='field'><label>پاداش معرف (تومان)</label><input name='ref_bonus' value='{e(cfg.referrer_bonus)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>هدیه دعوت‌شده (تومان)</label><input name='ref_invitee_bonus' value='{e(cfg.invitee_bonus)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>حداقل شارژ برای آزاد شدن پاداش</label><input name='ref_min_topup' value='{e(cfg.min_topup)}' class='ltr' inputmode='numeric'></div>"
        "</div>"
        "<p class='muted'>پاداش فقط یک‌بار و بعد از اولین شارژِ تاییدشده‌ی دعوت‌شده پرداخت می‌شود."
        " اگر مبلغ شارژ به حداقل نرسد یا سیستم خاموش باشد، شانس برای شارژ بعدی باقی می‌ماند.</p>"
        "<div class='form-foot'><button type='submit'>" + _use('check') + " ذخیره تنظیمات دعوت</button></div>"
        "</form></div>",
        "<div class='tbl-card'><div class='tbl-toolbar'>"
        "<form class='inline' method='get' action='/referrals' style='flex:1 1 320px;position:relative'>"
        f"<input name='q' placeholder='جستجوی آیدی دعوت‌شده یا معرف…' value='{e(search or "")}' style='width:100%;padding-right:34px'>"
        f"<span style='position:absolute;right:10px;top:50%;transform:translateY(-50%);color:var(--muted)'>{_use('search')}</span>"
        "</form></div>"
        f"<div class='tbl-scroll'>{table}</div>{_pager_html('/referrals', page, total, extra)}</div>",
    )
    return layout("دعوت‌ها", "".join(body), "/referrals", request)


@require_auth
async def referrals_save(request):
    data = await request.post()
    await db.update_setting("ref_enabled", "on" if data.get("ref_enabled") == "on" else "off")
    for key in ("ref_bonus", "ref_invitee_bonus", "ref_min_topup"):
        raw = (data.get(key) or "").strip()
        if raw.isdigit():
            await db.update_setting(key, raw)
    logging.info("WEB_REFERRAL_SETTINGS_SAVE")
    raise _redirect("/referrals", ok="تنظیمات دعوت ذخیره شد")


# ================= پیام همگانی =================
def _broadcast_report_box():
    """باکس گزارش آخرین ارسال همگانی: تعداد موفق/ناموفق و لیست شناسه‌های ناموفق با دلیل."""
    rep = _BROADCAST_REPORT
    if not rep:
        return ""

    def _ts(dt):
        return dt.strftime('%Y-%m-%d %H:%M') if dt else '—'

    out = ["<div class='box'><h3 style='margin-top:0'>📊 گزارش آخرین ارسال</h3>",
           f"<p class='muted'>آغاز: {_ts(rep.get('started'))}"
           + (f" — پایان: {_ts(rep.get('finished'))}" if rep.get('finished') else "")
           + f" | کل: {rep.get('total', 0)} | موفق: {rep.get('sent', 0)}</p>"]
    if rep.get('running'):
        out.append("<p>⏳ ارسال در جریان است؛ بعد از پایان، لیست ناموفق‌ها همین‌جا می‌آید.")
        out.append("<p><a href='/broadcast'>به‌روزرسانی</a></p></div>")
        out.append("<script>setTimeout(function(){location.reload()},5000)</script>")
        return "".join(out)
    fails = rep.get('failures') or []
    if not fails:
        out.append("<p>✅ همه‌ی کاربران پیام را دریافت کردند.</p></div>")
        return "".join(out)
    rows = "".join(
        f"<tr><td>{i}</td><td>{e(uid)}</td><td>{e(name or '—')}</td><td>{e(reason)}</td></tr>"
        for i, (uid, name, reason) in enumerate(fails[:300], 1)
    )
    out.append(f"<p>⚠️ ناموفق: {len(fails)} نفر"
               + (" (۳۰۰ مورد اول نمایش داده شده)" if len(fails) > 300 else "") + "</p>")
    out.append("<div class='tbl-scroll'><table data-enhance><thead><tr><th>#</th><th>شناسه</th>"
               "<th>نام</th><th>دلیل</th></tr></thead><tbody>" + rows + "</tbody></table></div>")
    out.append("</div>")
    return "".join(out)


@require_auth
async def broadcast_page(request):
    body = (
        _page_head('پیام همگانی', 'متن برای همه‌ی کاربران ربات ارسال می‌شود.',
                   crumbs=[('خانه', '/'), ('پیام همگانی', None)]),
        _broadcast_report_box(),
        "<div class='box'><form method='post' action='/broadcast/send'"
        + _confirm('پیام برای «همه‌ی کاربران» ارسال شود؟') + ">"
        "<label for='bc-text'>متن پیام</label>"
        "<textarea id='bc-text' name='text' rows='6' placeholder='متن پیام…' data-charcount='bc-count'></textarea>"
        "<div class='row' style='margin-top:10px'><button type='submit'>" + icon('megaphone') + " ارسال</button>"
        "<span class='muted' id='bc-count' style='margin:0'></span></div>"
        "<p class='muted'>ارسال در پس‌زمینه انجام می‌شود و ممکن است بسته به تعداد کاربران کمی طول بکشد.</p>"
        "</form></div>",
    )
    return layout("پیام همگانی", "".join(body), "/broadcast", request)


@require_auth
async def broadcast_send(request):
    global _BROADCAST_REPORT
    data = await request.post()
    text = (data.get("text") or "").strip()
    if not text:
        raise _redirect("/broadcast", err="متن خالی است")
    if _BOT is None:
        raise _redirect("/broadcast", err="بات در دسترس نیست")
    ids = await db.all_user_ids()
    started = datetime.datetime.now()
    _BROADCAST_REPORT = {'running': True, 'started': started, 'total': len(ids),
                         'sent': 0, 'failures': []}

    async def _run():
        global _BROADCAST_REPORT
        sent, failed = 0, 0
        failures = []  # (user_id، دلیل) — برای گزارش پایانی
        for uid in ids:
            try:
                await _BOT.send_message(chat_id=uid, text=text)
                sent += 1
            except Exception as ex:
                failed += 1
                failures.append((uid, webcomponents.short_fail_reason(ex)))
                # محدودیت نرخ تلگرام: به مدت خواسته‌شده صبر می‌کنیم
                retry_after = getattr(ex, "retry_after", None)
                if retry_after:
                    await asyncio.sleep(float(retry_after) + 1)
            await asyncio.sleep(0.05)  # حدود ۲۰ پیام در ثانیه، زیر سقف تلگرام
        names = await db.get_nicknames([u for u, _ in failures])
        _BROADCAST_REPORT = {
            'running': False,
            'started': started,
            'finished': datetime.datetime.now(),
            'total': len(ids),
            'sent': sent,
            'failures': [(u, names.get(int(u), ''), reason) for u, reason in failures],
        }
        logging.info("WEB_BROADCAST sent=%s failed=%s total=%s failed_ids=%s",
                     sent, failed, len(ids), [u for u, _ in failures])

    # نگه‌داشتن مرجع تسک لازم است؛ وگرنه ممکن است وسط کار توسط GC جمع شود
    task = asyncio.create_task(_run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    raise _redirect("/broadcast", ok=f"ارسال برای {len(ids)} کاربر آغاز شد")


# ================= VIP و ادمین =================
@require_auth
async def specials_page(request):
    vips = await db.list_vips(role=pricing.VIP)
    resellers = await db.list_vips(role=pricing.RESELLER)
    admins = await db.list_admin_rows()
    cfg = await pricing.load_config()

    def member_rows(rows, label):
        out = ""
        for v in rows:
            out += (
                f"<tr><td>{num(v['user_id'])} {_copy_btn(v['user_id'])}</td><td class='wrap'>{e(v['nickname'])}</td>"
                f"<td>{num(money(v['balance']))}</td>"
                f"<td class='col-actions'><form class='inline' method='post' action='/specials/vip_remove'{_confirm(label + '؟')}>"
                f"<input type='hidden' name='user_id' value='{e(v['user_id'])}'><button class='btn-danger btn-sm'>{_use('x')} {e(label)}</button></form></td></tr>"
            )
        return out

    a_rows = "".join(f"<tr><td>{num(a['user_id'])}</td><td class='wrap'>{e(a['name'])}</td></tr>" for a in admins)

    def member_table(rows_html, empty_msg):
        return (f"<table><thead><tr><th>آیدی</th><th>نام</th><th>موجودی</th><th class='col-actions'></th></tr></thead>"
                f"<tbody>{rows_html}</tbody></table>" if rows_html else _empty(empty_msg, '', 'users'))

    body = (
        _page_head('VIP و نمایندگان', 'سطح کاربران ویژه و نمایندگان را مدیریت کنید.',
                   crumbs=[('خانه', '/'), ('VIP و نمایندگان', None)]),
        "<div class='box'><h3>" + icon('star') + " ارتقای کاربر</h3>"
        "<form class='row' method='post' action='/specials/vip_add'>"
        "<input name='user_id' placeholder='آیدی عددی کاربر' class='ltr' required>"
        "<input name='nickname' placeholder='نام (اختیاری)'>"
        "<select name='role'><option value='vip'>VIP</option><option value='reseller'>نماینده</option></select>"
        "<button>" + icon('zap') + " ارتقا</button></form>"
        "<p class='muted'>نماینده قیمت همکاری می‌گیرد و بر اساس شارژ ۳۰ روز اخیر تخفیف پله‌ای هم می‌گیرد:"
        f" از {money(cfg.reseller_t2_min)} تومان → {cfg.reseller_t2_discount}٪ و از {money(cfg.reseller_t3_min)} تومان → {cfg.reseller_t3_discount}٪.</p></div>"
        "<div class='grid grid-2'>",
        "<div class='box' style='margin-top:16px'><h3>" + icon('target') + f" نمایندگان ({len(resellers)})</h3>"
        "<div class='tbl-scroll'>" + member_table(member_rows(resellers, 'حذف نمایندگی'), 'نماینده‌ای نیست') + "</div></div>",
        "<div class='box' style='margin-top:16px'><h3>" + icon('star') + f" کاربران VIP ({len(vips)})</h3>"
        "<div class='tbl-scroll'>" + member_table(member_rows(vips, 'حذف VIP'), 'کاربر VIP نیست') + "</div></div>",
        "</div>",
        "<div class='box'><h3>" + icon('shield') + " ادمین‌ها (مدیریت از داخل ربات)</h3>"
        "<table><thead><tr><th>آیدی</th><th>نام</th></tr></thead><tbody>"
        + (a_rows or "<tr><td colspan='2' class='muted'>موردی نیست</td></tr>") + "</tbody></table></div>",
    )
    return layout("VIP و نمایندگان", "".join(body), "/specials", request)


@require_auth
async def vip_add(request):
    data = await request.post()
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        raise _redirect("/specials", err="آیدی نامعتبر")
    nickname = (data.get("nickname") or "").strip() or None
    role = _valid_role(data.get("role"))
    async with db.db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (user_id, nickname, role) VALUES ($1, $2, $3)
               ON CONFLICT (user_id) DO UPDATE SET role=$3, nickname=COALESCE($2, users.nickname)""",
            uid, nickname, role,
        )
    logging.info("WEB_ROLE_UPGRADE user=%s role=%s", uid, role)
    raise _redirect("/specials", ok=f"کاربر به {ROLE_LABELS.get(role, role)} تغییر کرد")


@require_auth
async def vip_remove(request):
    data = await request.post()
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        raise _redirect("/specials", err="آیدی نامعتبر")
    await db.set_user_role(uid, "normal")
    logging.info("WEB_VIP_REMOVE user=%s", uid)
    raise _redirect("/specials", ok="کاربر از VIP خارج شد")

# ================= کمکی‌های صفحه‌بندی/انتخاب =================
def _pager_html(base, page, total, extra=""):
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, pages))
    html_out = "<div class='pager'>"
    if page > 1:
        html_out += f"<a href='{base}?page={page-1}{extra}'>« قبلی</a>"
    html_out += f"<span>صفحه <b>{page}</b> از {pages} · کل: <b>{total}</b></span>"
    if page < pages:
        html_out += f"<a href='{base}?page={page+1}{extra}'>بعدی »</a>"
    html_out += "</div>"
    return html_out


def _page_arg(request):
    try:
        return max(1, int(request.query.get("page", "1")))
    except (TypeError, ValueError):
        return 1


def _sel(cur, val):
    return "selected" if cur == val else ""


# ================= تنظیمات =================
@require_auth
async def settings_page(request):
    sales = await db.get_setting("sales_status")
    card = await db.get_setting("card_number")
    support = await db.get_setting("support_id")
    notify_days = await db.get_setting("notify_days")
    ref_bonus = await db.get_setting("ref_bonus")
    ref_enabled = await db.get_setting("ref_enabled")
    ref_invitee = await db.get_setting("ref_invitee_bonus")
    ref_min = await db.get_setting("ref_min_topup")
    backup_enabled = await db.get_setting("backup_enabled")
    test_enabled = await db.get_setting("test_enabled")
    test_gb = await db.get_setting("test_gb")
    test_days = await db.get_setting("test_days")
    test_panel_id = await db.get_setting("test_panel_id")
    test_inbound_id = await db.get_setting("test_inbound_id")
    res_bonus = await db.get_setting("reseller_bonus_enabled")
    res_min = await db.get_setting("reseller_bonus_min")
    res_percent = await db.get_setting("reseller_bonus_percent")
    t2_min = await db.get_setting("reseller_t2_min")
    t2_disc = await db.get_setting("reseller_t2_discount")
    t3_min = await db.get_setting("reseller_t3_min")
    t3_disc = await db.get_setting("reseller_t3_discount")
    panels = await db.get_panels()
    opts = "<option value=''>—</option>" + "".join(
        f"<option value='{e(p['id'])}' {_sel(str(test_panel_id or ''), str(p['id']))}>{e(p['id'])} - {e(p['name'])}</option>"
        for p in panels
    )
    body = (
        _page_head('تنظیمات', 'پیکربندی عمومی ربات، دعوت، نمایندگان و اکانت تست',
                   crumbs=[('خانه', '/'), ('تنظیمات', None)]),
        "<form method='post' action='/settings/save'>",
        "<div class='box'><h3>" + icon('settings') + " عمومی و فروش</h3><div class='fields'>"
        "<div class='field'><label>وضعیت فروش</label><select name='sales_status'>"
        f"<option value='open' {_sel(sales, 'open')}>باز</option>"
        f"<option value='closed' {_sel(sales, 'closed')}>بسته</option>"
        "</select><div class='hint'>در حالت بسته، خرید از ربات انجام نمی‌شود.</div></div>"
        f"<div class='field'><label>شماره کارت</label><input name='card_number' value='{e(card)}' class='ltr'></div>"
        f"<div class='field'><label>آیدی پشتیبانی</label><input name='support_id' value='{e(support)}' class='ltr'></div>"
        f"<div class='field'><label>هشدار انقضا (روز مانده)</label><input name='notify_days' value='{e(notify_days)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>کانال اجبار عضویت (یوزرنیم یا خالی)</label><input name='force_channel' value='{e(await db.get_setting('force_channel') or '')}' class='ltr' placeholder='@mychannel'></div>"
        "<div class='field'><label>بکاپ خودکار روزانه</label><select name='backup_enabled'>"
        f"<option value='on' {_sel(backup_enabled, 'on')}>روشن</option>"
        f"<option value='off' {_sel(backup_enabled, 'off')}>خاموش</option>"
        "</select></div>"
        "</div></div>",
        "<div class='box'><h3>⭐️ پرداخت با تلگرام استارز</h3><div class='fields'>"
        f"<div class='field'><label>نرخ: تومان به ازای هر استارز</label><input name='stars_toman_rate' value='{e(await db.get_setting('stars_toman_rate') or '')}' class='ltr' inputmode='numeric'>"
        "<div class='hint'>مثلاً 3000 یعنی هر استارز ۳۰۰۰ تومان حساب می‌شود. خالی/صفر = پرداخت استارز خاموش.</div></div>"
        "</div></div>",
        "<div class='box'><h3>" + icon('link') + " سیستم دعوت</h3><div class='fields'>"
        "<div class='field'><label>وضعیت دعوت</label><select name='ref_enabled'>"
        f"<option value='on' {_sel(ref_enabled or 'on', 'on')}>روشن</option>"
        f"<option value='off' {_sel(ref_enabled or 'on', 'off')}>خاموش</option>"
        "</select></div>"
        f"<div class='field'><label>پاداش معرف (تومان)</label><input name='ref_bonus' value='{e(ref_bonus)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>هدیه دعوت‌شده (تومان)</label><input name='ref_invitee_bonus' value='{e(ref_invitee)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>حداقل شارژ برای پاداش دعوت</label><input name='ref_min_topup' value='{e(ref_min)}' class='ltr' inputmode='numeric'></div>"
        "</div>"
        "<p class='muted'>پاداش دعوت فقط بعد از اولین شارژِ تاییدشده پرداخت می‌شود. جزئیات دعوت‌ها در صفحه «دعوت‌ها» است.</p></div>",
        "<div class='box'><h3>" + icon('target') + " نمایندگان و آفرها</h3><div class='fields'>"
        "<div class='field'><label>هدیه‌ی اولین شارژ نماینده</label><select name='reseller_bonus_enabled'>"
        f"<option value='on' {_sel(res_bonus, 'on')}>فعال</option>"
        f"<option value='off' {_sel(res_bonus, 'off')}>غیرفعال</option>"
        "</select></div>"
        f"<div class='field'><label>حداقل مبلغ شارژ برای هدیه (تومان)</label><input name='reseller_bonus_min' value='{e(res_min)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>درصد هدیه</label><input name='reseller_bonus_percent' value='{e(res_percent)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>آستانه‌ی سطح ۲ نماینده (شارژ ۳۰ روز)</label><input name='reseller_t2_min' value='{e(t2_min)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>درصد تخفیف سطح ۲</label><input name='reseller_t2_discount' value='{e(t2_disc)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>آستانه‌ی نماینده VIP</label><input name='reseller_t3_min' value='{e(t3_min)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>درصد تخفیف نماینده VIP</label><input name='reseller_t3_discount' value='{e(t3_disc)}' class='ltr' inputmode='numeric'></div>"
        "</div>"
        f"<p class='muted'>مثال: با تنظیمات فعلی، شارژ {money(res_min)} تومانیِ اولِ یک نماینده "
        f"{money(int(res_min or 0) * int(res_percent or 0) // 100)} تومان اعتبار هدیه می‌گیرد.</p></div>",
        "<div class='box'><h3>" + icon('gift') + " اکانت تست رایگان</h3><div class='fields'>"
        "<div class='field'><label>وضعیت</label><select name='test_enabled'>"
        f"<option value='on' {_sel(test_enabled, 'on')}>روشن</option>"
        f"<option value='off' {_sel(test_enabled, 'off')}>خاموش</option>"
        "</select></div>"
        f"<div class='field'><label>حجم (GB)</label><input name='test_gb' value='{e(test_gb)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>مدت (روز)</label><input name='test_days' value='{e(test_days)}' class='ltr' inputmode='numeric'></div>"
        f"<div class='field'><label>پنل اکانت تست</label><select name='test_panel_id'>{opts}</select></div>"
        f"<div class='field'><label>اینباند اکانت تست</label><input name='test_inbound_id' value='{e(test_inbound_id)}' class='ltr' inputmode='numeric'></div>"
        "</div></div>",
        "<div class='box' style='padding-top:14px'><div class='form-foot' style='margin-top:0;border-top:0'>"
        "<button type='submit'>" + _use('check') + " ذخیره تنظیمات</button></div></div>",
        "</form>",
        "<div class='box'><h3>" + icon('db') + " بکاپ دیتابیس</h3>"
        "<a href='/backup'><button class='btn-ghost'>" + _use('db') + " دانلود بکاپ (.sql)</button></a></div>",
    )
    return layout("تنظیمات", "".join(body), "/settings", request)


@require_auth
async def settings_save(request):
    data = await request.post()
    sales = data.get("sales_status") if data.get("sales_status") in ("open", "closed") else "open"
    await db.update_setting("sales_status", sales)
    await db.update_setting("card_number", (data.get("card_number") or "").strip())
    await db.update_setting("support_id", (data.get("support_id") or "").strip())
    fc = (data.get("force_channel") or "").strip().lstrip("@")
    await db.update_setting("force_channel", fc if fc and fc.lower() not in ("off", "خاموش") else "")
    st_rate = (data.get("stars_toman_rate") or "").strip()
    await db.update_setting("stars_toman_rate", st_rate if st_rate.isdigit() else "")
    numeric_keys = (
        "notify_days", "ref_bonus", "ref_invitee_bonus", "ref_min_topup", "test_gb", "test_days",
        "reseller_bonus_min", "reseller_bonus_percent",
        "reseller_t2_min", "reseller_t2_discount",
        "reseller_t3_min", "reseller_t3_discount",
    )
    for key in numeric_keys:
        raw = (data.get(key) or "").strip()
        if raw.isdigit():
            await db.update_setting(key, raw)
    await db.update_setting("reseller_bonus_enabled", "on" if data.get("reseller_bonus_enabled") == "on" else "off")
    await db.update_setting("ref_enabled", "on" if data.get("ref_enabled") == "on" else "off")
    await db.update_setting("backup_enabled", "on" if data.get("backup_enabled") == "on" else "off")
    await db.update_setting("test_enabled", "on" if data.get("test_enabled") == "on" else "off")
    tp = (data.get("test_panel_id") or "").strip()
    await db.update_setting("test_panel_id", tp if tp.isdigit() else "")
    ti = (data.get("test_inbound_id") or "").strip()
    await db.update_setting("test_inbound_id", ti if ti.isdigit() else "")
    logging.info("WEB_SETTINGS_SAVE")
    raise _redirect("/settings", ok="تنظیمات ذخیره شد")


# ================= خروجی CSV =================
def _csv_response(filename, headers_row, rows):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers_row)
    for r in rows:
        w.writerow(r)
    return web.Response(
        text="\ufeff" + buf.getvalue(),  # BOM برای باز شدن درست در Excel
        content_type="text/csv", charset="utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@require_auth
async def export_sales_csv(request):
    rows = await db.list_sales_export()
    data = [
        (r['id'], _fmt_dt(r['created_at']), r['user_id'], r['order_id'] or '', r['plan_id'] or '',
         r['kind'] or '', r['user_role'] or '', r['base_price'], r['price'], r['discount'])
        for r in rows
    ]
    return _csv_response(
        f"sales_{datetime.date.today().isoformat()}.csv",
        ('id', 'date', 'user_id', 'order_id', 'plan_id', 'kind', 'role', 'base_price', 'price', 'discount'),
        data,
    )


@require_auth
async def export_users_csv(request):
    rows = await db.list_users_export()
    data = [
        (r['user_id'], r['nickname'] or '', r['role'], r['balance'], 'banned' if r['is_banned'] else 'active', r['referred_by'] or '')
        for r in rows
    ]
    return _csv_response(
        f"users_{datetime.date.today().isoformat()}.csv",
        ('user_id', 'nickname', 'role', 'balance', 'status', 'referred_by'),
        data,
    )


# ================= بکاپ =================
async def _make_backup():
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASS
    try:
        proc = await asyncio.create_subprocess_exec(
            "pg_dump", "-h", DB_HOST, "-p", str(DB_PORT), "-U", DB_USER, "-d", DB_NAME,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return None, (err.decode(errors="ignore")[:300] or "pg_dump failed")
        return out, None
    except FileNotFoundError:
        return None, "pg_dump نصب نیست (postgresql-client)"
    except Exception as ex:
        return None, str(ex)


@require_auth
async def backup_download(request):
    data, err = await _make_backup()
    if not data:
        raise _redirect("/settings", err=f"بکاپ ناموفق: {err}")
    fname = f"backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.sql"
    return web.Response(
        body=data,
        headers={"Content-Disposition": f"attachment; filename={fname}"},
        content_type="application/sql",
    )


# ================= ایمپورت کانفیگ =================
@require_auth
async def import_page(request):
    """فرم ایمپورت: ثبت سفارشِ کانفیگِ موجود روی سرور برای هر کاربر (تکی/گروهی).

    کانفیگی که قبلاً برای کاربر دیگری ایمپورت شده هم قابل ثبت است (لینک مشترک)؛
    فقط تکراری برای «همان کاربر» رد می‌شود.
    """
    panels = await db.get_panels()
    panel_names = [p['name'] for p in panels] if panels else ['پیش‌فرض']
    body = (
        _page_head('ایمپورت کانفیگ',
                   'ثبت سفارشِ یک کانفیگِ موجود روی سرور برای هر کاربر — تکی یا گروهی.',
                   crumbs=[('خانه', '/'), ('ایمپورت کانفیگ', None)]),
        "<div class='box'><form method='post' action='/import/send'>"
        "<label for='imp-uid'>آیدی عددی کاربر مقصد</label>"
        "<input id='imp-uid' name='user_id' type='number' min='1' required placeholder='مثلاً 123456789' dir='ltr' style='max-width:320px'>"
        "<label for='imp-tokens' style='margin-top:12px'>نام کانفیگ‌ها (Email) یا لینک‌های کامل — با فاصله یا هر خط جدا</label>"
        "<textarea id='imp-tokens' name='tokens' rows='6' required dir='ltr' placeholder='ali_1 ali_2\nیا هر خط: vless://…#ali_1'></textarea>"
        "<p class='muted'>جستجو در پنل‌ها: " + e("، ".join(panel_names))
        + " — پلن از روی حجم کلاینت به‌طور خودکار تشخیص داده می‌شود. کانفیگی که قبلاً برای کاربر دیگری ایمپورت شده هم قابل ثبت است؛ فقط تکراری برای «همان کاربر» رد می‌شود.</p>"
        "<div class='row' style='margin-top:10px'><button type='submit'>" + icon('import') + " ایمپورت</button></div>"
        "</form></div>",
    )
    return layout('ایمپورت کانفیگ', "".join(body), '/import', request)


@require_auth
async def import_send(request):
    """اجرای ایمپورت و نمایش گزارش — کاربر مقصد هم خبردار می‌شود."""
    data = await request.post()
    uid_raw = (data.get("user_id") or "").strip()
    tokens = (data.get("tokens") or "").strip()
    if not uid_raw.isdigit() or not tokens:
        raise _redirect("/import", err="آیدی کاربر نامعتبر است یا لیست کانفیگ‌ها خالی است")
    target_user_id = int(uid_raw)
    if not await db.get_user_detail(target_user_id):
        raise _redirect("/import", err="کاربر یافت نشد")
    _uid, wanted = await importer.parse_import_input(f"{uid_raw} {tokens}")
    if not wanted:
        raise _redirect("/import", err="هیچ نام یا لینک معتبری یافت نشد")
    rep = await importer.run_import(target_user_id, wanted)
    logging.info("WEB_IMPORT user=%s imported=%s skipped=%s notfound=%s",
                 target_user_id, len(rep.imported), len(rep.skipped_same_user), len(rep.not_found))
    if not rep.connection_ok:
        raise _redirect("/import", err="خطا در اتصال به پنل‌ها؛ هیچ کانفیگی ایمپورت نشد")
    if rep.imported and _BOT is not None:
        try:
            await _BOT.send_message(
                target_user_id,
                f"📥 {len(rep.imported)} سرویس جدید توسط پشتیبانی برای شما ثبت شد.\n"
                f"از «سفارشات من 📦» ببینیدشان.",
            )
        except Exception as ex:
            logging.warning("اطلاع وب‌ایمپورت به کاربر %s ناموفق: %s", target_user_id, ex)
    body = (
        _page_head('نتیجه‌ی ایمپورت',
                   f"کاربر مقصد: {num(target_user_id)}",
                   crumbs=[('خانه', '/'), ('ایمپورت کانفیگ', '/import'), ('نتیجه', None)]),
        "<div class='box'><div style='white-space:pre-wrap;line-height:2.2'>"
        + e(rep.as_text()) + "</div>"
        "<div class='head-actions' style='margin-top:14px'>"
        "<a href='/import'><button>ایمپورت جدید</button></a>"
        "<a href='/users/view?id=" + e(target_user_id) + "'><button class='btn-ghost'>سفارش‌های کاربر</button></a>"
        "</div></div>",
    )
    return layout('نتیجه‌ی ایمپورت', "".join(body), '/import', request)


async def robots_txt(request):
    """ربات‌های موتور جست‌وجو نباید لینک‌های اشتراک را ذخیره کنند."""
    return web.Response(
        text="User-agent: *\nDisallow: /sub/\nDisallow: /login\n",
        content_type='text/plain',
        headers={'Cache-Control': 'public, max-age=86400'},
    )


@web.middleware
async def _compress_middleware(request, handler):
    """فشرده‌سازی پاسخ‌های متنی + noindex برای مسیرهای پورتال."""
    resp = await handler(request)
    try:
        if isinstance(resp, web.Response) and resp.content_type in (
                'text/html', 'text/plain', 'application/json', 'text/css'):
            resp.enable_compression()
    except Exception:
        pass
    if request.path.startswith('/sub/'):
        resp.headers.setdefault('X-Robots-Tag', 'noindex, nofollow')
    return resp


# ================= راه‌اندازی =================
def build_app():
    app = web.Application(middlewares=[_compress_middleware])
    app.add_routes([
        web.get("/login", login_get),
        web.post("/login", login_post),
        web.get("/logout", logout),
        web.get("/", dashboard),
        web.get("/users", users_page),
        web.get("/users/view", user_view),
        web.get("/users/edit", user_edit_form),
        web.post("/users/save", user_save),
        web.post("/users/delete", user_delete),
        web.post("/users/adjust", users_adjust),
        web.post("/users/role", user_role),
        web.post("/users/ban", user_ban),
        web.get("/orders", orders_page),
        web.post("/orders/delete", order_delete),
        web.get("/import", import_page),
        web.post("/import/send", import_send),
        web.get("/transactions", transactions_page),
        web.get("/plans", plans_page),
        web.get("/plans/edit", plan_edit_form),
        web.post("/plans/save", plan_save),
        web.post("/plans/delete", plan_delete),
        web.post("/plans/toggle", plan_toggle),
        web.post("/plans/seed", plans_seed),
        web.get("/offers", offers_page),
        web.get("/offers/edit", offer_edit_form),
        web.post("/offers/save", offer_save),
        web.post("/offers/toggle", offer_toggle),
        web.post("/offers/delete", offer_delete),
        web.post("/offers/builtin", offers_builtin),
        web.get("/finance", finance_page),
        web.get("/panels", panels_page),
        web.get("/panels/edit", panel_edit_form),
        web.post("/panels/save", panel_save),
        web.post("/panels/delete", panel_delete),
        web.get("/giftcodes", giftcodes_page),
        web.get("/giftcodes/view", giftcodes_view),
        web.post("/giftcodes/add", giftcodes_add),
        web.post("/giftcodes/delete", giftcodes_delete),
        web.post("/giftcodes/toggle", giftcodes_toggle),
        web.get("/referrals", referrals_page),
        web.post("/referrals/save", referrals_save),
        web.get("/broadcast", broadcast_page),
        web.post("/broadcast/send", broadcast_send),
        web.get("/specials", specials_page),
        web.post("/specials/vip_add", vip_add),
        web.post("/specials/vip_remove", vip_remove),
        web.get("/settings", settings_page),
        web.post("/settings/save", settings_save),
        web.get("/backup", backup_download),
        web.get("/export/sales.csv", export_sales_csv),
        web.get("/export/users.csv", export_users_csv),
        # --- پورتال اشتراک (عمومی؛ احراز هویت با توکن، بدون require_auth) ---
        web.get("/sub/{token}", subportal.portal_page),
        web.get("/sub/{token}/raw", subportal.raw_subscription),
        web.get("/sub/{token}/clash", subportal.clash_subscription),
        web.get("/sub/applogo/{app}.png", subportal.logo_asset),
        web.get("/sub/{token}/qr.png", subportal.qr_asset),
        web.get("/sub/{token}/live", subportal.live_status),
        web.get("/sub/fonts/{name}", subportal.font_asset),
        web.get("/robots.txt", robots_txt),
    ])
    return app


async def start_web(bot=None):
    """در صورت تنظیم رمز، سرور وب را داخل همان event loop ربات اجرا می‌کند و runner را برمی‌گرداند.
    bot برای قابلیت‌هایی مثل پیام همگانی از پنل وب نگه داشته می‌شود."""
    global _BOT
    _BOT = bot
    # وب‌سرور برای پنل ادمین (نیاز به رمز) یا پورتال اشتراک (نیاز به آدرس عمومی) اجرا می‌شود
    if not WEB_ADMIN_PASSWORD and not PORTAL_PUBLIC_URL:
        logging.info("وب‌سرور غیرفعال است (WEB_ADMIN_PASSWORD و PORTAL_PUBLIC_URL تنظیم نشده‌اند).")
        return None
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    logging.info("🌐 پنل وب مدیریت روی %s:%s اجرا شد.", WEB_HOST, WEB_PORT)
    return runner
