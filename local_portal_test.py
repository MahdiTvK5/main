"""Local visual test for the OverWall subscription portal.

This file deliberately does NOT connect to PostgreSQL or 3x-UI. It injects a
realistic subscription object into the existing subportal renderer so the UI
can be tested locally on Windows without production credentials.

Run:
    python local_portal_test.py

Then open the printed Portal URL in your browser.
"""
import asyncio
import base64
import os
import time

from aiohttp import web

# Keep imports compatible with the project's normal runtime config.
os.environ.setdefault("BOT_TOKEN", "local-test-token")
os.environ.setdefault("WEB_ADMIN_PASSWORD", "local-test")
os.environ.setdefault("PORTAL_PUBLIC_URL", "")

import subportal  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("LOCAL_PORTAL_PORT", "8090"))
TOKEN = "TEST-OVERWALL-123456"
BASE = f"http://{HOST}:{PORT}"

NOW_MS = int(time.time() * 1000)
EXPIRY_MS = NOW_MS + 30 * 24 * 60 * 60 * 1000
TOTAL = 200 * 1024**3
USED = 134 * 1024**3

INBOUND = {
    "id": 1,
    "protocol": "vless",
    "port": 443,
    "streamSettings": (
        '{"network":"ws","security":"tls",'
        '"tlsSettings":{"serverName":"cdn.example.com"},'
        '"wsSettings":{"path":"/ws"}}'
    ),
}

CLIENT = {
    "id": "uuid-12345678-1234-1234-1234-123456789abc",
    "email": "201_mahdi",
    "enable": True,
    "totalGB": TOTAL,
    "expiryTime": EXPIRY_MS,
}

SAMPLE_LINK = (
    "vless://uuid-12345678-1234-1234-1234-123456789abc"
    "@cdn.example.com:443?type=ws&security=tls&path=%2Fws"
    "&sni=cdn.example.com#201_mahdi"
)


def sample_subscription():
    """Build the exact data shape expected by render_portal()."""
    raw_url = f"{BASE}/sub/{TOKEN}/raw"
    clash_url = f"{BASE}/sub/{TOKEN}/clash"
    return {
        "token": TOKEN,
        "order_id": 55,
        "name": "201_mahdi",
        "role": "vip",
        "plan": "پلن ۲۰۰ گیگ",
        "panel_name": "آلمان",
        "config_link": SAMPLE_LINK,
        "links": [SAMPLE_LINK],
        "server": "cdn.example.com",
        "port": 443,
        "status": {
            "key": "active",
            "label": "فعال",
        },
        # Use the project's own pure helpers. This prevents the local mock
        # from drifting away from the real renderer's expected structure.
        "traffic": subportal.traffic_view(USED, TOTAL),
        "expiry": subportal.expiry_view(EXPIRY_MS, "active", NOW_MS),
        "details": subportal.stream_details(INBOUND),
        "raw_url": raw_url,
        "clash_url": clash_url,
        "qr_url": (
            "https://api.qrserver.com/v1/create-qr-code/?size=300x300&margin=10&data="
            + subportal.quote(raw_url, safe="")
        ),
        "apps": subportal.quick_connect_apps(raw_url, clash_url, "201_mahdi"),
    }


async def fake_load_subscription(token):
    if token != TOKEN:
        return None
    return sample_subscription()


async def fake_load_full(token):
    if token != TOKEN:
        return None
    return {
        "inbound": INBOUND,
        "client": CLIENT,
        "cfg_ip": "cdn.example.com",
    }


# Inject only the data-loading layer. The actual renderer, raw encoder and
# Clash converter from subportal.py remain untouched and are what we test.
subportal.load_subscription = fake_load_subscription
subportal._load_full = fake_load_full


def html_response(text, status=200):
    return web.Response(
        text=text,
        status=status,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def portal(request):
    sub = await subportal.load_subscription(request.match_info.get("token", ""))
    if sub is None:
        return html_response(subportal.render_error_page("not_found"), 404)

    token = sub["token"]
    base = f"{request.scheme}://{request.host}"
    sub["raw_url"] = subportal.raw_url(token, base)
    sub["clash_url"] = subportal.clash_url(token, base)
    sub["qr_url"] = (
        "https://api.qrserver.com/v1/create-qr-code/?size=300x300&margin=10&data="
        + subportal.quote(sub["raw_url"], safe="")
    )
    sub["apps"] = subportal.quick_connect_apps(
        sub["raw_url"], sub["clash_url"], sub["name"]
    )
    return html_response(subportal.render_portal(sub))


async def raw(request):
    sub = await subportal.load_subscription(request.match_info.get("token", ""))
    if sub is None:
        return web.Response(status=404, text="not found")
    headers = {"Cache-Control": "no-store"}
    headers.update(
        subportal.subscription_headers(
            sub["name"], sub["traffic"], sub["expiry"]["ms"]
        )
    )
    headers["profile-web-page-url"] = f"{request.scheme}://{request.host}/sub/{sub['token']}"
    return web.Response(
        text=subportal.build_raw_body(sub["links"]),
        content_type="text/plain",
        charset="utf-8",
        headers=headers,
    )


async def clash(request):
    sub = await subportal.load_subscription(request.match_info.get("token", ""))
    if sub is None:
        return web.Response(status=404, text="not found")
    full = await subportal._load_full(sub["token"])
    if not full:
        return web.Response(status=503, text="temporarily unavailable")
    return web.Response(
        text=subportal.build_clash_yaml(
            sub["name"], full["inbound"], full["client"], full["cfg_ip"]
        ),
        content_type="text/yaml",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


def create_app():
    app = web.Application()
    app.router.add_get("/sub/{token}", portal)
    app.router.add_get("/sub/{token}/raw", raw)
    app.router.add_get("/sub/{token}/clash", clash)

    async def health(_request):
        return web.json_response({"status": "ok", "mode": "LOCAL TEST"})

    app.router.add_get("/health", health)
    return app


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  OverWall Subscription Portal - LOCAL TEST")
    print("=" * 60)
    print(f"\nPortal:\n{BASE}/sub/{TOKEN}")
    print(f"\nRaw Subscription:\n{BASE}/sub/{TOKEN}/raw")
    print(f"\nClash:\n{BASE}/sub/{TOKEN}/clash")
    print(f"\nHealth:\n{BASE}/health")
    print("\nPress CTRL+C to stop.")
    print("=" * 60 + "\n")
    web.run_app(create_app(), host=HOST, port=PORT)
