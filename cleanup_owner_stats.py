# -*- coding: utf-8 -*-
"""پاک‌سازی یک‌بارِ مصرف داده‌های fake مدیر کل (SUPER_ADMIN_ID).

قبل از هر تغییر، ردیف‌های تغییرپذیر در جدول‌های `_backup_owner_*` بکاپ می‌شوند.
اجرای دوباره بی‌خطر است: اگر بکاپ‌ها وجود داشته باشند، بدون تغییر خارج می‌شود.

اقدام‌ها:
  1. بکاپ فوری از ردیف‌های مدیر کل → `_backup_owner_sales` / `_backup_owner_transactions` / `_backup_owner_orders`
  2. موجودی مدیر کل → ۰
  3. حذف فروش‌های ثبت‌شده‌ی مدیر کل از جدول sales (آمار درآمد واقعی شود)
  4. صفر کردن قیمت سفارش‌های خود مدیر کل (لیست سفارش‌ها و CSV واقعی شوند)
  5. حذف تراکنش‌های شخصی مدیر کل (شارژ حساب، برگشت وجه، خرید، تمدید)

اجرا:  python cleanup_owner_stats.py   (در مسیر پروژه، با همان venv ربات)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncpg

import config

BACKUPS = ("_backup_owner_sales", "_backup_owner_transactions", "_backup_owner_orders")


async def main():
    uid = config.SUPER_ADMIN_ID
    if not uid:
        print("❌ SUPER_ADMIN_ID در .env تنظیم نشده است.")
        return
    conn = await asyncpg.connect(
        user=config.DB_USER, password=config.DB_PASS, database=config.DB_NAME,
        host=config.DB_HOST, port=config.DB_PORT, timeout=10)

    existing = {r["t"] for r in await conn.fetch(
        "SELECT c.relname AS t FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relname=ANY($1)", list(BACKUPS))}

    if existing:
        print("بکاپ‌هایی از قبل وجود دارند: " + ", ".join(sorted(existing)))
        print("به‌نظر می‌رسد این پاک‌سازی قبلاً انجام شده؛ بدون تغییر خارج شد.")
        await conn.close()
        return

    for name, table in (("_backup_owner_sales", "sales"),
                        ("_backup_owner_transactions", "transactions"),
                        ("_backup_owner_orders", "orders")):
        await conn.execute(f"CREATE TABLE {name} AS SELECT * FROM {table} WHERE user_id = {uid}")
        print(f"بکاپ: {name} ساخته شد")

    before = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", uid)
    st_sales = await conn.execute("DELETE FROM sales WHERE user_id=$1", uid)
    st_tx = await conn.execute("DELETE FROM transactions WHERE user_id=$1", uid)
    await conn.execute("UPDATE users SET balance=0 WHERE user_id=$1", uid)
    st_orders = await conn.execute(
        "UPDATE orders SET price=0, base_price=0, discount=0 WHERE user_id=$1", uid)

    after = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", uid)
    totals = await conn.fetchval("SELECT COALESCE(SUM(price),0) FROM sales")
    left = await conn.fetchrow(
        "SELECT COALESCE(SUM(discount),0) AS d, COUNT(*) AS n FROM sales WHERE user_id=$1", uid)
    print(f"موجودی: {before} → {after}")
    print(f"فروش‌های حذف‌شده: {st_sales}")
    print(f"تراکنش‌های حذف‌شده: {st_tx}")
    print(f"سفارش‌های صفر شده: {st_orders}")
    print(f"جمع درآمد واقعی جدید (sales): {totals}")
    print(f"ردیف باقی‌مانده‌ی مدیر کل در sales: n={left['n']}")
    await conn.close()

asyncio.run(main())
