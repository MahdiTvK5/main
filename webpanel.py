"""پنل وب مدیریت ساده مبتنی بر aiohttp.

اگر WEB_ADMIN_PASSWORD تنظیم نشده باشد، پنل اجرا نمی‌شود.
احراز هویت با یک رمز عبور و کوکی امضاشده (HMAC) انجام می‌شود.
"""
import html
import hmac
import hashlib
import logging

from aiohttp import web

import db
from config import WEB_ADMIN_PASSWORD, WEB_PORT, WEB_HOST


def _secret():
    return hashlib.sha256((WEB_ADMIN_PASSWORD or "no-secret").encode()).digest()


def _token():
    return hmac.new(_secret(), b"admin-session-v1", hashlib.sha256).hexdigest()


def _authed(request):
    if not WEB_ADMIN_PASSWORD:
        return False
    return hmac.compare_digest(request.cookies.get("session", ""), _token())


def e(v):
    return html.escape(str(v if v is not None else ""))


PAGE_CSS = """
<style>
*{box-sizing:border-box}body{margin:0;font-family:Tahoma,Arial,sans-serif;background:#0f172a;color:#e2e8f0;direction:rtl}
a{color:#38bdf8;text-decoration:none}
.nav{background:#1e293b;padding:12px 16px;display:flex;gap:14px;flex-wrap:wrap;align-items:center;border-bottom:1px solid #334155}
.nav a{padding:6px 10px;border-radius:8px}
.nav a.active,.nav a:hover{background:#334155}
.wrap{max-width:1100px;margin:18px auto;padding:0 16px}
.cards{display:flex;gap:14px;flex-wrap:wrap}
.card{background:#1e293b;border:1px solid #334155;border-radius:14px;padding:16px;min-width:170px;flex:1}
.card .n{font-size:22px;font-weight:bold;color:#fff}
.card .l{color:#94a3b8;font-size:13px;margin-top:6px}
table{width:100%;border-collapse:collapse;background:#1e293b;border-radius:12px;overflow:hidden;margin-top:14px}
th,td{padding:10px;border-bottom:1px solid #334155;text-align:right;font-size:13px}
th{background:#0b1220;color:#94a3b8}
input,select,button{font-family:inherit;padding:8px 10px;border-radius:8px;border:1px solid #334155;background:#0b1220;color:#e2e8f0}
button{background:#2563eb;border:0;cursor:pointer}
button:hover{background:#1d4ed8}
form.inline{display:inline-flex;gap:6px;align-items:center}
h2{margin:18px 0 8px}
.box{background:#1e293b;border:1px solid #334155;border-radius:14px;padding:16px;margin-top:14px}
.login{max-width:360px;margin:12vh auto;text-align:center}
</style>
"""

NAV = [
    ("/", "داشبورد"),
    ("/users", "کاربران"),
    ("/orders", "سفارش‌ها"),
    ("/transactions", "تراکنش‌ها"),
    ("/plans", "پلن‌ها"),
    ("/panels", "پنل‌ها"),
    ("/giftcodes", "کدهای هدیه"),
    ("/settings", "تنظیمات"),
]


def layout(title, body, active=""):
    nav = "".join(f'<a class="{"active" if path == active else ""}" href="{path}">{e(label)}</a>' for path, label in NAV)
    nav += '<a href="/logout" style="margin-right:auto;color:#f87171">خروج</a>'
    return web.Response(
        text=f"<!doctype html><html lang='fa'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{e(title)}</title>{PAGE_CSS}</head><body><div class='nav'>{nav}</div><div class='wrap'>{body}</div></body></html>",
        content_type="text/html",
    )


def require_auth(handler):
    async def wrapper(request):
        if not _authed(request):
            raise web.HTTPFound("/login")
        return await handler(request)
    return wrapper


async def login_get(request):
    if _authed(request):
        raise web.HTTPFound("/")
    err = request.query.get("err")
    msg = "<p style='color:#f87171'>رمز اشتباه است.</p>" if err else ""
    body = f"""{PAGE_CSS}<div class='login'><h2>ورود مدیریت</h2>{msg}
    <form method='post' action='/login'>
      <input type='password' name='password' placeholder='رمز عبور' autofocus style='width:100%;margin:8px 0'>
      <button type='submit' style='width:100%'>ورود</button>
    </form></div>"""
    return web.Response(text=f"<!doctype html><html lang='fa'><head><meta charset='utf-8'><title>ورود</title></head><body style='background:#0f172a'>{body}</body></html>", content_type="text/html")


async def login_post(request):
    data = await request.post()
    if WEB_ADMIN_PASSWORD and hmac.compare_digest(data.get("password", ""), WEB_ADMIN_PASSWORD):
        resp = web.HTTPFound("/")
        resp.set_cookie("session", _token(), httponly=True, max_age=86400, samesite="Lax")
        logging.info("WEB_LOGIN success from %s", request.remote)
        return resp
    logging.warning("WEB_LOGIN failed from %s", request.remote)
    raise web.HTTPFound("/login?err=1")


