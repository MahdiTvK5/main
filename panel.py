import json
import uuid
import time
import base64
import logging
from urllib.parse import urlparse, quote

import httpx

from config import PANEL_URL, PANEL_USER, PANEL_PASS, CONFIG_IP, PANEL_TIMEOUT


def _host_from_url(url):
    """دامنه/آی‌پیِ داخل یک URL را برمی‌گرداند (بدون پورت و مسیر).
    آدرس‌های بدون اسکیم (مثل `1.2.3.4:54321/path`) را هم پشتیبانی می‌کند."""
    if not url:
        return ""
    try:
        u = url if "://" in url else "//" + url
        host = urlparse(u).hostname
        return host or ""
    except Exception:
        return ""


# انواع پنل پشتیبانی‌شده. مقادیر ورودی آزاد (مثل «3x-ui» یا «VPN-UI») نرمال می‌شوند.
PANEL_TYPE_XUI = "xui"
PANEL_TYPE_VPNUI = "vpn-ui"

_PANEL_TYPE_ALIASES = {
    "xui": PANEL_TYPE_XUI,
    "3x": PANEL_TYPE_XUI,
    "3x-ui": PANEL_TYPE_XUI,
    "3xui": PANEL_TYPE_XUI,
    "x-ui": PANEL_TYPE_XUI,
    "سنایی": PANEL_TYPE_XUI,
    "vpn-ui": PANEL_TYPE_VPNUI,
    "vpnui": PANEL_TYPE_VPNUI,
    "vpn_ui": PANEL_TYPE_VPNUI,
}


def normalize_panel_type(value):
    """نوع پنل ورودی (از دیتابیس/فرم) را به کلید ثابت تبدیل می‌کند.
    خالی/نامعتبر → xui (رفتار قدیمی به‌عنوان پیش‌فرض)."""
    return _PANEL_TYPE_ALIASES.get((value or "").strip().lower(), PANEL_TYPE_XUI)


def is_vpnui_panel(panel):
    """آیا این ردیف پنل از نوع vpn-ui است؟ (None → نه)"""
    if panel is None:
        return False
    try:
        return normalize_panel_type(panel.get('panel_type')) == PANEL_TYPE_VPNUI
    except Exception:
        return False


def file_link_kind(link):
    """نوع «لینک نشانگرِ فایل» را برمی‌گرداند: openvpn / wireguard / l2tp / '' (لینک معمولی).

    اینباندهای OpenVPN/WireGuard/L2TP در vpn-ui URI اشتراکی به‌معنای معمول ندارند؛ پس ربات در
    config_link سفارش یک نشانگر ذخیره می‌کند (ovpn://udp/name یا wg://name یا l2tp://name) و
    فایل/اطلاعات اتصال هنگام «دریافت کانفیگ» از پنل ساخته می‌شود."""
    if not link:
        return ""
    s = str(link).strip()
    if s.startswith("ovpn://"):
        return "openvpn"
    if s.startswith("wg://"):
        return "wireguard"
    if s.startswith("l2tp://"):
        return "l2tp"
    return ""


def parse_file_link(link):
    """ovpn://udp/name یا wg://name یا l2tp://name را باز می‌کند: (نوع، ترنسپورت، نام سرویس)."""
    kind = file_link_kind(link)
    if not kind:
        return "", "", ""
    s = str(link).strip()
    if kind == "openvpn":
        rest = s[len("ovpn://"):]
        transport, _, name = rest.partition('/')
        return kind, (transport or 'udp'), name
    if kind == "l2tp":
        return kind, "", s[len("l2tp://"):]
    return kind, "udp", s[len("wg://"):]


def build_xui(panel):
    """از روی ردیف پنل، کلاینت X-UI و config_ip مربوطه را می‌سازد.
    اگر panel برابر None باشد، به مقادیر پیش‌فرضِ .env برمی‌گردد (سازگاری با نسخه‌ی تک‌پنل).
    اگر «IP کانفیگ» پنل تنظیم نشده باشد، برای جلوگیری از ساختِ لینک خرابِ بدون هاست،
    به‌ترتیب از CONFIG_IP و سپس دامنه‌ی خود پنل استفاده می‌شود."""
    if panel:
        panel_url = panel['url']
        cfg_ip = (panel['config_ip'] or CONFIG_IP or _host_from_url(panel_url))
        if normalize_panel_type(panel.get('panel_type')) == PANEL_TYPE_VPNUI:
            api = AsyncVpnUiAPI(panel_url, panel['username'], panel['password'])
        else:
            api = AsyncXuiAPI(panel_url, panel['username'], panel['password'])
        api._config_host = cfg_ip or ''
        return api, cfg_ip
    cfg_ip = CONFIG_IP or _host_from_url(PANEL_URL)
    api = AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS)
    api._config_host = cfg_ip or ''
    return api, cfg_ip


