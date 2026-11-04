"""تست‌های سرویس‌های فایلی (OpenVPN / WireGuard / L2TP) روی پنل vpn-ui."""
import asyncio
import json

import panel
from panel import (
    AsyncVpnUiAPI,
    file_link_kind,
    parse_file_link,
)
from links import order_email


# ================= نشانگرهای فایل =================

def test_file_link_kind():
    assert file_link_kind('ovpn://udp/fardin') == 'openvpn'
    assert file_link_kind('ovpn://tcp/fardin') == 'openvpn'
    assert file_link_kind('wg://fardin') == 'wireguard'
    assert file_link_kind('vless://uuid@host:443?x#fardin') == ''
    assert file_link_kind('') == ''
    assert file_link_kind(None) == ''


def test_parse_file_link():
    assert parse_file_link('ovpn://udp/fardin') == ('openvpn', 'udp', 'fardin')
    assert parse_file_link('ovpn://tcp/fardin') == ('openvpn', 'tcp', 'fardin')
    assert parse_file_link('wg://fardin') == ('wireguard', 'udp', 'fardin')
    assert parse_file_link('l2tp://fardin') == ('l2tp', '', 'fardin')


def test_order_email_reads_file_link_fragment():
    # email باید حتی از نشانگر فایل هم استخراج شود (fallback برای رکوردهای بدون ستون email)
    assert order_email({'config_link': 'ovpn://udp/fardin'}) == 'fardin'
    assert order_email({'config_link': 'wg://fardin'}) == 'fardin'
    assert order_email({'config_link': 'l2tp://fardin'}) == 'fardin'
    assert order_email({'config_link': 'vless://u@h:1?x#ali'}) == 'ali'


# ================= add_client روی اینباند فایلی =================

