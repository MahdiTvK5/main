"""پیکربندی مشترک تست‌ها.

`config.py` متغیرهای محیطی را در لحظه‌ی import می‌خواند، پس مقادیر تست باید پیش از
import شدن هر ماژولی تنظیم شوند؛ conftest زودتر از خود تست‌ها بارگذاری می‌شود.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("DB_NAME", os.getenv("TEST_DB_NAME", "overwall_test"))
os.environ.setdefault("DB_USER", os.getenv("TEST_DB_USER", "overwall_user"))
os.environ.setdefault("DB_PASS", os.getenv("TEST_DB_PASS", ""))
