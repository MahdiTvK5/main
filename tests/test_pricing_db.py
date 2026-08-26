"""تست یکپارچه‌ی قیمت‌گذاری روی یک PostgreSQL واقعی.

این تست‌ها فقط وقتی اجرا می‌شوند که دیتابیس تست در دسترس باشد؛ در غیر این صورت skip
می‌شوند تا اجرای معمولی `pytest` روی هر ماشینی کار کند.

راه‌اندازی دیتابیس تست:
    createdb overwall_test
    TEST_DB_NAME=overwall_test TEST_DB_USER=... TEST_DB_PASS=... python -m pytest tests -q
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# پیکربندی باید *قبل* از import ماژول‌ها انجام شود چون config در زمان import خوانده می‌شود
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("DB_NAME", os.getenv("TEST_DB_NAME", "overwall_test"))
os.environ.setdefault("DB_USER", os.getenv("TEST_DB_USER", "overwall_user"))
os.environ.setdefault("DB_PASS", os.getenv("TEST_DB_PASS", "testpass"))

import db  # noqa: E402
import pricing  # noqa: E402


# یک event loop مشترک برای همه‌ی تست‌ها؛ استخر اتصال asyncpg به loop خودش گره خورده
# است و ساختن loop تازه برای هر تست باعث خطای «Event loop is closed» در تمیزکاری می‌شود.
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def _db_available():
    async def probe():
        try:
            await db.init_db()
            return True
        except Exception:
            return False
    try:
        return run(probe())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="دیتابیس تست در دسترس نیست")


async def fresh_db():
    """دیتابیس خالی + اجرای کامل مهاجرت‌ها."""
    import asyncpg
    from config import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
    conn = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    if db.db_pool:
        await db.db_pool.close()
        db.db_pool = None
    await db.init_db()


class TestMigrations:
    def test_schema_and_defaults(self):
        async def scenario():
            await fresh_db()
            async with db.db_pool.acquire() as conn:
                cols = {r['column_name'] for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'plans'")}
                assert {'reseller_price', 'renew_price', 'first_price', 'is_active'} <= cols
                cols = {r['column_name'] for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'orders'")}
                assert {'plan_id', 'price', 'base_price', 'discount', 'purchase_kind', 'user_role'} <= cols
                cols = {r['column_name'] for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")}
                assert {'first_offer_used', 'reseller_bonus_used', 'referred_by', 'ref_rewarded'} <= cols
                cols = {r['column_name'] for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'gift_codes'")}
                assert {'expires_at', 'is_active', 'note', 'audience', 'created_at'} <= cols
                tables = {r['table_name'] for r in await conn.fetch(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
                assert {'offers', 'offer_redemptions', 'sales', 'gift_codes', 'gift_redemptions'} <= tables
                settings = {r['key'] for r in await conn.fetch("SELECT key FROM settings")}
                assert {'ref_enabled', 'ref_bonus', 'ref_invitee_bonus', 'ref_min_topup'} <= settings
        run(scenario())

    def test_default_plans_match_the_approved_table(self):
        async def scenario():
            await fresh_db()
            plans = await db.list_plans()
            assert len(plans) == 7
            first = plans[0]
            assert (first['gb'], first['duration_days']) == (10, 30)
            assert (first['reseller_price'], first['vip_price'], first['price']) == (80_000, 95_000, 110_000)
            assert (first['renew_price'], first['first_price']) == (95_000, 79_000)
            last = plans[-1]
            assert (last['gb'], last['duration_days'], last['price']) == (120, 90, 500_000)
        run(scenario())

    def test_migration_is_idempotent(self):
        async def scenario():
            await fresh_db()
            await db.init_db()          # اجرای دوباره نباید چیزی خراب کند
            await db.init_db()
            assert len(await db.list_plans()) == 7   # پلن‌ها تکراری نمی‌شوند
        run(scenario())

    def test_existing_customers_are_excluded_from_first_offer(self):
        async def scenario():
            await fresh_db()
            # کاربری که «از قبل» سفارش دارد، بعد از مهاجرت نباید آفر خرید اول بگیرد
            await db.get_user(555)
            async with db.db_pool.acquire() as conn:
                await conn.execute("INSERT INTO orders (user_id, config_link, date) VALUES (555, 'vless://x@h:1#a', '2024-01-01')")
                await conn.execute("UPDATE users SET first_offer_used = FALSE WHERE user_id = 555")
                await db._init_pricing(conn)
            profile = await db.get_pricing_profile(555)
            assert profile['first_offer_used'] is True
        run(scenario())


class TestPricingWithDb:
    def test_price_by_role(self):
        async def scenario():
            await fresh_db()
            plan = (await db.list_plans())[2]        # 30GB
            await db.get_user(101)
            assert (await pricing.quote_for_user(101, plan)).price == 129_000   # آفر خرید اول

            await db.set_user_role(102, pricing.VIP)
            async with db.db_pool.acquire() as conn:  # کاربر VIP با سابقه‌ی خرید
                await conn.execute("UPDATE users SET first_offer_used = TRUE WHERE user_id = 102")
            assert (await pricing.quote_for_user(102, plan)).price == 165_000

            await db.set_user_role(103, pricing.RESELLER)
            async with db.db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET first_offer_used = TRUE WHERE user_id = 103")
            assert (await pricing.quote_for_user(103, plan)).price == 140_000
        run(scenario())

    def test_first_offer_is_consumed_once(self):
        async def scenario():
            await fresh_db()
            plan = (await db.list_plans())[0]        # 10GB
            await db.get_user(201)
            quote = await pricing.quote_for_user(201, plan)
            assert quote.price == 79_000 and quote.first_offer

            await pricing.commit_quote(201, quote, pricing.KIND_BUY, plan['id'], role=pricing.NORMAL)

            again = await pricing.quote_for_user(201, plan)
            assert again.price == 110_000 and not again.first_offer
        run(scenario())

    def test_renewal_price(self):
        async def scenario():
            await fresh_db()
            plan = (await db.list_plans())[2]
            await db.get_user(301)
            async with db.db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET first_offer_used = TRUE WHERE user_id = 301")
            assert (await pricing.quote_for_user(301, plan, pricing.KIND_RENEW)).price == 169_000
        run(scenario())

    def test_reseller_tier_from_real_topups(self):
        async def scenario():
            await fresh_db()
            plan = (await db.list_plans())[2]
            await db.set_user_role(401, pricing.RESELLER)
            async with db.db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET first_offer_used = TRUE WHERE user_id = 401")
            assert (await pricing.quote_for_user(401, plan)).price == 140_000

            await db.credit_balance(401, 1_500_000, kind='شارژ حساب')
            assert (await pricing.quote_for_user(401, plan)).price == 133_000   # ۵٪

            await db.credit_balance(401, 2_000_000, kind='شارژ حساب')
            assert (await pricing.quote_for_user(401, plan)).price == 128_800   # ۸٪
        run(scenario())

    def test_reseller_bonus_only_once(self):
        async def scenario():
            await fresh_db()
            await db.set_user_role(501, pricing.RESELLER)
            bonus, percent = await pricing.reseller_bonus_on_topup(501, 1_000_000)
            assert (bonus, percent) == (100_000, 10)
            assert await pricing.reseller_bonus_on_topup(501, 1_000_000) == (0, 0)

            # کاربر عادی هدیه نمی‌گیرد
            await db.get_user(502)
            assert await pricing.reseller_bonus_on_topup(502, 5_000_000) == (0, 0)
        run(scenario())

    def test_offer_limits_enforced_in_db(self):
        async def scenario():
            await fresh_db()
            plan = (await db.list_plans())[2]
            offer_id = await db.save_offer(None, 'تست', 'percent', 50, 'all', None, None, None, 1, 1, True)
            await db.get_user(601)
            async with db.db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET first_offer_used = TRUE WHERE user_id = 601")
            quote = await pricing.quote_for_user(601, plan)
            assert quote.price == 95_000 and quote.offer_id == offer_id

            assert await db.redeem_offer(offer_id, 601) is True
            assert await db.redeem_offer(offer_id, 601) is False       # سقف هر کاربر
            assert await db.redeem_offer(offer_id, 602) is False       # سقف کل
            # بعد از مصرف، دیگر روی قیمت اعمال نمی‌شود
            assert (await pricing.quote_for_user(601, plan)).price == 190_000
        run(scenario())

    def test_order_price_is_frozen(self):
        async def scenario():
            await fresh_db()
            plan = (await db.list_plans())[2]
            await db.get_user(701)
            quote = await pricing.quote_for_user(701, plan)
            order_id = await db.add_order(
                701, 'vless://uuid@host:443#701_test', None, email='701_test',
                plan_id=plan['id'], price=quote.price, base_price=quote.base_price,
                discount=quote.discount, purchase_kind=pricing.KIND_BUY, user_role=pricing.NORMAL,
            )
            await db.update_plan_field(plan['id'], 'price', 999_999)   # ادمین قیمت را عوض می‌کند
            async with db.db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT price, base_price, discount FROM orders WHERE id = $1", order_id)
            assert row['price'] == 129_000 and row['base_price'] == 190_000 and row['discount'] == 61_000
        run(scenario())

    def test_inactive_plan_hidden_from_shop(self):
        async def scenario():
            await fresh_db()
            plan = (await db.list_plans())[0]
            await db.toggle_plan_active(plan['id'])
            assert len(await db.list_plans(only_active=True)) == 6
            assert len(await db.list_plans()) == 7
        run(scenario())


class TestFinancialReport:
    def test_report_aggregates_sales(self):
        async def scenario():
            await fresh_db()
            plans = await db.list_plans()
            await db.get_user(801)
            await db.set_user_role(802, pricing.VIP)
            await db.set_user_role(803, pricing.RESELLER)

            await db.record_sale(801, pricing.KIND_BUY, pricing.NORMAL, 190_000, 129_000, 61_000,
                                 plan_id=plans[2]['id'], first_offer=True)
            await db.record_sale(802, pricing.KIND_BUY, pricing.VIP, 165_000, 165_000, 0, plan_id=plans[2]['id'])
            await db.record_sale(803, pricing.KIND_RENEW, pricing.RESELLER, 140_000, 133_000, 7_000, plan_id=plans[2]['id'])
            await db.credit_balance(803, 50_000, kind='برگشت وجه')

            r = await db.get_financial_report()
            assert r['users']['normal'] >= 1 and r['users']['vip'] == 1 and r['users']['reseller'] == 1
            assert r['total'] == 129_000 + 165_000 + 133_000
            assert r['today'] == r['total'] and r['week'] == r['total'] and r['month'] == r['total']
            assert r['discount'] == 68_000
            assert r['first_offers'] == 1 and r['renewals'] == 1 and r['sales_count'] == 3
            assert r['by_role']['vip']['total'] == 165_000
            assert r['refunds'] == 50_000
            assert r['net'] == r['total'] - 50_000
            assert r['gifts'] == 0 and r['referrals'] == 0
        run(scenario())


class TestExistingFeaturesStillWork:
    """اطمینان از اینکه قابلیت‌های قبلی با تغییرات این فاز نشکسته‌اند."""

    def test_wallet_gift_and_referral_flow(self):
        async def scenario():
            await fresh_db()
            balance, _nick, role, _bulk = await db.get_user(901)
            assert (balance, role) == (0, 'normal')
            assert await db.credit_balance(901, 200_000) == 200_000
            assert await db.deduct_balance(901, 50_000) == 150_000
            assert await db.deduct_balance(901, 10_000_000) is None      # موجودی ناکافی

            await db.add_gift_code('WELCOME', 25_000, 1)
            ok, _msg = await db.redeem_gift_code(901, 'welcome')   # بدون حساسیت به حروف
            assert ok and await db.get_balance(901) == 175_000
            ok, _msg = await db.redeem_gift_code(901, 'WELCOME')
            assert not ok                                                 # فقط یک‌بار

            await db.set_referrer(902, 901)
            await db.update_setting('ref_bonus', 30_000)
            await db.update_setting('ref_invitee_bonus', 10_000)
            reward = await db.try_reward_referrer(902, 80_000)
            assert reward is not None
            assert (reward.referrer_id, reward.referrer_bonus, reward.invitee_bonus) == (901, 30_000, 10_000)
            assert await db.get_balance(901) == 205_000
            assert await db.get_balance(902) == 10_000
            assert await db.try_reward_referrer(902, 80_000) is None       # فقط یک‌بار
        run(scenario())

    def test_gift_expiry_audience_and_toggle(self):
        async def scenario():
            import datetime as dt
            await fresh_db()
            past = dt.datetime.now() - dt.timedelta(days=1)
            await db.add_gift_code('OLD', 5_000, 1, expires_at=past)
            ok, msg = await db.redeem_gift_code(910, 'OLD')
            assert not ok and 'مهلت' in msg

            await db.add_gift_code('NEWONLY', 8_000, 2, audience='new')
            # کاربر با سفارش، مخاطب «جدید» نیست
            pid = await db.add_panel('p', 'https://p.example', 'u', 'p', '1.2.3.4', '')
            await db.add_order(911, 'vless://x@1.2.3.4:443#911_x', pid, email='911_x')
            ok, msg = await db.redeem_gift_code(911, 'NEWONLY')
            assert not ok and 'جدید' in msg
            ok, _msg = await db.redeem_gift_code(912, 'NEWONLY')
            assert ok

            await db.add_gift_code('PAUSED', 3_000, 1)
            assert await db.toggle_gift_code_active('PAUSED') is False
            ok, msg = await db.redeem_gift_code(913, 'PAUSED')
            assert not ok and 'غیرفعال' in msg
        run(scenario())

    def test_referral_min_topup_and_disabled_do_not_consume(self):
        async def scenario():
            await fresh_db()
            await db.get_user(920)
            await db.get_user(921)
            assert await db.set_referrer(921, 920) is True
            assert await db.set_referrer(921, 920) is False          # تکراری
            assert await db.set_referrer(920, 920) is False          # خودمعرف
            assert await db.set_referrer(922, 999999) is False       # معرف ناموجود

            await db.update_setting('ref_bonus', 15_000)
            await db.update_setting('ref_min_topup', 50_000)
            assert await db.try_reward_referrer(921, 10_000) is None  # کمتر از حداقل
            reward = await db.try_reward_referrer(921, 50_000)
            assert reward is not None and reward.referrer_bonus == 15_000
            assert await db.get_balance(920) == 15_000

            await db.get_user(930)
            await db.get_user(931)
            await db.set_referrer(931, 930)
            await db.update_setting('ref_enabled', 'off')
            await db.update_setting('ref_bonus', 20_000)
            await db.update_setting('ref_min_topup', 0)
            assert await db.try_reward_referrer(931, 80_000) is None  # خاموش؛ پرچم نمی‌خورد
            await db.update_setting('ref_enabled', 'on')
            reward = await db.try_reward_referrer(931, 80_000)
            assert reward is not None and reward.referrer_id == 930
        run(scenario())

    def test_orders_and_panels_untouched(self):
        async def scenario():
            await fresh_db()
            pid = await db.add_panel('آلمان', 'https://p.example:2096', 'u', 'p', '1.2.3.4', '')
            oid = await db.add_order(903, 'vless://uuid@1.2.3.4:443#903_x', pid, email='903_x', inbound_id=2)
            row = await db.get_order(oid)
            assert row['email'] == '903_x' and row['panel_id'] == pid and row['inbound_id'] == 2
            assert await db.order_belongs_to(oid, 903) is True
            assert await db.order_belongs_to(oid, 904) is False
            assert len(await db.get_orders_by_user(903)) == 1
        run(scenario())

    def test_old_plan_without_new_columns_still_prices(self):
        async def scenario():
            await fresh_db()
            # پلنی شبیه نصب‌های قدیمی: فقط قیمت عادی دارد
            async with db.db_pool.acquire() as conn:
                plan_id = await conn.fetchval(
                    "INSERT INTO plans (name, gb, duration_days, price, inbound_id) VALUES ('legacy', 5, 30, 60000, 1) RETURNING id")
            plan = await db.get_plan(plan_id)
            await db.set_user_role(905, pricing.RESELLER)
            async with db.db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET first_offer_used = TRUE WHERE user_id = 905")
            # نبود قیمت همکاری/VIP نباید خطا بدهد؛ به قیمت عادی برمی‌گردد
            assert (await pricing.quote_for_user(905, plan)).price == 60_000
            assert (await pricing.quote_for_user(905, plan, pricing.KIND_RENEW)).price == 60_000
        run(scenario())
