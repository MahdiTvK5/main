"""پورتال اشتراک: صفحه‌ی وب عمومیِ هر سرویس + خروجی خامِ subscription.

معماری (سازگار با بقیه‌ی پروژه):
* لایه‌ی خالص (قابل تست بدون دیتابیس/شبکه): توکن، محاسبه‌ی ترافیک/وضعیت، ساخت
  لینک ایمپورت اپ‌ها، انکودر subscription خام، مبدل Clash، رندر HTML.
* لایه‌ی متصل: یافتن سفارش با توکن و خواندن «وضعیت زنده» از پنل 3x-UI از طریق
  همون لایه‌ی `panel.py` که بات استفاده می‌کند. پنل 3x-UI منبعِ حقیقت است؛
  پورتال هیچ کپی‌ای از ترافیک/انقضا/UUID نگه نمی‌دارد (فقط کش کوتاه چندثانیه‌ای).

امنیت:
* توکن تصادفی (`secrets`) روی خود سفارش ذخیره می‌شود؛ هیچ user_id یا panel_id
  در URL عمومی نیست و بدون توکن معتبر هیچ داده‌ای برگردانده نمی‌شود (ضد IDOR).
* هیچ اطلاعات حساسی (یوزر/پسورد پنل، توکن ربات، ...) در خروجی‌ها قرار نمی‌گیرد.
"""
import base64
import datetime
import html
import json
import logging
import secrets
import time
from urllib.parse import quote

import db
from config import PORTAL_PUBLIC_URL, format_size
from links import order_email
from panel import _stream_params, build_share_link, build_xui, file_link_kind

log = logging.getLogger("subportal")

BRAND = "OverWall"

# ================= اتصال سریع (فهرست اپ‌ها کاملاً قابل‌تغییر است) =================
# هر اپ: id یکتا، نام نمایشی، و قالبِ URI ایمپورت. placeholder ها:
#   {url} لینک اشتراک خام | {url_enc} همان با URL-encode | {url_b64} Base64
#   {clash} لینک اشتراک با فرمت Clash | {clash_enc} همان با URL-encode
#   {name} نام سرویس | {name_enc} همان با URL-encode
# scheme=None یعنی برنامه اسکیم ایمپورت ندارد → دکمه‌ی «کپی لینک» جای آن می‌نشیند.
# initial/accent: آواتار برند در UI؛ recommended: بج «پیشنهادی» آن پلتفرم.
QUICK_CONNECT = {
    'android': {
        'label': 'اندروید',
        'icon': '🤖',
        'apps': [
            {'id': 'v2rayng', 'name': 'V2RayNG', 'scheme': 'v2rayng://install-sub?url={url_enc}',
             'initial': 'V', 'accent': ('#7C5CFF', '#4F46E5'), 'recommended': True},
            {'id': 'happ', 'name': 'Happ', 'scheme': 'happ://add/{url}',
             'initial': 'H', 'accent': ('#2563EB', '#1D4ED8')},
            {'id': 'incy', 'name': 'INCY', 'scheme': 'incy://import/{url}',
             'initial': 'I', 'accent': ('#0EA5E9', '#0284C7')},
            {'id': 'clashmeta', 'name': 'Clash Meta', 'scheme': 'clashmeta://install-config?url={clash_enc}&name={name_enc}',
             'initial': 'C', 'accent': ('#F59E0B', '#D97706')},
            {'id': 'nekobox', 'name': 'NekoBox', 'scheme': None,
             'initial': 'N', 'accent': ('#10B981', '#059669'),
             'note': 'لینک اشتراک را کپی کنید و در برنامه از Subscription اضافه کنید.'},
        ],
    },
    'ios': {
        'label': 'iOS',
        'icon': '🍏',
        'apps': [
            {'id': 'shadowrocket', 'name': 'Shadowrocket', 'scheme': 'sub://{url_b64}',
             'initial': 'S', 'accent': ('#3B82F6', '#2563EB'), 'recommended': True},
            {'id': 'streisand', 'name': 'Streisand', 'scheme': 'streisand://import/{url}',
             'initial': 'S', 'accent': ('#EC4899', '#DB2777')},
            {'id': 'v2box', 'name': 'V2Box', 'scheme': 'v2box://install-sub?url={url_enc}&name={name_enc}',
             'initial': 'V', 'accent': ('#6366F1', '#4F46E5')},
            {'id': 'happ_ios', 'name': 'Happ', 'scheme': 'happ://add/{url}',
             'initial': 'H', 'accent': ('#2563EB', '#1D4ED8')},
            {'id': 'incy_ios', 'name': 'INCY', 'scheme': 'incy://import/{url}',
             'initial': 'I', 'accent': ('#0EA5E9', '#0284C7')},
        ],
    },
    'windows': {
        'label': 'ویندوز',
        'icon': '🖥️',
        'apps': [
            {'id': 'clashverge', 'name': 'Clash Verge', 'scheme': 'clash://install-config?url={clash_enc}&name={name_enc}',
             'initial': 'C', 'accent': ('#F59E0B', '#D97706')},
            {'id': 'v2rayn', 'name': 'v2rayN', 'scheme': None,
             'initial': 'V', 'accent': ('#3B82F6', '#2563EB'), 'recommended': True,
             'note': 'لینک اشتراک را کپی کنید و در v2rayN از Subscriptions اضافه کنید.'},
            {'id': 'nekoray', 'name': 'Nekoray', 'scheme': None,
             'initial': 'N', 'accent': ('#10B981', '#059669'),
             'note': 'لینک اشتراک را کپی کنید و در Nekoray از Profile اضافه کنید.'},
        ],
    },
    'macos': {
        'label': 'macOS',
        'icon': '🍎',
        'apps': [
            {'id': 'clashverge_mac', 'name': 'Clash Verge', 'scheme': 'clash://install-config?url={clash_enc}&name={name_enc}',
             'initial': 'C', 'accent': ('#F59E0B', '#D97706'), 'recommended': True},
            {'id': 'v2box_mac', 'name': 'V2Box', 'scheme': 'v2box://install-sub?url={url_enc}&name={name_enc}',
             'initial': 'V', 'accent': ('#6366F1', '#4F46E5')},
        ],
    },
}


def make_sub_token():
    """توکن امن و تصادفی برای شناسایی اشتراک (برگشت‌ناپذیر به user_id)."""
    return secrets.token_urlsafe(24)


