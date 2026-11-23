"""تست‌های رندر پورتال در حالت‌های غیرعادی (بدون دیتابیس/شبکه).

هدف: تغییرهای ظاهری بعدی بدون ترس از شکستن حالت‌های منقضی/غیرفعال/پایان حجم،
نامحدود و سرویس فایلی انجام شود.
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("WEB_ADMIN_PASSWORD", "test-pass")

import subportal  # noqa: E402

TODAY = datetime.date.today()


def make_sub(**over):
    sub = {
        'token': 'tok',
        'name': 'user_1',
        'support_url': 'https://t.me/support',
        'online': True,
        'ip_limit': 0,
        'history': [{'day': TODAY, 'used': 3 * 1024**3},
                    {'day': TODAY - datetime.timedelta(days=1), 'used': 1024**3}],
        'apps': {},
        'status': {'key': 'active', 'label': 'فعال'},
        'traffic': {'used': 10, 'total': 100, 'unlimited': False, 'remaining': 90,
                    'percent': 10, 'used_text': '1 GB', 'total_text': '100 GB',
                    'remaining_text': '99 GB'},
        'expiry': {'unlimited': False, 'ms': 0, 'date_text': '2027-01-01',
                   'remain_text': '30 روز', 'remain_days': 30},
        'details': {'protocol': 'vless', 'transport': 'ws', 'security': 'tls'},
        'links': ['vless://x'],
        'raw_url': 'https://portal.example/sub/tok/raw',
        'qr_url': '/sub/tok/qr.png',
        'port': 443,
        'config_link': 'vless://x',
        'file_kind': None,
    }
    sub.update(over)
    return sub


@pytest.mark.parametrize('status,expect', [
    ('active', 'اتصال پایدار'),
    ('expired', 'اتصال منقضی'),
    ('disabled', 'سرویس غیرفعال'),
    ('exhausted', 'حجم تمام شده'),
])
def test_every_status_renders(status, expect):
    sub = make_sub(status={'key': status, 'label': status})
    doc = subportal.render_portal(sub)
    assert 'Subscription Info' in doc
    assert expect in doc
    assert doc.count('<section') >= 2


def test_unlimited_traffic_shows_infinity():
    sub = make_sub(traffic={'used': 10, 'total': 0, 'unlimited': True, 'remaining': None,
                            'percent': None, 'used_text': '1 GB', 'total_text': 'نامحدود',
                            'remaining_text': 'نامحدود'})
    doc = subportal.render_portal(sub)
    assert '&#8734;' in doc


def test_file_service_without_subscription_links():
    sub = make_sub(file_kind='openvpn', raw_url='', qr_url='', links=[],
                   config_link='https://panel.example/dl/ovpn')
    doc = subportal.render_portal(sub)
    assert 'OpenVPN' in doc
    assert 'دانلود فایل کانفیگ' in doc
    assert 'id=\'qrwrap\'' not in doc


def test_english_mode_has_no_persian_labels():
    doc = subportal.render_portal(make_sub(), 'en')
    for fa in ('وضعیت اشتراک', 'ترافیک مصرفی', 'ارتباط با پشتیبانی', 'اتصال دستگاه‌ها'):
        assert fa not in doc
    assert 'dir="ltr"' in doc
    assert 'Subscription status' in doc


def test_daily_series_marks_missing_days():
    hist = [{'day': TODAY, 'used': 500},
            {'day': TODAY - datetime.timedelta(days=5), 'used': 200}]
    series = subportal.daily_series(hist, 7)
    values = dict(series)
    assert values[TODAY] is None          # روز قبل داده ندارد → اختلاف نامعلوم
    assert len(series) == 7
    assert series[-1][0] == TODAY


def test_daily_series_uses_daily_baseline():
    hist = [{'day': TODAY, 'used': 900, 'base': 800}]
    series = dict(subportal.daily_series(hist, 2))
    assert series[TODAY] == 100


def test_daily_series_computes_delta():
    hist = [{'day': TODAY, 'used': 900},
            {'day': TODAY - datetime.timedelta(days=1), 'used': 700}]
    series = dict(subportal.daily_series(hist, 3))
    assert series[TODAY] == 200
    assert series[TODAY - datetime.timedelta(days=1)] is None


def test_chart_svg_draws_only_known_days():
    series = [(TODAY, 100), (TODAY - datetime.timedelta(days=1), None)]
    svg = subportal.chart_svg(series)
    assert svg.count('<rect') == 1
    assert '<svg' in svg and '</svg>' in svg


def test_qr_png_is_a_png():
    png = subportal.qr_png('https://portal.example/sub/tok/raw')
    assert png and png[:8] == b'\x89PNG\r\n\x1a\n'
