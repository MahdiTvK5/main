import os
import math
import re
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

_LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(format=_LOG_FORMAT, level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# لاگ روی فایل با چرخش خودکار (نگهداری تاریخچه‌ی فعالیت‌ها و خطاها)
try:
    _file_handler = RotatingFileHandler(os.path.join(BASE_DIR, "bot.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    _file_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(_file_handler)
except Exception:
    pass

# ================= تنظیمات ثابت =================
TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")

DB_USER = os.getenv("DB_USER", "overwall_user")
DB_PASS = os.getenv("DB_PASS", "OverWall@12345")
DB_NAME = os.getenv("DB_NAME", "overwall_db")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5432"))

try:
    SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", 0))
except ValueError:
    SUPER_ADMIN_ID = 0

PANEL_URL = os.getenv("PANEL_URL")
PANEL_USER = os.getenv("PANEL_USER")
PANEL_PASS = os.getenv("PANEL_PASS")
CONFIG_IP = os.getenv("CONFIG_IP")

SECURITY_WARNING = "\n\n⚠️ **هشدار امنیتی بسیار مهم:**\nلطفاً از ارسال لینک در پیام‌رسان‌های داخلی جداً خودداری کنید."


def format_size(size_bytes):
    if size_bytes <= 0:
        return "0 MB"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    return f"{round(size_bytes / p, 2)} {size_name[i]}"


def clean_num(text):
    return int(re.sub(r'\D', '', str(text)))
