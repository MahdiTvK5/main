"""تست‌های «پنل‌های مجاز کاربر» (allowed_panels) — بدون دیتابیس واقعی.

هر کاربر می‌تواند به زیرمجموعه‌ای از پنل‌ها محدود شود؛ خالی/NULL یعنی همه‌ی پنل‌ها
(رفتار دیفالت فعلی). فیلتر در list_plans و گارد خرید اعمال می‌شود.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "test:token")

import db


def test_parse_allowed_panels_empty_and_invalid():
    # خالی/None = همه‌ی پنل‌ها (None)
    assert db is not None  # فقط برای import پذیر بودن ماژول
    from db import _parse_allowed_panels
    assert _parse_allowed_panels(None) is None
    assert _parse_allowed_panels('') is None
    assert _parse_allowed_panels('  ') is None
    # مقدار نامعتبر هم به «همه» برمی‌گردد تا کاربر قفل نشود
    assert _parse_allowed_panels('abc,,') is None
    # لیست معتبر
    assert _parse_allowed_panels('1, 3,7') == {1, 3, 7}
    assert _parse_allowed_panels('2') == {2}
    # موارد غیرعددی داخل لیست نادیده گرفته می‌شوند
    assert _parse_allowed_panels('1,x,3') == {1, 3}


def test_set_user_allowed_panels_value_formatting():
    from db import _allowed_panels_value
    assert _allowed_panels_value(None) == ''
    assert _allowed_panels_value([]) == ''
    assert _allowed_panels_value({3, 1}) == '1,3'
    assert _allowed_panels_value([7]) == '7'
