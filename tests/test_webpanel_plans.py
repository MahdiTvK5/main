"""تست دودی صفحه‌ی «ویرایش پلن» پنل وب بدون دیتابیس واقعی.

رندر فرم پلن باید ساختار تب‌ها را درست تولید کند: رادیوهای تب باید «برادرِ مستقیم»
نوار .tabs و بدنه‌های tab-body باشند تا سلکتورهای ~ در CSS کار کنند؛ در غیر این
صورت همه‌ی بدنه‌های فرم مخفی می‌شدند (فرم پلن خالی رندر می‌شد).
"""
import os
import sys
import types
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("WEB_ADMIN_PASSWORD", "test-pass")


def _fake_plan(pid=7):
    return {
        "id": pid, "name": "پلن تست", "icon": "🟦", "gb": 10,
        "duration_days": 30, "price": 100000, "vip_price": 95000,
        "reseller_price": 80000, "first_price": 79000, "vip_bulk_price": 90000,
        "inbound_id": 1, "panel_id": 1, "is_active": True,
        "renew_normal_price": 95000, "renew_vip_price": 90000,
        "renew_reseller_price": 85000, "sort_order": None,
    }


def _patch_db(monkey_like_plan):
    """ماژول db داخل webpanel با توکن‌های جعلی جایگزین می‌شود."""
    import db as real_db  # noqa: F401  (فقط برای اطمینان از import پذیر بودن)
    import webpanel

    class _DB:
        async def get_plan(self, pid):
            return monkey_like_plan if pid == 7 else None

        async def get_panels(self):
            return [{"id": 1, "name": "پنل پیش‌فرض", "url": "http://x"}]

    webpanel.db = _DB()
    return webpanel


def test_plan_edit_form_structure():
    import webpanel
    fake = types.SimpleNamespace(query={"id": "7"}, remote="127.0.0.1",
                                 cookies={"session": webpanel._new_session()})
    wp = _patch_db(_fake_plan(7))
    resp = asyncio.run(wp.plan_edit_form(fake))
    html = resp.text

    # ۱) رادیوهای تب قبل از .tabs و بیرون از آن (برادرِ مستقیم بدنه‌ها)
    pos_radio = html.find("id='tab-basic'")
    pos_tabs = html.find("<div class='tabs'>")
    assert pos_radio != -1 and pos_tabs != -1 and pos_radio < pos_tabs, \
        "رادیوهای تب باید بیرون و قبل از div.tabs باشند"

    # ۲) بدنه‌ی تب پیش‌فرض نمایان با سلکتور مستقیم (~ .tb-basic)
    assert "#tab-basic:checked ~ .tb-basic" in wp.PAGE_CSS

    # ۳) دکمه ذخیره باید داخل <form action='/plans/save'> باشد
    f_start = html.find("action='/plans/save'")
    f_end = html.find("</form>", f_start)
    btn = html.find("type='submit'", f_start)
    assert f_start != -1 and f_end != -1 and f_start < btn < f_end, \
        "دکمه‌ی ذخیره باید داخل فرم /plans/save باشد"

    # ۴) مقدار فعلی فیلدها باید از ردیف پلن پر شده باشد
    assert "value='پلن تست'" in html
    assert "value='100000'" in html


def test_plan_edit_form_new_plan():
    """حالت افزودن پلن جدید هم نباید خطا بدهد و hidden_id نداشته باشد."""
    import webpanel
    fake = types.SimpleNamespace(query={}, remote="127.0.0.1",
                                 cookies={"session": webpanel._new_session()})
    wp = _patch_db(_fake_plan())
    resp = asyncio.run(wp.plan_edit_form(fake))
    html = resp.text
    assert "action='/plans/save'" in html
    assert "<input type='hidden' name='id'" not in html


if __name__ == "__main__":
    test_plan_edit_form_structure()
    print("OK: structure")
    test_plan_edit_form_new_plan()
    print("OK: new plan")
