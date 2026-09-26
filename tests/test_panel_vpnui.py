import asyncio
import json

import panel
from panel import (
    PANEL_TYPE_VPNUI,
    PANEL_TYPE_XUI,
    AsyncVpnUiAPI,
    AsyncXuiAPI,
    build_xui,
    normalize_panel_type,
)


# ================= normalize_panel_type =================

def test_normalize_panel_type_defaults_to_xui():
    assert normalize_panel_type('') == PANEL_TYPE_XUI
    assert normalize_panel_type(None) == PANEL_TYPE_XUI
    assert normalize_panel_type('unknown-kind') == PANEL_TYPE_XUI


def test_normalize_panel_type_aliases():
    assert normalize_panel_type('xui') == PANEL_TYPE_XUI
    assert normalize_panel_type('3x-ui') == PANEL_TYPE_XUI
    assert normalize_panel_type('3XUI') == PANEL_TYPE_XUI
    assert normalize_panel_type('VPN-UI') == PANEL_TYPE_VPNUI
    assert normalize_panel_type('vpnui') == PANEL_TYPE_VPNUI
    assert normalize_panel_type('vpn_ui') == PANEL_TYPE_VPNUI


def _run(coro):
    """اجرای coroutine در حلقه‌ی رویداد تازه (بدون نیاز به pytest-asyncio)."""
    return asyncio.run(coro)


# ================= build_xui =================

def _panel_row(panel_type=None):
    row = {
        'id': 1, 'name': 'p', 'url': 'https://panel.example:2083',
        'username': 'u', 'password': 'p', 'config_ip': '1.2.3.4',
        'sub_url': '',
    }
    if panel_type is not None:
        row['panel_type'] = panel_type
    return row


def test_build_xui_defaults_to_xui_class():
    # بدون ستون panel_type (ردیف قدیمی دیتابیس) هم باید xui انتخاب شود
    for pt in (None, '', 'xui', '3x-ui'):
        xui, ip = build_xui(_panel_row(pt))
        assert type(xui) is AsyncXuiAPI
        assert ip == '1.2.3.4'


def test_build_xui_selects_vpnui_class():
    xui, _ip = build_xui(_panel_row('vpn-ui'))
    assert type(xui) is AsyncVpnUiAPI
    # کلاس vpn-ui از xui ارث می‌برد تا متدهای مشترک یکسان بمانند
    assert isinstance(xui, AsyncXuiAPI)


# ================= _client_identity =================

def test_client_identity_per_protocol():
    ident = AsyncVpnUiAPI._client_identity
    # پروتکل‌های password-محور
    assert ident('trojan', {'password': 'pw', 'email': 'e'}) == 'pw'
    assert ident('openvpn', {'password': 'pw'}) == 'pw'
    # shadowsocks با email آدرس‌دهی می‌شود
    assert ident('shadowsocks', {'email': 'e'}) == 'e'
    # hysteria با auth
    assert ident('hysteria2', {'auth': 'au'}) == 'au'
    # vless/vmess/tuic با uuid
    assert ident('vless', {'id': 'uuid-1', 'email': 'e'}) == 'uuid-1'
    # پروتکل‌های email-محور (wg-c/awg/gre/mtproto/ssh) که id=email است
    assert ident('ssh', {'id': 'user@mail', 'email': 'user@mail'}) == 'user@mail'
    assert ident('awg', {'email': 'user@mail'}) == 'user@mail'


# ================= update_client (سبک vpn-ui) =================

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """جایگزین httpx.AsyncClient داخل panel برای ضبط درخواست POST."""
    last_post = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        _FakeAsyncClient.last_post = (url, kwargs)
        return _FakeResponse({'success': True, 'msg': '', 'obj': {}})