async def logout(request):
    resp = web.HTTPFound("/login")
    resp.del_cookie("session")
    return resp


@require_auth
async def dashboard(request):
    r = await db.get_sales_report()
    cards = [
        ("کل فروش", f"{r['spent']:,}"),
        ("فروش امروز", f"{r['today_spent']:,}"),
        ("کل شارژ", f"{r['topup']:,}"),
        ("برگشت وجه", f"{r['refunds']:,}"),
        ("سفارش‌ها", f"{r['orders']:,}"),
        ("کاربران", f"{r['users']:,}"),
        ("موجودی کیف‌پول‌ها", f"{r['balances']:,}"),
    ]
    cards_html = "".join(f"<div class='card'><div class='n'>{e(n)}</div><div class='l'>{e(l)}</div></div>" for l, n in cards)
    return layout("داشبورد", f"<h2>داشبورد</h2><div class='cards'>{cards_html}</div>", "/")


@require_auth
async def users_page(request):
    search = request.query.get("q", "").strip() or None
    users = await db.list_users(search=search, limit=200)
    rows = ""
    for u in users:
        rows += (
            f"<tr><td>{e(u['user_id'])}</td><td>{e(u['nickname'])}</td><td>{u['balance']:,}</td>"
            f"<td>{e(u['role'])}</td><td>{'✅' if u['can_bulk'] else '—'}</td>"
            f"<td><form class='inline' method='post' action='/users/adjust'>"
            f"<input type='hidden' name='user_id' value='{e(u['user_id'])}'>"
            f"<input name='amount' placeholder='مبلغ (±)' style='width:110px'>"
            f"<button>اعمال</button></form></td></tr>"
        )
    body = f"""<h2>کاربران</h2>
    <form class='inline' method='get' action='/users'><input name='q' placeholder='جستجو (آیدی/نام)' value='{e(search or "")}'><button>جستجو</button></form>
    <table><tr><th>آیدی</th><th>نام</th><th>موجودی</th><th>نقش</th><th>عمده</th><th>تنظیم موجودی (مثبت/منفی)</th></tr>{rows}</table>"""
    return layout("کاربران", body, "/users")


@require_auth
async def users_adjust(request):
    data = await request.post()
    try:
        uid = int(data.get("user_id"))
        amount = int(data.get("amount"))
    except (TypeError, ValueError):
        raise web.HTTPFound("/users")
    if amount >= 0:
        await db.credit_balance(uid, amount, kind="تنظیم مدیریت", description="پنل وب")
    else:
        await db.credit_balance(uid, amount, kind="تنظیم مدیریت", description="پنل وب")
    logging.info("WEB_BALANCE_ADJUST user=%s amount=%s", uid, amount)
    raise web.HTTPFound("/users")


@require_auth
async def orders_page(request):
    search = request.query.get("q", "").strip() or None
    orders = await db.list_recent_orders(limit=100, search=search)
    rows = ""
    for o in orders:
        link = o['config_link']
        email = link.split("#")[-1] if "#" in link else ""
        rows += f"<tr><td>{e(o['id'])}</td><td>{e(o['user_id'])}</td><td>{e(email)}</td><td>{e(o['date'])}</td><td>{e(o['panel_id'])}</td></tr>"
    body = f"""<h2>سفارش‌ها</h2>
    <form class='inline' method='get' action='/orders'><input name='q' placeholder='جستجو' value='{e(search or "")}'><button>جستجو</button></form>
    <table><tr><th>#</th><th>کاربر</th><th>نام سرویس</th><th>تاریخ</th><th>پنل</th></tr>{rows}</table>"""
    return layout("سفارش‌ها", body, "/orders")


@require_auth
async def transactions_page(request):
    txns = await db.list_recent_transactions(limit=100)
    rows = ""
    for t in txns:
        d = t['date'].strftime('%Y-%m-%d %H:%M') if t['date'] else ''
        rows += f"<tr><td>{e(t['user_id'])}</td><td>{t['amount']:,}</td><td>{e(t['kind'])}</td><td>{e(t['description'])}</td><td>{e(d)}</td></tr>"
    body = f"<h2>تراکنش‌ها</h2><table><tr><th>کاربر</th><th>مبلغ</th><th>نوع</th><th>توضیح</th><th>تاریخ</th></tr>{rows}</table>"
    return layout("تراکنش‌ها", body, "/transactions")


