# OverWallVpn Telegram Bot

رباتِ تلگرامیِ فروش کانفیگ VPN از روی پنل X-UI با پایگاه‌داده‌ی PostgreSQL.

## امکانات
- خرید تکی و خرید عمده‌ی کانفیگ (عمده فقط برای کاربران VIP و ادمین)
- کیف پول، شارژ حساب با ارسال رسید و تایید ادمین
- مدیریت سفارش‌ها: مشاهده‌ی وضعیت/مصرف، تغییر وضعیت، تغییر UUID، تغییر نام، تمدید (ریست کامل حجم و زمان مطابق پلن)
- پنل مدیریت: مدیریت پلن‌ها، قیمت اختصاصی کاربر، VIP، ادمین‌ها، پیام همگانی، ایمپورت کانفیگ

## پیش‌نیازها
- Python 3.10+
- PostgreSQL
- یک پنل X-UI در دسترس

## نصب سریع روی سرور (از گیت)
دیگر نیازی به آپلود/دانلود دستی نیست. روی سرور یک‌بار کلون کن و بعد فقط با `git` به‌روزرسانی کن.

**۱) پیش‌نیازهای سیستمی (یک‌بار):**
```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git postgresql postgresql-client
```
> ⚠️ اگر `python3-venv` نصب نباشد، `deploy.sh`/`run.sh` هنگام ساخت venv خطا می‌دهند. حتماً این مرحله را انجام بده.

**۲) گرفتن کد:**
```bash
git clone https://github.com/MahdiTvK5/main.git overwallbot
cd overwallbot
# اگر هنوز همه‌چیز در شاخه‌ی main مرج نشده، شاخه‌ای که همه‌ی امکانات را دارد چک‌اوت کن:
# git checkout <نام-شاخه>
```

**۳) ساخت دیتابیس و کاربر PostgreSQL (یک‌بار):**
> ⚠️ اگر این مرحله را انجام ندهی، هنگام اجرا خطای `ConnectionRefusedError ... 127.0.0.1:5432` می‌گیری.
```bash

sudo systemctl enable --now postgresql  # مطمئن شو سرویس دیتابیس روشن است
ss -ltnp | grep 5432          # باید نشان دهد postgres روی 127.0.0.1:5432 گوش می‌دهد

# کاربر و دیتابیس را بساز (به‌جای YOUR_STRONG_PASSWORD یک رمز قوی بگذار)
sudo -u postgres psql -c "CREATE USER overwall_user WITH PASSWORD 'YOUR_STRONG_PASSWORD';" \
  || sudo -u postgres psql -c "ALTER USER overwall_user WITH PASSWORD 'YOUR_STRONG_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE overwall_db OWNER overwall_user;" || true
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE overwall_db TO overwall_user;"
```
همین رمز را باید در `.env` مقابل `DB_PASS` بگذاری. اگر نام کاربر/دیتابیس را عوض کردی، `DB_USER`/`DB_NAME` را هم هماهنگ کن.

**۴) آماده‌سازی و پر کردن تنظیمات:**
```bash
bash deploy.sh      # ساخت venv + نصب وابستگی‌ها + ساخت .env
nano .env           # مقادیر را پر کن (توکن، پنل، DB_PASS مطابق مرحله ۳، WEB_ADMIN_PASSWORD و ...)
```

**۵) اجرا:**
```bash
bash run.sh                 # اجرای دستی (foreground)
# یا اجرای دائمی به‌صورت سرویس (پیشنهادی):
bash install_service.sh     # نصب سرویس systemd (خودکار با ریبوت/کرش بالا می‌آید)
```

**به‌روزرسانی بعدی (فقط همین):**
```bash
cd overwallbot && bash deploy.sh   # کد جدید را می‌گیرد و سرویس را ری‌استارت می‌کند
```
دستورهای سرویس: `systemctl status overwallbot` · `journalctl -u overwallbot -f` · `systemctl restart overwallbot`

## نصب دستی (جایگزین)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## پیکربندی
فایل `.env.example` را به `.env` کپی کنید و مقادیر را پر کنید:
```bash
cp .env.example .env
```
متغیرها:

| متغیر | توضیح |
|-------|-------|
| `BOT_TOKEN` | توکن ربات تلگرام |
| `SUPER_ADMIN_ID` | آیدی عددی مدیر کل |
| `PROXY_URL` | پروکسی اتصال به تلگرام (اختیاری) |
| `PANEL_URL` | آدرس کامل پنل X-UI |
| `PANEL_USER` / `PANEL_PASS` | یوزر/پسورد پنل |
| `CONFIG_IP` | آی‌پی/دامنه‌ی استفاده‌شده در لینک کانفیگ |
| `DB_USER` / `DB_PASS` / `DB_NAME` / `DB_HOST` / `DB_PORT` | تنظیمات دیتابیس (`DB_PASS` اجباری است) |
| `WEB_ADMIN_PASSWORD` | رمز پنل وب؛ خالی یعنی پنل وب اجرا نشود |
| `WEB_HOST` / `WEB_PORT` | نشانی پنل وب (پیش‌فرض `127.0.0.1:8080`) |
| `WEB_SESSION_TTL` | عمر نشست پنل وب به ثانیه (پیش‌فرض ۱۲ ساعت) |

> ⚠️ رمز دیتابیس قبلاً در سورس هاردکد بود و در تاریخچه‌ی گیت لو رفته است؛ حتماً آن را تغییر دهید. اگر `DB_PASS` تنظیم نشود، ربات با پیام واضح بالا نمی‌آید.

