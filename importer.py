"""منطق مشترک ایمپورت کانفیگ (ربات + پنل وب).

یک کانفیگِ ایمپورت‌شده می‌تواند برای کاربرهای دیگر هم ایمپورت شود (چون همان
کلاینتِ واقعی روی پنل است و هر دو کاربر لینکِ مشترک دریافت می‌کنند). تنها قانون:
ایمپورتِ تکراری برای «همان کاربر» به‌طور خودکار رد می‌شود؛ برای کاربر دیگر
مجاز است و فقط در گزارش علامت می‌خورد.
"""
import logging
from dataclasses import dataclass, field

import db
from links import email_from_link, order_email
from panel import build_xui, build_share_link, build_vless_link


async def parse_import_input(text):
    """ورودی متنی مدیر: «<user_id> <email|link> <email|link> ...».

    خروجی: (target_user_id, لیستی از emailها) — در فرمت اشتباه (None, []).
    هر توکنِ لینک کامل (vless:// و…) اگر باشد، email از دل لینک استخراج می‌شود.
    """
    parts = [p for p in str(text or '').split() if p]
    if len(parts) < 2 or not parts[0].isdigit():
        return None, []
    target = int(parts[0])
    wanted = []
    seen = set()
    for tok in parts[1:]:
        tok = tok.strip()
        em = email_from_link(tok) if '://' in tok else tok
        if not em:
            continue
        em = em.strip()
        if em.lower() in seen:
            continue
        seen.add(em.lower())
        wanted.append(em)
    return target, wanted


@dataclass
class ImportReport:
    """نتیجه‌ی یک اجرای ایمپورت، برای نمایش در ربات یا پنل وب."""
    target_user: int
    imported: list = field(default_factory=list)   # (email, panel_name, plan_id, prior_owner_or_None)
    skipped_same_user: list = field(default_factory=list)
    not_found: list = field(default_factory=list)
    failed_positions: list = field(default_factory=list)  # (email, detail)
    connection_ok: bool = True

    def as_text(self):
        rep = f"✅ عملیات ایمپورت پایان یافت.\n\n📦 موفق: {len(self.imported)}\n"
        per_panel = {}
        for _email, panel_name, _plan, _owner in self.imported:
            per_panel[panel_name] = per_panel.get(panel_name, 0) + 1
        if per_panel:
            rep += "🖥 بر اساس پنل: " + "، ".join(f"{k}: {v}" for k, v in per_panel.items()) + "\n"
        again = [row for row in self.imported if row[3] is not None]
        if again:
            rep += "🔁 کانفیگِ قبلاً ایمپورت‌شده — این‌بار برای این کاربر هم ثبت شد:\n"
            for email, _pn, _pl, prior in again:
                rep += f"   • {email} (قبلاً برای کاربر {prior['user_id']})\n"
        if self.skipped_same_user:
            rep += f"↩️ تکراری برای همین کاربر (رد شد): {len(self.skipped_same_user)}\n"
        if self.not_found:
            rep += f"❌ روی هیچ پنلی یافت نشد: {', '.join(self.not_found)}\n"
        for email, detail in self.failed_positions:
            rep += f"⚠️ {email}: {detail}\n"
        if self.imported:
            rep += "💡 حجم کلاینت‌ها با پلن‌های هم‌پنل مقایسه شد و پلنِ هم‌خوان به‌صورت خودکار ثبت شد."
        return rep.rstrip()


async def _panel_clients():
    """لاگین روی همه‌ی پنل‌ها؛ خروجی: [(panel_row_or_None, xui, ip, ok), ...]."""
    panels = await db.get_panels()
    clients = []
    if panels:
        for prow in panels:
            x, ip = build_xui(prow)
            ok, _ = await x.login()
            clients.append((prow, x, ip, ok))
    else:
        x, ip = build_xui(None)
        ok, _ = await x.login()
        clients.append((None, x, ip, ok))
    return clients


async def run_import(target_user_id, wanted):
    """تلاش برای یافتن و ثبت هر کانفیگ برای target_user_id روی همه‌ی پنل‌ها.

    - تکراری برای خودِ کاربر: رد می‌شود (skipped_same_user).
    - کانفیگی که قبلاً به کاربر دیگری تعلق دارد: **مجاز است** و ثبت می‌شود؛
      مالکِ قبلی در گزارش علامت می‌خورد (prior_owner).
    - پلن بر اساس حجمِ کلاینت با پلن‌های همان پنل تطبیق داده می‌شود.
    """
    rep = ImportReport(target_user=target_user_id)
    panel_clients = await _panel_clients()
    if not any(ok for _p, _x, _i, ok in panel_clients):
        rep.connection_ok = False
        return rep

    # ایمیل‌های فعلیِ کاربر برای جلوگیری از ایمپورت تکراری برای همان کاربر
    rows = await db.get_orders_by_user(target_user_id)
    existing_emails = set()
    for r in rows:
        em = order_email(r)
        if em:
            existing_emails.add(em.strip().lower())

    # پلن‌های هر پنل برای تشخیص خودکار پلنِ سفارش ایمپورتی (بر اساس حجم)
    plans_by_panel = {}
    for prow, _x, _ip, ok in panel_clients:
        if ok and prow:
            plans_by_panel[prow['id']] = await db.list_plans(panel_id=prow['id'], only_active=True)

    for email in wanted:
        if email.lower() in existing_emails:
            rep.skipped_same_user.append(email)
            continue
        prior_owner = await db.find_order_by_email(email)
        found = False
        for prow, x, ip, ok in panel_clients:
            if not ok:
                continue
            pname = prow['name'] if prow else 'پیش‌فرض'
            try:
                inbound_id, port, client_dict = await x.get_client_exact_info(email)
            except Exception as ex:
                rep.failed_positions.append((email, f"خطا در اتصال به پنل «{pname}»"))
                logging.warning("IMPORT %s روی پنل %s ناموفق: %s", email, pname, ex)
                continue
            if not client_dict:
                continue
            real_email = client_dict.get('email', email)
            pid = prow['id'] if prow else None
            inbound = await x.get_inbound(inbound_id)
            if inbound:
                config_link = build_share_link(inbound, client_dict, ip)
            else:
                config_link = build_vless_link(client_dict.get('id', ''), ip, port, real_email)
            # تشخیص خودکار پلن: حجم کلاینت با پلن‌های همان پنل مقایسه می‌شود
            matched_plan_id = None
            total_gb = int(client_dict.get('totalGB') or 0) // (1024 ** 3)
            if pid and total_gb > 0:
                for p in plans_by_panel.get(pid, []):
                    if int(p['gb']) == total_gb:
                        matched_plan_id = p['id']
                        break
            urow = await db.get_user(target_user_id)
            user_role = urow[2] if urow else None
            await db.add_order(
                target_user_id, config_link, pid, email=real_email,
                inbound_id=inbound_id, plan_id=matched_plan_id,
                user_role=user_role,
            )
            existing_emails.add(real_email.strip().lower())
            rep.imported.append((real_email, pname, matched_plan_id, prior_owner))
            found = True
            break
        if not found and not any(pos[0] == email for pos in rep.failed_positions):
            rep.not_found.append(email)
    return rep
