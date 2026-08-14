"""تست موتور قیمت‌گذاری با همان اعداد جدول‌های مصوب.

اجرا: python -m pytest tests -q   (نیاز به دیتابیس یا شبکه ندارد)
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricing import (  # noqa: E402
    KIND_BULK, KIND_RENEW, NORMAL, RESELLER, VIP,
    Buyer, PricingConfig, apply_offer, base_price_for, button_text, can_buy_bulk,
    normalize_role, offer_matches, reseller_bonus_for, reseller_tier, resolve_quote,
)

# جدول مصوب: (حجم، روز، همکاری، VIP، عادی، تمدید، خرید اول)
PRICE_TABLE = [
    (10, 30, 80_000, 95_000, 110_000, 95_000, 79_000),
    (20, 30, 110_000, 130_000, 150_000, 130_000, 99_000),
    (30, 30, 140_000, 165_000, 190_000, 169_000, 129_000),
    (40, 30, 170_000, 195_000, 220_000, 199_000, 159_000),
    (50, 30, 200_000, 230_000, 260_000, 239_000, 189_000),
    (80, 60, 290_000, 325_000, 360_000, 329_000, 279_000),
    (120, 90, 400_000, 450_000, 500_000, 459_000, 389_000),
]


def plan_of(gb, days, reseller, vip, normal, renew, first, **extra):
    plan = {
        'id': gb, 'name': f'{gb}GB - {days} روزه', 'gb': gb, 'duration_days': days,
        'price': normal, 'vip_price': vip, 'reseller_price': reseller,
        'renew_price': renew, 'first_price': first, 'vip_bulk_price': reseller,
        'icon': '🟦', 'is_active': True,
    }
    plan.update(extra)
    return plan


PLANS = [plan_of(*row) for row in PRICE_TABLE]


def new_buyer(role=NORMAL, **kw):
    return Buyer(user_id=1, role=role, **kw)


def returning_buyer(role=NORMAL, **kw):
    kw.setdefault('order_count', 3)
    kw.setdefault('first_offer_used', True)
    return Buyer(user_id=2, role=role, **kw)


class TestRoles:
    def test_unknown_role_is_normal(self):
        assert normalize_role(None) == NORMAL
        assert normalize_role('') == NORMAL
        assert normalize_role('Bogus') == NORMAL
        assert normalize_role('RESELLER') == RESELLER

    def test_bulk_access(self):
        assert can_buy_bulk(NORMAL) is False
        assert can_buy_bulk(VIP) is True
        assert can_buy_bulk(RESELLER) is True
        assert can_buy_bulk(NORMAL, admin_status=True) is True


class TestBasePrices:
    def test_every_row_of_the_price_table(self):
        for (gb, _d, reseller, vip, normal, _r, _f), plan in zip(PRICE_TABLE, PLANS):
            assert base_price_for(plan, NORMAL) == normal, gb
            assert base_price_for(plan, VIP) == vip, gb
            assert base_price_for(plan, RESELLER) == reseller, gb

    def test_missing_tier_falls_back_upwards(self):
        plan = plan_of(10, 30, 0, 0, 110_000, 0, 0)
        assert base_price_for(plan, VIP) == 110_000
        assert base_price_for(plan, RESELLER) == 110_000

    def test_custom_price_wins(self):
        assert base_price_for(PLANS[0], NORMAL, custom_price=50_000) == 50_000


class TestRenewal:
    def test_renewal_uses_the_renew_column(self):
        for (gb, _d, _res, _vip, normal, renew, _f), plan in zip(PRICE_TABLE, PLANS):
            q = resolve_quote(plan, returning_buyer(), kind=KIND_RENEW)
            assert q.price == renew, gb
            # قیمت اصلی برای کاربر قابل نمایش می‌ماند تا سود تمدید دیده شود
            assert q.base_price == normal
            assert q.label == '♻️ قیمت ویژه‌ی تمدید'

    def test_renewal_never_costs_more_than_the_user_tier(self):
        # نماینده‌ی ۱۰ گیگ: قیمت همکاری ۸۰٬۰۰۰ از قیمت تمدید ۹۵٬۰۰۰ کمتر است
        q = resolve_quote(PLANS[0], returning_buyer(RESELLER), kind=KIND_RENEW)
        assert q.price == 80_000
        assert q.has_discount is False

    def test_renewal_can_be_disabled(self):
        cfg = PricingConfig(renew_offer_enabled=False)
        assert resolve_quote(PLANS[0], returning_buyer(), kind=KIND_RENEW, config=cfg).price == 110_000

    def test_base_price_ignores_renewal_column(self):
        assert base_price_for(PLANS[0], NORMAL, KIND_RENEW) == 110_000


class TestFirstPurchaseOffer:
    def test_new_customer_gets_the_offer(self):
        for (gb, _d, _res, _vip, normal, _r, first), plan in zip(PRICE_TABLE, PLANS):
            q = resolve_quote(plan, new_buyer())
            assert q.price == first, gb
            assert q.base_price == normal
            assert q.first_offer is True
            assert q.discount == normal - first

    def test_returning_customer_pays_full_price(self):
        q = resolve_quote(PLANS[2], returning_buyer())
        assert q.price == 190_000
        assert q.first_offer is False
        assert q.has_discount is False

    def test_offer_blocked_once_flag_is_set(self):
        buyer = Buyer(user_id=3, order_count=0, first_offer_used=True)
        assert resolve_quote(PLANS[2], buyer).price == 190_000

    def test_offer_blocked_when_user_already_has_orders(self):
        buyer = Buyer(user_id=4, order_count=1, first_offer_used=False)
        assert resolve_quote(PLANS[2], buyer).price == 190_000

    def test_offer_can_be_disabled_globally(self):
        cfg = PricingConfig(first_offer_enabled=False)
        assert resolve_quote(PLANS[2], new_buyer(), config=cfg).price == 190_000

    def test_offer_does_not_apply_to_renewal(self):
        assert resolve_quote(PLANS[2], new_buyer(), kind=KIND_RENEW).price == 169_000

    def test_offer_never_raises_the_price(self):
        # نماینده‌ی تازه: قیمت همکاری ۱۴۰٬۰۰۰ از آفر ۱۲۹٬۰۰۰ بیشتر است، پس آفر برنده است
        q = resolve_quote(PLANS[2], new_buyer(RESELLER))
        assert q.price == 129_000
        # ولی روی پلنی که همکاری‌اش ارزان‌تر از آفر است، قیمت همکاری می‌ماند
        plan = plan_of(10, 30, 50_000, 95_000, 110_000, 95_000, 79_000)
        assert resolve_quote(plan, new_buyer(RESELLER)).price == 50_000


class TestResellerTiers:
    def test_tier_thresholds(self):
        cfg = PricingConfig()
        assert reseller_tier(0, cfg)[0] == 1
        assert reseller_tier(999_999, cfg)[0] == 1
        assert reseller_tier(1_000_000, cfg)[:2] == (2, 5)
        assert reseller_tier(2_900_000, cfg)[:2] == (2, 5)
        assert reseller_tier(3_000_000, cfg)[:2] == (3, 8)

    def test_tier_discount_applies_on_top_of_reseller_price(self):
        buyer = returning_buyer(RESELLER, monthly_topup=1_500_000)
        q = resolve_quote(PLANS[2], buyer)          # همکاری ۱۴۰٬۰۰۰ منهای ۵٪
        assert q.price == 133_000
        assert q.tier_discount == 5

    def test_vip_reseller_tier(self):
        buyer = returning_buyer(RESELLER, monthly_topup=5_000_000)
        q = resolve_quote(PLANS[2], buyer)          # ۱۴۰٬۰۰۰ منهای ۸٪
        assert q.price == 128_800

    def test_tiers_are_configurable(self):
        cfg = PricingConfig(reseller_t3_min=2_000_000, reseller_t3_discount=10)
        buyer = returning_buyer(RESELLER, monthly_topup=2_000_000)
        assert resolve_quote(PLANS[2], buyer, config=cfg).price == 126_000

    def test_normal_user_never_gets_reseller_pricing(self):
        buyer = returning_buyer(NORMAL, monthly_topup=9_000_000)
        q = resolve_quote(PLANS[2], buyer)
        assert q.price == 190_000
        assert q.tier_discount == 0

    def test_tier_does_not_stack_on_the_first_purchase_offer(self):
        # نماینده‌ی تازه با شارژ بالا: بهترین قیمت برنده است، نه تخفیفِ روی تخفیف
        buyer = new_buyer(RESELLER, monthly_topup=5_000_000)
        q = resolve_quote(PLANS[2], buyer)
        assert q.price == min(129_000, 140_000 - 140_000 * 8 // 100)
        assert q.price == 128_800          # پله‌ی نماینده VIP، نه ۱۲۹٬۰۰۰ منهای ۸٪
        assert q.first_offer is False


class TestOffers:
    def base_offer(self, **kw):
        offer = {'id': 7, 'name': 'نوروز', 'kind': 'percent', 'value': 20, 'audience': 'all',
                 'plan_id': None, 'starts_at': None, 'ends_at': None, 'max_uses': 0,
                 'per_user_limit': 1, 'used_count': 0, 'is_active': True}
        offer.update(kw)
        return offer

    def test_percent_and_fixed(self):
        assert apply_offer(200_000, self.base_offer(value=25)) == 150_000
        assert apply_offer(200_000, self.base_offer(kind='fixed', value=99_000)) == 99_000

    def test_offer_applied_to_quote(self):
        q = resolve_quote(PLANS[2], returning_buyer(), offers=[self.base_offer()])
        assert q.price == 152_000
        assert q.offer_id == 7

    def test_inactive_or_expired_offer_ignored(self):
        past = datetime.datetime.now() - datetime.timedelta(days=1)
        future = datetime.datetime.now() + datetime.timedelta(days=1)
        buyer = returning_buyer()
        assert not offer_matches(self.base_offer(is_active=False), buyer, 30)
        assert not offer_matches(self.base_offer(ends_at=past), buyer, 30)
        assert not offer_matches(self.base_offer(starts_at=future), buyer, 30)
        assert offer_matches(self.base_offer(starts_at=past, ends_at=future), buyer, 30)

    def test_usage_limits(self):
        buyer = returning_buyer()
        assert not offer_matches(self.base_offer(max_uses=5, used_count=5), buyer, 30)
        buyer.offer_uses = {7: 1}
        assert not offer_matches(self.base_offer(per_user_limit=1), buyer, 30)
        assert offer_matches(self.base_offer(per_user_limit=2), buyer, 30)

    def test_audience_filter(self):
        assert offer_matches(self.base_offer(audience='new'), new_buyer(), 30)
        assert not offer_matches(self.base_offer(audience='new'), returning_buyer(), 30)
        assert offer_matches(self.base_offer(audience='existing'), returning_buyer(), 30)
        assert offer_matches(self.base_offer(audience='reseller'), returning_buyer(RESELLER), 30)
        assert not offer_matches(self.base_offer(audience='reseller'), returning_buyer(VIP), 30)
        assert offer_matches(self.base_offer(audience='vip'), returning_buyer(VIP), 30)

    def test_plan_scoped_offer(self):
        buyer = returning_buyer()
        assert offer_matches(self.base_offer(plan_id=30), buyer, 30)
        assert not offer_matches(self.base_offer(plan_id=10), buyer, 30)

    def test_best_price_wins(self):
        offers = [self.base_offer(id=1, value=10), self.base_offer(id=2, value=40)]
        q = resolve_quote(PLANS[2], returning_buyer(), offers=offers)
        assert q.price == 114_000
        assert q.offer_id == 2


class TestBulk:
    def test_bulk_uses_bulk_column(self):
        q = resolve_quote(PLANS[2], returning_buyer(RESELLER), kind=KIND_BULK)
        assert q.price == 140_000

    def test_bulk_ignores_first_purchase_offer(self):
        q = resolve_quote(PLANS[2], new_buyer(VIP), kind=KIND_BULK)
        assert q.first_offer is False


class TestBonus:
    def test_bonus_only_for_resellers_above_threshold(self):
        cfg = PricingConfig()
        assert reseller_bonus_for(1_000_000, returning_buyer(RESELLER), cfg) == 100_000
        assert reseller_bonus_for(999_999, returning_buyer(RESELLER), cfg) == 0
        assert reseller_bonus_for(1_000_000, returning_buyer(VIP), cfg) == 0

    def test_bonus_only_once(self):
        cfg = PricingConfig()
        buyer = returning_buyer(RESELLER, reseller_bonus_used=True)
        assert reseller_bonus_for(2_000_000, buyer, cfg) == 0

    def test_bonus_is_configurable(self):
        cfg = PricingConfig(reseller_bonus_percent=15, reseller_bonus_min=500_000)
        assert reseller_bonus_for(600_000, returning_buyer(RESELLER), cfg) == 90_000

    def test_bonus_can_be_disabled(self):
        cfg = PricingConfig(reseller_bonus_enabled=False)
        assert reseller_bonus_for(2_000_000, returning_buyer(RESELLER), cfg) == 0


class TestDisplay:
    def test_button_marks_discounted_plans(self):
        q = resolve_quote(PLANS[2], new_buyer())
        assert '🔥' in button_text(PLANS[2], q)
        q2 = resolve_quote(PLANS[2], returning_buyer())
        assert '🔥' not in button_text(PLANS[2], q2)

    def test_discount_percent(self):
        q = resolve_quote(PLANS[2], new_buyer())
        assert q.discount_percent == 32   # 190,000 -> 129,000

    def test_zero_base_price_is_safe(self):
        plan = plan_of(5, 30, 0, 0, 0, 0, 0)
        q = resolve_quote(plan, new_buyer())
        assert q.price == 0 and q.discount_percent == 0


class TestPriceIsFrozen:
    def test_quote_is_a_snapshot(self):
        plan = dict(PLANS[2])
        q = resolve_quote(plan, returning_buyer())
        plan['price'] = 999_999          # ادمین بعداً قیمت را عوض می‌کند
        assert q.price == 190_000        # نقلِ قیمتِ ثبت‌شده تغییر نمی‌کند