> ℹ️ شماره کارت دیگر مقدار پیش‌فرض ندارد و باید از «مدیریت ⚙️ ← کارت 💳» یا پنل وب تنظیم شود؛ تا آن زمان دکمه‌ی شارژ حساب به کاربر پیام می‌دهد.

## پنل وب مدیریت
اگر `WEB_ADMIN_PASSWORD` را در `.env` تنظیم کنید، یک پنل وب مدیریت در کنار ربات روی `WEB_HOST:WEB_PORT` اجرا می‌شود:
- صفحات: داشبورد آمار، کاربران (با جزئیات و تنظیم موجودی)، سفارش‌ها، تراکنش‌ها، پلن‌ها، پنل‌ها، کدهای هدیه، پیام همگانی، VIP و ادمین، تنظیمات و دانلود بکاپ.
- **مدیریت کامل (افزودن/ویرایش/حذف):** کاربران، پلن‌ها و پنل‌ها. در فرم پلن، **انتخاب پنل** وجود دارد تا بتوانید پلن جدید را روی هر پنلی (از جمله پنل تازه‌ساخته‌شده) بسازید.

### امنیت پنل وب
- نشست‌ها تصادفی و دارای انقضا هستند و «خروج» آن‌ها را سمت سرور هم باطل می‌کند.
- بعد از ۵ تلاش ناموفق ورود، همان IP به مدت ۵ دقیقه قفل می‌شود.
- پیش‌فرض `WEB_HOST` روی `127.0.0.1` است چون پنل روی HTTP ساده کار می‌کند و نباید مستقیم روی اینترنت باز باشد.

**دسترسی از بیرون (پیشنهادی): Nginx + گواهی TLS**
```nginx
server {
    listen 443 ssl;
    server_name panel.example.com;
    ssl_certificate     /etc/letsencrypt/live/panel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/panel.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;   # برای درست کار کردن قفل ورود
        proxy_set_header X-Forwarded-Proto $scheme;        # تا کوکی با فلگ Secure ست شود
    }
}
```
```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo certbot --nginx -d panel.example.com
```
اگر ناچارید بدون Nginx پنل را باز کنید، `WEB_HOST=0.0.0.0` بگذارید و پورت را با فایروال به IP خودتان محدود کنید:
```bash
sudo ufw allow from <YOUR_IP> to any port 8080 proto tcp
```

## بکاپ دیتابیس
قابلیت بکاپ خودکار/دستی از «مدیریت ⚙️ ← 💾 بکاپ دیتابیس» در دسترس است و فایل `.sql` برای ادمین‌ها ارسال می‌شود. این قابلیت به ابزار `pg_dump` نیاز دارد:
```bash
sudo apt install postgresql-client
```

## اجرا
```bash
python main.py
```
جدول‌های دیتابیس در اولین اجرا به‌صورت خودکار ساخته می‌شوند.

## چند پنل (Multi-Panel)
ربات از چند پنل X-UI پشتیبانی می‌کند:
- مقادیر `PANEL_*` و `CONFIG_IP` در `.env` فقط برای ساختِ «پنل پیش‌فرض» در اولین اجرا استفاده می‌شوند. پلن‌ها و سفارش‌های قدیمی به‌طور خودکار به همین پنل نسبت داده می‌شوند.
- از مسیر «مدیریت ⚙️ ← مدیریت پنل‌ها 🖥» می‌توانید پنل جدید اضافه/حذف کنید (نام، URL، یوزر، پسورد، و IP/دامنه‌ی لینک).
- هنگام ساخت پلن، اگر بیش از یک پنل وجود داشته باشد، پنل مقصد پرسیده می‌شود؛ هر سفارش به پنل خودش متصل می‌ماند و وضعیت/تمدید/تغییرات روی همان پنل اعمال می‌شود.
- اگر برای پنل «لینک ساب (Sub URL)» تنظیم شده باشد، به‌جای کانفیگ خام، **لینک اشتراک** به کاربر داده می‌شود که با تغییر UUID همچنان معتبر می‌ماند.

## پروتکل‌های پشتیبانی‌شده
لینک اشتراک از روی تنظیمات واقعیِ همان اینباند ساخته می‌شود، پس VLESS، VMess و Trojan با ترنسپورت‌های ws/grpc/tcp/xhttp و امنیت tls/reality/none پشتیبانی می‌شوند.

نام سرویس (email) و اینباندِ هر سفارش در دیتابیس ذخیره می‌شود و از متن لینک حدس زده نمی‌شود؛ این برای VMess ضروری است چون لینک آن فرگمنت `#` ندارد و همه‌چیز داخل Base64 است.

## ساختار ماژول‌ها
- `config.py` — بارگذاری `.env`، ثابت‌ها و توابع کمکی (`format_size`, `clean_num`).
- `links.py` — توابع خالصِ لینک: استخراج نام سرویس و شناسه‌ی کلاینت متناسب با پروتکل.
- `db.py` — استخر اتصال PostgreSQL و تمام توابع دیتابیس (کاربر، موجودی، سفارش، تنظیمات، ادمین).
- `panel.py` — کلاس `AsyncXuiAPI` برای ارتباط با پنل X-UI و ساخت لینک اشتراک.
- `keyboards.py` — کیبوردهای منو و لیست سفارش‌ها.
- `main.py` — هندلرهای تلگرام و نقطه‌ی شروع برنامه.
- `webpanel.py` — پنل وب مدیریت (aiohttp).
- `tests/` — تست‌های واحد توابع خالص (`python -m pytest tests -q`).

## تست
```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest tests -q
```
