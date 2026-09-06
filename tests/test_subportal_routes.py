"""تست مسیرهای واقعی aiohttp پورتال اشتراک با پنلِ شبیه‌سازی‌شده.

پنل 3x-UI شبیه‌سازی می‌شود تا مسیرهای /sub/... بدون پنل واقعی هم قابل تست باشند.
"""
import base64
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("WEB_ADMIN_PASSWORD", "test-pass")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import db as db_mod  # noqa: E402
import subportal  # noqa: E402
import webpanel  # noqa: E402

NOW_MS = 1_800_000_000_000

INBOUND = {
    'id': 1, 'protocol': 'vless', 'port': 443,
    'streamSettings': '{"network":"ws","security":"tls",'
                      '"tlsSettings":{"serverName":"cdn.example.com"},'
                      '"wsSettings":{"path":"/ws"}}',
}
CLIENT = {'id': 'uuid-1', 'email': '201_svc', 'enable': True,
          'totalGB': 200 * 1024**3, 'expiryTime': NOW_MS + 30 * 86400000}
STATS = {'up': 10 * 1024**3, 'down': 20 * 1024**3, 'total': 200 * 1024**3,
         'expiryTime': NOW_MS + 30 * 86400000}


def _order(email):
    return {'id': 55, 'user_id': 201, 'config_link': f'vless://uuid-1@host:443?type=ws#{email}',
            'email': email, 'date': '2026-01-01', 'panel_id': 1, 'inbound_id': 1,
            'plan_id': None, 'purchase_kind': 'buy', 'user_role': 'vip'}


class FakeXui:
    def __init__(self, client=CLIENT, login_ok=True):
        self.client, self.login_ok = client, login_ok

    async def login(self):
        return self.login_ok, 'OK' if self.login_ok else 'bad'

    async def get_client_exact_info(self, email):
        if self.client is None or email != '201_svc':
            return None, None, None
        return 1, 443, dict(self.client)

    async def get_inbound(self, inbound_id):
        return dict(INBOUND)

    async def get_client_stats(self, email):
        return dict(STATS)


@pytest.fixture()
def client_factory(monkeypatch):
    def _make(order_row=None, fake_xui=None):
        order_row = order_row if order_row is not None else _order('201_svc')
        fake_xui = fake_xui or FakeXui()

        async def fake_get_order_by_token(token):
            if token == 'tok-valid':
                return order_row
            return None

        async def fake_get_panel(panel_id):
            return {'id': 1, 'name': 'آلمان', 'url': 'https://p', 'username': 'u',
                    'password': 'p', 'config_ip': 'host.example.com'}

        monkeypatch.setattr(subportal.db, 'get_order_by_token', fake_get_order_by_token)
        monkeypatch.setattr(subportal.db, 'get_panel', fake_get_panel)
        monkeypatch.setattr(subportal, 'build_xui', lambda panel: (fake_xui, 'host.example.com'))
        subportal._cache.clear()

        class _CM:
            """TestClient باید داخل رویدادِ در حال اجرا ساخته شود (نیاز به running loop)."""

            async def __aenter__(self):
                self._client = TestClient(TestServer(webpanel.build_app()))
                await self._client.__aenter__()
                return self._client

            async def __aexit__(self, *exc):
                return await self._client.__aexit__(*exc)

        return _CM()
    return _make

def _get(factory, path):
    """درخواست GET به اپ واقعی؛ خروجی: (status, text, headers).

    factory می‌تواند خودِ فیکسچر (قابل‌فراخوانی) یا نمونه‌ی آماده‌ی _CM باشد.
    """
    cm = factory() if callable(factory) else factory

    async def inner():
        async with cm as client:
            resp = await client.get(path)
            return resp.status, await resp.text(), resp.headers
    return asyncio.new_event_loop().run_until_complete(inner())


def test_portal_page_renders_live_data(client_factory):
    status, html_out, _h = _get(client_factory, "/sub/tok-valid")
    assert status == 200
    assert '<html lang="fa" dir="rtl">' in html_out
    assert '201_svc' in html_out
    assert '/sub/tok-valid/raw' in html_out
    assert 'روز باقی‌مانده' in html_out
    # مشخصات از روی اینباند واقعی
    assert 'VLESS' in html_out and 'WebSocket' in html_out and 'TLS' in html_out
    # سرور و تعداد کانفیگ دیگر در بخش مشخصات نمایش داده نمی‌شوند
    assert '<b>host.example.com</b>' not in html_out
    assert 'تعداد کانفیگ' not in html_out


def test_invalid_token_returns_404_page(client_factory):
    status, text, _h = _get(client_factory, "/sub/tok-invalid")
    assert status == 404
    assert 'یافت نشد' in text


def test_raw_subscription_content_and_headers(client_factory):
    status, body, headers = _get(client_factory, "/sub/tok-valid/raw")
    assert status == 200
    decoded = base64.b64decode(body).decode()
    assert decoded.startswith("vless://uuid-1@host.example.com:443")
    # هدرهای استاندارد اشتراک برای نمایش ترافیک داخل اپ‌ها
    assert 'subscription-userinfo' in headers
    ui = headers['subscription-userinfo']
    assert f'download={30 * 1024**3}' in ui and f'total={200 * 1024**3}' in ui


def test_clash_subscription_endpoint(client_factory):
    status, yaml_text, _h = _get(client_factory, "/sub/tok-valid/clash")
    assert status == 200
    assert 'type: vless' in yaml_text and 'uuid: "uuid-1"' in yaml_text


def test_missing_client_on_panel_is_404(client_factory):
    factory = client_factory(fake_xui=FakeXui(client=None))
    status, _t, _h = _get(factory, "/sub/tok-valid")
    assert status == 404
    status2, _t2, _h2 = _get(factory, "/sub/tok-valid/raw")
    assert status2 == 404


def test_panel_down_is_503_not_crash(client_factory):
    factory = client_factory(fake_xui=FakeXui(login_ok=False))
    status, text, _h = _get(factory, "/sub/tok-valid")
    assert status == 503
    assert 'در دسترس نیست' in text


def test_no_idor_between_services(client_factory):
    """توکنِ سرویس A هرگز داده‌ی سرویس B را برنمی‌گرداند."""
    factory = client_factory()
    status, html_a, _h = _get(factory, "/sub/tok-valid")
    assert status == 200
    assert '201_svc' in html_a
    # توکن نامعتبر/دستکاری‌شده → 404 بدون هیچ داده‌ای
    status2, text2, _h2 = _get(factory, "/sub/tok-valid-but-other")
    assert status2 == 404 and '201_svc' not in text2


def test_disabled_client_shows_status(client_factory):
    disabled = dict(CLIENT, enable=False)
    factory = client_factory(fake_xui=FakeXui(client=disabled))
    status, html_out, _h = _get(factory, "/sub/tok-valid")
    assert status == 200 and 'غیرفعال' in html_out


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
