"""
refresh_cookies_server.py — Refresh PBP cookies on the DO server.
Runs via cron every 6 hours.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, '/app')
from extract_thejar import harvest_cookies_via_playwright

PBP_EMAIL = "blakerenfrey@yahoo.com.au"
PBP_PASSWORD = "Barkers16"
COOKIE_PATH = "/app/.pbp_cookies.json"


async def main():
    print("Logging in to PlayByPoint...")
    cookies, user_id = await harvest_cookies_via_playwright(
        email=PBP_EMAIL,
        password=PBP_PASSWORD,
        headless=True,
    )
    if not cookies:
        print("ERROR: No cookies returned")
        sys.exit(1)

    data = {"cookies": cookies, "user_id": user_id, "email": PBP_EMAIL}
    Path(COOKIE_PATH).write_text(json.dumps(data))
    print(f"✓ Saved {len(cookies)} cookies to {COOKIE_PATH}")

    # Notify API server to reload cookies
    import httpx
    try:
        r = httpx.post(
            "http://localhost:8000/api/internal/refresh-cookies",
            json={"pbp_cookies_json": json.dumps(data)},
            timeout=5,
        )
        print(f"✓ API server notified: {r.status_code}")
    except Exception as e:
        print(f"API notify failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
