"""کامپوننت‌های قابل‌استفاده‌ی مجدد داشبورد (React-style به‌صورت توابع خالص).

هر کامپوننت یک تابع خالص است که فقط HTML/CSS می‌سازد — هیچ وابستگی به دیتابیس
یا درخواست ندارد و کاملاً قابل تست است. داده را از بالا دریافت می‌کند (props).

ساختار معادل خواسته‌ی طراحی:
    components/dashboard/StatsCard        → stats_card()
    components/dashboard/RevenueChart     → revenue_chart()
    components/dashboard/ServerStatus     → server_status()
    components/dashboard/SystemHealth     → system_health()
    components/dashboard/ResourceMonitor  → resource_monitor()
    components/dashboard/OrdersTable      → orders_table()
    components/dashboard/ActivityFeed     → activity_feed()
"""
import html


def e(v):
    return html.escape(str(v if v is not None else ""))


# ============================================================
# StatsCard — کارت KPI با مقدار اصلی، رشد درصدی و میکرو-چارت
# ============================================================
def _sparkline(values, color='#7C5CFF', w=92, h=30):
    """نمودار خطی مینیاتوری داخل کارت KPI (SVG خالص، بدون کتابخانه)."""
    vals = [max(0, int(v or 0)) for v in (values or [])]
    if len(vals) < 2:
        return ''
    mx = max(vals) or 1
    pts = []
    for i, v in enumerate(vals):
        x = round(i * (w - 2) / (len(vals) - 1) + 1, 1)
        y = round(h - 3 - v * (h - 6) / mx, 1)
        pts.append(f"{x},{y}")
    polyline = ' '.join(pts)
    return (
        f'<svg class="kpi-spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'fill="none" aria-hidden="true">'
        f'<polyline points="{polyline}" stroke="{color}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )


def stats_card(label, value, icon_html='', delta=None, delta_tone='up',
               spark=None, tone='primary', sub=''):
    """کارت KPI.

    delta: متن درصد رشد مثل '+14.8%' (None یعنی نمایش نده)؛
    delta_tone: 'up' | 'down' | 'flat'؛
    spark: لیست اعداد برای میکرو-چارت؛
    tone: primary|green|gold|red|violet|teal (رنگ آیکون/دلتا).
    """
    delta_html = ''
    if delta:
        cls = {'up': 'delta-up', 'down': 'delta-down', 'flat': 'delta-flat'}.get(delta_tone, 'delta-flat')
        arrow = {'up': '▲', 'down': '▼', 'flat': '■'}.get(delta_tone, '■')
        delta_html = f'<span class="kpi-delta {cls}"><i>{arrow}</i>{e(delta)}</span>'
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ''
    spark_html = f'<div class="kpi-spark-wrap">{spark}</div>' if spark else ''
    return (
        f'<article class="kpi tone-{tone}">'
        f'<div class="kpi-head"><span class="kpi-ic">{icon_html}</span>'
        f'<span class="kpi-l">{e(label)}</span>{delta_html}</div>'
        f'<div class="kpi-body">{spark_html}<div class="kpi-v">{e(value)}</div></div>'
        f'{sub_html}</article>'
    )


# ============================================================
# RevenueChart — نمودار سطحی درآمد ۳۰ روزه با گرادیان بنفش
# ============================================================
def revenue_chart(series, prev_total=None, height=240, title='نمودار درآمد ۳۰ روز اخیر'):
    """نمودار سطحی smooth درآمد.

    series: لیست اعداد روزانه (قدیم→جدید). prev_total: جمع دوره‌ی قبل برای
    بج درصد رشد. اگر رشد منفی/ناچیز باشد بج محتاطانه‌تر رنگ می‌شود.
    """
    vals = [max(0, int(v or 0)) for v in (series or [])]
    n = len(vals)
    if n < 2:
        return ''
    W, H, PAD_X, PAD_T, PAD_B = 760, height, 14, 26, 34
    mx = max(vals) or 1
    # smooth curve با میان‌یابی Catmull-Rom ساده‌شده
    def _pt(i):
        x = PAD_X + i * (W - 2 * PAD_X) / (n - 1)
        y = PAD_T + (H - PAD_T - PAD_B) * (1 - vals[i] / mx)
        return x, y
    pts = [_pt(i) for i in range(n)]
    path = f"M {pts[0][0]:.1f} {pts[0][1]:.1f}"
    for i in range(n - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        cx = (x0 + x1) / 2
        path += f" C {cx:.1f} {y0:.1f} {cx:.1f} {y1:.1f} {x1:.1f} {y1:.1f}"
    area = path + f" L {pts[-1][0]:.1f} {H - PAD_B} L {pts[0][0]:.1f} {H - PAD_B} Z"

    cur = sum(vals)
    growth_html = ''
    if prev_total is not None and prev_total > 0:
        pct = (cur - prev_total) * 100 / prev_total
        cls = 'delta-up' if pct >= 0 else 'delta-down'
        arrow = '▲' if pct >= 0 else '▼'
        growth_html = (f'<span class="rev-growth {cls}"><i>{arrow}</i>'
                       f'{abs(pct):.1f}% نسبت به ۳۰ روز قبل</span>')
    elif prev_total == 0 and cur > 0:
        growth_html = '<span class="rev-growth delta-up"><i>▲</i>شروع دوره‌ی رشد</span>'

    # شبکه‌ی افقی ظریف
    grid = ''.join(
        f'<line x1="{PAD_X}" y1="{PAD_T + k * (H - PAD_T - PAD_B) / 4:.0f}" '
        f'x2="{W - PAD_X}" y2="{PAD_T + k * (H - PAD_T - PAD_B) / 4:.0f}" '
        f'class="rev-grid"/>' for k in range(5)
    )
    # دات آخرین نقطه
    lx, ly = pts[-1]
    dot = (f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4.5" fill="#7C5CFF" '
           f'stroke="rgba(124,92,255,.25)" stroke-width="6"/>')

    return (
        f'<section class="box chart-box">'
        f'<div class="chart-head"><h3>📈 {e(title)}</h3>{growth_html}</div>'
        f'<div class="rev-wrap"><svg class="rev-svg" viewBox="0 0 {W} {H}" '
        f'preserveAspectRatio="none" role="img" aria-label="نمودار درآمد سی روزه">'
        f'<defs><linearGradient id="revGrad" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="#7C5CFF" stop-opacity=".38"/>'
        f'<stop offset="100%" stop-color="#7C5CFF" stop-opacity="0"/></linearGradient></defs>'
        f'{grid}<path d="{area}" fill="url(#revGrad)" class="rev-area"/>'
        f'<path d="{path}" fill="none" stroke="#7C5CFF" stroke-width="2.5" '
        f'stroke-linecap="round" class="rev-line"/>{dot}</svg></div>'
        f'<div class="rev-foot"><span>۳۰ روز پیش</span><span class="rev-sum">جمع دوره: '
        f'<b>{cur:,}</b> تومان</span><span>امروز</span></div></section>'
    )


# ============================================================
# ServerStatus — وضعیت زنده‌ی پنل‌ها (ping واقعی)
# ============================================================
COUNTRY_BY_HOST = [
    ('de', '🇩🇪'), ('fi', '🇫🇮'), ('nl', '🇳🇱'), ('tr', '🇹🇷'), ('fr', '🇫🇷'),
    ('uk', '🇬🇧'), ('us', '🇺🇸'), ('ru', '🇷🇺'), ('se', '🇸🇪'), ('ch', '🇨🇭'),
]


def _host_flag(name):
    n = (name or '').lower()
    for code, flag in COUNTRY_BY_HOST:
        if code in n:
            return flag
    return '🌐'


def server_status(panels_ping, title='وضعیت سرورها'):
    """کارت وضعیت سرورها.

    panels_ping: لیست dict با کلیدهای name (نمایشی)، host، ms (int|None) و
    state ('online'|'warning'|'down').
    """
    items = []
    for p in (panels_ping or []):
        st = p.get('state') or ('online' if p.get('ms') is not None else 'down')
        ms = p.get('ms')
        ms_html = f'<b class="sv-ms">{ms}<small>ms</small></b>' if ms is not None else '<b class="sv-ms">—</b>'
        dot_cls = {'online': 'st-ok', 'warning': 'st-warn', 'down': 'st-down'}.get(st, 'st-down')
        st_text = {'online': 'Online', 'warning': 'Warning', 'down': 'Offline'}.get(st, 'Offline')
        flag = p.get('flag') or _host_flag(p.get('host') or p.get('name') or '')
        items.append(
            f'<li class="sv-row"><span class="sv-flag">{flag}</span>'
            f'<div class="sv-info"><b>{e(p.get("name") or "")}</b>'
            f'<span class="sv-state {dot_cls}">{st_text}</span></div>{ms_html}</li>'
        )
    rows = ''.join(items) or '<li class="sv-row muted">سروری ثبت نشده</li>'
    return (
        f'<section class="box sv-box"><h3>🖥 {e(title)}</h3>'
        f'<ul class="sv-list">{rows}</ul></section>'
    )


async def ping_panels(panels, timeout=4.0):
    """پینگ واقعی پنل‌ها برای ServerStatus.

    برای هر پنل فقط یک درخواست HEAD به آدرس پنل زده می‌شود؛ خطا یعنی Down.
    پینگ بالای ۱۵۰ms → Warning.
    """
    import asyncio
    import httpx

    async def _one(panel):
        url = (panel['url'] or '').strip()
        if not url:
            return {'name': panel['name'], 'host': '', 'ms': None, 'state': 'down'}
        candidates = [url] if url.startswith(('http://', 'https://')) else [f'https://{url}', f'http://{url}']
        last_ms = None
        for base in candidates:
            try:
                async with httpx.AsyncClient(verify=False, timeout=timeout, trust_env=False) as cli:
                    import time as _t
                    t0 = _t.perf_counter()
                    await cli.head(base, follow_redirects=True)
                    last_ms = int((_t.perf_counter() - t0) * 1000)
                    break
            except Exception:
                continue
        if last_ms is None:
            return {'name': panel['name'], 'host': url, 'ms': None, 'state': 'down'}
        state = 'warning' if last_ms > 150 else 'online'
        return {'name': panel['name'], 'host': url, 'ms': last_ms, 'state': state}

    return await asyncio.gather(*(_one(p) for p in (panels or [])))


# ============================================================
# SystemHealth — سلامت اجزای سیستم + uptime
# ============================================================
def system_health(components, uptime='99.8%', title='سلامت سیستم'):
    """components: لیست (نام، state) که state یکی از ok|warn|down."""
    STATE_TEXT = {'ok': 'سالم', 'warn': 'هشدار', 'down': 'قطع'}
    rows = ''.join(
        f'<li class="hl-row"><span class="hl-name">{e(name)}</span>'
        f'<span class="hl-dot st-{state}"></span>'
        f'<span class="hl-label">{STATE_TEXT.get(state, "—")}</span></li>'
        for name, state in (components or [])
    )
    return (
        f'<section class="box hl-box"><div class="chart-head">'
        f'<h3>🧬 {e(title)}</h3><span class="hl-uptime" title="آپ‌تایم ۳۰ روزه">آپ‌تایم {e(uptime)}</span></div>'
        f'<ul class="hl-list">{rows}</ul></section>'
    )


# ============================================================
# ResourceMonitor — CPU / RAM / Disk با نوار پیشرفت
# ============================================================
def resource_monitor(resources, title='منابع سرور'):
    """resources: لیست (نام، درصد ۰-۱۰۰، برچسب اختیاری)."""
    bars = ''
    for name, pct, *rest in (resources or []):
        pct = max(0, min(100, int(pct or 0)))
        label = rest[0] if rest else f'{pct}%'
        tone = 'ok' if pct < 70 else ('warn' if pct < 90 else 'down')
        bars += (
            f'<div class="rs-row"><div class="rs-head"><span>{e(name)}</span>'
            f'<b class="num" dir="ltr">{e(label)}</b></div>'
            f'<div class="rs-track"><div class="rs-fill st-{tone}" style="width:{pct}%"></div></div></div>'
        )
    rows = bars or '<div class="muted">منابع در دسترس نیست</div>'
    return f'<section class="box rs-box"><h3>⚙️ {e(title)}</h3>{rows}</section>'


# ============================================================
# OrdersTable — آخرین سفارش‌ها با badge وضعیت
# ============================================================
def orders_table(orders, title='آخرین سفارش‌ها'):
    """orders: لیست dict با id، user_id، plan_name، amount، status، date."""
    def _badge(status):
        m = {
            'done': ('b-green', 'تکمیل شده'),
            'pending': ('b-gold', 'در انتظار'),
            'failed': ('b-red', 'ناموفق'),
        }
        cls, text = m.get(status, ('b-gray', '—'))
        return f'<span class="badge {cls}">{text}</span>'

    rows = ''
    for o in (orders or []):
        rows += (
            f'<tr><td class="num" dir="ltr">#{e(o.get("id"))}</td>'
            f'<td><a href="/users/view?id={e(o.get("user_id"))}">{num_span(o.get("user_id"))}</a></td>'
            f'<td class="wrap">{e(o.get("plan_name") or "—")}</td>'
            f'<td class="num" dir="ltr">{int(o.get("amount") or 0):,}</td>'
            f'<td>{_badge(o.get("status"))}</td>'
            f'<td class="muted">{e(o.get("date") or "")}</td></tr>'
        )
    body = (
        f'<table><thead><tr><th>شناسه</th><th>کاربر</th><th>پلن</th>'
        f'<th>مبلغ (تومان)</th><th>وضعیت</th><th>زمان</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        if rows else '<div class="empty"><h3>سفارشی ثبت نشده</h3></div>'
    )
    return (
        f'<section class="box tbl-box"><div class="chart-head"><h3>📦 {e(title)}</h3>'
        f'<a class="back-link" href="/orders">مشاهده همه ←</a></div>'
        f'<div class="tbl-scroll">{body}</div></section>'
    )


def num_span(v):
    return f'<span class="num" dir="ltr">{e(v)}</span>'


# ============================================================
# ActivityFeed — تایم‌لاین رویدادهای اخیر (از داده‌ی واقعی)
# ============================================================
TONE_DOT = {'ok': 'act-ok', 'warn': 'act-warn', 'down': 'act-down', 'info': 'act-info'}


def activity_feed(events, title='فعالیت اخیر'):
    """events: لیست dict با text، tone (ok|warn|down|info)، time."""
    items = ''.join(
        f'<li class="act-row"><span class="act-dot {TONE_DOT.get(ev.get("tone", "info"))}"></span>'
        f'<div class="act-body"><p>{e(ev.get("text") or "")}</p>'
        f'<time>{e(ev.get("time") or "")}</time></div></li>'
        for ev in (events or [])
    )
    rows = items or '<li class="act-row muted">رویدادی ثبت نشده</li>'
    return (
        f'<section class="box act-box"><h3>🔔 {e(title)}</h3>'
        f'<ul class="act-list">{rows}</ul></section>'
    )
