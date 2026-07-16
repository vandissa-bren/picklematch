"""
Standalone Playwright login for PBP — called by /api/pbp/connect endpoint.
Returns JSON: {cookies, user_id, email} or {error}
"""
import asyncio, json, sys, re
from playwright.async_api import async_playwright


async def login(email: str, password: str) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            await page.goto("https://app.playbypoint.com/users/sign_in", timeout=30000)

            # Wait for Cloudflare
            for _ in range(15):
                await page.wait_for_timeout(2000)
                title = (await page.title()).lower()
                if "just a moment" not in title and "cloudflare" not in title:
                    break

            # Fill standard form
            await page.wait_for_selector("#user_email", state="visible", timeout=15000)
            await page.fill("#user_email", email)
            await page.fill("#user_password", password)
            await page.click("input[name='commit']")

            # Wait for redirect
            await page.wait_for_url(lambda url: "sign_in" not in url, timeout=20000)
            await page.wait_for_timeout(2000)

            # Check we're not back on sign_in (wrong password)
            if "sign_in" in page.url:
                raise RuntimeError("Invalid email or password.")

            # Get all cookies
            cookies = await context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            if "_paybycourt_session" not in cookie_dict:
                raise RuntimeError("Login succeeded but no session cookie found.")

            # Get user_id from home page HTML
            r = await page.goto("https://app.playbypoint.com/home", timeout=15000)
            await page.wait_for_timeout(1000)
            html = await page.content()
            uid_match = re.search(r'"user_id"\s*:\s*(\d+)', html)
            user_id = int(uid_match.group(1)) if uid_match else 0

            # Verify cookies work on API
            api_resp = await page.goto(
                "https://app.playbypoint.com/api/public/clinics?q=pickleball",
                timeout=10000
            )
            api_ok = api_resp and api_resp.ok
            print(f"API verification: {api_resp.status if api_resp else 'failed'}", file=sys.stderr)

            return {
                "cookies": cookie_dict,
                "user_id": user_id,
                "email": email,
                "api_verified": api_ok,
            }

        finally:
            await browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: pbp_connect_playwright.py <email> <password>"}))
        sys.exit(1)
    try:
        result = asyncio.run(login(sys.argv[1], sys.argv[2]))
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
