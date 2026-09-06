"""تست لایه‌ی خالص پورتال اشتراک (بدون دیتابیس/شبکه).

اجرا: python -m pytest tests -q
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "test:token")

import subportal  # noqa: E402


# ================= توکن =================
class TestToken:
    def test_token_format_and_uniqueness(self):
        tokens = {subportal.make_sub_token() for _ in range(200)}
        assert len(tokens) == 200
        for t in tokens:
            assert 20 <= len(t) <= 128
            assert '/' not in t  # URL-safe

    def test_portal_and_raw_urls(self):
        subportal.PORTAL_PUBLIC_URL = "https://sub.example.com"
        assert subportal.portal_url("tok") == "https://sub.example.com/sub/tok"
        assert subportal.raw_url("tok") == "https://sub.example.com/sub/tok/raw"
        assert subportal.clash_url("tok") == "https://sub.example.com/sub/tok/clash"
        # بدون آدرس عمومی: لینک ربات ساخته نمی‌شود ولی با base دستی می‌شود
        subportal.PORTAL_PUBLIC_URL = ""
        assert subportal.portal_url("tok") is None
        assert subportal.raw_url("tok") is None
        assert subportal.raw_url("tok", base="https://x.io/") == "https://x.io/sub/tok/raw"


# ================= اتصال سریع =================
class TestQuickConnect:
    def test_v2rayng_uses_encoded_url(self):
        app = {'id': 'v2rayng', 'name': 'V2RayNG', 'scheme': 'v2rayng://install-sub?url={url_enc}'}
        url = subportal.build_import_url(app, "https://x.io/sub/a b/raw", "", "ali")
        assert url == "v2rayng://install-sub?url=https%3A%2F%2Fx.io%2Fsub%2Fa%20b%2Fraw"

    def test_happ_imports_raw_url(self):
        app = {'id': 'happ', 'name': 'Happ', 'scheme': 'happ://add/{url}'}
        assert subportal.build_import_url(
            app, "https://x.io/sub/t/raw", "", "") == "happ://add/https://x.io/sub/t/raw"

    def test_incy_imports_raw_url(self):
        app = {'id': 'incy', 'name': 'INCY', 'scheme': 'incy://import/{url}'}
        assert subportal.build_import_url(
            app, "https://x.io/sub/t/raw", "", "") == "incy://import/https://x.io/sub/t/raw"

    def test_shadowrocket_uses_base64(self):
        app = {'id': 'shadowrocket', 'name': 'Shadowrocket', 'scheme': 'sub://{url_b64}'}
        url = subportal.build_import_url(app, "https://x.io/s/raw", "", "")
        assert url == "sub://" + base64.b64encode(b"https://x.io/s/raw").decode()

    def test_clash_apps_use_clash_url(self):
        app = {'id': 'clashverge', 'name': 'Clash Verge',
               'scheme': 'clash://install-config?url={clash_enc}&name={name_enc}'}
        url = subportal.build_import_url(app, "https://x.io/s/raw", "https://x.io/s/clash", "my sub")
        assert url.startswith("clash://install-config?url=")
        assert "https%3A%2F%2Fx.io%2Fs%2Fclash" in url
        assert "name=my%20sub" in url

    def test_app_without_scheme_returns_none(self):
        app = {'id': 'nekobox', 'name': 'NekoBox', 'scheme': None}
        assert subportal.build_import_url(app, "https://x.io/s/raw", "", "") is None

    def test_all_configured_apps_resolve(self):
        out = subportal.quick_connect_apps("https://x.io/sub/t/raw", "https://x.io/sub/t/clash", "svc")
        assert set(out.keys()) == set(subportal.QUICK_CONNECT.keys())
        for pdata in out.values():
            for app in pdata['apps']:
                assert 'import_url' in app and 'name' in app
        android = {a['id']: a for a in out['android']['apps']}
        assert android['v2rayng']['import_url'].startswith("v2rayng://")


# ================= ترافیک و وضعیت =================
class TestTraffic:
    def test_percent_and_remaining(self):
        tv = subportal.traffic_view(134 * 1024**3, 200 * 1024**3)
        assert tv['percent'] == 67
        assert not tv['unlimited']
        assert tv['remaining'] == 66 * 1024**3

    def test_unlimited_total_is_never_divided(self):
        tv = subportal.traffic_view(500 * 1024**3, 0)
        assert tv['unlimited'] and tv['percent'] is None
        assert tv['total_text'] == 'نامحدود' and tv['remaining_text'] == 'نامحدود'

    def test_zero_used_and_missing_values(self):
        tv = subportal.traffic_view(None, None)
        assert tv['used'] == 0 and tv['unlimited']
        tv2 = subportal.traffic_view(0, 100 * 1024**3)
        assert tv2['percent'] == 0

    def test_over_usage_caps_at_100(self):
        tv = subportal.traffic_view(300 * 1024**3, 200 * 1024**3)
        assert tv['percent'] == 100 and tv['remaining'] == 0


class TestStatus:
    NOW = 1_800_000_000_000  # ms

    def test_active(self):
        assert subportal.service_status(True, self.NOW + 86400000, 100, 50, self.NOW) == 'active'

    def test_disabled_wins(self):
        assert subportal.service_status(False, self.NOW + 86400000, 100, 50, self.NOW) == 'disabled'

    def test_expired(self):
        assert subportal.service_status(True, self.NOW - 1000, 100, 50, self.NOW) == 'expired'

    def test_exhausted(self):
        assert subportal.service_status(True, self.NOW + 86400000, 100, 100, self.NOW) == 'exhausted'

    def test_unlimited_expiry_and_traffic(self):
        assert subportal.service_status(True, 0, 0, 10**12, self.NOW) == 'active'


class TestExpiryView:
    NOW = 1_800_000_000_000

    def test_unlimited(self):
        ex = subportal.expiry_view(0, 'active')
        assert ex['unlimited'] and ex['remain_text'] == 'نامحدود'

    def test_remaining_days_and_hours(self):
        ex = subportal.expiry_view(self.NOW + 3 * 86400000, 'active', self.NOW)
        assert ex['remain_days'] == 3
        ex2 = subportal.expiry_view(self.NOW + 5 * 3600000, 'active', self.NOW)
        assert 'ساعت' in ex2['remain_text']

    def test_past_expiry(self):
        ex = subportal.expiry_view(self.NOW - 86400000, 'expired', self.NOW)
        assert ex['remain_text'] == 'منقضی شده'

# ================= خروجی خام / Clash =================
class TestRawAndClash:
    def test_raw_body_is_base64_of_links(self):
        body = subportal.build_raw_body(["vless://a@h:443?x=1#one", "vless://b@h:443?x=2#two"])
        decoded = base64.b64decode(body).decode()
        assert decoded.count("vless://") == 2 and "\n" in decoded

    def test_clash_yaml_contains_real_values(self):
        inbound = {
            'protocol': 'vless', 'port': 443,
            'streamSettings': '{"network":"ws","security":"tls",'
                              '"tlsSettings":{"serverName":"cdn.example.com"},'
                              '"wsSettings":{"path":"/wspath"}}',
        }
        client = {'id': 'uuid-123', 'email': 'svc'}
        yaml_text = subportal.build_clash_yaml('svc', inbound, client, 'host.example.com')
        assert 'type: vless' in yaml_text and 'uuid: "uuid-123"' in yaml_text
        assert 'server: "host.example.com"' in yaml_text and 'port: 443' in yaml_text
        assert 'servername: "cdn.example.com"' in yaml_text
        assert 'path: "/wspath"' in yaml_text and 'tls: true' in yaml_text

    def test_clash_yaml_trojan_and_grpc(self):
        inbound = {
            'protocol': 'trojan', 'port': 8443,
            'streamSettings': '{"network":"grpc","security":"tls",'
                              '"grpcSettings":{"serviceName":"grpcsvc"}}',
        }
        yaml_text = subportal.build_clash_yaml('svc', inbound, {'password': 'pass1'}, 'h')
        assert 'type: trojan' in yaml_text and 'password: "pass1"' in yaml_text
        assert 'grpc-service-name: "grpcsvc"' in yaml_text


# ================= رندر صفحات =================
def _sample_sub(**overrides):
    sub = {
        'token': 'tok123',
        'order_id': 1,
        'name': '12345_mahdi',
        'role': 'vip',
        'plan': 'پلن ۲۰ گیگ',
        'panel_name': 'آلمان',
        'config_link': 'vless://uuid@host:443?x=1#cfg',
        'links': ['vless://uuid@host:443?x=1#cfg'],
        'server': 'host.example.com',
        'port': 443,
        'status': {'key': 'active', 'label': 'فعال'},
        'traffic': subportal.traffic_view(134 * 1024**3, 200 * 1024**3),
        'expiry': subportal.expiry_view(1900000000000, 'active', 1800000000000),
        'details': {'protocol': 'VLESS', 'transport': 'WebSocket', 'security': 'TLS',
                    'type': 'ws', 'security_raw': 'tls'},
        'raw_url': 'https://x.io/sub/tok123/raw',
        'clash_url': 'https://x.io/sub/tok123/clash',
        'qr_url': 'https://qr/x',
        'apps': subportal.quick_connect_apps(
            'https://x.io/sub/tok123/raw', 'https://x.io/sub/tok123/clash', '12345_mahdi'),
    }
    sub.update(overrides)
    return sub


class TestPortalRendering:
    def test_rtl_and_real_data(self):
        html_out = subportal.render_portal(_sample_sub())
        assert '<html lang="fa" dir="rtl">' in html_out
        assert '12345_mahdi' in html_out and 'فعال' in html_out
        assert 'پلن ۲۰ گیگ' in html_out and 'اتصال سریع' in html_out
        assert 'https://x.io/sub/tok123/raw' in html_out

    def test_no_secrets_in_page(self):
        html_out = subportal.render_portal(_sample_sub())
        assert 'BOT_TOKEN' not in html_out and 'PANEL_PASS' not in html_out

    def test_xss_is_escaped(self):
        sub = _sample_sub(name="<script>alert(1)</script>")
        html_out = subportal.render_portal(sub)
        assert '<script>alert(1)</script>' not in html_out
        assert '&lt;script&gt;' in html_out

    def test_status_banners(self):
        html_out = subportal.render_portal(_sample_sub(status={'key': 'expired', 'label': 'منقضی شده'}))
        assert 'منقضی شده' in html_out and 'تمدید' in html_out
        html_out2 = subportal.render_portal(_sample_sub(status={'key': 'disabled', 'label': 'غیرفعال'}))
        assert 'غیرفعال' in html_out2

    def test_error_pages(self):
        for kind in ('not_found', 'panel'):
            page = subportal.render_error_page(kind)
            assert 'dir="rtl"' in page or "dir='rtl'" in page
        assert 'یافت نشد' in subportal.render_error_page('not_found')
        assert 'در دسترس نیست' in subportal.render_error_page('panel')

    def test_quick_connect_apps_rendered(self):
        html_out = subportal.render_portal(_sample_sub())
        assert 'V2RayNG' in html_out and 'Shadowrocket' in html_out
        assert 'Happ' in html_out and 'INCY' in html_out
        assert 'happ://add/' in html_out and 'incy://import/' in html_out
        assert 'Hiddify' not in html_out
        assert 'v2rayng://install-sub' in html_out
        # اپ بدون اسکیم → دکمه‌ی کپی با لینک اشتراک
        assert "data-copy='https://x.io/sub/tok123/raw'" in html_out


class TestSubscriptionHeaders:
    def test_userinfo_header(self):
        tv = subportal.traffic_view(134 * 1024**3, 200 * 1024**3)
        headers = subportal.subscription_headers('svc', tv, 1900000000000)
        assert headers['subscription-userinfo'] == (
            f'upload=0; download={134 * 1024**3}; total={200 * 1024**3}; expire=1900000000'
        )
        assert headers['Profile-Title'].startswith('base64:')

    def test_unlimited_header(self):
        headers = subportal.subscription_headers('svc', subportal.traffic_view(10, 0), 0)
        assert 'total=0; expire=0' in headers['subscription-userinfo']


class TestStreamDetails:
    def test_details_from_real_stream(self):
        inbound = {
            'protocol': 'VLESS',
            'streamSettings': '{"network":"ws","security":"reality",'
                              '"realitySettings":{"serverNames":["x.com"],"settings":{"publicKey":"pbk"}}}',
        }
        det = subportal.stream_details(inbound)
        assert det['protocol'] == 'VLESS' and det['transport'] == 'WebSocket'
        assert det['security_raw'] == 'reality' and det['security'] == 'Reality'

    def test_missing_stream_is_safe(self):
        det = subportal.stream_details({})
        assert det['protocol'] == 'VLESS' and det['security_raw'] == 'none'


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

