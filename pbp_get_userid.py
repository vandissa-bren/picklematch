import asyncio, re, sys, json
from curl_cffi.requests import AsyncSession

EMAIL = sys.argv[1]
PASSWORD = sys.argv[2]

async def main():
    session = AsyncSession(impersonate="chrome124")
    r = await session.get("https://app.playbypoint.com/users/sign_in")
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text).group(1)
    cookies = dict(r.cookies)

    r2 = await session.post(
        "https://app.playbypoint.com/users/sign_in",
        json={"user": {"email": EMAIL, "password": PASSWORD, "remember_me": "1"}},
        headers={"Accept": "application/json", "Content-Type": "application/json", "X-CSRF-Token": token, "X-Requested-With": "XMLHttpRequest"},
        cookies=cookies,
    )
    print("Login response:", r2.text[:500])

    all_cookies = {**cookies, **dict(r2.cookies)}

    # Get a new page with valid CSRF after login
    r3 = await session.get("https://app.playbypoint.com/home", cookies=all_cookies)
    print("Home status:", r3.status_code)

    # Extract new CSRF token from home page
    csrf2 = re.search(r'<meta name="csrf-token" content="([^"]+)"', r3.text)
    token2 = csrf2.group(1) if csrf2 else token
    print("New CSRF:", token2[:20], "...")

    # Extract user_id from page HTML
    uid_match = re.search(r'"user_id"\s*:\s*(\d+)', r3.text) or \
                re.search(r'"id"\s*:\s*(\d+).*?"email"', r3.text) or \
                re.search(r'data-user-id="(\d+)"', r3.text) or \
                re.search(r'"current_user_id"\s*:\s*(\d+)', r3.text)
    if uid_match:
        print("user_id from page:", uid_match.group(1))
    else:
        print("user_id not found in page HTML")

    # Try API with fresh CSRF
    api_headers = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Token": token2,
        "Referer": "https://app.playbypoint.com/home",
    }
    for endpoint in ["/api/users/current_facility", "/api/recommendations/facilities"]:
        r4 = await session.get(
            f"https://app.playbypoint.com{endpoint}",
            cookies=all_cookies,
            headers=api_headers,
        )
        print(f"{endpoint}: {r4.status_code} — {r4.text[:300]}")

asyncio.run(main())