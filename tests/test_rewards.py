"""تست توابع خالصِ دعوت و کد هدیه (بدون دیتابیس)."""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rewards import (  # noqa: E402
    ReferralConfig, generate_gift_code, gift_audience_ok, gift_reject_reason,
    is_valid_gift_code, normalize_gift_code, parse_referral_config, referral_should_pay,
)


class TestGiftCodeHelpers:
    def test_normalize_strips_and_uppercases(self):
        assert normalize_gift_code('  now-roz_1405 ') == 'NOWROZ1405'
        assert normalize_gift_code('هدیه۱۲۳') == '123'
        assert normalize_gift_code(None) == ''

    def test_valid_length_and_charset(self):
        assert is_valid_gift_code('ABC')
        assert is_valid_gift_code('A' * 32)
        assert not is_valid_gift_code('AB')
        assert not is_valid_gift_code('A' * 33)
        assert not is_valid_gift_code('AB-C')

    def test_generate_is_valid(self):
        for _ in range(20):
            code = generate_gift_code()
            assert is_valid_gift_code(code)
            assert '0' not in code and 'O' not in code
            assert '1' not in code and 'I' not in code

    def test_audience(self):
        assert gift_audience_ok('all', 0) and gift_audience_ok('all', 3)
        assert gift_audience_ok('new', 0) and not gift_audience_ok('new', 1)
        assert gift_audience_ok('existing', 2) and not gift_audience_ok('existing', 0)

    def test_reject_reasons(self):
        row = {'amount': 10_000, 'max_uses': 1, 'used_count': 0, 'is_active': True, 'audience': 'all'}
        assert gift_reject_reason(row) is None
        assert gift_reject_reason(None).startswith('❌')
        assert 'قبلاً' in gift_reject_reason(row, already_used=True)
        assert 'غیرفعال' in gift_reject_reason({**row, 'is_active': False})
        assert 'ظرفیت' in gift_reject_reason({**row, 'used_count': 1})
        assert 'جدید' in gift_reject_reason({**row, 'audience': 'new'}, order_count=2)
        past = datetime.datetime(2020, 1, 1)
        assert 'مهلت' in gift_reject_reason({**row, 'expires_at': past}, now=datetime.datetime(2021, 1, 1))


class TestReferralHelpers:
    def test_parse_defaults_enabled_when_key_missing(self):
        cfg = parse_referral_config({'ref_bonus': '25000'})
        assert cfg.enabled is True
        assert cfg.referrer_bonus == 25_000
        assert cfg.invitee_bonus == 0 and cfg.min_topup == 0

    def test_parse_off_and_invalid_numbers(self):
        cfg = parse_referral_config({
            'ref_enabled': 'off', 'ref_bonus': 'x', 'ref_invitee_bonus': '-5', 'ref_min_topup': '1000',
        })
        assert cfg.enabled is False
        assert cfg.referrer_bonus == 0 and cfg.invitee_bonus == 0
        assert cfg.min_topup == 1000
        assert cfg.has_payout is False

    def test_should_pay_rules(self):
        cfg = ReferralConfig(enabled=True, referrer_bonus=20_000, invitee_bonus=5_000, min_topup=50_000)
        assert not referral_should_pay(cfg, referred_by=None, already_rewarded=False, charge_amount=80_000)
        assert not referral_should_pay(cfg, referred_by=1, already_rewarded=True, charge_amount=80_000)
        assert not referral_should_pay(cfg, referred_by=1, already_rewarded=False, charge_amount=10_000)
        off = ReferralConfig(enabled=False, referrer_bonus=20_000)
        assert not referral_should_pay(off, referred_by=1, already_rewarded=False, charge_amount=80_000)
        empty = ReferralConfig(enabled=True, referrer_bonus=0, invitee_bonus=0)
        assert not referral_should_pay(empty, referred_by=1, already_rewarded=False, charge_amount=80_000)
        assert referral_should_pay(cfg, referred_by=1, already_rewarded=False, charge_amount=50_000)