class _FakeResponse:
    def __init__(self, payload, status_code=200, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


class _PostCapture:
    last_post = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        _PostCapture.last_post = (url, kwargs)
        return _FakeResponse({'success': True, 'msg': '', 'obj': {}})


def _file_inbound(protocol='openvpn'):
    return {
        'id': 11, 'port': 1194, 'protocol': protocol, 'remark': 'inb',
        'listen': '', 'settings': json.dumps({'clients': []}),
        'streamSettings': '{}', 'sniffing': '{}',
    }


def _make(monkeypatch, inbound):
    monkeypatch.setattr(panel.httpx, 'AsyncClient', _PostCapture)

    async def fake_get_inbound(inbound_id):
        return inbound

    api = AsyncVpnUiAPI('https://panel.example:2083', 'u', 'p')
    monkeypatch.setattr(api, 'get_inbound', fake_get_inbound)
    return api


def test_add_client_openvpn_returns_marker(monkeypatch):
    api = _make(monkeypatch, _file_inbound('openvpn'))
    link, err = _run(api.add_client(11, 'fardin', 50, 30, '1.2.3.4'))
    assert err is None
    assert link == 'ovpn://udp/fardin'

    url, kwargs = _PostCapture.last_post
    assert url.endswith('/panel/api/inbounds/update/11')
    assert 'json' not in kwargs and 'data' in kwargs  # فرم‌انکود
    settings = json.loads(kwargs['data']['settings'])
    acct = settings['clients'][0]
    assert acct['email'] == 'fardin'
    assert acct['id'] == 'fardin'          # در پروتکل‌های فایلی id همان email است
    assert acct['password']                # رمز RADIUS ساخته شده
    assert acct['totalGB'] == 50 * 1024 ** 3
    assert acct['expiryTime'] > 0


def test_add_client_wg_returns_marker(monkeypatch):
    api = _make(monkeypatch, _file_inbound('wg-c'))
    link, err = _run(api.add_client(11, 'fardin', 50, 30, '1.2.3.4'))
    assert err is None
    assert link == 'wg://fardin'


def test_add_client_rejects_xray_protocol(monkeypatch):
    api = _make(monkeypatch, _file_inbound('vless'))
    link, err = _run(api.add_client(11, 'fardin', 50, 30, '1.2.3.4'))
    assert link is None and 'پشتیبانی نمی‌شود' in err


# ================= download_config =================

class _GetFake:
    """کلاینت httpx جعلی با پاسخ‌های configurable برای GET."""

    behavior = None  # تابع (url) -> _FakeResponse | متن خام

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        return type(self).behavior(url)


_OVPN_TEXT = "client\nremote 1.2.3.4 1194 udp\n<ca>\nABC\n</ca>\n"
_WG_TEXT = "[Interface]\nPrivateKey = abc=\nAddress = 10.7.0.2/32\n[Peer]\nPublicKey = xyz=\n"


def test_download_config_openvpn(monkeypatch):
    def behavior(url):
        assert '/panel/api/inbounds/11/ovpn/tcp' in url  # اول tcp امتحان می‌شود
        return _FakeResponse({}, text=_OVPN_TEXT)

    _GetFake.behavior = behavior
    monkeypatch.setattr(panel.httpx, 'AsyncClient', _GetFake)
    api = AsyncVpnUiAPI('https://panel.example:2083', 'u', 'p')
    text, filename, err = _run(api.download_config(11, 'fardin', 'openvpn'))
    assert err is None
    assert text == _OVPN_TEXT
    assert filename == 'fardin.ovpn'


def test_download_config_openvpn_tcp_fallback(monkeypatch):
    """اگر tcp فایل نداد (اینباند udp باشد)، udp امتحان می‌شود."""
    def behavior(url):
        if url.endswith('/ovpn/tcp'):
            return _FakeResponse({'success': False, 'msg': 'no'}, text='{"success":false}')
        assert url.endswith('/ovpn/udp')
        return _FakeResponse({}, text=_OVPN_TEXT)

    _GetFake.behavior = behavior
    monkeypatch.setattr(panel.httpx, 'AsyncClient', _GetFake)
    api = AsyncVpnUiAPI('https://panel.example:2083', 'u', 'p')
    text, filename, err = _run(api.download_config(11, 'fardin', 'openvpn'))
    assert err is None and filename == 'fardin.ovpn'


def test_fix_ovpn_host_rewrites_remote(monkeypatch):
    """remote با IP کانفیگ (دامنه) جایگزین می‌شود و بقیه‌ی فایل دست نمی‌خورد."""
    api = AsyncVpnUiAPI('https://panel.example:2083', 'u', 'p')
    src = "client\nremote 1.2.3.4 1194 udp\n<ca>\nABC\n</ca>\nremote 5.6.7.8 443 tcp\n"
    out = api._fix_ovpn_host(src, 'vpn.example.com')
    assert 'remote vpn.example.com 1194 udp' in out
    assert 'remote vpn.example.com 443 tcp' in out
    assert '<ca>' in out
    # بدون host، فایل دست نمی‌خورد
    assert api._fix_ovpn_host(src, '') == src


def test_add_client_openvpn_transport_from_remark(monkeypatch):
    """ترنسپورت نشانگر از روی نام اینباند تشخیص داده می‌شود (tcp در remark)."""
    inbound = _file_inbound('openvpn')
    inbound['remark'] = 'ovpn-tcp-1443'
    api = _make(monkeypatch, inbound)
    link, err = _run(api.add_client(11, 'fardin', 50, 30, '1.2.3.4'))
    assert err is None
    assert link == 'ovpn://tcp/fardin'


def test_download_config_wg(monkeypatch):
    def behavior(url):
        assert 'wgc-configs?email=fardin' in url
        return _FakeResponse({'success': True, 'obj': [
            {'deviceIndex': 0, 'ip': '10.7.0.2', 'config': _WG_TEXT},
        ]})

    _GetFake.behavior = behavior
    monkeypatch.setattr(panel.httpx, 'AsyncClient', _GetFake)
    api = AsyncVpnUiAPI('https://panel.example:2083', 'u', 'p')
    text, filename, err = _run(api.download_config(11, 'fardin', 'wireguard'))
    assert err is None
    assert '[Interface]' in text
    assert filename == 'fardin.conf'


def test_download_config_wg_single_interface(monkeypatch):
    """چند-دستگاهه: فقط اولین کانفیگِ دارای [Interface] فرستاده می‌شود؛
    چسباندن همه به هم فایل نامعتبر می‌سازد که اپ WireGuard رد می‌کند."""
    dev0 = "[Interface]\nPrivateKey = a=\nAddress = 10.7.0.2/32\n[Peer]\nPublicKey = s=\nEndpoint = 1.2.3.4:51820\n"
    dev1 = "[Interface]\nPrivateKey = b=\nAddress = 10.7.0.3/32\n[Peer]\nPublicKey = s=\nEndpoint = 1.2.3.4:51820\n"
    def behavior(url):
        return _FakeResponse({'success': True, 'obj': [
            {'deviceIndex': 0, 'ip': '10.7.0.2', 'config': dev0},
            {'deviceIndex': 1, 'ip': '10.7.0.3', 'config': dev1},
        ]})

    _GetFake.behavior = behavior
    monkeypatch.setattr(panel.httpx, 'AsyncClient', _GetFake)
    api = AsyncVpnUiAPI('https://panel.example:2083', 'u', 'p')
    text, filename, err = _run(api.download_config(11, 'fardin', 'wireguard'))
    assert err is None
    assert text.count('[Interface]') == 1   # فقط یک دستگاه
    assert 'PrivateKey = a=' in text        # همان دستگاه اول
    assert '10.7.0.3' not in text


def test_download_config_wg_endpoint_rewritten(monkeypatch):
    """Endpoint با IP کانفیگ (دامنه) جایگزین می‌شود؛ پورت دست نمی‌خورد."""
    def behavior(url):
        return _FakeResponse({'success': True, 'obj': [
            {'deviceIndex': 0, 'ip': '10.7.0.2', 'config': _WG_TEXT + 'Endpoint = 1.2.3.4:51820\n'},
        ]})

    _GetFake.behavior = behavior
    monkeypatch.setattr(panel.httpx, 'AsyncClient', _GetFake)
    api = AsyncVpnUiAPI('https://panel.example:2083', 'u', 'p')
    api._config_host = 'vpn.example.com'
    text, _fn, err = _run(api.download_config(11, 'fardin', 'wireguard'))
    assert err is None
    assert 'Endpoint = vpn.example.com:51820' in text
    assert '1.2.3.4' not in text


# ================= L2TP =================

def test_file_link_l2tp_kind():
    assert file_link_kind('l2tp://fardin') == 'l2tp'
    assert file_link_kind('vless://uuid@host:443?x#fardin') == ''


def test_add_client_l2tp_returns_marker(monkeypatch):
    api = _make(monkeypatch, _file_inbound('l2tp'))
    link, err = _run(api.add_client(11, 'fardin', 50, 30, '1.2.3.4'))
    assert err is None
    assert link == 'l2tp://fardin'


def test_download_config_l2tp_card(monkeypatch):
    """کارت اتصال: سرور/پورت/یوزر/پسورد/PSK از روی اینباند ساخته می‌شود."""
    inbound = _file_inbound('l2tp')
    inbound['port'] = 1701
    inbound['settings'] = json.dumps({
        'clients': [{'email': 'fardin', 'id': 'fardin', 'password': 'secret'}],
        'ipsecEnable': True, 'ipsecPsk': 'sh4r3d-psk',
    })
    api = _make(monkeypatch, inbound)
    api._config_host = 'vpn.example.com'
    text, filename, err = _run(api.download_config(11, 'fardin', 'l2tp'))
    assert err is None
    assert 'Server: vpn.example.com' in text
    assert 'Port: 1701' in text
    assert 'Username: fardin' in text
    assert 'Password: secret' in text
    assert 'PSK: sh4r3d-psk' in text
    assert filename == 'fardin-l2tp.txt'


def test_download_config_l2tp_raw_no_psk(monkeypatch):
    """اینباند RAW (بدون IPsec): کارتْ PSK ندارد."""
    inbound = _file_inbound('l2tp')
    inbound['port'] = 1701
    inbound['settings'] = json.dumps({
        'clients': [{'email': 'fardin', 'id': 'fardin', 'password': 'secret'}],
        'ipsecEnable': False, 'allowRaw': True,
    })
    api = _make(monkeypatch, inbound)
    api._config_host = 'vpn.example.com'
    text, _fn, err = _run(api.download_config(11, 'fardin', 'l2tp'))
    assert err is None and 'PSK:' not in text   # خط مقدار PSK ] راست راهنماهای رشته‌ای مجاز است


def test_download_config_l2tp_ipsec_without_psk_fails(monkeypatch):
    """ipsecEnable روشن ولی PSK خالی → خطای شفاف به‌جای کارت ناقص."""
    inbound = _file_inbound('l2tp')
    inbound['port'] = 1701
    inbound['settings'] = json.dumps({
        'clients': [{'email': 'fardin', 'id': 'fardin', 'password': 'secret'}],
        'ipsecEnable': True, 'ipsecPsk': '',
    })
    api = _make(monkeypatch, inbound)
    text, _fn, err = _run(api.download_config(11, 'fardin', 'l2tp'))
    assert text is None and 'PSK' in err


def _run(coro):
    return asyncio.run(coro)
