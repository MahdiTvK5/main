import json
import uuid
import time
import base64
import logging
from urllib.parse import urlparse, quote

import httpx

from config import PANEL_URL, PANEL_USER, PANEL_PASS, CONFIG_IP


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


def build_xui(panel):
    """از روی ردیف پنل، کلاینت X-UI و config_ip مربوطه را می‌سازد.
    اگر panel برابر None باشد، به مقادیر پیش‌فرضِ .env برمی‌گردد (سازگاری با نسخه‌ی تک‌پنل).
    اگر «IP کانفیگ» پنل تنظیم نشده باشد، برای جلوگیری از ساختِ لینک خرابِ بدون هاست،
    به‌ترتیب از CONFIG_IP و سپس دامنه‌ی خود پنل استفاده می‌شود."""
    if panel:
        panel_url = panel['url']
        cfg_ip = (panel['config_ip'] or CONFIG_IP or _host_from_url(panel_url))
        return AsyncXuiAPI(panel_url, panel['username'], panel['password']), cfg_ip
    cfg_ip = CONFIG_IP or _host_from_url(PANEL_URL)
    return AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS), cfg_ip


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
        last_err = "آدرس پنل نامعتبر است"
        for base in self._candidate_urls():
            # follow_redirects تا ریدایرکتِ http→https پنل خودکار دنبال شود
            async with httpx.AsyncClient(verify=False, timeout=10.0, trust_env=False, follow_redirects=True) as client:
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
