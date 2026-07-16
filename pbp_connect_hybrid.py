"""
Hybrid PBP login:
1. curl_cffi gets _paybycourt_session (fast, bypasses CF)
2. Playwright loads home page with that cookie to get full browser session
"""
import asyncio, json, sys, re
from curl_cffi.requests import AsyncSession
from playwright.async_api import async_playwright


async def login(email: str, password: str) -> dict:
    # Step 1: curl_cffi login — fast, gets _paybycourt_session
    print("Step 1: curl_cffi login...", file=sys.stderr)
    session = AsyncSession(impersonate="chrome124")
    r = await session.get("https://app.playbypoint.com/users/sign_in")
    csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    if not csrf_match:
        raise RuntimeError("Could not load PBP login page.")
    token = csrf_match.group(1)
    cookies = dict(r.cookies)

    r2 = await session.post(
        "https://app.playbypoint.com/users/sign_in",
        json={"user": {"email": email, "password": password, "remember_me": "1"}},
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": token,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://app.playbypoint.com/users/sign_in",
        },
        cookies=cookies,
    )
    resp = r2.json() if "application/json" in r2.headers.get("content-type", "") else {}
    if not resp.get("success"):
        raise RuntimeError("Invalid email or password.")

    session_cookie = dict(r2.cookies).get("_paybycourt_session") or cookies.get("_paybycourt_session")
    if not session_cookie:
        raise RuntimeError("No session cookie returned from login.")
    print(f"Got _paybycourt_session: {session_cookie[:20]}...", file=sys.stderr)

    # Step 2: Playwright loads home page with that cookie — no form, no CF challenge
    print("Step 2: Playwright loading home page...", file=sys.stderr)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )

        # Inject the session cookie before loading any page
        await context.add_cookies([{
            "name": "_paybycourt_session",
            "value": session_cookie,
            "domain": "app.playbypoint.com",
            "path": "/",
        }])

        page = await context.new_page()

        # Load home — already authenticated, no login form needed
        await page.goto("https://app.playbypoint.com/home", timeout=30000)

        # Wait for CF if needed
        for _ in range(10):
            await page.wait_for_timeout(2000)
            title = (await page.title()).lower()
            if "just a moment" not in title and "cloudflare" not in title:
                break

        print(f"Home title: {await page.title()}", file=sys.stderr)
        print(f"Home URL: {page.url}", file=sys.stderr)

        # Get user_id from page
        html = await page.content()
        uid_match = re.search(r'"user_id"\s*:\s*(\d+)', html)
        user_id = int(uid_match.group(1)) if uid_match else 0
        print(f"user_id: {user_id}", file=sys.stderr)

        # Get full cookie set
        pw_cookies = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in pw_cookies}
        print(f"Cookies: {list(cookie_dict.keys())}", file=sys.stderr)

        # Verify API access
        api_resp = await page.goto(
            "https://app.playbypoint.com/api/public/clinics/69306",
            timeout=10000
        )
        api_status = api_resp.status if api_resp else 0
        print(f"API check: {api_status}", file=sys.stderr)

        await browser.close()

    return {
        "cookies": cookie_dict,
        "user_id": user_id,
        "email": email,
        "api_ok": api_status == 200,
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: pbp_connect_hybrid.py <email> <password>"}))
        sys.exit(1)
    try:
        result = asyncio.run(login(sys.argv[1], sys.argv[2]))
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