def build_vless_link(client_uuid, host, port, remark, path="/", sni=None):
    """ساخت لینک اشتراک VLESS+WS+TLS دقیقاً مطابق قالبِ خروجیِ خودِ پنل X-UI.
    پارامترهای غیراستاندارد قدیمی (insecure/allowInsecure) حذف شده‌اند تا کلاینت‌ها
    بدون مشکل وصل شوند."""
    from urllib.parse import quote
    sni = sni or host
    enc_path = quote(path, safe="")
    enc_alpn = quote("h2,http/1.1,h3", safe="")
    query = (
        f"type=ws&encryption=none&path={enc_path}&host="
        f"&security=tls&fp=chrome&alpn={enc_alpn}&sni={sni}"
    )
    return f"vless://{client_uuid}@{host}:{port}?{query}#{remark}"


def _existing_flow(inbound):
    """اگر اینباند کلاینتی با flow دارد (مثل reality/vision)، همان را کپی می‌کنیم."""
    try:
        for c in json.loads(inbound.get('settings', '{}')).get('clients', []) or []:
            if c.get('flow'):
                return c['flow']
    except Exception:
        pass
    return ""


def _stream_params(stream):
    """پارامترهای مشترکِ ترنسپورت/امنیت را از streamSettings استخراج می‌کند."""
    net = (stream.get('network') or 'tcp').lower()
    security = (stream.get('security') or 'none').lower()
    p = {'type': net, 'security': security}

    if net == 'ws':
        ws = stream.get('wsSettings', {}) or {}
        p['path'] = ws.get('path', '/') or '/'
        host = ws.get('host') or (ws.get('headers', {}) or {}).get('Host', '')
        if host:
            p['host'] = host
    elif net == 'grpc':
        g = stream.get('grpcSettings', {}) or {}
        p['serviceName'] = g.get('serviceName', '') or ''
        if g.get('multiMode'):
            p['mode'] = 'multi'
    elif net in ('xhttp', 'splithttp', 'http'):
        # ترنسپورت xhttp/splithttp (و http قدیمی) — path/host/mode
        xs = (stream.get('xhttpSettings') or stream.get('splithttpSettings')
              or stream.get('httpSettings') or {})
        p['path'] = xs.get('path', '/') or '/'
        host = xs.get('host', '')
        if not host:
            hosts = xs.get('hosts') or xs.get('host') or []
            if isinstance(hosts, list) and hosts:
                host = hosts[0]
        if host:
            p['host'] = host
        if xs.get('mode'):
            p['mode'] = xs['mode']
    elif net in ('tcp', 'raw'):
        tcp = stream.get('tcpSettings') or stream.get('rawSettings') or {}
        header = (tcp.get('header', {}) or {}).get('type', 'none')
        p['headerType'] = header
        if header == 'http':
            req = (tcp.get('header', {}) or {}).get('request', {}) or {}
            paths = req.get('path') or ['/']
            p['path'] = paths[0] if paths else '/'
            hosts = (req.get('headers', {}) or {}).get('Host') or []
            if hosts:
                p['host'] = hosts[0] if isinstance(hosts, list) else hosts

    if security == 'tls':
        tls = stream.get('tlsSettings', {}) or {}
        if tls.get('serverName'):
            p['sni'] = tls['serverName']
        st = tls.get('settings', {}) or {}
        if st.get('fingerprint'):
            p['fp'] = st['fingerprint']
        if tls.get('alpn'):
            p['alpn'] = ','.join(tls['alpn'])
    elif security == 'reality':
        r = stream.get('realitySettings', {}) or {}
        st = r.get('settings', {}) or {}
        names = r.get('serverNames') or []
        if names:
            p['sni'] = names[0]
        if st.get('publicKey'):
            p['pbk'] = st['publicKey']
        sids = r.get('shortIds') or []
        if sids:
            p['sid'] = sids[0]
        if st.get('fingerprint'):
            p['fp'] = st['fingerprint']
        if st.get('spiderX'):
            p['spx'] = st['spiderX']
    return p


