"""تست یکپارچه‌ی توکن‌های پورتال اشتراک روی PostgreSQL واقعی.

بدون دیتابیس تست به‌صورت خودکار skip می‌شود (مثل test_pricing_db.py).
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("DB_NAME", os.getenv("TEST_DB_NAME", "overwall_test"))
os.environ.setdefault("DB_USER", os.getenv("TEST_DB_USER", "overwall_user"))
os.environ.setdefault("DB_PASS", os.getenv("TEST_DB_PASS", "testpass"))

import db  # noqa: E402

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
    import asyncpg
    from config import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
    conn = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    if db.db_pool:
        await db.db_pool.close()
        db.db_pool = None
    await db.init_db()


async def _mk_order(user_id, email):
    return await db.add_order(
        user_id, f"vless://uuid-{email}@host:443?type=ws#{email}",
        panel_id=1, email=email, inbound_id=1, plan_id=None,
        purchase_kind='buy', user_role='normal',
    )


class TestSubTokens:
    def test_add_order_creates_unique_token(self):
        async def scenario():
            await fresh_db()
            oid1 = await _mk_order(101, "101_a")
            oid2 = await _mk_order(102, "102_b")
            token1 = await db.ensure_sub_token(oid1)
            token2 = await db.ensure_sub_token(oid2)
            assert token1 and token2 and token1 != token2
            # ensure تکراری همان توکن را برمی‌گرداند (idempotent)
            assert await db.ensure_sub_token(oid1) == token1
            # توکن سفارشِ ناموجود → None
            assert await db.ensure_sub_token(999999) is None
        run(scenario())

    def test_get_order_by_token_and_idor_protection(self):
        async def scenario():
            await fresh_db()
            oid_a = await _mk_order(201, "201_a")
            oid_b = await _mk_order(202, "202_b")
            tok_a = await db.ensure_sub_token(oid_a)
            tok_b = await db.ensure_sub_token(oid_b)

            row = await db.get_order_by_token(tok_a)
            assert row and int(row['id']) == int(oid_a)
            assert row['email'] == "201_a"
            # توکنِ A هرگز سفارشِ B را برنمی‌گرداند (ضد IDOR)
            row_b = await db.get_order_by_token(tok_b)
            assert int(row_b['id']) == int(oid_b) and int(row_b['id']) != int(row['id'])
            # توکن‌های نامعتبر/دستکاری‌شده → بدون هیچ داده‌ای
            assert await db.get_order_by_token("invalid-token") is None
            assert await db.get_order_by_token(tok_a + "x") is None
            assert await db.get_order_by_token("") is None
            assert await db.get_order_by_token(None) is None
            assert await db.get_order_by_token("a" * 500) is None
        run(scenario())

    def test_migration_backfills_existing_orders(self):
        async def scenario():
            await fresh_db()
            oid = await _mk_order(301, "301_a")
            # شبیه‌سازی سفارش قدیمی بدون توکن
            async with db.db_pool.acquire() as conn:
                await conn.execute("UPDATE orders SET sub_token = NULL WHERE id = $1", int(oid))
                await db.init_db()  # اجرای دوباره‌ی مهاجرت‌ها → backfill
                token = await conn.fetchval("SELECT sub_token FROM orders WHERE id = $1", int(oid))
            assert token and await db.get_order_by_token(token)
        run(scenario())

    def test_migration_adds_sub_token_column(self):
        async def scenario():
            await fresh_db()
            async with db.db_pool.acquire() as conn:
                cols = {r['column_name'] for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'orders'")}
                assert {'sub_token'} <= cols
                idx = await conn.fetchval(
                    "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_orders_sub_token'")
                assert idx
        run(scenario())


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