def build_import_url(app, sub_url, clash_url, name=''):
    """لینک ایمپورت واقعی برای یک اپ؛ اگر اسکیم نداشته باشد None (fallback کپی)."""
    scheme = (app or {}).get('scheme')
    if not scheme or not sub_url:
        return None
    try:
        return scheme.format(
            url=str(sub_url),
            url_enc=quote(str(sub_url), safe=''),
            url_b64=base64.b64encode(str(sub_url).encode()).decode(),
            clash=str(clash_url or ''),
            clash_enc=quote(str(clash_url or ''), safe=''),
            name=str(name or ''),
            name_enc=quote(str(name or ''), safe=''),
        )
    except (KeyError, IndexError, ValueError):
        return None


def quick_connect_apps(sub_url, clash_url, name=''):
    """فهرست اپ‌های هر پلتفرم به‌همراه لینک ایمپورتِ آماده (یا None برای fallback)."""
    out = {}
    for pid, pdata in QUICK_CONNECT.items():
        apps = []
        for app in pdata['apps']:
            apps.append({
                'id': app['id'],
                'name': app['name'],
                'import_url': build_import_url(app, sub_url, clash_url, name),
                'note': app.get('note') or '',
                'initial': app.get('initial') or app['name'][:1].upper(),
                'accent': app.get('accent') or ('#7C5CFF', '#4F46E5'),
                'recommended': bool(app.get('recommended')),
            })
        out[pid] = {'label': pdata['label'], 'icon': pdata['icon'], 'apps': apps}
    return out

# ================= محاسبات خالصِ ترافیک / وضعیت =================
STATUS_LABELS = {
    'active': 'فعال',
    'expired': 'منقضی شده',
    'disabled': 'غیرفعال',
    'exhausted': 'پایان حجم',
}