@require_auth
async def plans_page(request):
    plans = await db.list_plans()
    rows = ""
    for p in plans:
        rows += (
            f"<tr><td>{e(p['id'])}</td><td>{e(p['name'])}</td><td>{e(p['gb'])}</td><td>{e(p['duration_days'])}</td>"
            f"<td>{p['price']:,}</td><td>{p['vip_price']:,}</td><td>{p['bulk_price']:,}</td><td>{e(p['inbound_id'])}</td><td>{e(p['panel_id'])}</td></tr>"
        )
    body = f"<h2>پلن‌ها</h2><table><tr><th>#</th><th>نام</th><th>حجم</th><th>روز</th><th>عادی</th><th>VIP</th><th>عمده</th><th>اینباند</th><th>پنل</th></tr>{rows}</table>"
    return layout("پلن‌ها", body, "/plans")


@require_auth
async def panels_page(request):
    panels = await db.get_panels()
    rows = ""
    for p in panels:
        rows += f"<tr><td>{e(p['id'])}</td><td>{e(p['name'])}</td><td>{e(p['url'])}</td><td>{e(p['config_ip'])}</td><td>{e(p['sub_url'])}</td></tr>"
    body = f"<h2>پنل‌ها</h2><table><tr><th>#</th><th>نام</th><th>URL</th><th>IP</th><th>Sub</th></tr>{rows}</table>"
    return layout("پنل‌ها", body, "/panels")


@require_auth
async def giftcodes_page(request):
    codes = await db.list_gift_codes()
    rows = ""
    for c in codes:
        rows += f"<tr><td>{e(c['code'])}</td><td>{c['amount']:,}</td><td>{e(c['used_count'])}/{e(c['max_uses'])}</td></tr>"
    body = f"""<h2>کدهای هدیه</h2>
    <div class='box'><form class='inline' method='post' action='/giftcodes/add'>
      <input name='code' placeholder='کد'>
      <input name='amount' placeholder='مبلغ'>
      <input name='max_uses' placeholder='حداکثر دفعات' value='1'>
      <button>افزودن</button>
    </form></div>
    <table><tr><th>کد</th><th>مبلغ</th><th>استفاده</th></tr>{rows}</table>"""
    return layout("کدهای هدیه", body, "/giftcodes")


@require_auth
async def giftcodes_add(request):
    data = await request.post()
    try:
        code = data.get("code", "").strip()
        amount = int(data.get("amount"))
        max_uses = max(1, int(data.get("max_uses") or 1))
        if code:
            await db.add_gift_code(code, amount, max_uses)
            logging.info("WEB_GIFTCODE_ADD code=%s amount=%s", code, amount)
    except (TypeError, ValueError):
        pass
    raise web.HTTPFound("/giftcodes")


@require_auth
async def settings_page(request):
    sales = await db.get_setting("sales_status")
    card = await db.get_setting("card_number")
    support = await db.get_setting("support_id")
    body = f"""<h2>تنظیمات</h2>
    <div class='box'>
      <p>وضعیت فروش: <b>{e(sales)}</b></p>
      <form class='inline' method='post' action='/settings/sales'><button>تغییر وضعیت فروش</button></form>
    </div>
    <div class='box'><form class='inline' method='post' action='/settings/card'>
      <input name='card' value='{e(card)}' style='width:240px'><button>ذخیره کارت</button></form></div>
    <div class='box'><form class='inline' method='post' action='/settings/support'>
      <input name='support' value='{e(support)}' style='width:240px'><button>ذخیره پشتیبانی</button></form></div>"""
    return layout("تنظیمات", body, "/settings")


@require_auth
async def settings_sales(request):
    cur = await db.get_setting("sales_status")
    await db.update_setting("sales_status", "closed" if cur == "open" else "open")
    raise web.HTTPFound("/settings")


@require_auth
async def settings_card(request):
    data = await request.post()
    await db.update_setting("card_number", data.get("card", "").strip())
    raise web.HTTPFound("/settings")


@require_auth
async def settings_support(request):
    data = await request.post()
    await db.update_setting("support_id", data.get("support", "").strip())
    raise web.HTTPFound("/settings")


def build_app():
    app = web.Application()
    app.add_routes([
        web.get("/login", login_get),
        web.post("/login", login_post),
        web.get("/logout", logout),
        web.get("/", dashboard),
        web.get("/users", users_page),
        web.post("/users/adjust", users_adjust),
        web.get("/orders", orders_page),
        web.get("/transactions", transactions_page),
        web.get("/plans", plans_page),
        web.get("/panels", panels_page),
        web.get("/giftcodes", giftcodes_page),
        web.post("/giftcodes/add", giftcodes_add),
        web.get("/settings", settings_page),
        web.post("/settings/sales", settings_sales),
        web.post("/settings/card", settings_card),
        web.post("/settings/support", settings_support),
    ])
    return app


async def start_web():
    """در صورت تنظیم رمز، سرور وب را داخل همان event loop ربات اجرا می‌کند و runner را برمی‌گرداند."""
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