def _inbound(protocol='vless', clients=None):
    clients = clients if clients is not None else [
        {'id': 'uuid-1', 'email': 'user@mail', 'enable': True,
         'totalGB': 10 * 1024 ** 3, 'expiryTime': 1234567890, 'limitIp': 2},
    ]
    return {
        'id': 7, 'port': 443, 'protocol': protocol, 'remark': 'inb',
        'listen': '', 'settings': json.dumps({'clients': clients}),
        'streamSettings': '{}', 'sniffing': '{}',
    }


def _make_api(monkeypatch, inbound):
    monkeypatch.setattr(panel.httpx, 'AsyncClient', _FakeAsyncClient)

    async def fake_get_inbound(inbound_id):
        return inbound

    api = AsyncVpnUiAPI('https://panel.example:2083', 'u', 'p')
    monkeypatch.setattr(api, 'get_inbound', fake_get_inbound)
    return api


def test_update_client_posts_form_encoded_full_client(monkeypatch):
    """بدنه باید form-encoded باشد و کلاینتِ کامل (با حفظ فیلدهای موجود) ارسال شود."""
    api = _make_api(monkeypatch, _inbound('vless'))
    ok = _run(api.update_client(7, 'user@mail', {'totalGB': 99 * 1024 ** 3}))
    assert ok is True

    url, kwargs = _FakeAsyncClient.last_post
    assert '/panel/api/inbounds/updateClient/uuid-1' in url  # identity = uuid برای vless
    assert 'json' not in kwargs and 'data' in kwargs         # form-encoded، نه JSON

    sent = json.loads(kwargs['data']['settings'])['clients'][0]
    assert sent['totalGB'] == 99 * 1024 ** 3                # تغییر اعمال شده
    assert sent['expiryTime'] == 1234567890                 # فیلدهای موجود صفر نشده‌اند
    assert sent['id'] == 'uuid-1'                           # UUID حفظ شده
    assert sent['limitIp'] == 2


def test_update_client_trojan_identity_is_password(monkeypatch):
    stored = {'password': 'pw-1', 'email': 'user@mail', 'totalGB': 5, 'expiryTime': 1}
    api = _make_api(monkeypatch, _inbound('trojan', [stored]))
    ok = _run(api.update_client(7, 'user@mail', {'totalGB': 6}))
    assert ok is True
    url, _kwargs = _FakeAsyncClient.last_post
    assert '/panel/api/inbounds/updateClient/pw-1' in url


def test_update_client_failure_returns_error(monkeypatch):
    class _FailClient(_FakeAsyncClient):
        async def post(self, url, **kwargs):
            return _FakeResponse({'success': False, 'msg': 'boom'}, status_code=200)

    monkeypatch.setattr(panel.httpx, 'AsyncClient', _FailClient)

    async def fake_get_inbound(inbound_id):
        return _inbound('vless')

    api = AsyncVpnUiAPI('https://panel.example:2083', 'u', 'p')
    monkeypatch.setattr(api, 'get_inbound', fake_get_inbound)
    ok = _run(api.update_client(7, 'user@mail', {'totalGB': 1}))
    assert ok is False


def test_update_client_full_returns_error_tuple(monkeypatch):
    class _FailClient(_FakeAsyncClient):
        async def post(self, url, **kwargs):
            return _FakeResponse({'success': False, 'msg': 'boom'}, status_code=200)

    monkeypatch.setattr(panel.httpx, 'AsyncClient', _FailClient)

    async def fake_get_inbound(inbound_id):
        return _inbound('vless')

    api = AsyncVpnUiAPI('https://panel.example:2083', 'u', 'p')
    monkeypatch.setattr(api, 'get_inbound', fake_get_inbound)
    ok, err = _run(api._update_client_full(7, 'user@mail', {'totalGB': 1}))
    assert ok is False and err == 'boom'


def test_update_client_missing_inbound(monkeypatch):
    async def fake_get_inbound(inbound_id):
        return None

    api = AsyncVpnUiAPI('https://panel.example:2083', 'u', 'p')
    monkeypatch.setattr(api, 'get_inbound', fake_get_inbound)
    ok = _run(api.update_client(9, 'x@mail', {'totalGB': 1}))
    assert ok is False