def build_share_link(inbound, client, host):
    """لینک اشتراک را مطابق پروتکل و streamSettingsِ واقعیِ همان اینباند می‌سازد.
    از VLESS، VMess و Trojan با ترنسپورت‌های ws/grpc/tcp و امنیت tls/reality/none پشتیبانی می‌کند."""
    protocol = (inbound.get('protocol') or 'vless').lower()
    port = inbound.get('port')
    remark = client.get('email', '')
    try:
        stream = json.loads(inbound.get('streamSettings') or '{}')
    except Exception:
        stream = {}
    p = _stream_params(stream)

    if protocol == 'vmess':
        net = p.get('type', 'tcp')
        conf = {
            "v": "2", "ps": remark, "add": host, "port": str(port),
            "id": client.get('id', ''), "aid": str(client.get('alterId', 0)),
            "net": net, "type": p.get('headerType', 'none'),
            "host": p.get('host', ''),
            "path": p.get('serviceName', '') if net == 'grpc' else p.get('path', ''),
            "tls": p.get('security') if p.get('security') in ('tls', 'reality') else '',
            "sni": p.get('sni', ''), "alpn": p.get('alpn', ''), "fp": p.get('fp', ''),
        }
        b64 = base64.b64encode(json.dumps(conf, ensure_ascii=False).encode()).decode()
        return f"vmess://{b64}"

    if protocol == 'trojan':
        cred = client.get('password', '')
        scheme = 'trojan'
    else:  # vless و پیش‌فرض
        cred = client.get('id', '')
        scheme = 'vless'
        p['encryption'] = 'none'
        if client.get('flow'):
            p['flow'] = client['flow']

    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in p.items() if v not in (None, ''))
    return f"{scheme}://{cred}@{host}:{port}?{query}#{quote(remark)}"


def sub_link_for(panel, email):
    """در صورتی که پنل آدرس اشتراک داشته باشد، لینک ساب را برمی‌گرداند؛ وگرنه None."""
    if not panel or not email:
        return None
    try:
        sub = panel['sub_url']
    except (KeyError, TypeError):
        sub = None
    if not sub:
        return None
    return sub.rstrip('/') + '/' + quote(str(email), safe="")


