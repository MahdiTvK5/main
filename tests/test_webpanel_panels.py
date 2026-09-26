"""تست رگرسین صفحات «سرورها» (پنل‌ها) پنل وب بدون دیتابیس واقعی.

باگ واقعی: بعد از افزودن سلکت «نوع پنل» به فرم ویرایش پنل، متغیر cur_type در
panel_edit_form تعریف نشده بود و صفحه‌ی /panels/edit?id=… با NameError کرش
می‌کرد (500 Internal Server Error). این تست‌ها هر دو حالت ویرایش/افزودن و خود
صفحه‌ی لیست را پوشش می‌دهند.
"""
import os
import sys
import types
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("WEB_ADMIN_PASSWORD", "test-pass")


def _fake_panel(pid=1, panel_type="vpn-ui"):
    return {
        "id": pid, "name": "سرور تست", "url": "http://127.0.0.1:2083",
        "username": "admin", "password": "secret", "config_ip": "1.2.3.4",
        "sub_url": "", "panel_type": panel_type,
    }


def _patch_db(panel_row):
    import webpanel

    class _DB:
        async def get_panels(self):
            return [panel_row] if panel_row else []

        async def get_panel(self, pid):
            return panel_row if panel_row and pid == panel_row["id"] else None

    webpanel.db = _DB()
    return webpanel


def _request(query=None):
    import webpanel
    return types.SimpleNamespace(query=query or {}, remote="127.0.0.1",
                                 cookies={"session": webpanel._new_session()})


def test_panels_page_renders_with_panel_type():
    wp = _patch_db(_fake_panel(1, "vpn-ui"))
    resp = asyncio.run(wp.panels_page(_request()))
    html = resp.text
    assert "سرور تست" in html
    assert "VPN-UI" in html  # ستون نوع باید نوع واقعی را نشان دهد


def test_panel_edit_form_existing_shows_correct_type():
    wp = _patch_db(_fake_panel(1, "vpn-ui"))
    resp = asyncio.run(wp.panel_edit_form(_request({"id": "1"})))
    html = resp.text
    # سلکت نوع پنل باید با مقدار فعلی انتخاب‌شده باشد
    assert "value='vpn-ui' selected" in html


def test_panel_edit_form_new_panel_defaults_to_xui():
    wp = _patch_db(None)
    resp = asyncio.run(wp.panel_edit_form(_request()))
    html = resp.text
    assert "value='xui' selected" in html


def test_panel_edit_form_legacy_row_without_panel_type_column():
    """ردیف‌های قدیمی دیتابیس که ستون panel_type ندارند نباید کرش کنند."""
    p = _fake_panel(1)
    del p["panel_type"]  # شبیه‌سازی ردیف قدیمی (dict بدون کلید)
    wp = _patch_db(p)
    resp = asyncio.run(wp.panel_edit_form(_request({"id": "1"})))
    assert "value='xui' selected" in resp.text
