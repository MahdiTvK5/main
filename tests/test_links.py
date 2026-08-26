"""تست توابع خالصِ لینک و شناسه‌ی کلاینت.

اجرا: python -m pytest tests -q   (نیاز به دیتابیس یا شبکه ندارد)
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from links import client_key, email_from_link, new_client_key, order_email  # noqa: E402


def _vmess(ps):
    conf = {"v": "2", "ps": ps, "add": "1.2.3.4", "port": "443", "id": "uuid-1", "net": "ws"}
    return "vmess://" + base64.b64encode(json.dumps(conf).encode()).decode()


class TestEmailFromLink:
    def test_vless_fragment(self):
        assert email_from_link("vless://uuid@1.2.3.4:443?type=ws#123_ali") == "123_ali"

    def test_trojan_fragment(self):
        assert email_from_link("trojan://pw@1.2.3.4:443?security=tls#123_ali") == "123_ali"

    def test_url_encoded_fragment(self):
        assert email_from_link("vless://uuid@1.2.3.4:443#user%20one") == "user one"

    def test_vmess_reads_ps_from_base64(self):
        # لینک vmess فرگمنت # ندارد؛ نام داخل Base64 است
        assert email_from_link(_vmess("123_ali")) == "123_ali"

    def test_vmess_without_padding(self):
        link = _vmess("abc").rstrip("=")
        assert email_from_link(link) == "abc"

    def test_broken_vmess_returns_empty(self):
        assert email_from_link("vmess://!!!not-base64!!!") == ""

    def test_empty_inputs(self):
        assert email_from_link("") == ""
        assert email_from_link(None) == ""


class TestOrderEmail:
    def test_db_column_wins(self):
        row = {"email": "from_db", "config_link": "vless://x@h:1#from_link"}
        assert order_email(row) == "from_db"

    def test_falls_back_to_link(self):
        row = {"email": None, "config_link": "vless://x@h:1#from_link"}
        assert order_email(row) == "from_link"

    def test_missing_email_column(self):
        row = {"config_link": "vless://x@h:1#only_link"}
        assert order_email(row) == "only_link"

    def test_vmess_order_without_email_column(self):
        row = {"config_link": _vmess("vm_user")}
        assert order_email(row) == "vm_user"

    def test_unknown_service_returns_empty(self):
        assert order_email({"config_link": "something-odd"}) == ""


class TestClientKey:
    def test_vless_uses_id(self):
        assert client_key({"id": "uuid-1", "email": "a"}) == "uuid-1"

    def test_trojan_uses_password(self):
        assert client_key({"password": "pw-1", "email": "a"}) == "pw-1"

    def test_empty_client(self):
        assert client_key({}) == ""
        assert client_key(None) == ""

    def test_new_key_matches_protocol(self):
        _, field = new_client_key({"id": "uuid-1"})
        assert field == "id"
        _, field = new_client_key({"password": "pw-1"})
        assert field == "password"