# ================= پنل X-UI =================
class AsyncXuiAPI:
    def __init__(self, panel_url, username, password):
        self.url = (panel_url or "").rstrip('/')
        self.username = username
        self.password = password
        self.cookies = None
        # IP کانفیگ (دامنه/آی‌پی داخل لینک‌ها و فایل‌ها)؛ در build_xui ست می‌شود
        self._config_host = ''
        # لیست اینباندها در طول یک عملیات ثابت است؛ کش می‌کنیم تا هر متد
        # درخواست تکراری به پنل نفرستد (هر نمونه فقط برای یک عملیات ساخته می‌شود).
        self._inbounds_cache = None

    def _candidate_urls(self):
        """اگر آدرس پنل اسکیم نداشته باشد، اول https و بعد http امتحان می‌شود."""
        u = self.url
        if u.startswith("http://") or u.startswith("https://"):
            return [u]
        return [f"https://{u}", f"http://{u}"]

    async def login(self):
        """ورود به پنل. برای مقاومت در برابر شبکه‌های ناپایدار، هر آدرسِ کاندید
        یک‌بار دیگر (مجموعاً دو تلاش) امتحان می‌شود و خطای اتصال با راهنمای
        عیب‌یابی فارسی برگردانده می‌شود."""
        last_err = "آدرس پنل نامعتبر است"
        candidates = [(base, attempt) for base in self._candidate_urls() for attempt in (1, 2)]
        for base, attempt in candidates:
            # follow_redirects تا ریدایرکتِ http→https پنل خودکار دنبال شود
            async with httpx.AsyncClient(verify=False, timeout=PANEL_TIMEOUT, trust_env=False, follow_redirects=True) as client:
                try:
                    res = await client.post(f"{base}/login", data={"username": self.username, "password": self.password})
                    if res.status_code == 200 and res.json().get('success'):
                        # آدرس نهایی (بعد از ریدایرکت‌ها) را مبنای درخواست‌های بعدی قرار می‌دهیم
                        final = str(res.url)
                        if final.endswith('/login'):
                            final = final[:-len('/login')]
                        self.url = final.rstrip('/') or base
                        self.cookies = res.cookies
                        return True, "OK"
                    # پاسخ گرفتیم ولی ناموفق (مثلاً یوزر/پس اشتباه یا مسیر نادرست)
                    last_err = f"{base}/login → HTTP {res.status_code}: {str(res.text)[:200]}"
                except httpx.ConnectTimeout as e:
                    hint = ("سرور پنل اصلاً پاسخ نداد؛ پورت پنل روی فایروال باز است؟ آدرس/پورت پنل در «مدیریت پنل‌ها» درست است؟ "
                            "اگر ربات و پنل روی یک سرورند، PANEL_URL را با 127.0.0.1 امتحان کنید")
                    last_err = f"{base}/login → ConnectTimeout ({PANEL_TIMEOUT}s): {hint}"
                    logging.warning("panel login connect timeout (%s/%s) %s", base, attempt, e)
                except Exception as e:
                    last_err = f"{base}/login → {type(e).__name__}: {e}"
        return False, last_err

    async def _fetch_inbounds(self):
        """لیست اینباندها را یک‌بار می‌گیرد و کش می‌کند. خروجی: list یا None در صورت خطا."""
        if self._inbounds_cache is not None:
            return self._inbounds_cache
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=15.0, trust_env=False) as client:
            try:
                res = await client.get(f"{self.url}/panel/api/inbounds/list")
                if res.status_code == 200 and res.json().get('success'):
                    self._inbounds_cache = res.json().get('obj', []) or []
                    return self._inbounds_cache
                logging.warning("inbounds/list → HTTP %s", res.status_code)
            except Exception as e:
                logging.warning("inbounds/list failed: %s", e)
        return None

    def _invalidate_cache(self):
        self._inbounds_cache = None

    async def get_inbounds(self):
        """لیست کامل اینباندها (شامل id, port, protocol, remark) یا None در صورت خطا.
        برای ادمین تا آیدی اینباندها را بدون devtools ببیند."""
        return await self._fetch_inbounds()

    async def get_inbound_port(self, inbound_id):
        inbound = await self.get_inbound(inbound_id)
        return inbound.get('port') if inbound else None

    async def get_inbound(self, inbound_id):
        """ردیف کاملِ یک اینباند (شامل protocol و streamSettings) را برمی‌گرداند."""
        inbounds = await self._fetch_inbounds()
        for inbound in inbounds or []:
            try:
                if int(inbound.get('id')) == int(inbound_id):
                    return inbound
            except (TypeError, ValueError):
                continue
        return None

    async def add_client(self, inbound_id, client_email, total_gb, expire_days, host, limit_ip=1, inbound=None):
        """یک کلاینت روی اینباند می‌سازد و لینک اشتراک را مطابق پروتکل/تنظیماتِ همان اینباند برمی‌گرداند.
        host = دامنه/آی‌پیِ داخل لینک. خروجی: (share_link, error)."""
        if inbound is None:
            inbound = await self.get_inbound(inbound_id)
        if not inbound:
            return None, f"اینباند با آیدی {inbound_id} یافت نشد"
        protocol = (inbound.get('protocol') or 'vless').lower()
        total_bytes = total_gb * 1024 * 1024 * 1024
        expire_time = int((time.time() + (expire_days * 86400)) * 1000)

        # subId برابر ایمیل قرار می‌گیرد تا لینک اشتراک (sub) به‌ازای هر کلاینت بسازیم
        client = {
            "email": client_email, "enable": True, "limitIp": limit_ip,
            "totalGB": total_bytes, "expiryTime": expire_time, "tgId": "", "subId": client_email,
        }
        if protocol == 'trojan':
            client["password"] = uuid.uuid4().hex
        else:  # vless / vmess و مشابه، از id استفاده می‌کنند
            client["id"] = str(uuid.uuid4())
            if protocol == 'vmess':
                client["alterId"] = 0
            elif protocol == 'vless':
                flow = _existing_flow(inbound)
                if flow:
                    client["flow"] = flow

        payload = {"id": int(inbound_id), "settings": json.dumps({"clients": [client]})}
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as c:
            try:
                res = await c.post(f"{self.url}/panel/api/inbounds/addClient", json=payload, headers={'Accept': 'application/json'})
                data = res.json()
                if res.status_code == 200 and data.get('success'):
                    self._invalidate_cache()
                    return build_share_link(inbound, client, host), None
                return None, data.get('msg', res.text)
            except Exception as e:
                return None, str(e)

    async def add_raw_client(self, inbound_id, client_dict):
        """افزودن کلاینتِ آماده (همان UUID/تنظیمات) به اینباند — برای مهاجرت بین اینباندها.
        برخلاف add_client، هیچ چیزی نمی‌سازد؛ فقط dict داده‌شده را همان‌طور ثبت می‌کند."""
        payload = {"id": int(inbound_id), "settings": json.dumps({"clients": [client_dict]})}
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as c:
            try:
                res = await c.post(f"{self.url}/panel/api/inbounds/addClient", json=payload, headers={'Accept': 'application/json'})
                data = res.json()
                if res.status_code == 200 and data.get('success'):
                    self._invalidate_cache()
                    return True, None
                return False, data.get('msg', res.text)
            except Exception as e:
                return False, str(e)

    async def remove_client(self, inbound_id, client_key):
        """حذف کلاینت از اینباند با کلیدِ (uuid/password)."""
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as c:
            try:
                res = await c.post(
                    f"{self.url}/panel/api/inbounds/{int(inbound_id)}/delClient/{quote(str(client_key), safe='')}",
                    headers={'Accept': 'application/json'},
                )
                data = res.json()
                if res.status_code == 200 and data.get('success'):
                    self._invalidate_cache()
                    return True, None
                return False, data.get('msg', res.text)
            except Exception as e:
                return False, str(e)

    async def get_client_stats(self, email):
        safe_email = quote(str(email).strip(), safe="")
        if not safe_email:
            return None
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.get(f"{self.url}/panel/api/inbounds/getClientTraffics/{safe_email}")
                if res.status_code == 200 and res.json().get('success'):
                    return res.json().get('obj', {})
            except Exception as e:
                logging.warning("get_client_stats failed: %s", e)
        return None

    async def get_all_client_stats(self):
        """با یک درخواست، آمار تمام کلاینت‌ها را برمی‌گرداند.
        خروجی: dict با کلید ایمیلِ lowercase و مقدارِ دیکشنری آمار (مشابه get_client_stats)."""
        inbounds = await self._fetch_inbounds()
        if inbounds is None:
            return None
        result = {}
        for inbound in inbounds:
            for cs in inbound.get('clientStats', []) or []:
                email = str(cs.get('email', '')).strip().lower()
                if email:
                    result[email] = cs
        return result

    async def get_all_client_emails(self):
        """مجموعه‌ی ایمیلِ تمام کلاینت‌های *تعریف‌شده* روی پنل.

        برخلاف clientStats (که فقط شامل کلاینت‌های دارای رکورد ترافیک است) این متد
        از settings هر اینباند می‌خواند؛ پس کلاینت تازه‌ساخته و بدون مصرف را هم می‌بیند.
        خروجی: set یا None در صورت خطا (تا صدازننده اشتباهاً «وجود ندارد» برداشت نکند).
        """
        inbounds = await self._fetch_inbounds()
        if inbounds is None:
            return None
        emails = set()
        for inbound in inbounds:
            try:
                clients = json.loads(inbound.get('settings', '{}')).get('clients', []) or []
            except Exception:
                continue
            for c in clients:
                email = str(c.get('email', '')).strip().lower()
                if email:
                    emails.add(email)
        return emails

    async def get_client_exact_info(self, email):
        target_email = str(email).strip().lower()
        if not target_email:
            return None, None, None
        inbounds = await self._fetch_inbounds()
        for inbound in inbounds or []:
            try:
                clients = json.loads(inbound.get('settings', '{}')).get('clients', []) or []
            except Exception:
                continue
            for c in clients:
                if str(c.get('email', '')).strip().lower() == target_email:
                    return inbound.get('id'), inbound.get('port'), c
        return None, None, None

    async def update_client(self, inbound_id, old_key, client_dict):
        """به‌روزرسانی کلاینت. old_key برای vless/vmess همان uuid و برای trojan همان password است."""
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [client_dict]})}
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.post(f"{self.url}/panel/api/inbounds/updateClient/{quote(str(old_key), safe='')}", json=payload, headers={'Accept': 'application/json'})
                ok = res.status_code == 200 and res.json().get('success', False)
                if ok:
                    self._invalidate_cache()
                return ok
            except Exception as e:
                logging.warning("update_client failed: %s", e)
                return False

    async def reset_client_traffic(self, inbound_id, email):
        """ریست مصرف (up/down) کلاینت.
        مسیر صحیح در پنل سنایی (3x-ui): /panel/api/inbounds/{inboundId}/resetClientTraffic/{email}
        """
        from urllib.parse import quote
        safe_email = quote(str(email), safe="")
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.post(
                    f"{self.url}/panel/api/inbounds/{int(inbound_id)}/resetClientTraffic/{safe_email}",
                    headers={'Accept': 'application/json'},
                )
                if res.status_code == 200 and res.json().get('success', False):
                    return True
            except Exception as e:
                logging.warning("reset_client_traffic failed: %s", e)
        return False


