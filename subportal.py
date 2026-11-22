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
from portal_logos import APP_LOGOS, logo_key, logo_ver
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


# زیرنویس و لینک دانلود رسمی هر اپ (کارت «اتصال دستگاه‌ها»).
APP_META = {
    'v2rayng': ('وی‌تو‌ری‌ان‌جی / اندروید', 'https://github.com/2dust/v2rayNG/releases'),
    'happ': ('هپ / اندروید و ویندوز', 'https://happ.su'),
    'incy': ('اینسی / اندروید', ''),
    'clashmeta': ('کلش متا / اندروید', 'https://github.com/MetaCubeX/ClashMetaForAndroid/releases'),
    'nekobox': ('نیکوباکس / اندروید', 'https://github.com/MatsuriDayo/NekoBoxForAndroid/releases'),
    'shadowrocket': ('شدو راکت / پیشنهادی / پولی', 'https://apps.apple.com/app/shadowrocket/id932747118'),
    'streisand': ('استریسند / پیشنهادی', 'https://apps.apple.com/app/streisand/id6450534064'),
    'v2box': ('وی‌تو‌باکس / آیفون', 'https://apps.apple.com/app/v2box-v2ray-client/id6446814690'),
    'happ_ios': ('هپ / آیفون', 'https://apps.apple.com/app/happ-proxy-utility/id6504287215'),
    'incy_ios': ('اینسی / آیفون', ''),
    'clashverge': ('کلش ورج / ویندوز', 'https://github.com/clash-verge-rev/clash-verge-rev/releases'),
    'v2rayn': ('وی‌تو‌ری‌ان / پیشنهادی', 'https://github.com/2dust/v2rayN/releases'),
    'nekoray': ('نیکوری / ویندوز', 'https://github.com/MatsuriDayo/nekoray/releases'),
    'clashverge_mac': ('کلش ورج / مک', 'https://github.com/clash-verge-rev/clash-verge-rev/releases'),
    'v2box_mac': ('وی‌تو‌باکس / مک', 'https://apps.apple.com/app/v2box-v2ray-client/id6446814690'),
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
PAGE_CSS = """
<style>
:root{--paper:#EDE8DC;--ink:#111111;--card:#F1ECE2;--muted:#7E786A;--red:#E8452C;--yellow:#F5C842;
--green:#6FE08D;--blue:#1F63C8;--line:#111111;--sh:5px 5px 0 #111111}
[data-theme="dark"]{--paper:#1B1B1B;--ink:#F3EFE5;--card:#232323;--muted:#9C978C;--line:#F3EFE5;
--sh:5px 5px 0 #F3EFE5}
*{margin:0;padding:0;box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{background:var(--paper);color:var(--ink);min-height:100vh;padding:34px 16px 44px;
font-family:'Vazirmatn','Segoe UI',Tahoma,Arial,sans-serif;transition:background .2s,color .2s}
.wrap{max-width:560px;margin:0 auto}
.hdr{text-align:center;margin-bottom:24px}
.title{font-family:'Courier New',ui-monospace,Consolas,monospace;font-size:32px;font-weight:700;
letter-spacing:.5px;line-height:1.2;direction:ltr}
.handle{font-family:'Courier New',ui-monospace,Consolas,monospace;font-size:13px;color:var(--muted);
margin-top:8px;direction:ltr;word-break:break-all}
.icons{display:flex;justify-content:center;gap:12px;margin-top:18px}
.ic{width:54px;height:46px;display:flex;align-items:center;justify-content:center;border:2.5px solid var(--line);
box-shadow:4px 4px 0 var(--line);background:var(--card);cursor:pointer;padding:0;text-decoration:none}
.ic svg{width:22px;height:22px;stroke:#111;fill:none;stroke-width:2;stroke-linecap:round}
.ic-y{background:var(--yellow)}.ic-r{background:var(--red)}.ic-b{background:var(--blue)}
.ic-r svg,.ic-b svg{stroke:#F3EFE5}
.ic:active{transform:translate(2px,2px);box-shadow:2px 2px 0 var(--line)}
.card{background:var(--card);border:2.5px solid var(--line);box-shadow:var(--sh);padding:22px 18px;margin-bottom:22px;
position:relative;border-radius:0}
.card>*{position:relative}
.quarter{position:absolute;top:-2.5px;right:-2.5px;width:96px;height:96px;background:var(--blue);
border-bottom-left-radius:96px;z-index:0}
.statuscard{text-align:left}
.statuscard .gv{text-align:left}
.eyebrow{font-family:'Courier New',ui-monospace,Consolas,monospace;font-size:12px;letter-spacing:1.2px;
color:var(--muted);text-transform:uppercase}
.big{font-size:30px;font-weight:800;line-height:1.15;margin:8px 0 14px;letter-spacing:-.3px}
.pill{display:inline-flex;align-items:center;gap:7px;font-family:'Courier New',ui-monospace,Consolas,monospace;
font-size:12.5px;font-weight:700;background:var(--green);color:#111;border:2px solid #111;padding:6px 13px;
box-shadow:3px 3px 0 #111}
.pill i{width:7px;height:7px;background:#111;border-radius:50%;display:inline-block}
.pill.bad{background:var(--red);color:#F3EFE5}.pill.bad i{background:#F3EFE5}
.pill.warn{background:var(--yellow);color:#111}
.hr{height:2.5px;background:var(--line);margin:18px 0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px 12px}
.gv{font-size:22px;font-weight:800;margin-top:5px;direction:ltr;text-align:right;unicode-bidi:embed;line-height:1.3}
.traf{display:flex;align-items:flex-end;gap:14px;justify-content:space-between;flex-wrap:wrap}
.pctbox{background:var(--yellow);color:#111;border:2.5px solid #111;box-shadow:4px 4px 0 #111;
padding:2px 12px;display:inline-flex;align-items:flex-start;gap:1px;margin-top:10px;direction:ltr}
.pctbox .n{font-size:38px;font-weight:800;line-height:1.05}
.pctbox .s{font-size:22px;font-weight:800;line-height:1.2}
.pctbox.red{background:var(--red);color:#F3EFE5}
.pctbox.green{background:var(--green);color:#111}
.bar{flex:1;min-width:110px;max-width:230px;height:58px;border:2.5px solid var(--line);background:var(--card);
padding:6px;display:flex;align-items:center;margin-top:10px}
.bar .fill{height:100%;background:var(--red);min-width:3px}
.bar .fill.yellow{background:var(--yellow)}.bar .fill.green{background:var(--green)}
.metaline{font-family:'Courier New',ui-monospace,Consolas,monospace;font-size:11.5px;color:var(--muted);
margin-top:18px;letter-spacing:.6px;direction:ltr;text-align:right;word-break:break-word}
.btn-black{display:flex;align-items:center;justify-content:center;gap:9px;width:100%;background:#111;color:#F3EFE5;
border:2.5px solid #111;box-shadow:4px 4px 0 rgba(17,17,17,.35);padding:15px 12px;margin-top:18px;cursor:pointer;
font-family:'Courier New',ui-monospace,Consolas,monospace;font-size:14px;font-weight:700;letter-spacing:.6px}
.btn-black svg{width:18px;height:18px}
.btn-black:active{transform:translate(2px,2px);box-shadow:2px 2px 0 rgba(17,17,17,.35)}
[data-theme="dark"] .btn-black{border-color:#F3EFE5;box-shadow:4px 4px 0 #F3EFE5;color:#F3EFE5}
[data-theme="dark"] .btn-black:active{box-shadow:2px 2px 0 #F3EFE5}
.qrwrap[hidden]{display:none}
.qrwrap{margin-bottom:22px}
.qrwrap .card{padding:18px;text-align:center;margin-bottom:0}
.qrwrap img{width:100%;max-width:220px;background:#fff;padding:8px;border:2.5px solid #111}
.dev-head{display:flex;align-items:center;gap:12px}
.bolt{width:44px;height:44px;background:var(--red);border:2.5px solid #111;box-shadow:4px 4px 0 #111;
display:flex;align-items:center;justify-content:center;flex:none}
.bolt svg{width:22px;height:22px}
.dtitle{flex:1;font-size:25px;font-weight:800;text-align:center}
.acount{font-family:'Courier New',ui-monospace,Consolas,monospace;font-size:12.5px;color:var(--muted);
letter-spacing:1.2px;flex:none;text-align:left;line-height:1.4}
.lead{font-size:14px;line-height:2;text-align:center;margin:18px 0 4px;color:var(--ink)}
.plats{display:flex;gap:10px;margin-top:18px}
.plats input{position:absolute;opacity:0;pointer-events:none}
.plats label{flex:1;text-align:center;font-size:14px;font-weight:700;padding:12px 4px;cursor:pointer;
background:var(--card);border:2.5px solid var(--line);box-shadow:4px 4px 0 var(--line);color:var(--ink)}
.plats input:checked+label{background:#111;color:#F3EFE5;border-color:#111;box-shadow:4px 4px 0 var(--muted)}
[data-theme="dark"] .plats input:checked+label{background:#F3EFE5;color:#111;border-color:#F3EFE5}
.pane{display:none}.pane.on{display:block;margin-top:18px}
.app{border:2.5px solid var(--line);box-shadow:4px 4px 0 var(--line);background:var(--card);
padding:14px 14px;margin-bottom:16px}
.app summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:12px}
.app summary::-webkit-details-marker{display:none}
.app-ico{width:44px;height:44px;border:2px solid #111;display:flex;align-items:center;justify-content:center;
font-weight:800;font-size:18px;color:#fff;flex:none;font-family:'Courier New',monospace}
.app-ico img{width:100%;height:100%;display:block;object-fit:contain}
.app-t{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.app-name{font-size:17px;font-weight:800;direction:ltr;text-align:right;line-height:1.25}
.app-sub{font-size:12px;color:var(--muted);text-align:right;line-height:1.6}
.chev{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2.6;stroke-linecap:round;flex:none}
details[open] .chev{transform:rotate(180deg)}
.steps{margin-top:16px}
.step{display:flex;align-items:center;gap:10px}
.stepn{width:26px;height:26px;background:#111;color:#F3EFE5;display:flex;align-items:center;justify-content:center;
font-size:13px;font-weight:800;flex:none;font-family:'Courier New',monospace}
.stepl{flex:1;font-size:14.5px;font-weight:700}
.sbtn{font-size:13.5px;font-weight:700;padding:9px 15px;border:2.5px solid #111;box-shadow:3px 3px 0 #111;
cursor:pointer;text-decoration:none;color:#111;display:inline-flex;align-items:center;gap:7px;
font-family:inherit;background:var(--card)}
.sbtn.y{background:var(--yellow)}.sbtn.g{background:var(--green)}
.sbtn:active{transform:translate(2px,2px);box-shadow:1px 1px 0 #111}
.dash{border-top:2.5px dashed var(--line);margin:14px 0}
.note{background:#3B3B3B;color:#EDE8DC;padding:14px 12px;font-size:13.5px;line-height:2;margin-top:14px;text-align:center}
[data-theme="dark"] .note{background:#111;border:1px solid #3A3A3A}
[data-theme="dark"] .pctbox,[data-theme="dark"] .pill,[data-theme="dark"] .bar,[data-theme="dark"] .bolt,
[data-theme="dark"] .sbtn,[data-theme="dark"] .app-ico,[data-theme="dark"] .qrwrap img{border-color:#F3EFE5}
[data-theme="dark"] .pctbox{box-shadow:4px 4px 0 #F3EFE5;color:#111}
[data-theme="dark"] .pill{box-shadow:3px 3px 0 #F3EFE5}
[data-theme="dark"] .bolt{box-shadow:4px 4px 0 #F3EFE5}
[data-theme="dark"] .sbtn{box-shadow:3px 3px 0 #F3EFE5;color:#111}
[data-theme="dark"] .stepn{background:#F3EFE5;color:#111}
.card.direct{padding:0}
.card.direct summary{list-style:none;cursor:pointer;padding:16px 18px;font-size:14px;font-weight:700;
display:flex;align-items:center;gap:8px}
.card.direct summary::-webkit-details-marker{display:none}
.card.direct .subbox{padding:0 18px 18px;display:flex;flex-direction:column;gap:10px}
.card.direct code{background:var(--paper);border:2px solid var(--line);padding:12px;font-size:11px;
font-family:ui-monospace,Consolas,monospace;direction:ltr;text-align:left;word-break:break-all;line-height:1.8}
.banner{border:2.5px solid #111;box-shadow:var(--sh);padding:13px 14px;margin-bottom:22px;font-size:13.5px;
line-height:2;font-weight:700;background:var(--yellow);color:#111}
.banner.bad{background:var(--red);color:#F3EFE5}
.banner.warn{background:var(--yellow);color:#111}
.foot{text-align:center;font-family:'Courier New',ui-monospace,Consolas,monospace;font-size:11.5px;
color:var(--muted);letter-spacing:.6px;margin-top:26px;line-height:2}
.err{max-width:440px;margin:14vh auto 0;padding:34px 24px;text-align:center;background:var(--card);
border:2.5px solid var(--line);box-shadow:var(--sh)}
.err .ico{font-size:42px}.err h1{font-family:'Courier New',monospace;font-size:19px;margin:16px 0 10px}
.err p{font-size:13.5px;color:var(--muted);line-height:2}
.toast{position:fixed;bottom:24px;left:50%;transform:translate(-50%,80px);background:#111;color:#F3EFE5;
border:2.5px solid #111;box-shadow:4px 4px 0 var(--muted);padding:11px 22px;font-size:13px;font-weight:700;
transition:.3s;z-index:9;pointer-events:none;opacity:0}
.toast.show{transform:translate(-50%,0);opacity:1}
@media (max-width:430px){.title{font-size:27px}.big{font-size:26px}.dtitle{font-size:21px}
.gv{font-size:19px}.pctbox .n{font-size:32px}.bar{height:52px}}
</style>
"""

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
(function(){
  var root=document.documentElement;
  try{var saved=localStorage.getItem('ow-theme');if(saved)root.setAttribute('data-theme',saved);}catch(e){}
  var tb=document.getElementById('theme');
  if(tb)tb.addEventListener('click',function(){
    var dark=root.getAttribute('data-theme')==='dark';
    root.setAttribute('data-theme',dark?'light':'dark');
    try{localStorage.setItem('ow-theme',dark?'light':'dark');}catch(e){}
  });
  var qb=document.getElementById('qrbtn'),qw=document.getElementById('qrwrap');
  if(qb&&qw)qb.addEventListener('click',function(){qw.hidden=!qw.hidden;});
  var els=document.querySelectorAll('[data-exp-ms]');
  for(var i=0;i<els.length;i++){
    var ms=parseInt(els[i].getAttribute('data-exp-ms'),10);
    if(!ms)continue;
    try{els[i].textContent=new Date(ms).toLocaleDateString('en-US');}catch(e){}
  }
})();
function owCopy(b){var v=b.getAttribute('data-copy');if(!v)return;
function ok(){var t=document.getElementById('toast');t.classList.add('show');clearTimeout(t._h);
t._h=setTimeout(function(){t.classList.remove('show')},1600);}
function fb(){var ta=document.createElement('textarea');ta.value=v;ta.style.position='fixed';ta.style.opacity='0';
document.body.appendChild(ta);ta.select();try{document.execCommand('copy');ok();}catch(e){}ta.remove();}
if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(v).then(ok,fb);}else fb();}
function owPlat(r){var p=document.querySelectorAll('.qc .pane');
for(var i=0;i<p.length;i++)p[i].classList.remove('on');
var el=document.getElementById('pane-'+r.value);if(el){el.classList.add('on');
var c=document.getElementById('appcount');
if(c&&el.getAttribute('data-count'))c.textContent=el.getAttribute('data-count');}}
</script>"""

_DONUT_C = 2 * 3.14159265 * 54  # محیط دایره‌ی دونات (r=54)


def esc(v):
    return html.escape(str(v if v is not None else ''), quote=True)


_STATUS_EN = {
    'active': ('Active / Online', 'ok', 'Connection stable', ''),
    'expired': ('Expired', 'warn', 'Connection expired', 'سرویس شما منقضی شده است؛ از ربات تمدید کنید.'),
    'disabled': ('Disabled', 'bad', 'Service disabled', 'این سرویس توسط پشتیبانی غیرفعال شده است.'),
    'exhausted': ('Data Finished', 'bad', 'Data limit reached', 'حجم سرویس به پایان رسیده است؛ پلن را تمدید کنید.'),
}

_GLYPH = {
    'globe': "<svg viewBox='0 0 24 24'><circle cx='12' cy='12' r='9'/>"
             "<path d='M3 12h18M12 3c2.6 3 2.6 15 0 18M12 3c-2.6 3-2.6 15 0 18'/></svg>",
    'moon': "<svg viewBox='0 0 24 24'><path d='M20 14.5A8.5 8.5 0 019.5 4 8.5 8.5 0 1020 14.5z'/></svg>",
    'qr': "<svg viewBox='0 0 24 24'><path d='M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4z'/>"
          "<path d='M14 14h2v2h-2zM18 14h2v2h-2zM14 18h2v2h-2zM18 18h2v2h-2z'/></svg>",
    'bolt': "<svg viewBox='0 0 24 24' fill='#F3EFE5' stroke='none'><path d='M13 2L4 14h6l-1 8 9-12h-6z'/></svg>",
    'link': "<svg viewBox='0 0 24 24' fill='currentColor' stroke='none'>"
            "<path d='M3.9 12A3.1 3.1 0 017 8.9h4V7H7a5 5 0 100 10h4v-1.9H7A3.1 3.1 0 013.9 12z"
            "M8 13h8v-2H8v2zm9-6h-4v1.9h4a3.1 3.1 0 010 6.2h-4V17h4a5 5 0 000-10z'/></svg>",
    'chev': "<svg class='chev' viewBox='0 0 24 24'><path d='M6 9l6 6 6-6'/></svg>",
}

def render_portal(sub):
    """ساخت صفحه‌ی پورتال از دیتای واقعیِ آماده‌شده در load_subscription."""
    name = sub.get('name') or ''
    st_key = sub['status']['key']
    big, pill_cls, pill_txt, warn_text = _STATUS_EN.get(st_key, _STATUS_EN['active'])
    tv, ex, det = sub['traffic'], sub['expiry'], sub['details']
    raw = sub.get('raw_url') or ''
    qr = sub.get('qr_url') or ''
    fkind = sub.get('file_kind')

    banner = ''
    if warn_text:
        cls = 'warn' if st_key == 'expired' else 'bad'
        banner = f"<div class='banner {cls}'>{esc(warn_text)}</div>"

    if fkind:
        metaline = {'openvpn': 'OpenVPN', 'wireguard': 'WireGuard',
                    'l2tp': 'L2TP / IPsec'}.get(fkind, fkind)
    else:
        metaline = ' · '.join(x for x in [
            det['protocol'], det['transport'], det['security'],
            (':' + str(sub['port'])) if sub.get('port') else '',
        ] if x)

    pct = tv['percent']
    slot = 'red' if (pct is not None and pct >= 90) else 'yellow'
    if tv['unlimited']:
        slot = 'green'
        pct_html = "<span class='n'>&#8734;</span>"
        bar_w = 100
    else:
        pct_html = f"<span class='n'>{pct or 0}</span><span class='s'>%</span>"
        bar_w = pct or 0
    bar_cls = '' if slot == 'red' else (' yellow' if slot == 'yellow' else ' green')

    if ex.get('ms'):
        exp_html = f"<div class='gv' data-exp-ms='{ex['ms']}'>{esc(ex['date_text'])}</div>"
    else:
        exp_html = f"<div class='gv'>{esc(ex['date_text'])}</div>"

    # ---------- اتصال دستگاه‌ها ----------
    apps_data = sub.get('apps') or {}
    order = ['windows', 'ios', 'android', 'macos']
    pids = [p for p in order if p in apps_data] + [p for p in apps_data if p not in order]
    tabs, panes = [], []
    for i, pid in enumerate(pids):
        pdata = apps_data[pid]
        chk = ' checked' if i == 0 else ''
        tabs.append(
            f"<input type='radio' name='plat' id='pt-{esc(pid)}' value='{esc(pid)}'{chk} "
            f"onchange='owPlat(this)'><label for='pt-{esc(pid)}'>{esc(pdata['label'])}</label>"
        )
        rows = []
        for app in pdata['apps']:
            sub_t, dl = APP_META.get(app['id'], ('', ''))
            subline = esc(sub_t or ('پیشنهادی' if app.get('recommended') else ''))
            logo = logo_key(app['id'])
            if logo:
                # لوگوی رسمی خود اپ؛ ?v=<hash> تا عوض شدن آیکون از کش رد نشه.
                ico = ("<span class='app-ico logo'>"
                       f"<img src='/sub/applogo/{esc(app['id'])}.png?v={logo_ver(logo)}' alt='' "
                       "width='40' height='40' loading='lazy'></span>")
            else:
                a1, a2 = app.get('accent') or ('#111111', '#3B3B3B')
                ico = (f"<span class='app-ico' style='background:linear-gradient(135deg,{esc(a1)},{esc(a2)})'>"
                       f"{esc(app.get('initial') or '')}</span>")
            if app.get('import_url'):
                add_btn = f"<a class='sbtn g' href='{esc(app['import_url'])}'>افزودن</a>"
            else:
                add_btn = (f"<button class='sbtn g' type='button' data-copy='{esc(raw)}' "
                           "onclick='owCopy(this)'>افزودن</button>")
            step_dl = ''
            if dl:
                step_dl = (
                    "<div class='step'><span class='stepn'>1</span>"
                    "<span class='stepl'>دانلود برنامه</span>"
                    f"<a class='sbtn y' href='{esc(dl)}' target='_blank' rel='noreferrer'>دانلود</a></div>"
                    "<div class='dash'></div>"
                )
            note = esc(app.get('note') or 'یک کلاینت برای مسیریابی امن ترافیک فراهم می‌کند.')
            rows.append(
                f"<details class='app'><summary>{ico}<span class='app-t'>"
                f"<span class='app-name'>{esc(app['name'])}</span>"
                f"<span class='app-sub'>{subline}</span></span>{_GLYPH['chev']}</summary>"
                f"<div class='steps'>{step_dl}"
                f"<div class='step'><span class='stepn'>{'2' if dl else '1'}</span>"
                f"<span class='stepl'>افزودن کانفیگ</span>{add_btn}</div>"
                f"<div class='note'>{note}</div></div></details>"
            )
        panes.append(
            f"<div class='pane{' on' if i == 0 else ''}' id='pane-{esc(pid)}' "
            f"data-count='{len(pdata['apps'])}'>{''.join(rows)}</div>"
        )
    first_count = len(apps_data[pids[0]]['apps']) if pids else 0
    devices = ''
    if panes:
        devices = (
            "<section class='card qc'><div class='dev-head'>"
            f"<span class='bolt'>{_GLYPH['bolt']}</span>"
            "<span class='dtitle'>اتصال دستگاه‌ها</span>"
            f"<span class='acount'>APPS <span id='appcount'>{first_count}</span></span></div>"
            "<p class='lead'>برنامه مورد نظر خود را انتخاب کنید.<br>پیشنهاد ما مشخص شده است.</p>"
            f"<div class='plats'>{''.join(tabs)}</div>{''.join(panes)}</section>"
        )

    direct = ''
    if sub.get('config_link') and not fkind:
        direct = (
            "<details class='card direct'><summary>کانفیگ مستقیم (VLESS / VMess / Trojan)</summary>"
            "<div class='subbox'><code>" + esc(sub['config_link']) + "</code>"
            f"<button class='sbtn y' type='button' data-copy='{esc(sub['config_link'])}' "
            "onclick='owCopy(this)'>کپی</button></div></details>"
        )

    file_guide = ''
    if fkind:
        known = {'openvpn': 'OpenVPN', 'wireguard': 'WireGuard', 'l2tp': 'L2TP/IPsec'}.get(fkind, fkind)
        file_guide = (
            "<section class='card'><div class='eyebrow'>CONFIG FILE</div>"
            f"<div class='big' style='font-size:22px'>{esc(known)}</div>"
            "<p style='font-size:13.5px;line-height:2;color:var(--muted)'>"
            "این سرویس فایلی است؛ فایل/اطلاعات اتصال را از ربات (سفارشات من ← دریافت کانفیگ) بگیرید."
            "</p></section>"
        )

    qr_block = ''
    if qr:
        qr_block = (
            "<div class='qrwrap' id='qrwrap' hidden><div class='card'>"
            f"<img src='{esc(qr)}' alt='QR' loading='lazy'>"
            "<div class='eyebrow' style='text-align:center;margin-top:12px'>SCAN TO CONNECT</div>"
            "</div></div>"
        )

    share_btn = ''
    if raw:
        share_btn = (
            f"<button class='btn-black' type='button' data-copy='{esc(raw)}' onclick='owCopy(this)'>"
            f"{_GLYPH['link']}<span>Get sharing link</span></button>"
        )

    doc = f"""<!doctype html><html lang="fa" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer"><meta name="color-scheme" content="light dark">
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/Vazirmatn-font-face.css">
<title>{esc(name)} | {BRAND}</title>{PAGE_CSS}</head><body>
<div class="wrap">
<header class="hdr">
<h1 class="title">Subscription Info</h1>
<div class="handle">{esc(name)}</div>
<div class="icons">
<button class="ic ic-b" type="button" id="qrbtn" title="qr">{_GLYPH['qr']}</button>
<button class="ic ic-r" type="button" id="theme" title="theme">{_GLYPH['moon']}</button>
<a class="ic ic-y" href="{esc(raw)}" target="_blank" rel="noreferrer" title="open link">{_GLYPH['globe']}</a>
</div>
</header>
{banner}
<section class="card statuscard">
<div class="quarter"></div>
<div class="eyebrow">Subscription status</div>
<div class="big">{esc(big)}</div>
<div class="pill {esc(pill_cls)}"><i></i>{esc(pill_txt)}</div>
<div class="hr"></div>
<div class="grid2">
<div><div class="eyebrow">Used</div><div class="gv">{esc(tv['used_text'])}</div></div>
<div><div class="eyebrow">Total data</div><div class="gv">{esc(tv['total_text'])}</div></div>
<div><div class="eyebrow">Expires</div>{exp_html}</div>
<div><div class="eyebrow">Remaining</div><div class="gv">{esc(tv['remaining_text'])}</div></div>
</div>
<div class="hr"></div>
<div class="traf">
<div class="bar"><div class="fill{bar_cls}" style="width:{bar_w}%"></div></div>
<div style="text-align:left"><div class="eyebrow">Traffic used</div><div class="pctbox {esc(slot)}">{pct_html}</div></div>
</div>
<div class="metaline">{esc(metaline)}</div>
{share_btn}
</section>
{qr_block}
{devices}
{direct}
{file_guide}
<footer class="foot">{BRAND} · SUBSCRIPTION PORTAL</footer>
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


async def logo_asset(request):
    """/sub/applogo/{app}.png — لوگوی رسمی اپ کارت «اتصال دستگاه‌ها»."""
    key = logo_key(request.match_info.get('app', ''))
    if not key:
        raise web.HTTPNotFound()
    return web.Response(
        body=base64.b64decode(APP_LOGOS[key]), content_type='image/png',
        headers={'Cache-Control': 'public, max-age=604800'},
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
