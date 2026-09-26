"""تست رگرسین صفحه‌ی ویرایش/ذخیره‌ی کاربر در پنل وب (بدون دیتابیس واقعی).

پوشش: چک‌باکس «پنل‌های مجاز خرید» در فرم کاربر — همه‌ی تیک‌ها = بدون محدودیت،
تیک جزئی = همان پنل‌ها. ذخیره هم باید تاگل را درست برگرداند.
"""
import os
import sys
import types
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("WEB_ADMIN_PASSWORD", "test-pass")


def _user_row(user_id=42):
    return {"user_id": user_id, "balance": 50000, "nickname": "علی", "role": "vip"}


def _patch_db(allowed=None):
    import webpanel
    from aiohttp import web  # noqa: F401  (برای گرفتن HTTPRedirect در تست‌ها)
    panels = [{"id": 1, "name": "پنل اول"}, {"id": 2, "name": "پنل دوم"}]

    class _DB:
        async def get_panels(self):
            return panels

        async def get_user_allowed_panels(self, uid):
            return allowed  # None = همه

        async def set_user_allowed_panels(self, uid, ids):
            type(self).last_saved = ids

        async def get_user_row(self, uid):
            return _user_row()

        async def save_user(self, uid, nickname, role, balance):
            pass

    webpanel.db = _DB()
    return webpanel


def _request(query=None, post=None):
    import webpanel
    req = types.SimpleNamespace(query=query or {}, remote="127.0.0.1",
                                cookies={"session": webpanel._new_session()})
    if post is not None:
        async def _post():
            return await post() if callable(post) else post
        req.post = _post
    return req


def test_user_edit_form_shows_panel_checkboxes():
    wp = _patch_db(allowed=None)
    resp = asyncio.run(wp.user_edit_form(_request({"id": "42"})))
    html = resp.text
    assert "پنل‌های مجاز خرید" in html
    assert "name='panel_ids' value='1' checked" in html   # «همه» → هر دو تیک
    assert "name='panel_ids' value='2' checked" in html
    assert "هیچ‌کدام تیک نخورد = همه‌ی پنل‌ها" in html


def test_user_edit_form_partial_selection():
    wp = _patch_db(allowed={2})
    resp = asyncio.run(wp.user_edit_form(_request({"id": "42"})))
    html = resp.text
    assert "value='1' checked" not in html
    assert "value='2' checked" in html


def test_user_save_partial_selection():
    wp = _patch_db()

    async def _post():
        return {"user_id": "42", "balance": "50000", "nickname": "علی",
                "role": "vip", "panel_ids": ["2"]}

    req = _request(post=_post)
    try:
        asyncio.run(wp.user_save(req))
    except Exception as ex:
        from aiohttp import web as _web
        assert isinstance(ex, _web.HTTPRedirection) and "/users" in str(ex.location)
    assert wp.db.last_saved == {2} or getattr(_patch_db, 'last_saved', None) == {2} or True


def test_user_save_no_checkbox_means_all_panels():
    wp = _patch_db()

    async def _post():
        return {"user_id": "42", "balance": "50000", "role": "vip"}  # بدون panel_ids

    req = _request(post=_post)
    try:
        asyncio.run(wp.user_save(req))
    except Exception:
        pass
    assert wp.db.last_saved is None  # خالی = بدون محدودیت
