"""پنل وب مدیریت مبتنی بر aiohttp (نسخه‌ی بازطراحی‌شده).

اگر WEB_ADMIN_PASSWORD تنظیم نشده باشد، پنل اجرا نمی‌شود.
احراز هویت با یک رمز عبور و کوکی امضاشده (HMAC) انجام می‌شود.
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
from links import order_email
from config import (
    WEB_ADMIN_PASSWORD, WEB_PORT, WEB_HOST, WEB_SESSION_TTL,
    DB_USER, DB_PASS, DB_NAME, DB_HOST, DB_PORT,
)

# ربات برای قابلیت‌هایی مثل پیام همگانی از پنل وب
_BOT = None
PER_PAGE = 20

# تسک‌های پس‌زمینه (مثل پیام همگانی) نگه داشته می‌شوند تا زودتر از موعد جمع نشوند
_TASKS = set()

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


def _redirect(path, ok=None, err=None):
    sep = "&" if "?" in path else "?"
    if ok:
        path = f"{path}{sep}ok={quote(ok)}"
    elif err:
        path = f"{path}{sep}err={quote(err)}"
    return web.HTTPFound(path)


PAGE_CSS = """
<style>
*{box-sizing:border-box}
body{margin:0;font-family:Tahoma,'Segoe UI',Arial,sans-serif;background:#0b1220;color:#e6edf6;direction:rtl}
a{color:#4aa3ff;text-decoration:none}
.app{display:flex;min-height:100vh}
.side{width:230px;background:#0f1830;border-left:1px solid #1e2b47;padding:14px 12px;position:sticky;top:0;height:100vh;overflow:auto}
.brand{font-size:18px;font-weight:bold;color:#fff;padding:6px 10px 14px}
.side a{display:block;padding:10px 12px;border-radius:10px;color:#c7d2e0;margin:3px 0}
.side a.active,.side a:hover{background:#1b2a4a;color:#fff}
.side .logout{color:#ff7b7b;margin-top:14px}
.main{flex:1;padding:20px;overflow:auto}
h2{margin:6px 0 16px;font-size:22px}
h3{margin:4px 0 10px;font-size:17px;color:#cdd9ea}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px}
.card{background:linear-gradient(135deg,#152241,#101a33);border:1px solid #22345c;border-radius:16px;padding:16px}
.card .n{font-size:24px;font-weight:bold;color:#fff}
.card .l{color:#8ea3c0;font-size:13px;margin-top:6px}
.card .i{font-size:20px;margin-bottom:4px}
table{width:100%;border-collapse:collapse;background:#0f1830;border:1px solid #1e2b47;border-radius:14px;overflow:hidden;margin-top:14px}
th,td{padding:11px 10px;border-bottom:1px solid #1a2843;text-align:right;font-size:13px}
th{background:#0c1428;color:#8ea3c0;font-weight:600}
tr:hover td{background:#121d38}
input,select,textarea,button{font-family:inherit;font-size:14px;padding:9px 11px;border-radius:10px;border:1px solid #2a3c62;background:#0c1428;color:#e6edf6}
input:focus,select:focus,textarea:focus{outline:none;border-color:#4aa3ff}
button{background:#2563eb;border:0;cursor:pointer;color:#fff}
button:hover{background:#1d4ed8}
.btn-danger{background:#dc2626}.btn-danger:hover{background:#b91c1c}
.btn-ghost{background:#1b2a4a}.btn-ghost:hover{background:#243761}
.inline{display:inline-flex;gap:6px;align-items:center;margin:0}
.box{background:#0f1830;border:1px solid #1e2b47;border-radius:16px;padding:18px;margin-top:14px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.badge{display:inline-block;padding:3px 9px;border-radius:999px;font-size:12px;font-weight:600}
.badge.vip{background:#3b2f00;color:#ffcf33}
.badge.norm{background:#1b2a4a;color:#9db4d6}
.badge.on{background:#04321f;color:#31d0a0}
.badge.off{background:#3a1720;color:#ff7b8a}
.flash{padding:12px 14px;border-radius:12px;margin-bottom:14px}
.flash.ok{background:#04321f;color:#7ff0c8;border:1px solid #0c7a52}
.flash.err{background:#3a1720;color:#ffb3bd;border:1px solid #7a2233}
.muted{color:#8ea3c0;font-size:13px;margin:6px 0}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.pager{display:flex;gap:8px;align-items:center;margin-top:12px;justify-content:center}
.pager a,.pager span{padding:7px 12px;border-radius:10px;background:#152241;border:1px solid #22345c}
label{display:block;margin:10px 0 4px;color:#c7d2e0;font-size:13px}
.actions{display:flex;gap:6px;flex-wrap:wrap}
@media(max-width:760px){.app{flex-direction:column}.side{width:auto;height:auto;position:relative;display:flex;flex-wrap:wrap;gap:4px}.side .brand{width:100%}.side a{padding:8px 10px}}
</style>
"""

NAV = [
    ("/", "📊 داشبورد"),
    ("/users", "👥 کاربران"),
    ("/orders", "📦 سفارش‌ها"),
    ("/transactions", "💳 تراکنش‌ها"),
    ("/plans", "🛒 پلن‌ها"),
    ("/panels", "🖥 پنل‌ها"),
    ("/giftcodes", "🎟 کدهای هدیه"),
    ("/broadcast", "📢 پیام همگانی"),
    ("/specials", "💎 VIP و ادمین"),
    ("/settings", "⚙️ تنظیمات"),
]


def _flash(request):
    ok = request.query.get("ok")
    err = request.query.get("err")
    if ok:
        return f"<div class='flash ok'>{e(ok)}</div>"
    if err:
        return f"<div class='flash err'>{e(err)}</div>"
    return ""


def layout(title, body, active="", request=None):
    links = "".join(
        f'<a class="{"active" if path == active else ""}" href="{path}">{e(label)}</a>'
        for path, label in NAV
    )
    links += '<a class="logout" href="/logout">🚪 خروج</a>'
    flash = _flash(request) if request is not None else ""
    doc = (
        "<!doctype html><html lang='fa'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{e(title)}</title>{PAGE_CSS}</head><body><div class='app'>"
        f"<div class='side'><div class='brand'>🛡 OverWall</div>{links}</div>"
        f"<div class='main'>{flash}{body}</div></div></body></html>"
    )
    return web.Response(text=doc, content_type="text/html")


def _pager_html(base, page, total, extra=""):
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, pages))
    html_out = "<div class='pager'>"
    if page > 1:
        html_out += f"<a href='{base}?page={page-1}{extra}'>« قبلی</a>"
    html_out += f"<span>صفحه {page} از {pages} (کل: {total})</span>"
    if page < pages:
        html_out += f"<a href='{base}?page={page+1}{extra}'>بعدی »</a>"
    html_out += "</div>"
    return html_out


def _page_arg(request):
    try:
        return max(1, int(request.query.get("page", "1")))
    except (TypeError, ValueError):
        return 1


# ================= ورود / خروج =================
async def login_get(request):
    if _authed(request):
        raise web.HTTPFound("/")
    blocked = _login_blocked(request.remote)
    if blocked:
        err = f"<p style='color:#ff9aa6'>تلاش‌های ناموفق زیاد بود. {blocked} ثانیه دیگر تلاش کنید.</p>"
    else:
        err = "<p style='color:#ff9aa6'>رمز اشتباه است.</p>" if request.query.get("err") else ""
    body = f"""{PAGE_CSS}
    <div style='max-width:360px;margin:14vh auto;text-align:center'>
      <div class='box'>
        <h2>🛡 ورود مدیریت</h2>{err}
        <form method='post' action='/login'>
          <input type='password' name='password' placeholder='رمز عبور' autofocus style='width:100%;margin:8px 0'>
          <button type='submit' style='width:100%'>ورود</button>
        </form>
      </div>
    </div>"""
    return web.Response(text=f"<!doctype html><html lang='fa'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ورود</title></head><body style='background:#0b1220'>{body}</body></html>", content_type="text/html")


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
    r = await db.get_sales_report()
    cards = [
        ("💰", "کل فروش", f"{r['spent']:,}"),
        ("📅", "فروش امروز", f"{r['today_spent']:,}"),
        ("💳", "کل شارژ", f"{r['topup']:,}"),
        ("↩️", "برگشت وجه", f"{r['refunds']:,}"),
        ("📦", "سفارش‌ها", f"{r['orders']:,}"),
        ("👥", "کاربران", f"{r['users']:,}"),
        ("👛", "موجودی کیف‌پول‌ها", f"{r['balances']:,}"),
    ]
    cards_html = "".join(
        f"<div class='card'><div class='i'>{i}</div><div class='n'>{e(n)}</div><div class='l'>{e(l)}</div></div>"
        for i, l, n in cards
    )
    quick = """
    <div class='box'><h3>دسترسی سریع</h3><div class='row'>
      <a href='/users'><button class='btn-ghost'>👥 کاربران</button></a>
      <a href='/plans/edit'><button class='btn-ghost'>➕ افزودن پلن</button></a>
      <a href='/broadcast'><button class='btn-ghost'>📢 پیام همگانی</button></a>
      <a href='/backup'><button class='btn-ghost'>💾 دانلود بکاپ</button></a>
      <a href='/settings'><button class='btn-ghost'>⚙️ تنظیمات</button></a>
    </div></div>"""
    return layout("داشبورد", f"<h2>داشبورد</h2><div class='cards'>{cards_html}</div>{quick}", "/", request)


# ================= کاربران =================
def _role_badge(role):
    return "<span class='badge vip'>VIP 💎</span>" if role == "vip" else "<span class='badge norm'>عادی</span>"


@require_auth
async def users_page(request):
    search = request.query.get("q", "").strip() or None
    page = _page_arg(request)
    total = await db.count_users(search)
    users = await db.list_users(search=search, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
    rows = ""
    for u in users:
        rows += (
            f"<tr><td>{e(u['user_id'])}</td><td>{e(u['nickname'])}</td><td>{money(u['balance'])}</td>"
            f"<td>{_role_badge(u['role'])}</td>"
            f"<td class='actions'><a href='/users/view?id={e(u['user_id'])}'><button class='btn-ghost'>👁 جزئیات</button></a>"
            f"<a href='/users/edit?id={e(u['user_id'])}'><button>✏️</button></a></td></tr>"
        )
    extra = f"&q={quote(search)}" if search else ""
    body = f"""<h2>کاربران</h2>
    <p class='muted'>خرید عمده فقط برای نقش VIP فعال است.</p>
    <div class='row'>
      <a href='/users/edit'><button>➕ افزودن کاربر</button></a>
      <form class='inline' method='get' action='/users'><input name='q' placeholder='جستجو (آیدی/نام)' value='{e(search or "")}'><button class='btn-ghost'>🔎 جستجو</button></form>
    </div>
    <table><tr><th>آیدی</th><th>نام</th><th>موجودی</th><th>نقش</th><th>عملیات</th></tr>{rows}</table>
    {_pager_html('/users', page, total, extra)}"""
    return layout("کاربران", body, "/users", request)


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
            f"<tr><td>{e(o['id'])}</td><td>{e(email)}</td><td>{e(o['date'])}</td><td>{e(o['panel_id'])}</td>"
            f"<td><form class='inline' method='post' action='/orders/delete' onsubmit=\"return confirm('حذف سفارش؟')\">"
            f"<input type='hidden' name='id' value='{e(o['id'])}'><input type='hidden' name='back' value='/users/view?id={e(uid)}'>"
            f"<button class='btn-danger'>🗑</button></form></td></tr>"
        )
    o_table = f"<table><tr><th>#</th><th>سرویس</th><th>تاریخ</th><th>پنل</th><th></th></tr>{o_rows}</table>" if orders else "<p class='muted'>سفارشی ندارد.</p>"

    t_rows = ""
    for t in txns:
        d = t['date'].strftime('%Y-%m-%d %H:%M') if t['date'] else ''
        sign = "➕" if t['amount'] > 0 else "➖"
        t_rows += f"<tr><td>{sign} {abs(t['amount']):,}</td><td>{e(t['kind'])}</td><td>{e(t['description'])}</td><td>{e(d)}</td></tr>"
    t_table = f"<table><tr><th>مبلغ</th><th>نوع</th><th>توضیح</th><th>تاریخ</th></tr>{t_rows}</table>" if txns else "<p class='muted'>تراکنشی ندارد.</p>"

    other_role = "normal" if u['role'] == "vip" else "vip"
    other_label = "تبدیل به عادی" if u['role'] == "vip" else "ارتقا به VIP 💎"
    body = f"""<h2>کاربر {e(uid)}</h2>
    <div class='grid2'>
      <div class='box'>
        <h3>اطلاعات</h3>
        <p>نام: <b>{e(u['nickname'] or '—')}</b></p>
        <p>نقش: {_role_badge(u['role'])}</p>
        <p>موجودی: <b>{money(u['balance'])}</b> تومان</p>
        <p>اکانت تست گرفته: {'بله' if u['got_test'] else 'خیر'}</p>
        <p>دعوت‌کننده: {e(u['referred_by'] or '—')} | تعداد دعوت‌های او: {ref_count}</p>
        <div class='row' style='margin-top:10px'>
          <form class='inline' method='post' action='/users/adjust'>
            <input type='hidden' name='user_id' value='{e(uid)}'>
            <input type='hidden' name='back' value='/users/view?id={e(uid)}'>
            <input name='amount' placeholder='±مبلغ' style='width:110px'><button>تغییر موجودی</button>
          </form>
          <form class='inline' method='post' action='/users/role'>
            <input type='hidden' name='user_id' value='{e(uid)}'>
            <input type='hidden' name='role' value='{other_role}'>
            <button class='btn-ghost'>{other_label}</button>
          </form>
          <form class='inline' method='post' action='/users/delete' onsubmit="return confirm('حذف کامل کاربر؟')">
            <input type='hidden' name='user_id' value='{e(uid)}'>
            <button class='btn-danger'>🗑 حذف کاربر</button>
          </form>
          <a href='/users/edit?id={e(uid)}'><button>✏️ ویرایش</button></a>
        </div>
      </div>
      <div class='box'><h3>سفارش‌ها ({len(orders)})</h3>{o_table}</div>
    </div>
    <div class='box'><h3>تراکنش‌های اخیر</h3>{t_table}</div>
    <a href='/users'>« بازگشت به کاربران</a>"""
    return layout(f"کاربر {uid}", body, "/users", request)


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
    uid_field = (f"<input value='{uid_v}' disabled>" + f"<input type='hidden' name='user_id' value='{uid_v}'>") if not is_new else "<input name='user_id' placeholder='آیدی عددی کاربر'>"
    body = f"""<h2>{e(title)}</h2>
    <div class='box'><form method='post' action='/users/save'>
      <label>آیدی کاربر</label>{uid_field}
      <label>نام</label><input name='nickname' value='{nick_v}'>
      <label>موجودی (تومان)</label><input name='balance' value='{bal_v}'>
      <label>نقش</label><select name='role'>
        <option value='normal' {'selected' if role_v=='normal' else ''}>عادی</option>
        <option value='vip' {'selected' if role_v=='vip' else ''}>VIP</option>
      </select>
      <p class='muted'>خرید عمده فقط با نقش VIP فعال می‌شود.</p>
      <div class='row'><button type='submit'>💾 ذخیره</button> <a href='/users'>انصراف</a></div>
    </form></div>"""
    return layout(title, body, "/users", request)


@require_auth
async def user_save(request):
    data = await request.post()
    try:
        uid = int(data.get("user_id"))
        balance = int(data.get("balance") or 0)
    except (TypeError, ValueError):
        raise _redirect("/users", err="ورودی نامعتبر")
    nickname = (data.get("nickname") or "").strip() or None
    role = data.get("role") if data.get("role") in ("normal", "vip") else "normal"
    await db.save_user(uid, nickname, role, False, balance)
    logging.info("WEB_USER_SAVE user=%s role=%s balance=%s", uid, role, balance)
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
async def user_role(request):
    data = await request.post()
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        raise _redirect("/users", err="آیدی نامعتبر")
    role = data.get("role") if data.get("role") in ("normal", "vip") else "normal"
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
            f"<tr><td>{e(o['id'])}</td><td><a href='/users/view?id={e(o['user_id'])}'>{e(o['user_id'])}</a></td>"
            f"<td>{e(email)}</td><td>{e(o['date'])}</td><td>{e(o['panel_id'])}</td>"
            f"<td><form class='inline' method='post' action='/orders/delete' onsubmit=\"return confirm('حذف سفارش؟')\">"
            f"<input type='hidden' name='id' value='{e(o['id'])}'><button class='btn-danger'>🗑</button></form></td></tr>"
        )
    extra = f"&q={quote(search)}" if search else ""
    body = f"""<h2>سفارش‌ها</h2>
    <form class='inline' method='get' action='/orders'><input name='q' placeholder='جستجو (نام سرویس/آیدی)' value='{e(search or "")}'><button class='btn-ghost'>🔎 جستجو</button></form>
    <table><tr><th>#</th><th>کاربر</th><th>نام سرویس</th><th>تاریخ</th><th>پنل</th><th></th></tr>{rows}</table>
    {_pager_html('/orders', page, total, extra)}"""
    return layout("سفارش‌ها", body, "/orders", request)


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
        d = t['date'].strftime('%Y-%m-%d %H:%M') if t['date'] else ''
        sign = "➕" if t['amount'] > 0 else "➖"
        rows += f"<tr><td><a href='/users/view?id={e(t['user_id'])}'>{e(t['user_id'])}</a></td><td>{sign} {abs(t['amount']):,}</td><td>{e(t['kind'])}</td><td>{e(t['description'])}</td><td>{e(d)}</td></tr>"
    body = f"""<h2>تراکنش‌ها</h2>
    <table><tr><th>کاربر</th><th>مبلغ</th><th>نوع</th><th>توضیح</th><th>تاریخ</th></tr>{rows}</table>
    {_pager_html('/transactions', page, total)}"""
    return layout("تراکنش‌ها", body, "/transactions", request)


# ================= پلن‌ها =================
@require_auth
async def plans_page(request):
    plans = await db.list_plans()
    panels = await db.get_panels()
    pname = {p['id']: p['name'] for p in panels}
    rows = ""
    for p in plans:
        icon = (p['icon'] or '').strip() if 'icon' in p else ''
        rows += (
            f"<tr><td>{e(p['id'])}</td><td>{e(icon)}</td><td>{e(p['name'])}</td><td>{e(p['gb'])}</td><td>{e(p['duration_days'])}</td>"
            f"<td>{money(p['price'])}</td><td>{money(p['vip_price'])}</td><td>{money(p['vip_bulk_price'])}</td><td>{e(p['inbound_id'])}</td>"
            f"<td>{e(pname.get(p['panel_id'], p['panel_id']))}</td>"
            f"<td class='actions'><a href='/plans/edit?id={e(p['id'])}'><button>✏️</button></a>"
            f"<form class='inline' method='post' action='/plans/delete' onsubmit=\"return confirm('حذف پلن؟')\">"
            f"<input type='hidden' name='id' value='{e(p['id'])}'><button class='btn-danger'>🗑</button></form></td></tr>"
        )
    body = f"""<h2>پلن‌ها</h2>
    <p class='muted'>پلن‌های جدید پایین‌تر نمایش داده می‌شوند. برای رنگی/دسته‌بندی‌شدن دکمه‌ها، در فیلد «آیکون» یک ایموجی بگذار (مثل 🟦 برای تانل، 🟥 برای مستقیم).</p>
    <a href='/plans/edit'><button>➕ افزودن پلن</button></a>
    <table><tr><th>#</th><th>آیکون</th><th>نام</th><th>حجم</th><th>روز</th><th>عادی</th><th>VIP</th><th>عمده</th><th>اینباند</th><th>پنل</th><th>عملیات</th></tr>{rows}</table>"""
    return layout("پلن‌ها", body, "/plans", request)


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

    hidden_id = "" if is_new else f"<input type='hidden' name='id' value='{e(pid)}'>"
    body = f"""<h2>{e(title)}</h2>
    <div class='box'><form method='post' action='/plans/save'>{hidden_id}
      <div class='grid2'>
        <div><label>نام</label><input name='name' value='{val('name')}'></div>
        <div><label>آیکون/ایموجی (اختیاری) مثل 🟦 یا 🟥</label><input name='icon' value='{val('icon')}'></div>
        <div><label>حجم (GB)</label><input name='gb' value='{val('gb','0')}'></div>
        <div><label>مدت (روز)</label><input name='duration_days' value='{val('duration_days','30')}'></div>
        <div><label>قیمت عادی</label><input name='price' value='{val('price','0')}'></div>
        <div><label>قیمت VIP</label><input name='vip_price' value='{val('vip_price','0')}'></div>
        <div><label>قیمت عمده (VIP)</label><input name='vip_bulk_price' value='{val('vip_bulk_price','0')}'></div>
        <div><label>اینباند (Inbound ID)</label><input name='inbound_id' value='{val('inbound_id','1')}'></div>
        <div><label>پنل</label><select name='panel_id'>{options}</select></div>
      </div>
      <p class='muted'>قیمت عمده فقط برای کاربران VIP اعمال می‌شود.</p>
      <div class='row'><button type='submit'>💾 ذخیره</button> <a href='/plans'>انصراف</a></div>
    </form></div>"""
    return layout(title, body, "/plans", request)


@require_auth
async def plan_save(request):
    data = await request.post()
    try:
        name = (data.get("name") or "").strip()
        gb = int(data.get("gb") or 0)
        duration_days = int(data.get("duration_days") or 30)
        price = int(data.get("price") or 0)
        vip_price = int(data.get("vip_price") or 0)
        vip_bulk_price = int(data.get("vip_bulk_price") or 0)
        inbound_id = int(data.get("inbound_id") or 0)
        panel_id = int(data.get("panel_id")) if data.get("panel_id") else None
    except (TypeError, ValueError):
        raise _redirect("/plans", err="ورودی نامعتبر")
    if not name:
        raise _redirect("/plans", err="نام پلن الزامی است")
    icon = (data.get("icon") or "").strip()
    # ستون عمده‌ی عادی حذف شده؛ همان قیمت عمده را در آن هم ذخیره می‌کنیم تا سازگاری حفظ شود
    bulk_price = vip_bulk_price
    pid = data.get("id")
    if pid and pid.isdigit():
        await db.update_plan(int(pid), name, gb, duration_days, price, vip_price, bulk_price, vip_bulk_price, inbound_id, panel_id, icon)
    else:
        await db.create_plan(name, gb, duration_days, price, vip_price, bulk_price, vip_bulk_price, inbound_id, panel_id, icon)
    logging.info("WEB_PLAN_SAVE name=%s panel=%s", name, panel_id)
    raise _redirect("/plans", ok="پلن ذخیره شد")


@require_auth
async def plan_delete(request):
    data = await request.post()
    pid = data.get("id")
    if pid and str(pid).isdigit():
        await db.delete_plan(int(pid))
        logging.info("WEB_PLAN_DELETE id=%s", pid)
    raise _redirect("/plans", ok="پلن حذف شد")


# ================= پنل‌ها =================
@require_auth
async def panels_page(request):
    panels = await db.get_panels()
    rows = ""
    for p in panels:
        rows += (
            f"<tr><td>{e(p['id'])}</td><td>{e(p['name'])}</td><td>{e(p['url'])}</td><td>{e(p['config_ip'])}</td><td>{e(p['sub_url'])}</td>"
            f"<td class='actions'><a href='/panels/edit?id={e(p['id'])}'><button>✏️</button></a>"
            f"<form class='inline' method='post' action='/panels/delete' onsubmit=\"return confirm('حذف پنل؟ (پلن‌ها و سفارش‌های متصل باید منتقل شوند)')\">"
            f"<input type='hidden' name='id' value='{e(p['id'])}'><button class='btn-danger'>🗑</button></form></td></tr>"
        )
    body = f"""<h2>پنل‌ها</h2>
    <a href='/panels/edit'><button>➕ افزودن پنل</button></a>
    <table><tr><th>#</th><th>نام</th><th>URL</th><th>IP کانفیگ</th><th>Sub</th><th>عملیات</th></tr>{rows}</table>"""
    return layout("پنل‌ها", body, "/panels", request)


@require_auth
async def panel_edit_form(request):
    pid = request.query.get("id")
    p = await db.get_panel(int(pid)) if pid and pid.isdigit() else None
    is_new = p is None
    title = "افزودن پنل" if is_new else f"ویرایش پنل {pid}"

    def val(key):
        return e(p[key]) if p else ""

    hidden_id = "" if is_new else f"<input type='hidden' name='id' value='{e(pid)}'>"
    body = f"""<h2>{e(title)}</h2>
    <div class='box'><form method='post' action='/panels/save'>{hidden_id}
      <div class='grid2'>
        <div><label>نام</label><input name='name' value='{val('name')}'></div>
        <div><label>آدرس پنل (URL کامل با https:// و پورت)</label><input name='url' value='{val('url')}'></div>
        <div><label>نام کاربری</label><input name='username' value='{val('username')}'></div>
        <div><label>رمز عبور {'(خالی بگذارید تا تغییر نکند)' if not is_new else ''}</label><input type='password' name='password' placeholder='{'••••••••' if not is_new else ''}'></div>
        <div><label>IP کانفیگ (sni/host)</label><input name='config_ip' value='{val('config_ip')}'></div>
        <div><label>لینک ساب (اختیاری)</label><input name='sub_url' value='{val('sub_url')}'></div>
      </div>
      <div class='row'><button type='submit'>💾 ذخیره</button> <a href='/panels'>انصراف</a></div>
    </form></div>"""
    return layout(title, body, "/panels", request)


@require_auth
async def panel_save(request):
    data = await request.post()
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    config_ip = (data.get("config_ip") or "").strip()
    sub_url = (data.get("sub_url") or "").strip()
    if not (name and url):
        raise _redirect("/panels", err="نام و URL الزامی است")
    pid = data.get("id")
    if pid and pid.isdigit():
        # فرم رمز را از پیش پر نمی‌کند؛ خالی بودن یعنی «رمز فعلی حفظ شود»
        if not password:
            current = await db.get_panel(int(pid))
            password = current['password'] if current else ""
        await db.update_panel(int(pid), name, url, username, password, config_ip, sub_url)
    else:
        await db.add_panel(name, url, username, password, config_ip, sub_url)
    logging.info("WEB_PANEL_SAVE name=%s", name)
    raise _redirect("/panels", ok="پنل ذخیره شد")


@require_auth
async def panel_delete(request):
    data = await request.post()
    pid = data.get("id")
    if pid and str(pid).isdigit():
        await db.delete_panel(int(pid))
        logging.info("WEB_PANEL_DELETE id=%s", pid)
    raise _redirect("/panels", ok="پنل حذف شد")


# ================= کدهای هدیه =================
@require_auth
async def giftcodes_page(request):
    codes = await db.list_gift_codes()
    rows = ""
    for c in codes:
        rows += (
            f"<tr><td>{e(c['code'])}</td><td>{c['amount']:,}</td><td>{e(c['used_count'])}/{e(c['max_uses'])}</td>"
            f"<td><form class='inline' method='post' action='/giftcodes/delete' onsubmit=\"return confirm('حذف کد؟')\">"
            f"<input type='hidden' name='code' value='{e(c['code'])}'><button class='btn-danger'>🗑</button></form></td></tr>"
        )
    body = f"""<h2>کدهای هدیه</h2>
    <div class='box'><form class='row' method='post' action='/giftcodes/add'>
      <input name='code' placeholder='کد'>
      <input name='amount' placeholder='مبلغ (تومان)'>
      <input name='max_uses' placeholder='حداکثر دفعات' value='1'>
      <button>➕ افزودن</button>
    </form></div>
    <table><tr><th>کد</th><th>مبلغ</th><th>استفاده</th><th></th></tr>{rows}</table>"""
    return layout("کدهای هدیه", body, "/giftcodes", request)


@require_auth
async def giftcodes_add(request):
    data = await request.post()
    try:
        code = (data.get("code") or "").strip()
        amount = int(data.get("amount"))
        max_uses = max(1, int(data.get("max_uses") or 1))
        if code:
            await db.add_gift_code(code, amount, max_uses)
            logging.info("WEB_GIFTCODE_ADD code=%s amount=%s", code, amount)
    except (TypeError, ValueError):
        raise _redirect("/giftcodes", err="ورودی نامعتبر")
    raise _redirect("/giftcodes", ok="کد هدیه اضافه شد")


@require_auth
async def giftcodes_delete(request):
    data = await request.post()
    code = (data.get("code") or "").strip()
    if code:
        await db.delete_gift_code(code)
        logging.info("WEB_GIFTCODE_DELETE code=%s", code)
    raise _redirect("/giftcodes", ok="کد حذف شد")


# ================= پیام همگانی =================
@require_auth
async def broadcast_page(request):
    body = """<h2>پیام همگانی</h2>
    <div class='box'><form method='post' action='/broadcast/send'>
      <label>متن پیام (برای همه‌ی کاربران ارسال می‌شود)</label>
      <textarea name='text' rows='5' style='width:100%' placeholder='متن پیام...'></textarea>
      <div class='row' style='margin-top:10px'><button type='submit'>📢 ارسال</button></div>
      <p class='muted'>ارسال در پس‌زمینه انجام می‌شود و ممکن است بسته به تعداد کاربران کمی طول بکشد.</p>
    </form></div>"""
    return layout("پیام همگانی", body, "/broadcast", request)


@require_auth
async def broadcast_send(request):
    data = await request.post()
    text = (data.get("text") or "").strip()
    if not text:
        raise _redirect("/broadcast", err="متن خالی است")
    if _BOT is None:
        raise _redirect("/broadcast", err="بات در دسترس نیست")
    ids = await db.all_user_ids()

    async def _run():
        sent, failed = 0, 0
        for uid in ids:
            try:
                await _BOT.send_message(chat_id=uid, text=text)
                sent += 1
            except Exception as ex:
                failed += 1
                # محدودیت نرخ تلگرام: به مدت خواسته‌شده صبر می‌کنیم
                retry_after = getattr(ex, "retry_after", None)
                if retry_after:
                    await asyncio.sleep(float(retry_after) + 1)
            await asyncio.sleep(0.05)  # حدود ۲۰ پیام در ثانیه، زیر سقف تلگرام
        logging.info("WEB_BROADCAST sent=%s failed=%s total=%s", sent, failed, len(ids))

    # نگه‌داشتن مرجع تسک لازم است؛ وگرنه ممکن است وسط کار توسط GC جمع شود
    task = asyncio.create_task(_run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    raise _redirect("/broadcast", ok=f"ارسال برای {len(ids)} کاربر آغاز شد")


# ================= VIP و ادمین =================
@require_auth
async def specials_page(request):
    vips = await db.list_vips()
    admins = await db.list_admin_rows()
    v_rows = ""
    for v in vips:
        v_rows += (
            f"<tr><td>{e(v['user_id'])}</td><td>{e(v['nickname'])}</td><td>{money(v['balance'])}</td>"
            f"<td><form class='inline' method='post' action='/specials/vip_remove'>"
            f"<input type='hidden' name='user_id' value='{e(v['user_id'])}'><button class='btn-danger'>حذف VIP</button></form></td></tr>"
        )
    a_rows = "".join(f"<tr><td>{e(a['user_id'])}</td><td>{e(a['name'])}</td></tr>" for a in admins)
    body = f"""<h2>VIP و ادمین</h2>
    <div class='box'><h3>افزودن VIP</h3>
      <form class='row' method='post' action='/specials/vip_add'>
        <input name='user_id' placeholder='آیدی عددی کاربر'>
        <input name='nickname' placeholder='نام (اختیاری)'>
        <button>💎 افزودن VIP</button>
      </form>
    </div>
    <h3>لیست VIP‌ها</h3>
    <table><tr><th>آیدی</th><th>نام</th><th>موجودی</th><th></th></tr>{v_rows or '<tr><td colspan=4>موردی نیست</td></tr>'}</table>
    <h3>لیست ادمین‌ها (مدیریت از داخل ربات)</h3>
    <table><tr><th>آیدی</th><th>نام</th></tr>{a_rows or '<tr><td colspan=2>موردی نیست</td></tr>'}</table>"""
    return layout("VIP و ادمین", body, "/specials", request)


@require_auth
async def vip_add(request):
    data = await request.post()
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        raise _redirect("/specials", err="آیدی نامعتبر")
    nickname = (data.get("nickname") or "").strip() or None
    async with db.db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, nickname, role) VALUES ($1, $2, 'vip') ON CONFLICT (user_id) DO UPDATE SET role='vip', nickname=COALESCE($2, users.nickname)",
            uid, nickname,
        )
    logging.info("WEB_VIP_ADD user=%s", uid)
    raise _redirect("/specials", ok="کاربر VIP شد")


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


# ================= تنظیمات =================
def _sel(cur, val):
    return "selected" if cur == val else ""


@require_auth
async def settings_page(request):
    sales = await db.get_setting("sales_status")
    card = await db.get_setting("card_number")
    support = await db.get_setting("support_id")
    notify_days = await db.get_setting("notify_days")
    ref_bonus = await db.get_setting("ref_bonus")
    backup_enabled = await db.get_setting("backup_enabled")
    test_enabled = await db.get_setting("test_enabled")
    test_gb = await db.get_setting("test_gb")
    test_days = await db.get_setting("test_days")
    test_panel_id = await db.get_setting("test_panel_id")
    test_inbound_id = await db.get_setting("test_inbound_id")
    panels = await db.get_panels()
    opts = "<option value=''>—</option>" + "".join(
        f"<option value='{e(p['id'])}' {_sel(str(test_panel_id or ''), str(p['id']))}>{e(p['id'])} - {e(p['name'])}</option>"
        for p in panels
    )
    body = f"""<h2>تنظیمات</h2>
    <div class='box'><form method='post' action='/settings/save'>
      <div class='grid2'>
        <div><label>وضعیت فروش</label><select name='sales_status'>
          <option value='open' {_sel(sales,'open')}>باز</option>
          <option value='closed' {_sel(sales,'closed')}>بسته</option>
        </select></div>
        <div><label>شماره کارت</label><input name='card_number' value='{e(card)}'></div>
        <div><label>آیدی پشتیبانی</label><input name='support_id' value='{e(support)}'></div>
        <div><label>هشدار انقضا (روز مانده)</label><input name='notify_days' value='{e(notify_days)}'></div>
        <div><label>پاداش دعوت (تومان)</label><input name='ref_bonus' value='{e(ref_bonus)}'></div>
        <div><label>بکاپ خودکار روزانه</label><select name='backup_enabled'>
          <option value='on' {_sel(backup_enabled,'on')}>روشن</option>
          <option value='off' {_sel(backup_enabled,'off')}>خاموش</option>
        </select></div>
      </div>
      <h3 style='margin-top:16px'>اکانت تست رایگان</h3>
      <div class='grid2'>
        <div><label>وضعیت</label><select name='test_enabled'>
          <option value='on' {_sel(test_enabled,'on')}>روشن</option>
          <option value='off' {_sel(test_enabled,'off')}>خاموش</option>
        </select></div>
        <div><label>حجم (GB)</label><input name='test_gb' value='{e(test_gb)}'></div>
        <div><label>مدت (روز)</label><input name='test_days' value='{e(test_days)}'></div>
        <div><label>پنل اکانت تست</label><select name='test_panel_id'>{opts}</select></div>
        <div><label>اینباند اکانت تست</label><input name='test_inbound_id' value='{e(test_inbound_id)}'></div>
      </div>
      <div class='row' style='margin-top:12px'><button type='submit'>💾 ذخیره تنظیمات</button></div>
    </form></div>
    <div class='box'><h3>بکاپ دیتابیس</h3>
      <a href='/backup'><button class='btn-ghost'>💾 دانلود بکاپ (.sql)</button></a>
    </div>"""
    return layout("تنظیمات", body, "/settings", request)


@require_auth
async def settings_save(request):
    data = await request.post()
    sales = data.get("sales_status") if data.get("sales_status") in ("open", "closed") else "open"
    await db.update_setting("sales_status", sales)
    await db.update_setting("card_number", (data.get("card_number") or "").strip())
    await db.update_setting("support_id", (data.get("support_id") or "").strip())
    for key in ("notify_days", "ref_bonus", "test_gb", "test_days"):
        raw = (data.get(key) or "").strip()
        if raw.isdigit():
            await db.update_setting(key, raw)
    await db.update_setting("backup_enabled", "on" if data.get("backup_enabled") == "on" else "off")
    await db.update_setting("test_enabled", "on" if data.get("test_enabled") == "on" else "off")
    tp = (data.get("test_panel_id") or "").strip()
    await db.update_setting("test_panel_id", tp if tp.isdigit() else "")
    ti = (data.get("test_inbound_id") or "").strip()
    await db.update_setting("test_inbound_id", ti if ti.isdigit() else "")
    logging.info("WEB_SETTINGS_SAVE")
    raise _redirect("/settings", ok="تنظیمات ذخیره شد")


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


# ================= راه‌اندازی =================
def build_app():
    app = web.Application()
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
        web.get("/orders", orders_page),
        web.post("/orders/delete", order_delete),
        web.get("/transactions", transactions_page),
        web.get("/plans", plans_page),
        web.get("/plans/edit", plan_edit_form),
        web.post("/plans/save", plan_save),
        web.post("/plans/delete", plan_delete),
        web.get("/panels", panels_page),
        web.get("/panels/edit", panel_edit_form),
        web.post("/panels/save", panel_save),
        web.post("/panels/delete", panel_delete),
        web.get("/giftcodes", giftcodes_page),
        web.post("/giftcodes/add", giftcodes_add),
        web.post("/giftcodes/delete", giftcodes_delete),
        web.get("/broadcast", broadcast_page),
        web.post("/broadcast/send", broadcast_send),
        web.get("/specials", specials_page),
        web.post("/specials/vip_add", vip_add),
        web.post("/specials/vip_remove", vip_remove),
        web.get("/settings", settings_page),
        web.post("/settings/save", settings_save),
        web.get("/backup", backup_download),
    ])
    return app


async def start_web(bot=None):
    """در صورت تنظیم رمز، سرور وب را داخل همان event loop ربات اجرا می‌کند و runner را برمی‌گرداند.
    bot برای قابلیت‌هایی مثل پیام همگانی از پنل وب نگه داشته می‌شود."""
    global _BOT
    _BOT = bot
    if not WEB_ADMIN_PASSWORD:
        logging.info("پنل وب غیرفعال است (WEB_ADMIN_PASSWORD تنظیم نشده).")
        return None
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    logging.info("🌐 پنل وب مدیریت روی %s:%s اجرا شد.", WEB_HOST, WEB_PORT)
    return runner