# ================= پنل vpn-ui (نسخه‌ی ارتقایافته‌ی 3x-ui) =================
class AsyncVpnUiAPI(AsyncXuiAPI):
    """کلاینت پنل vpn-ui (Sir-MmD/vpn-ui) — فورک ارتقایافته‌ی 3x-ui 2.9.3.

    تفاوت‌های اصلی با 3x-ui که این کلاس مدیریت می‌کند:
    • همه‌ی هندلرها با c.PostForm بایند می‌شوند → بدنه باید application/x-www-form-urlencoded
      باشد نه JSON (وگرنه پنل HTTP 200 با success:false می‌دهد!).
    • updateClient/:clientId کلاینتِ کامل را می‌خواهد؛ فرستادنِ فقط فیلدهای تغییر
      یافته، بقیه‌ی فیلدها (UUID، حجم، انقضا) را صفر می‌کند. کلاینتِ فعلی خوانده و
      تغییرات روی آن اعمال (overlay) می‌شود.
    • clientId برای vless/vmess/tuic همان UUID و برای اکثر پروتکل‌های تونلی
      (trojan/ssh/mtproto/wg-c/awg/gre/...) همان email است.
    • پاسخ‌ها همان پاکت {success, msg, obj} و مسیرها همان /panel/api/... هستند؛
    • بقیه‌ی متدها (login، getClientTraffics، resetClientTraffic، addClient،
      delClient و list) با 3x-ui یکسان‌اند و از کلاس پایه به ارث می‌رسند.
    """

    _IDENTITY_PASSWORD = ("trojan", "l2tp", "pptp", "openvpn", "openconnect", "sstp",
                          "ikev2", "anytls", "naive")

    @staticmethod
    def _client_identity(protocol, client):
        """فیلدی که کلاینت با آن آدرس‌دهی می‌شود (قرینه‌ی clientIdentityKey پنل)."""
        if protocol in AsyncVpnUiAPI._IDENTITY_PASSWORD:
            return client.get("password", "") or ""
        if protocol == "shadowsocks":
            return client.get("email", "") or ""
        if protocol in ("hysteria", "hysteria2"):
            return client.get("auth", "") or ""
        # vmess/vless/tuic (uuid) و پروتکل‌های email-محور (wg-c، awg، gre، mtproto، ssh)
        # که در settings شناسه‌شان id=email است.
        return client.get("id", "") or client.get("email", "") or ""

    async def update_client(self, inbound_id, old_key, client_dict):
        """به‌روزرسانی کلاینت به سبک vpn-ui — امضای سازگار با 3x-ui (خروجی bool).

        جزئیات و خطا در _update_client_full است؛ call-site های ربات مثل کلاس پایه
        فقط True/False انتظار دارند."""
        ok, err = await self._update_client_full(inbound_id, old_key, client_dict)
        if not ok:
            logging.warning("vpn-ui update_client failed: %s", err)
        return ok

    async def _update_client_full(self, inbound_id, old_key, client_dict):
        """به‌روزرسانی کلاینت — به سبک vpn-ui.

        old_key در api این ربات همان email/identifier است؛ کلاینتِ کامل از اینباند
        خوانده و تغییراتِ client_dict روی آن اعمال (overlay) می‌شود تا هیچ فیلدی صفر نشود.
        بدنه form-encoded است و clientId از خودِ کلاینتِ کامل استخراج می‌شود.
        خروجی: (موفقیت، خطا)."""
        inbound = await self.get_inbound(inbound_id)
        if not inbound:
            return False, f"اینباند با آیدی {inbound_id} یافت نشد"
        stored = None
        try:
            settings = json.loads(inbound.get('settings') or '{}')
            stored = next((c for c in (settings.get('clients') or [])
                           if str(c.get('email', '')).strip().lower() == str(old_key).strip().lower()), None)
        except Exception:
            stored = None
        if not stored:
            # اگر کلاینت روی پنل نبود، همان دیکشنری داده‌شده را کامل می‌پذیریم
            stored = client_dict
        else:
            merged = dict(stored)
            merged.update(client_dict)
            stored = merged
        proto = (inbound.get('protocol') or 'vless').lower()
        payload = {
            "id": str(inbound_id),
            "remark": inbound.get('remark', ''),
            "enable": "true",
            "listen": inbound.get('listen', '') or '',
            "protocol": proto,
            "port": str(inbound.get('port', 0)),
            "settings": json.dumps({"clients": [stored]}),
            "streamSettings": "{}",
            "sniffing": "{}",
        }
        client_id = self._client_identity(proto, stored)
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as c:
            try:
                res = await c.post(
                    f"{self.url}/panel/api/inbounds/updateClient/{quote(str(client_id), safe='')}",
                    data=payload, headers={'Accept': 'application/json'},
                )
                data = res.json()
                if res.status_code == 200 and data.get('success', False):
                    self._invalidate_cache()
                    return True, None
                return False, data.get('msg', res.text)
            except Exception as e:
                return False, str(e)

    # ---- اینباندهای فایلی (OpenVPN / WireGuard) -------------------------

    async def add_client(self, inbound_id, client_email, total_gb, expire_days, host, limit_ip=1, inbound=None):
        """ساخت اکانت روی اینباند فایلی (openvpn/wg-c/awg/l2tp).

        در vpn-ui اکانت این پروتکل‌ها از جفت «email + password» تشکیل می‌شود
        (email = نام سرویس، password = رمز RADIUS) و totalGB/expiryTime هم روی
        خود اکانت می‌نشیند. خروجی «لینک نشانگر» مثل `ovpn://udp/<email>` است که
        نقطه‌ی اتصال ربات و فایلِ واقعی کانفیگ است؛ فایل هنگام «دریافت کانفیگ»
        با download_config از پنل گرفته می‌شود. خروجی: (نشانگر، خطا)."""
        if inbound is None:
            inbound = await self.get_inbound(inbound_id)
        if not inbound:
            return None, f"اینباند با آیدی {inbound_id} یافت نشد"
        protocol = (inbound.get('protocol') or '').lower()
        if protocol not in ('openvpn', 'wg-c', 'awg', 'l2tp'):
            return None, f"پروتکل {protocol} به‌صورت فایلی پشتیبانی نمی‌شود"
        try:
            settings = json.loads(inbound.get('settings') or '{}')
        except Exception:
            settings = {}
        existing = settings.get('clients') or []
        # اگر قبلاً ساخته شده (مثلاً خرید دوم از همین اکانت)، همان را آپدیت می‌کنیم
        stored = next((c for c in existing
                       if str(c.get('email', '')).strip().lower() == str(client_email).strip().lower()), None)
        if not stored:
            stored = {}
        merged = dict(stored)
        merged.update({
            "id": client_email,          # در پروتکل‌های فایلی id همان email است
            "email": client_email,
            "enable": True,
            "totalGB": int(total_gb * 1024 * 1024 * 1024),
            "expiryTime": int((time.time() + (expire_days * 86400)) * 1000),
        })
        if not stored.get('password'):
            merged["password"] = uuid.uuid4().hex[:12]  # رمز RADIUS (openvpn/l2tp/...)
        existing = [c for c in existing
                    if str(c.get('email', '')).strip().lower() != str(client_email).strip().lower()]
        existing.append(merged)
        settings['clients'] = existing
        payload = {
            "id": str(inbound_id),
            "remark": inbound.get('remark', ''),
            "enable": "true",
            "listen": inbound.get('listen', '') or '',
            "protocol": protocol,
            "port": str(inbound.get('port', 0)),
            "settings": json.dumps(settings),
            "streamSettings": "{}",
            "sniffing": "{}",
        }
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as c:
            try:
                res = await c.post(f"{self.url}/panel/api/inbounds/update/{int(inbound_id)}",
                                   data=payload, headers={'Accept': 'application/json'})
                data = res.json()
                if res.status_code == 200 and data.get('success', False):
                    self._invalidate_cache()
                    scheme = 'wg' if protocol in ('wg-c', 'awg') else 'ovpn'
                    if protocol == 'l2tp':
                        # L2TP فایل کانفیگ ندارد؛ نشانگرِ سفارش فقط نام اکانت است و
                        # «دریافت کانفیگ» کارتِ اطلاعات اتصال (یوزر/پسورد/PSK) می‌سازد.
                        return f"l2tp://{client_email}", None
                    if protocol == 'openvpn':
                        # در vpn-ui هر ترنسپورت (udp/tcp) اینباندِ خودش را دارد؛
                        # ترنسپورت از روی نام اینباند تشخیص داده می‌شود (پورت‌های رایج + udp/tcp در remark)
                        r = (inbound.get('remark') or '').lower()
                        transport = 'tcp' if ('tcp' in r or '1443' in r or ' 443' in r) else 'udp'
                        return f"ovpn://{transport}/{client_email}", None
                    return f"wg://{client_email}", None
                return None, data.get('msg', res.text)
            except Exception as e:
                return None, str(e)

    async def download_config(self, inbound_id, email, protocol_hint=''):
        """فایل کانفیگ واقعی اکانت را از پنل می‌گیرد.

        openvpn → .ovpn (از /ovpn/udp و در نبودش /ovpn/tcp)
        wg-c/awg → متن کانفیگ از /wgc-configs یا /awg-configs (برای هر دستگاه یک بلاک [Interface])
        protocol_hint می‌تواند 'openvpn' یا 'wireguard' باشد (از نشانگر فایل سفارش)؛
        اگر خالی باشد نوع از روی اینباند تشخیص داده می‌شود. inbound_id هم اختیاری است
        چون endpoint های این دو پروتکل به شناسه‌ی اینباند تکیه‌ی منطقی ندارند ولی
        برای تشخیص نوع در نبود hint لازم می‌شود.
        خروجی: (متن فایل، نام فایل، خطا)."""
        host = self._config_host
        protocol = ''
        if str(protocol_hint).lower() == 'openvpn':
            protocol = 'openvpn'
        elif str(protocol_hint).lower() == 'wireguard':
            protocol = 'wg-c'
        if not protocol and inbound_id:
            inbound = await self.get_inbound(inbound_id)
            protocol = ((inbound or {}).get('protocol') or '').lower() if inbound else ''
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=15.0, trust_env=False) as c:
            try:
                if protocol == 'openvpn':
                    # ترنسپورت ترجیحی tcp است (ترتیب tcp سپس udp برای سازگاری با پنل‌هایی
                    # که اینباندشان یکی از این دو را سرو می‌کند)
                    transports = ('tcp', 'udp')
                    body_text = None
                    for transport in transports:
                        res = await c.get(f"{self.url}/panel/api/inbounds/{int(inbound_id)}/ovpn/{transport}")
                        # فایل ovpn واقعی یا حاوی کلمه‌ی openvpn است یا با «client» شروع می‌شود
                        body = (res.text or '')[:200].lower()
                        if res.status_code == 200 and ('openvpn' in body or body.lstrip().startswith('client')):
                            body_text = res.text
                            break
                    if body_text is None:
                        return None, None, "فایل ovpn از پنل دریافت نشد"
                    return self._fix_ovpn_host(body_text, host), f"{email}.ovpn", None
                if protocol in ('wg-c', 'awg'):
                    ep = 'wgc-configs' if protocol == 'wg-c' else 'awg-configs'
                    res = await c.get(f"{self.url}/panel/api/inbounds/{int(inbound_id)}/{ep}?email={quote(str(email), safe='')}")
                    if res.status_code == 200 and res.json().get('success', False):
                        items = res.json().get('obj') or []
                        cfgs = [i.get('config', '') for i in items if i.get('config')]
                        # فایل .conf باید دقیقاً یک [Interface] داشته باشد؛ چسباندن کانفیگِ
                        # همه‌ی دستگاه‌ها فایل نامعتبر می‌سازد که اپ WireGuard رد می‌کند.
                        # پس فقط اولین دستگاهِ سالم فرستاده می‌شود.
                        cfg = next((c0 for c0 in cfgs if '[interface]' in (c0 or '').lower()), '')
                        if cfg:
                            return self._fix_wg_endpoint(cfg, host), f"{email}.conf", None
                    return None, None, "کانفیگ وایرگارد از پنل دریافت نشد"
                if protocol == 'l2tp':
                    # vpn-ui برای L2TP فایل کانفیگ ندارد؛ مشخصات اتصال از خود اینباند
                    # خوانده و به‌صورت «کارتِ اطلاعات اتصال» (متن) ساخته می‌شود.
                    # (العنوان زیر + per-account PPP creds در settings؛ psk درbând-level است.)
                    inbound = await self.get_inbound(int(inbound_id)) if inbound_id else None
                    try:
                        st = json.loads((inbound or {}).get('settings') or '{}')
                    except Exception:
                        st = {}
                    acct = next((c for c in (st.get('clients') or [])
                                 if str(c.get('email', '')).strip().lower() == str(email).strip().lower()), None)
                    if not acct:
                        return None, None, f"اکانت {email} روی اینباند L2TP یافت نشد"
                    pwd = str(acct.get('password') or '')
                    if not pwd:
                        return None, None, "پسورد اکانت L2TP روی پنل ثبت نشده است"
                    ipsec_on = bool(st.get('ipsecEnable'))
                    psk = str(st.get('ipsecPsk') or '')
                    if ipsec_on and not psk:
                        return None, None, "IPsec روشن است ولی Secret (PSK) در اینباند تنظیم نشده — پنل را چک کنید"
                    host = self._config_host or _host_from_url(self.url)
                    try:
                        port = int((inbound or {}).get('port') or 1701)
                    except (TypeError, ValueError):
                        port = 1701
                    head = 'L2TP/IPsec' if ipsec_on else 'L2TP (RAW)'
                    lines = [
                        f"# {head} — OverWall",
                        f"Server: {host}",
                        f"Port: {port}",
                        f"Username: {email}",
                        f"Password: {pwd}",
                    ]
                    if psk:
                        lines.append(f"Secret: {psk}")
                    lines += [
                        "",
                        "iOS: Settings > VPN > Add VPN Configuration > Type: L2TP/IPsec",
                        "Android: Settings > VPN > + > VPN type: L2TP/IPsec PSK",
                        "Windows: Settings > VPN > Add VPN > Windows (built-in) > L2TP/IPsec with pre-shared key",
                        "",
                        "Credentials are private — do not share this file.",
                    ]
                    return "\n".join(lines), f"{email}-l2tp.txt", None
                return None, None, "نوع پروتکل قابل تشخیص نبود"
            except Exception as e:
                return None, None, str(e)

    @staticmethod
    def _fix_ovpn_host(text, host):
        """remote <host> <port> <proto> فایل .ovpn را با IP کانفیگِ پنل جایگزین می‌کند.

        پنل در remote آی‌پی سرور می‌گذارد؛ اگر ادمین IP کانفیگ (دامنه) تنظیم کرده،
        فایل باید همان دامنه را نشان دهد (برای SNI/TLS و عبور از فیلترینگ)."""
        if not host or not text:
            return text
        import re as _re
        try:
            # remote <IP> [port [proto]] → remote <host> [port [proto]]
            return _re.sub(
                r'(?m)^(\s*remote\s+)\S+',
                lambda m: m.group(1) + str(host),
                text,
                count=0,
            )
        except Exception:
            return text

    @staticmethod
    def _fix_wg_endpoint(text, host):
        """Endpoint = <host>:<port> کانفیگ وایرگارد را با IP کانفیگِ پنل جایگزین می‌کند.

        مثل ovpn: پنل آی‌پی خام سرور را می‌گذارد؛ اگر ادمین دامنه ثبت کرده باشد،
        فایل باید همان دامنه را نشان دهد."""
        if not host or not text:
            return text
        import re as _re
        try:
            # آخرین «:پورت» انتهای خط نگه داشته می‌شود؛ فقط هاست عوض می‌شود
            return _re.sub(
                r'(?m)^(\s*Endpoint\s*=\s*).+:(\d+)\s*$',
                lambda m: f"{m.group(1)}{host}:{m.group(2)}",
                text,
            )
        except Exception:
            return text