def service_status(enabled, expiry_ms, total, used, now_ms=None):
    """وضعیت واقعی سرویس بر اساس داده‌ی کلاینتِ 3x-UI.

    اولویت: غیرفعالِ ادمین > منقضی > پایان حجم > فعال. expiry/total صفر یا منفی
    (مقدارهای نامعتبرِ برخی نسخه‌های 3x-UI) یعنی نامحدود.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if not enabled:
        return 'disabled'
    if 0 < expiry_ms < now_ms:
        return 'expired'
    if total and total > 0 and used >= total:
        return 'exhausted'
    return 'active'


def traffic_view(used, total):
    """خلاصه‌ی ترافیک برای نمایش. خروجی dict با فیلدهای آماده‌ی قالب.

    total صفر/None یعنی نامحدود؛ هیچ‌جا تقسیم بر صفر اتفاق نمی‌افتد.
    """
    used = max(0, int(used or 0))
    total = max(0, int(total or 0))
    unlimited = total == 0
    remaining = max(0, total - used) if not unlimited else None
    percent = None
    if not unlimited and total > 0:
        percent = min(100, round(used * 100 / total))
    return {
        'used': used,
        'total': total,
        'unlimited': unlimited,
        'remaining': remaining,
        'percent': percent,
        'used_text': format_size(used),
        'total_text': 'نامحدود' if unlimited else format_size(total),
        'remaining_text': 'نامحدود' if unlimited else format_size(remaining or 0),
    }


def expiry_view(expiry_ms, status_key, now_ms=None):
    """متن تاریخ و زمان باقی‌مانده‌ی انقضا.

    * صفر → بدون انقضا (نامحدود)
    * منفی → قاعده‌ی 3x-UI «انقضا از اولین اتصال»: مقدار منفی مدت‌زمان است
      (مثلاً -2592000000 = ۳۰ روز) و وقتی کلاینت اولین بار وصل شود پنل آن را
      به تاریخِ مطلق تبدیل می‌کند. چون شمارش هنوز شروع نشده، تاریخ مطلق نداریم
      و فقط مدت را نشان می‌دهیم.
    * مثبت → timestamp میلی‌ثانیه‌ی معمولی
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if not expiry_ms:
        return {'unlimited': True, 'ms': 0, 'date_text': 'بدون انقضا',
                'remain_text': 'نامحدود', 'remain_days': None}
    expiry_ms = int(expiry_ms)
    if expiry_ms < 0:
        total_ms = -expiry_ms
        days = total_ms // 86400000
        if days >= 1:
            remain_text = f'{days} روز از اولین اتصال'
        else:
            hours = max(1, int(total_ms // 3600000))
            remain_text = f'{hours} ساعت از اولین اتصال'
        return {'unlimited': False, 'ms': 0, 'date_text': 'با اولین اتصال',
                'remain_text': remain_text, 'remain_days': None}
    remain_ms = expiry_ms - now_ms
    if remain_ms <= 0 or status_key == 'expired':
        remain_text, remain_days = 'منقضی شده', 0
    else:
        remain_days = int(remain_ms // 86400000)
        if remain_days >= 1:
            remain_text = f'{remain_days} روز باقی‌مانده'
        else:
            hours = max(1, int(remain_ms // 3600000))
            remain_text = f'{hours} ساعت باقی‌مانده'
    try:
        dt = datetime.datetime.fromtimestamp(expiry_ms / 1000)
        date_text = dt.strftime('%Y-%m-%d %H:%M')
    except (OSError, OverflowError, ValueError):
        date_text = 'نامشخص'
    return {
        'unlimited': False,
        'ms': expiry_ms,
        # نمایش نهایی در مرورگر با تقویم شمسی (Intl fa-IR) انجام می‌شود؛ این متن
        # fallback سمت سرور است تا بدون JS هم صفحه درست دیده شود.
        'date_text': date_text,
        'remain_text': remain_text,
        'remain_days': remain_days,
    }


def stream_details(inbound):
    """مشخصات فنیِ غیرحساس برای نمایش: پروتکل/ترنسپورت/امنیت."""
    try:
        stream = json.loads((inbound or {}).get('streamSettings') or '{}')
    except Exception:
        stream = {}
    p = _stream_params(stream)
    security = (p.get('security') or 'none').lower()
    labels = {'ws': 'WebSocket', 'grpc': 'gRPC', 'tcp': 'TCP', 'xhttp': 'XHTTP',
              'splithttp': 'XHTTP', 'http': 'HTTP', 'h2': 'HTTP/2', 'quic': 'QUIC'}
    sec_labels = {'tls': 'TLS', 'reality': 'Reality', 'none': 'بدون رمزنگاری'}
    return {
        'protocol': str((inbound or {}).get('protocol') or 'vless').upper(),
        'transport': labels.get(p.get('type', 'tcp'), p.get('type', 'tcp')),
        'security': sec_labels.get(security, security),
        'type': p.get('type', 'tcp'),
        'security_raw': security,
    }

# ================= خروجی خام اشتراک و مبدل Clash =================
def build_raw_body(links):
    """خروجی استاندارد subscription: Base64 از لینک‌های share (هر کدام یک خط).

    همه‌ی کلاینت‌های سازگار با V2Ray/Clash-sub این قالب را می‌خوانند؛ لینک‌ها با
    `panel.build_share_link` از روی تنظیمات واقعی اینباند ساخته شده‌اند.
    """
    text = "\n".join(str(ln) for ln in links if ln)
    return base64.b64encode(text.encode()).decode()


def build_clash_yaml(name, inbound, client, host):
    """مبدل مینیمال به فرمت Clash Meta برای اپ‌های خانواده‌ی Clash.

    از روی همان اینباند/کلاینتِ واقعی 3x-UI ساخته می‌شود (بدون حدس زدن). خروجی
    یک کانفیگ کامل ولی جمع‌وجور است: proxies + یک proxy-group + rule پایه.
    پشتیبانی: vless/vmess/trojan با ws/grpc/tcp و tls/reality.
    """
    try:
        stream = json.loads((inbound or {}).get('streamSettings') or '{}')
    except Exception:
        stream = {}
    p = _stream_params(stream)
    protocol = ((inbound or {}).get('protocol') or 'vless').lower()
    port = int((inbound or {}).get('port') or 0)
    net = (p.get('type') or 'tcp').lower()
    security = (p.get('security') or 'none').lower()
    sni = p.get('sni') or host

    q = lambda v: '"' + str(v).replace('\\', '\\\\').replace('"', '\\"') + '"'
    lines = [f'  - name: {q(name)}']
    if protocol == 'vmess':
        lines += ['    type: vmess', f'    uuid: {q(client.get("id", ""))}',
                  '    alterId: 0', '    cipher: auto']
    elif protocol == 'trojan':
        lines += ['    type: trojan', f'    password: {q(client.get("password", ""))}']
    else:
        lines += ['    type: vless', f'    uuid: {q(client.get("id", ""))}']
        if client.get('flow'):
            lines.append(f'    flow: {q(client["flow"])}')
    lines += [f'    server: {q(host)}', f'    port: {port}', '    udp: true']

    if security in ('tls', 'reality'):
        lines += ['    tls: true', f'    servername: {q(sni)}',
                  '    client-fingerprint: chrome']
        if security == 'reality':
            lines.append('    reality-opts:')
            if p.get('pbk'):
                lines.append(f'      public-key: {q(p["pbk"])}')
            if p.get('sid'):
                lines.append(f'      short-id: {q(p["sid"])}')
    if net == 'ws':
        lines.append('    network: ws')
        ws_opts = [f'      path: {q(p.get("path", "/"))}']
        if p.get('host'):
            ws_opts.append(f'      headers:')
            ws_opts.append(f'        Host: {q(p["host"])}')
        lines.append('    ws-opts:')
        lines += ws_opts
    elif net == 'grpc':
        lines += ['    network: grpc', '    grpc-opts:',
                  f'      grpc-service-name: {q(p.get("serviceName", ""))}']
    # tcp بدون هدر: network پیش‌فرض همان tcp است و نیازی به opts ندارد

    proxies = "\n".join(lines)
    return (
        "port: 7890\nsocks-port: 7891\nallow-lan: false\nmode: rule\nlog-level: warning\n\n"
        f"proxies:\n{proxies}\n\n"
        "proxy-groups:\n"
        f"  - name: {q('PROXY')}\n"
        "    type: select\n"
        f"    proxies:\n      - {q(name)}\n\n"
        "rules:\n  - MATCH,PROXY\n"
    )


def portal_url(token):
    """آدرس عمومی پورتال؛ اگر PORTAL_PUBLIC_URL تنظیم نشده باشد None."""
    return f"{PORTAL_PUBLIC_URL}/sub/{token}" if PORTAL_PUBLIC_URL else None


def raw_url(token, base=None):
    """آدرس عمومی اشتراک خام. base برای رندر داخل خود پورتال (از request.host)."""
    base = (base or PORTAL_PUBLIC_URL or '').rstrip('/')
    return f"{base}/sub/{token}/raw" if base else None


def clash_url(token, base=None):
    base = (base or PORTAL_PUBLIC_URL or '').rstrip('/')
    return f"{base}/sub/{token}/clash" if base else None


def subscription_headers(name, traffic, expiry_ms):
    """هدرهای استاندارد subscription برای نمایش ترافیک/انقضا داخل اپ‌ها."""
    headers = {}
    try:
        if name:
            headers['Profile-Title'] = 'base64:' + base64.b64encode(str(name).encode()).decode()
        if traffic:
            dl = int(traffic.get('used') or 0)
            total = 0 if traffic.get('unlimited') else int(traffic.get('total') or 0)
            headers['subscription-userinfo'] = (
                f'upload=0; download={dl}; total={total}; '
                f'expire={int(expiry_ms) // 1000 if expiry_ms else 0}'
            )
        headers['profile-web-page-url'] = ''
    except Exception:
        pass
    return headers

# ================= قالب HTML/CSS پورتال =================
PAGE_CSS = (
    "<style>"
    ":root{--bg:#060B18;--a1:#7C5CFF;--a2:#4F46E5;--ok:#10B981;--warn:#F59E0B;--bad:#EF4444;"
    "--card:rgba(16,26,51,.66);--line:rgba(255,255,255,.08);--txt:#eaf0fc;--mut:#8a99ba}"
    "*{margin:0;padding:0;box-sizing:border-box}"
    "html{-webkit-text-size-adjust:100%}"
    "body{background:var(--bg);color:var(--txt);min-height:100vh;"
    "font-family:'Vazirmatn','IRANSansX','Vazir','Segoe UI',Tahoma,Arial,sans-serif;"
    "background-image:radial-gradient(1000px 560px at 88% -12%,rgba(124,92,255,.20),transparent 62%),"
    "radial-gradient(780px 480px at 6% 4%,rgba(79,70,229,.16),transparent 58%),"
    "radial-gradient(900px 620px at 50% 112%,rgba(16,185,129,.06),transparent 60%);"
    "background-attachment:fixed}"
    ".wrap{max-width:680px;margin:0 auto;padding:24px 16px 48px}"
    ".glass{background:var(--card);border:1px solid var(--line);border-radius:20px;"
    "backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);"
    "box-shadow:0 18px 45px rgba(2,6,18,.45)}"
    ".profile{display:flex;align-items:center;gap:14px;padding:20px;margin-top:18px;position:relative;overflow:hidden;"
    "transition:border-color .3s,box-shadow .3s}"
    ".profile:hover{border-color:rgba(124,92,255,.4);box-shadow:0 22px 55px rgba(2,6,18,.6)}"
    ".profile:before{content:'';position:absolute;inset:-60% 55% 55% -60%;"
    "background:conic-gradient(from 180deg,rgba(124,92,255,.28),rgba(79,70,229,.25),transparent 65%);"
    "filter:blur(34px);pointer-events:none}"
    ".avatar{width:58px;height:58px;border-radius:19px;flex:none;display:flex;align-items:center;justify-content:center;"
    "font-size:26px;font-weight:800;color:#fff;"
    "background:linear-gradient(135deg,var(--a1),var(--a2));box-shadow:0 8px 24px rgba(124,92,255,.4)}"
    ".who{min-width:0;flex:1}"
    ".who h1{font-size:19px;font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:ltr;text-align:right}"
    ".badges{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}"
    ".badge{font-size:11.5px;padding:3px 10px;border-radius:999px;background:rgba(255,255,255,.07);border:1px solid var(--line);color:var(--mut)}"
    ".chip{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:800;padding:7px 14px;"
    "border-radius:999px;border:1px solid;flex:none;background:rgba(255,255,255,.04);letter-spacing:.2px}"
    ".chip i{width:9px;height:9px;border-radius:50%;display:inline-block;position:relative}"
    ".chip i:after{content:'';position:absolute;inset:-4px;border-radius:50%;border:1.5px solid currentColor;opacity:.5;animation:ping 1.6s cubic-bezier(0,0,.2,1) infinite}"
    ".chip.active{color:var(--ok);border-color:rgba(16,185,129,.45);background:rgba(16,185,129,.09);box-shadow:0 0 18px -4px rgba(16,185,129,.45)}"
    ".chip.active i{background:var(--ok)}"
    ".chip.expired{color:var(--warn);border-color:rgba(245,158,11,.45);background:rgba(245,158,11,.09)}.chip.expired i{background:var(--warn)}"
    ".chip.disabled,.chip.exhausted{color:var(--bad);border-color:rgba(239,68,68,.45);background:rgba(239,68,68,.09)}.chip.disabled i,.chip.exhausted i{background:var(--bad)}"
    "@keyframes ping{75%,100%{transform:scale(1.9);opacity:0}}"
    "@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}"
    ".card{padding:20px;margin-top:16px;transition:border-color .3s,transform .3s,box-shadow .3s}"
    ".card:hover{border-color:rgba(255,255,255,.14);transform:translateY(-2px);box-shadow:0 22px 55px rgba(2,6,18,.55)}"
    ".card h2{font-size:13px;font-weight:700;color:var(--mut);letter-spacing:.3px;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;gap:10px}"
    ".banner{margin-top:16px;padding:14px 16px;border-radius:16px;font-size:13.5px;line-height:1.9;border:1px solid}"
    ".banner.warn{background:rgba(245,158,11,.08);border-color:rgba(245,158,11,.35);color:#fcd34d}"
    ".banner.bad{background:rgba(239,68,68,.08);border-color:rgba(239,68,68,.35);color:#fca5a5}"
    ".traffic{display:flex;align-items:center;gap:20px;flex-wrap:wrap}"
    ".donut{position:relative;width:132px;height:132px;flex:none;margin:0 auto}"
    ".donut svg{transform:rotate(-90deg)}"
    ".donut .track{fill:none;stroke:rgba(255,255,255,.08);stroke-width:12}"
    ".donut .val{fill:none;stroke:url(#grad);stroke-width:12;stroke-linecap:round;transition:stroke-dashoffset 1.2s cubic-bezier(.22,1,.36,1);filter:drop-shadow(0 0 8px rgba(124,92,255,.5))}"
    ".donut .center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}"
    ".donut .center b{font-size:24px;font-weight:800}"
    ".donut .center span{font-size:10.5px;color:var(--mut);margin-top:2px}"
    ".tinfo{flex:1;min-width:180px;display:flex;flex-direction:column;gap:10px}"
    ".trow{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:13.5px;"
    "background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:13px;padding:10px 14px;transition:border-color .25s}"
    ".trow:hover{border-color:rgba(124,92,255,.35)}"
    ".trow span{color:var(--mut)}.trow b{font-weight:700;direction:ltr}"
    ".trow.hot b{color:var(--a1)}"
    ".expire{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}"
    ".expire .date{font-size:22px;font-weight:800}"
    ".expire .sub{font-size:12px;color:var(--mut);margin-top:4px}"
    ".expire .pill{font-size:12.5px;font-weight:800;padding:8px 14px;border-radius:999px;"
    "background:rgba(124,92,255,.1);border:1px solid rgba(124,92,255,.35);color:#c4b5fd;flex:none}"
    ".expire .pill.warn{background:rgba(245,158,11,.1);border-color:rgba(245,158,11,.35);color:#fcd34d}"
    ".spec{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}"
    ".spec .cell{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:14px;padding:12px 14px;transition:border-color .25s}"
    ".spec .cell:hover{border-color:rgba(124,92,255,.35)}"
    ".spec .cell span{display:block;font-size:11px;color:var(--mut);margin-bottom:5px}"
    ".spec .cell b{font-size:14px;font-weight:700;direction:ltr;unicode-bidi:embed}"
    ".tabs{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:14px;padding:4px;border:1px solid var(--line);border-radius:16px;background:rgba(0,0,0,.12)}"
    ".tabs label{flex:1 1 110px;text-align:center;font-size:12.5px;font-weight:700;padding:10px 12px;border-radius:12px;cursor:pointer;"
    "background:rgba(255,255,255,.05);border:1px solid var(--line);color:var(--mut);transition:.25s;user-select:none}"
    ".tabs label:hover{color:var(--txt);border-color:rgba(124,92,255,.35)}"
    ".qc input{display:none}"
    ".qc input:checked + label{background:linear-gradient(135deg,rgba(124,92,255,.22),rgba(79,70,229,.2));"
    "border-color:rgba(124,92,255,.5);color:var(--txt);box-shadow:0 4px 16px rgba(124,92,255,.18)}"
    ".pane{display:none}.pane.on{display:block}"
    ".apps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}"
    ".app{position:relative;display:flex;flex-direction:column;align-items:center;gap:9px;padding:18px 10px 14px;border-radius:16px;"
    "background:rgba(255,255,255,.04);border:1px solid var(--line);color:var(--txt);text-decoration:none;"
    "font-size:13px;font-weight:700;transition:.25s;min-height:118px;justify-content:center;text-align:center;overflow:hidden}"
    ".app:hover{transform:translateY(-3px);border-color:rgba(124,92,255,.5);box-shadow:0 12px 28px rgba(124,92,255,.18);background:rgba(124,92,255,.06)}"
    ".app:active{transform:translateY(-1px) scale(.98)}"
    ".app.copy{cursor:pointer}"
    ".app small{font-size:10px;color:var(--mut);font-weight:400;line-height:1.6}"
    ".app-ava{width:42px;height:42px;border-radius:13px;display:flex;align-items:center;justify-content:center;"
    "font-size:17px;font-weight:800;color:#fff;flex:none;"
    "box-shadow:0 6px 16px -4px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.25)}"
    ".app-name{letter-spacing:.2px}"
    ".app-rec{position:absolute;top:8px;left:8px;font-size:8.5px;font-weight:800;letter-spacing:.3px;"
    "color:#c4b5fd;background:rgba(124,92,255,.16);border:1px solid rgba(124,92,255,.4);"
    "padding:2px 7px;border-radius:999px;line-height:1.5}"
    ".subbox{display:flex;gap:8px;align-items:stretch}"
    ".subbox code{flex:1;min-width:0;background:rgba(0,0,0,.35);border:1px solid var(--line);border-radius:13px;"
    "padding:12px 14px;font-size:11.5px;direction:ltr;text-align:left;overflow:hidden;"
    "text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,Consolas,monospace;color:#c4b5fd}"
    ".btn{flex:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:11px 18px;"
    "border-radius:13px;border:1px solid rgba(124,92,255,.4);cursor:pointer;font-size:13px;font-weight:700;"
    "background:linear-gradient(135deg,rgba(124,92,255,.2),rgba(79,70,229,.18));color:var(--txt);transition:.25s;"
    "font-family:inherit;min-height:44px}"
    ".btn:hover{box-shadow:0 8px 22px rgba(124,92,255,.25);border-color:rgba(124,92,255,.65);transform:translateY(-1px)}"
    ".qrbox{margin-top:14px;display:flex;flex-direction:column;align-items:center;gap:10px}"
    ".qrbox img{width:210px;height:210px;border-radius:18px;background:#fff;padding:10px;"
    "box-shadow:0 0 0 1px rgba(124,92,255,.3),0 14px 40px rgba(2,6,18,.6)}"
    ".qrbox span{font-size:11.5px;color:var(--mut)}"
    "details.direct summary{cursor:pointer;list-style:none;font-size:13px;font-weight:700;color:var(--mut);"
    "display:flex;align-items:center;gap:8px;min-height:44px}"
    "details.direct summary::-webkit-details-marker{display:none}"
    "details.direct .subbox{margin-top:12px}"
    ".mono-full{flex-wrap:wrap}.mono-full code{white-space:normal;word-break:break-all;font-size:10.5px}"
    "footer{text-align:center;color:var(--mut);font-size:11.5px;margin-top:26px;opacity:.75}"
    "footer b{background:linear-gradient(135deg,var(--a1),var(--a2));-webkit-background-clip:text;"
    "background-clip:text;-webkit-text-fill-color:transparent;font-weight:800}"
    ".err{max-width:440px;margin:12vh auto 0;padding:36px 26px;text-align:center}"
    ".err .ico{font-size:46px}.err h1{font-size:19px;margin:14px 0 10px}.err p{font-size:13.5px;color:var(--mut);line-height:2}"
    ".toast{position:fixed;bottom:26px;left:50%;transform:translate(-50%,80px);background:rgba(16,185,129,.14);"
    "border:1px solid rgba(16,185,129,.45);color:#a7f3d0;padding:11px 20px;border-radius:999px;font-size:13px;"
    "font-weight:700;backdrop-filter:blur(12px);transition:.35s;z-index:9;pointer-events:none;opacity:0}"
    ".toast.show{transform:translate(-50%,0);opacity:1}"
    ".live-pill{font-size:10.5px;font-weight:700;color:var(--ok);background:rgba(16,185,129,.1);"
    "border:1px solid rgba(16,185,129,.3);padding:3px 9px;border-radius:999px;display:inline-flex;align-items:center;gap:5px}"
    ".live-pill i{width:6px;height:6px;border-radius:50%;background:var(--ok);animation:ping 1.6s cubic-bezier(0,0,.2,1) infinite}"
    ".live-pill.refreshing i{animation:pulse .8s infinite}"
    ".card.refreshing{position:relative;overflow:hidden}"
    ".card.refreshing:after{content:'';position:absolute;inset:0;background:linear-gradient(105deg,transparent 40%,rgba(124,92,255,.08) 50%,transparent 60%);"
    "animation:shimmer 1.1s linear infinite;pointer-events:none}"
    "@keyframes shimmer{from{transform:translateX(100%)}to{transform:translateX(-100%)}}"
    "@media (max-width:560px){.apps{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (max-width:420px){.donut{width:116px;height:116px}.expire .date{font-size:18px}.tabs label{flex-basis:calc(50% - 7px)}}"
    ""
    "</style>"
)

# ================= رندر صفحات =================
# به‌روزرسانی زنده: هر ۳۰ ثانیه وضعیت دوباره خوانده می‌شود (رفرش کامل صفحه؛
# کش سمت سرور ۱۰ ثانیه‌ای است پس داده تازه برمی‌گردد).
AUTO_REFRESH_JS = """<script>
(function(){
  var pill = document.querySelector('.live-pill');
  setInterval(function(){
    if(pill) pill.classList.add('refreshing');
    setTimeout(function(){ location.reload(); }, 700);
  }, 30000);
})();
</script>"""

PAGE_JS = """<script>
function owCopy(b){var v=b.getAttribute('data-copy');if(!v)return;
function ok(){var t=document.getElementById('toast');t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(function(){t.classList.remove('show')},1600);}
function fb(){var ta=document.createElement('textarea');ta.value=v;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();try{document.execCommand('copy');ok();}catch(e){}ta.remove();}
if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(v).then(ok,fb);}else fb();}
(function(){var els=document.querySelectorAll('[data-exp-ms]');for(var i=0;i<els.length;i++){var ms=parseInt(els[i].getAttribute('data-exp-ms'),10);if(!ms)continue;try{els[i].textContent=new Intl.DateTimeFormat('fa-IR',{year:'numeric',month:'long',day:'numeric'}).format(new Date(ms));}catch(e){}}})();
function owTab(r){var p=document.querySelectorAll('.qc .pane');for(var i=0;i<p.length;i++)p[i].classList.remove('on');var el=document.getElementById('pane-'+r.value);if(el)el.classList.add('on');}
</script>"""

_DONUT_C = 2 * 3.14159265 * 54  # محیط دایره‌ی دونات (r=54)


def esc(v):
    return html.escape(str(v if v is not None else ''), quote=True)


def _donut(tv):
    """حلقه‌ی نمایش مصرف. برای پلن نامحدود علامت بی‌نهایت نشان داده می‌شود."""
    if tv['unlimited']:
        center = "<b style='font-size:30px'>&#8734;</b><span>نامحدود</span>"
        val = f"<circle class='val' cx='66' cy='66' r='54' stroke-dasharray='{_DONUT_C:.1f}' stroke-dashoffset='0' opacity='.25'/>"
    else:
        pct = tv['percent'] or 0
        offset = _DONUT_C * (1 - pct / 100.0)
        center = f"<b>{pct}٪</b><span>مصرف‌شده</span>"
        val = f"<circle class='val' cx='66' cy='66' r='54' stroke-dasharray='{_DONUT_C:.1f}' stroke-dashoffset='{offset:.1f}'/>"
    return (
        "<div class='donut'><svg width='132' height='132' viewBox='0 0 132 132'>"
        "<defs><linearGradient id='grad' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0%' stop-color='#22d3ee'/><stop offset='100%' stop-color='#8b5cf6'/>"
        "</linearGradient></defs>"
        f"<circle class='track' cx='66' cy='66' r='54'/>{val}</svg>"
        f"<div class='center'>{center}</div></div>"
    )


_STATUS_BANNER = {
    'expired': ('warn', '⚠️ سرویس شما منقضی شده است. برای ادامه‌ی استفاده، پلن خود را از طریق ربات تمدید کنید.'),
    'disabled': ('bad', '⛔️ این سرویس توسط پشتیبانی غیرفعال شده است. در صورت نیاز با پشتیبانی در تماس باشید.'),
    'exhausted': ('bad', '📉 حجم سرویس شما به پایان رسیده است. برای ادامه می‌توانید پلن جدیدی تهیه یا تمدید کنید.'),
}

_ROLE_LABELS = {'vip': 'کاربر VIP 💎', 'reseller': 'نماینده 🤝', 'normal': 'کاربر عادی'}

def render_portal(sub):
    """ساخت صفحه‌ی پورتال از دیتای واقعیِ آماده‌شده در load_subscription."""
    name = sub.get('name') or ''
    st_key = sub['status']['key']
    tv, ex, det = sub['traffic'], sub['expiry'], sub['details']
    raw = sub.get('raw_url') or ''
    qr = sub.get('qr_url') or ''
    initial = esc((name.strip()[:1] or 'U').upper())

    role = sub.get('role')
    badges = [f"<span class='badge'>{esc(_ROLE_LABELS.get(role, 'کاربر عادی'))}</span>"]
    if sub.get('plan'):
        badges.append(f"<span class='badge'>🛍 {esc(sub['plan'])}</span>")

    banner = ''
    if st_key in _STATUS_BANNER:
        cls, text = _STATUS_BANNER[st_key]
        banner = f"<div class='banner {cls}'>{text}</div>"

    remain_pill = 'warn' if st_key == 'expired' else ''
    pill = f"<span class='pill {remain_pill}'>{esc(ex['remain_text'])}</span>"

    fkind = sub.get('file_kind')
    if fkind:
        # سرویس فایلی (OpenVPN/WireGuard): مشخصات پروتکلی معمولی ندارد
        spec = (
            _spec_cell('پروتکل', 'OpenVPN' if fkind == 'openvpn' else 'WireGuard')
            + _spec_cell('نوع کانفیگ', 'فایل')
            + _spec_cell('دریافت', 'از داخل ربات')
        )
    else:
        spec = (
            _spec_cell('پروتکل', det['protocol'])
            + _spec_cell('ترنسپورت', det['transport'])
            + _spec_cell('امنیت', det['security'])
            + _spec_cell('پورت', str(sub['port']) if sub.get('port') else '—')
        )

    # ---------- اتصال سریع ----------
    tabs, panes = [], []
    apps_data = sub.get('apps') or {}
    for i, (pid, pdata) in enumerate(apps_data.items()):
        checked = ' checked' if i == 0 else ''
        tabs.append(
            f"<input type='radio' name='qc' value='{esc(pid)}' id='tab-{esc(pid)}'{checked} "
            f"onchange='owTab(this)'>"
            f"<label for='tab-{esc(pid)}'>{pdata['icon']} {esc(pdata['label'])}</label>"
        )
        buttons = []
        for app in pdata['apps']:
            note = f"<small>{esc(app['note'])}</small>" if app.get('note') else ''
            rec = "<span class='app-rec'>پیشنهادی</span>" if app.get('recommended') else ''
            a1, a2 = app.get('accent') or ('#7C5CFF', '#4F46E5')
            avatar = (f"<span class='app-ava' style='background:linear-gradient(135deg,{esc(a1)},{esc(a2)})'"
                      f">{esc(app.get('initial') or '')}</span>")
            if app.get('import_url'):
                buttons.append(
                    f"<a class='app' href='{esc(app['import_url'])}'>{rec}{avatar}"
                    f"<span class='app-name'>{esc(app['name'])}</span>{note}</a>"
                )
            else:
                buttons.append(
                    f"<div class='app copy' data-copy='{esc(raw)}' onclick='owCopy(this)' "
                    f"role='button' tabindex='0'>{rec}{avatar}"
                    f"<span class='app-name'>{esc(app['name'])}</span>{note}</div>"
                )
        panes.append(
            f"<div class='pane{' on' if i == 0 else ''}' id='pane-{esc(pid)}'>"
            f"<div class='apps'>{''.join(buttons)}</div></div>"
        )
    # Dedicated tab bar keeps the platform selector styled correctly.
    tab_bar = "<div class='tabs'>" + ''.join(tabs) + "</div>"
    qc = (
        "<section class='card glass qc'><h2>⚡️ اتصال سریع</h2>"
        f"{tab_bar}{''.join(panes)}</section>"
    )

    direct = ''
    if sub.get('config_link') and not sub.get('file_kind'):
        direct = (
            "<details class='card glass direct'><summary>⚙️ کانفیگ مستقیم (VLESS / VMess / Trojan)</summary>"
            "<div class='subbox mono-full'><code>" + esc(sub['config_link']) + "</code>"
            f"<button class='btn' data-copy='{esc(sub['config_link'])}' onclick='owCopy(this)'>کپی</button>"
            "</div></details>"
        )

    # سرویس فایلی (OpenVPN/WireGuard): بدون اتصال سریع/QR/اشتراک؛
    # کارت راهنمای دریافت فایل از ربات جای آن‌ها می‌نشیند.
    file_guide = ''
    if fkind:
        app_name = 'OpenVPN Connect' if fkind == 'openvpn' else 'WireGuard'
        file_guide = (
            "<section class='card glass'><h2>📥 دریافت کانفیگ</h2>"
            f"<p style='font-size:13.5px;color:var(--mut);line-height:2'>این سرویس از نوع "
            f"<b style='color:var(--txt)'>{'OpenVPN' if fkind == 'openvpn' else 'WireGuard'}</b> است؛ "
            "فایل کانفیگ را از ربات (سفارشات من → دریافت کانفیگ) دانلود کنید و در اپ "
            f"<b style='color:var(--txt)'>{app_name}</b> ایمپورت کنید.</p></section>"
        )
        qc = ''
        sub_section = ''
    else:
        qc = (
            "<section class='card glass qc'><h2>⚡️ اتصال سریع</h2>"
            f"{tab_bar}{''.join(panes)}</section>"
        )
        sub_section = (
            "<section class='card glass'><h2>🔗 اشتراک</h2>"
            "<div class='subbox'><code id=\"sub-url\">" + esc(raw) + "</code>"
            f"<button class='btn' data-copy='{esc(raw)}' onclick='owCopy(this)'>کپی</button></div>"
            f"<div class='qrbox'><img src='{esc(qr)}' alt='QR اشتراک' loading='lazy'>"
            "<span>اسکن کنید تا متصل شوید</span></div>"
            "</section>"
        )
    doc = f"""<!doctype html><html lang="fa" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/Vazirmatn-font-face.css">
<title>{esc(name)} | پورتال اشتراک {BRAND}</title>{PAGE_CSS}</head><body>
<div class="wrap">
<header class="profile glass">
<div class="avatar">{initial}</div>
<div class="who"><h1>{esc(name)}</h1><div class="badges">{''.join(badges)}</div></div>
<span class="chip {esc(st_key)}"><i></i>{esc(sub['status']['label'])}</span>
</header>
{banner}
<section class="card glass"><h2><span>📊 مصرف ترافیک</span><span class="live-pill" title="وضعیت زنده از سرور"><i></i>زنده</span></h2>
<div class="traffic">{_donut(tv)}
<div class="tinfo">
<div class="trow"><span>مصرف‌شده</span><b>{esc(tv['used_text'])}</b></div>
<div class="trow"><span>حجم کل</span><b>{esc(tv['total_text'])}</b></div>
<div class="trow hot"><span>باقی‌مانده</span><b>{esc(tv['remaining_text'])}</b></div>
</div></div></section>
<section class="card glass"><h2>📅 انقضا</h2>
<div class="expire"><div>
<div class="date" data-exp-ms="{ex['ms']}">{esc(ex['date_text'])}</div>
<div class="sub">تاریخ پایان سرویس</div>
</div>{pill}</div></section>
<section class="card glass"><h2>🧩 مشخصات سرویس</h2><div class="spec">{spec}</div></section>
{file_guide}{qc}
{sub_section}
{direct}
<footer>سرویس‌دهی توسط <b>{BRAND}</b> • پورتال اشتراک</footer>
</div>
<div class="toast" id="toast">✓ کپی شد</div>
{PAGE_JS}{AUTO_REFRESH_JS}</body></html>"""
    return doc


def _spec_cell(label, value):
    return f"<div class='cell'><span>{esc(label)}</span><b>{esc(value)}</b></div>"


_ERROR_PAGES = {
    'not_found': ('❌', 'اشتراک یافت نشد',
                  'اشتراک درخواستی نامعتبر است یا دیگر وجود ندارد.<br>لینک را از ربات دوباره دریافت کنید.'),
    'panel': ('⚠️', 'سرویس موقتاً در دسترس نیست',
              'دریافت آخرین وضعیت سرویس با مشکل مواجه شد.<br>چند لحظه بعد دوباره تلاش کنید.'),
}


def render_error_page(kind):
    ico, title, text = _ERROR_PAGES.get(kind, _ERROR_PAGES['not_found'])
    return (
        f"<!doctype html><html lang='fa' dir='rtl'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{BRAND}</title>{PAGE_CSS}</head><body><div class='wrap'>"
        f"<div class='err glass'><div class='ico'>{ico}</div><h1>{title}</h1><p>{text}</p></div>"
        f"</div>{PAGE_JS}</body></html>"
    )

# ================= لایه‌ی متصل به دیتابیس/پنل =================
# کش کوتاه برای جلوگیری از فشار به پنل 3x-UI؛ خطاها کش نمی‌شوند تا stale داده
# هیچ‌وقت جلوی کارکرد را نگیرد. پنل 3x-UI همچنان منبعِ حقیقت است.
_CACHE_TTL = 10.0
_cache = {}  # token -> (monotonic_ts, data)


def _cache_get(token):
    hit = _cache.get(token)
    if hit and (time.monotonic() - hit[0]) < _CACHE_TTL:
        return hit[1]
    return None


def _cache_put(token, data):
    if len(_cache) > 2048:  # جلوگیری از رشد بی‌حد
        _cache.clear()
    _cache[token] = (time.monotonic(), data)


async def load_subscription(token):
    """بارگذاری وضعیت «زنده»ی یک اشتراک با توکن.

    خروجی:
      None                       → توکن نامعتبر / سفارش یا کلاینت وجود ندارد (404)
      {'error': 'panel', ...}    → پنل در دسترس نیست (503؛ عمداً کش نمی‌شود)
      dict آماده‌ی رندر           → موفق
    """
    token = (token or '').strip()
    if not token or len(token) > 128:
        return None
    cached = _cache_get(token)
    if cached is not None:
        return cached

    order = await db.get_order_by_token(token)
    if not order:
        return None
    email = order_email(order)
    if not email or not order['inbound_id']:
        return None

    try:
        panel = await db.get_panel(order['panel_id'])
        xui, cfg_ip = build_xui(panel)
        is_logged, _err = await xui.login()
        if not is_logged:
            raise RuntimeError('panel login failed')
        inbound_id, port, client = await xui.get_client_exact_info(email)
        if not client:
            return None  # کلاینت روی پنل حذف شده → مثل اشتراک ناموجود
        inbound = await xui.get_inbound(inbound_id or order['inbound_id'])
        if not inbound:
            raise RuntimeError('inbound missing')
        stats = await xui.get_client_stats(email) or {}
    except Exception:
        log.exception("خطا در بارگذاری اشتراک token=%s", token[:8])
        return {'error': 'panel', 'token': token}

    # OpenVPN/WireGuard (vpn-ui): فایل کانفیگ مستقیم از پنل دانلود می‌شود؛
    # لینک اشتراک/Clash برای این پروتکل‌ها معنا ندارد و یک لیست خالی برمی‌گردد.
    fkind = file_link_kind(order['config_link'])
    if fkind:
        links = []
    else:
        links = [build_share_link(inbound, client, cfg_ip)]
    plan_name = None
    if order['plan_id']:
        try:
            plan = await db.get_plan(order['plan_id'])
            plan_name = plan['name'] if plan else None
        except Exception:
            plan_name = None

    enabled = bool(client.get('enable', True))
    total = int(stats.get('total') or client.get('totalGB') or 0)
    used = int(stats.get('up') or 0) + int(stats.get('down') or 0)
    expiry_ms = int(stats.get('expiryTime') or client.get('expiryTime') or 0)
    st_key = service_status(enabled, expiry_ms, total, used)

    data = {
        'token': token,
        'order_id': int(order['id']),
        'name': email,
        'role': order['user_role'] or 'normal',
        'plan': plan_name,
        'panel_name': panel['name'] if panel else None,
        'config_link': order['config_link'],
        'file_kind': fkind,
        'links': links,
        'server': cfg_ip,
        'port': port,
        'status': {'key': st_key, 'label': STATUS_LABELS[st_key]},
        'traffic': traffic_view(used, total),
        'expiry': expiry_view(expiry_ms, st_key),
        'details': stream_details(inbound),
    }
    _cache_put(token, data)
    return data

# ================= مسیرهای aiohttp =================
def _public_base(request):
    """آدرس عمومی پایه برای لینک‌های داخل صفحه؛ X-Forwarded-Proto دیده می‌شود."""
    if PORTAL_PUBLIC_URL:
        return PORTAL_PUBLIC_URL
    scheme = request.headers.get('X-Forwarded-Proto') or request.scheme
    return f"{scheme}://{request.host}"


def _html(text, status=200):
    return web.Response(
        text=text, status=status, content_type='text/html', charset='utf-8',
        headers={'Cache-Control': 'no-store'},
    )


async def portal_page(request):
    """/sub/{token} — صفحه‌ی پورتال (عمومی؛ فقط با توکن معتبر)."""
    sub = await load_subscription(request.match_info.get('token', ''))
    if sub is None:
        return _html(render_error_page('not_found'), status=404)
    if sub.get('error'):
        return _html(render_error_page('panel'), status=503)

    token = sub['token']
    base = _public_base(request)
    sub['raw_url'] = raw_url(token, base)
    sub['clash_url'] = clash_url(token, base)
    sub['qr_url'] = (
        'https://api.qrserver.com/v1/create-qr-code/?size=300x300&margin=10&data='
        + quote(sub['raw_url'], safe='')
    )
    sub['apps'] = quick_connect_apps(sub['raw_url'], sub['clash_url'], sub['name'])
    return _html(render_portal(sub))


async def raw_subscription(request):
    """/sub/{token}/raw — اشتراک خام (Base64) برای همه‌ی کلاینت‌ها."""
    sub = await load_subscription(request.match_info.get('token', ''))
    if sub is None:
        return web.Response(status=404, text='not found')
    if sub.get('error'):
        return web.Response(status=503, text='temporarily unavailable')
    if sub.get('file_kind'):
        return web.Response(status=404, text='file-based service: get config from the bot')
    headers = {'Cache-Control': 'no-store'}
    headers.update(subscription_headers(sub['name'], sub['traffic'], sub['expiry']['ms']))
    purl = portal_url(sub['token'])
    if not purl:
        purl = f"{_public_base(request)}/sub/{sub['token']}"
    headers['profile-web-page-url'] = purl
    return web.Response(
        text=build_raw_body(sub['links']),
        content_type='text/plain', charset='utf-8', headers=headers,
    )


async def clash_subscription(request):
    """/sub/{token}/clash — خروجی Clash Meta از روی همان تنظیمات واقعی 3x-UI."""
    sub = await load_subscription(request.match_info.get('token', ''))
    if sub is None:
        return web.Response(status=404, text='not found')
    if sub.get('error'):
        return web.Response(status=503, text='temporarily unavailable')
    if sub.get('file_kind'):
        return web.Response(status=404, text='file-based service: get config from the bot')
    full = await _load_full(sub['token'])
    if not full:
        return web.Response(status=503, text='temporarily unavailable')
    yaml_text = build_clash_yaml(sub['name'], full['inbound'], full['client'], full['cfg_ip'])
    return web.Response(
        text=yaml_text, content_type='text/yaml', charset='utf-8',
        headers={'Cache-Control': 'no-store'},
    )


async def _load_full(token):
    """دریافت inbound/client خام از پنل برای مبدل Clash."""
    try:
        order = await db.get_order_by_token(token)
        if not order:
            return None
        email = order_email(order)
        if not email or not order['inbound_id']:
            return None
        panel = await db.get_panel(order['panel_id'])
        xui, cfg_ip = build_xui(panel)
        if not (await xui.login())[0]:
            return None
        inbound_id, _port, client = await xui.get_client_exact_info(email)
        if not client:
            return None
        inbound = await xui.get_inbound(inbound_id or order['inbound_id'])
        if not inbound:
            return None
        return {'inbound': inbound, 'client': client, 'cfg_ip': cfg_ip}
    except Exception:
        log.exception("خطا در ساخت اشتراک Clash token=%s", (token or '')[:8])
        return None


from aiohttp import web  # noqa: E402  (ایمپورت موضعی برای جلوگیری از وابستگی در لایه‌ی خالص)
