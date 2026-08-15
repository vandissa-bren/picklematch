"""
pbp_debug.py — Screenshot PBP login page to see what's blocking visibility.
"""
import asyncio
import base64
from playwright.async_api import async_playwright


async def debug():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        await page.goto("https://app.playbypoint.com/users/sign_in", timeout=30000)

        # Wait for CF
        for i in range(20):
            await page.wait_for_timeout(2000)
            title = (await page.title()).strip().lower()
            print(f"  [{i*2}s] title: {title!r}")
            if title not in ("just a moment...", "just a moment…", "attention required! | cloudflare", ""):
                break

        print(f"Final URL: {page.url}")
        print(f"Final title: {await page.title()}")

        # Check what inputs exist and their visibility
        inputs = await page.query_selector_all("input")
        for inp in inputs:
            itype = await inp.get_attribute("type")
            iid = await inp.get_attribute("id")
            iname = await inp.get_attribute("name")
            visible = await inp.is_visible()
            print(f"  input type={itype} id={iid} name={iname} visible={visible}")

        # Screenshot
        await page.screenshot(path="/app/pbp_login_debug.png", full_page=True)
        print("Screenshot saved to /app/pbp_login_debug.png")

        await browser.close()

asyncio.run(debug())
