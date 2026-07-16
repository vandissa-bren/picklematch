"""
pbp_login.py — Log in to PlayByPoint using Playwright and return session cookies.
"""
import asyncio
import json
import sys
from playwright.async_api import async_playwright


async def login_and_get_cookies(email: str, password: str) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        try:
            await page.goto("https://app.playbypoint.com/users/sign_in", timeout=30000)

            # Wait for Cloudflare to pass
            for _ in range(20):
                await page.wait_for_timeout(2000)
                title = (await page.title()).strip().lower()
                if title not in ("just a moment...", "just a moment…", "attention required! | cloudflare", ""):
                    break

            print(f"Page title: {await page.title()}", file=sys.stderr)

            # Use the standard Devise form fields (not the SSO/magic link inputs)
            email_sel = "#user_email, input[name='user[email]']"
            pwd_sel = "#user_password, input[name='user[password]']"

            await page.wait_for_selector(email_sel, state="visible", timeout=15000)
            await page.fill(email_sel, email)
            await page.wait_for_timeout(300)

            await page.wait_for_selector(pwd_sel, state="visible", timeout=10000)
            await page.fill(pwd_sel, password)
            await page.wait_for_timeout(300)

            # Submit via the commit button
            await page.click("input[name='commit'], button[type='submit']")

            # Wait for redirect away from sign_in
            await page.wait_for_url(lambda url: "sign_in" not in url, timeout=20000)
            await page.wait_for_timeout(2000)

            print(f"Landed on: {page.url}", file=sys.stderr)

            # Extract cookies
            cookies = await context.cookies()
            pbp_cookies = {
                c["name"]: c["value"] for c in cookies
                if "playbypoint" in c.get("domain", "") or "paybycourt" in c.get("name", "")
            }

            if "_paybycourt_session" not in pbp_cookies:
                raise RuntimeError("Login succeeded but no session cookie found. Wrong credentials?")

            # Get user_id from API
            user_id = 0
            try:
                api_resp = await page.goto("https://app.playbypoint.com/api/users/current_user", timeout=10000)
                if api_resp and api_resp.ok:
                    data = await api_resp.json()
                    user_id = data.get("id") or data.get("user", {}).get("id") or 0
            except Exception as e:
                print(f"Could not get user_id: {e}", file=sys.stderr)

            return {
                "cookies": pbp_cookies,
                "user_id": user_id,
                "email": email,
            }

        finally:
            await browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: pbp_login.py <email> <password>"}))
        sys.exit(1)
    try:
        result = asyncio.run(login_and_get_cookies(sys.argv[1], sys.argv[2]))
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
