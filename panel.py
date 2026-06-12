import json
import uuid
import time
import logging

import httpx

from config import PANEL_URL, PANEL_USER, PANEL_PASS, CONFIG_IP


def build_xui(panel):
    """از روی ردیف پنل، کلاینت X-UI و config_ip مربوطه را می‌سازد.
    اگر panel برابر None باشد، به مقادیر پیش‌فرضِ .env برمی‌گردد (سازگاری با نسخه‌ی تک‌پنل)."""
    if panel:
        return AsyncXuiAPI(panel['url'], panel['username'], panel['password']), (panel['config_ip'] or CONFIG_IP)
    return AsyncXuiAPI(PANEL_URL, PANEL_USER, PANEL_PASS), CONFIG_IP


def sub_link_for(panel, email):
    """در صورتی که پنل آدرس اشتراک داشته باشد، لینک ساب را برمی‌گرداند؛ وگرنه None."""
    if not panel:
        return None
    try:
        sub = panel['sub_url']
    except (KeyError, TypeError):
        sub = None
    if not sub:
        return None
    return sub.rstrip('/') + '/' + email


# ================= پنل X-UI =================
class AsyncXuiAPI:
    def __init__(self, panel_url, username, password):
        self.url = panel_url.rstrip('/')
        self.username = username
        self.password = password
        self.cookies = None

    async def login(self):
        async with httpx.AsyncClient(verify=False, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.post(f"{self.url}/login", data={"username": self.username, "password": self.password})
                if res.status_code == 200 and res.json().get('success'):
                    self.cookies = res.cookies
                    return True, "OK"
                return False, res.text
            except Exception as e:
                return False, str(e)

    async def get_inbound_port(self, inbound_id):
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.get(f"{self.url}/panel/api/inbounds/list")
                if res.status_code == 200:
                    for inbound in res.json().get('obj', []):
                        if int(inbound.get('id')) == int(inbound_id):
                            return inbound.get('port')
            except Exception as e:
                logging.warning("get_inbound_port failed: %s", e)
        return None

    async def add_client(self, inbound_id, client_email, total_gb, expire_days, limit_ip=1):
        client_uuid = str(uuid.uuid4())
        total_bytes = total_gb * 1024 * 1024 * 1024
        expire_time = int((time.time() + (expire_days * 86400)) * 1000)
        # subId برابر ایمیل قرار می‌گیرد تا لینک اشتراک (sub) به‌ازای هر کلاینت بسازیم
        settings = {"clients": [{"id": client_uuid, "email": client_email, "enable": True, "limitIp": limit_ip, "totalGB": total_bytes, "expiryTime": expire_time, "tgId": "", "subId": client_email}]}
        payload = {"id": inbound_id, "settings": json.dumps(settings)}

        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.post(f"{self.url}/panel/api/inbounds/addClient", json=payload, headers={'Accept': 'application/json'})
                data = res.json()
                if res.status_code == 200 and data.get('success'):
                    return client_uuid, None
                else:
                    return None, data.get('msg', res.text)
            except Exception as e:
                return None, str(e)

    async def get_client_stats(self, email):
        target_email = str(email).strip().lower()
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.get(f"{self.url}/panel/api/inbounds/getClientTraffics/{email}")
                if res.status_code == 200 and res.json().get('success'):
                    return res.json().get('obj', {})
            except Exception as e:
                logging.warning("get_client_stats failed: %s", e)
        return None

    async def get_all_client_stats(self):
        """با یک درخواست، آمار تمام کلاینت‌ها را برمی‌گرداند.
        خروجی: dict با کلید ایمیلِ lowercase و مقدارِ دیکشنری آمار (مشابه get_client_stats)."""
        result = {}
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=15.0, trust_env=False) as client:
            try:
                res = await client.get(f"{self.url}/panel/api/inbounds/list")
                if res.status_code == 200 and res.json().get('success'):
                    for inbound in res.json().get('obj', []):
                        for cs in inbound.get('clientStats', []) or []:
                            email = str(cs.get('email', '')).strip().lower()
                            if email:
                                result[email] = cs
            except Exception as e:
                logging.warning("get_all_client_stats failed: %s", e)
                return None
        return result

    async def get_client_exact_info(self, email):
        target_email = str(email).strip().lower()
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.get(f"{self.url}/panel/api/inbounds/list")
                if res.status_code == 200 and res.json().get('success'):
                    for inbound in res.json().get('obj', []):
                        settings = json.loads(inbound.get('settings', '{}'))
                        for c in settings.get('clients', []):
                            c_email = str(c.get('email', '')).strip().lower()
                            if c_email == target_email:
                                return inbound.get('id'), inbound.get('port'), c
            except Exception as e:
                logging.warning("get_client_exact_info failed: %s", e)
        return None, None, None

    async def update_client(self, inbound_id, old_uuid, client_dict):
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [client_dict]})}
        async with httpx.AsyncClient(verify=False, cookies=self.cookies, timeout=10.0, trust_env=False) as client:
            try:
                res = await client.post(f"{self.url}/panel/api/inbounds/updateClient/{old_uuid}", json=payload, headers={'Accept': 'application/json'})
                return res.status_code == 200 and res.json().get('success', False)
            except Exception as e:
                logging.warning("update_client failed: %s", e)
                return False
