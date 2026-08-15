import asyncio, json, re, sys
from curl_cffi.requests import AsyncSession

EMAIL = sys.argv[1] if len(sys.argv) > 1 else ""
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else ""

async def main():
    session = AsyncSession(impersonate="chrome124")

    # Step 1: GET login page for CSRF token + cookies
    r = await session.get("https://app.playbypoint.com/users/sign_in")
    print("GET status:", r.status_code)

    csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    if not csrf_match:
        print("ERROR: No CSRF token found")
        print(r.text[:300])
        return

    token = csrf_match.group(1)
    cookies = dict(r.cookies)
    print("CSRF:", token[:30], "...")
    print("Cookies:", list(cookies.keys()))

    # Step 2: POST login with CSRF token
    r2 = await session.post(
        "https://app.playbypoint.com/users/sign_in",
        json={
            "user": {
                "email": "blakerenfrey@yahoo.com.au",
                "password": "Barkers16",
                "remember_me": "1"
            }
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": token,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://app.playbypoint.com/users/sign_in",
        },
        cookies=cookies,
    )
    print("POST status:", r2.status_code)
    print("Response:", r2.text[:300])
    print("Response cookies:", list(r2.cookies.keys()))

    if "_paybycourt_session" in r2.cookies:
        print("SUCCESS - session cookie obtained")
    else:
        print("FAILED - no session cookie")

asyncio.run(main())
