"""
test_member_prices.py
Manual test: compare member vs non-member court hire prices.
Run via GitHub Actions (workflow_dispatch only) — no writes to Supabase.
Uses Esta's cookies for all requests, but passes Blake's pbp_user_id for non-member price.
"""
import asyncio
import json
import time
import os
import httpx
from datetime import date, timedelta

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ESTA_SUPABASE_USER_ID = os.environ["ESTA_SUPABASE_USER_ID"]

BLAKE_PBP_USER_ID = 1973346  # non-member, hardcoded

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

PBP_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://app.playbypoint.com",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "X-Requested-With": "XMLHttpRequest",
}

TARGET_DATE = date.today() + timedelta(days=2)
TARGET_SEC  = 10 * 3600  # 10:00 AM

VENUES = [
    {"name": "Dink & Drive",      "facility_id": 1557, "surfaces": ["standard_courts", "championship_courts"]},
    {"name": "The Real Dill",     "facility_id": 1461, "surfaces": ["pickleball"]},
    {"name": "PicklePlex",        "facility_id": 1532, "surfaces": ["pickleball"]},
    {"name": "Pickle Playground", "facility_id": 1487, "surfaces": ["pickleball"]},
    {"name": "The Rally",         "facility_id": 1664, "surfaces": ["pickleball"]},
]


async def get_credentials(supabase_user_id: str):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/pbp_credentials",
            params={"user_id": f"eq.{supabase_user_id}", "select": "pbp_cookies,pbp_user_id"},
            headers=SB_HEADERS,
        )
        row = r.json()[0]
        cookies_raw = row["pbp_cookies"]
        pbp_user_id = row["pbp_user_id"]
        if isinstance(cookies_raw, str):
            cookies_raw = json.loads(cookies_raw)
        if isinstance(cookies_raw, list):
            cookies = {c["name"]: c["value"] for c in cookies_raw}
        else:
            cookies = cookies_raw
        return cookies, int(pbp_user_id)


async def get_available_courts(cookies: dict, facility_id: int, surface: str) -> list:
    date_ts = int(time.mktime(TARGET_DATE.timetuple()))
    async with httpx.AsyncClient(cookies=cookies, headers=PBP_HEADERS) as client:
        r = await client.get(
            f"https://app.playbypoint.com/api/facilities/{facility_id}/available_courts",
            params={
                "date": date_ts,
                "surface": surface,
                "start_hour": TARGET_SEC,
                "hour_end": TARGET_SEC + 3600,
                "kind": "reservation",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json() or []
        return []


async def get_court_price(cookies: dict, court_id: int, pbp_user_id: int) -> float | None:
    date_ts = int(time.mktime(TARGET_DATE.timetuple()))
    async with httpx.AsyncClient(cookies=cookies, headers=PBP_HEADERS) as client:
        r = await client.get(
            f"https://app.playbypoint.com/api/courts/{court_id}/price",
            params={
                "date": date_ts,
                "admin_book": "false",
                "hour_start": TARGET_SEC,
                "hour_end": TARGET_SEC + 3600,
                "players": "reservation_type_1",
                "payment_method": "credit_card",
                "user_id": pbp_user_id,
            },
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            fare = (data.get("total") or {}).get("original_reservation_fare")
            return float(fare) if fare is not None else None
        print(f"    price status: {r.status_code}")
        return None


async def main():
    print(f"Testing member vs non-member court prices on {TARGET_DATE} at 10:00 AM")
    print("=" * 60)

    print("Fetching Esta's credentials...")
    esta_cookies, esta_pbp_id = await get_credentials(ESTA_SUPABASE_USER_ID)
    print(f"Esta:  pbp_user_id={esta_pbp_id}, cookies={len(esta_cookies)}")
    print(f"Blake: pbp_user_id={BLAKE_PBP_USER_ID} (hardcoded, non-member)")
    print()

    any_difference = False

    for venue in VENUES:
        print(f"── {venue['name']} (facility_id={venue['facility_id']}) ──")
        courts = []
        for surface in venue["surfaces"]:
            courts = await get_available_courts(esta_cookies, venue["facility_id"], surface)
            if courts:
                print(f"  Surface: {surface}, courts found: {len(courts)}")
                break
        if not courts:
            print(f"  No courts available on {TARGET_DATE} at 10:00 AM")
            print()
            continue

        for court in courts[:2]:  # check first 2 courts per venue
            court_id = court.get("id")
            court_name = court.get("name", str(court_id))

            # Use Esta's cookies for both calls — only user_id param differs
            blake_price = await get_court_price(esta_cookies, court_id, BLAKE_PBP_USER_ID)
            esta_price  = await get_court_price(esta_cookies, court_id, esta_pbp_id)

            print(f"  Court: {court_name} (id={court_id})")
            print(f"    Non-member (user_id={BLAKE_PBP_USER_ID}): ${blake_price}")
            print(f"    Member     (user_id={esta_pbp_id}):  ${esta_price}")

            if blake_price is not None and esta_price is not None:
                diff = blake_price - esta_price
                if diff > 0.01:
                    print(f"    ✅ Member saves ${diff:.2f}/hr")
                    any_difference = True
                elif diff < -0.01:
                    print(f"    ⚠️  Member pays more? Diff=${diff:.2f}")
                else:
                    print(f"    — Same price, no member discount")
            else:
                print(f"    ⚠️  Could not fetch one or both prices")
            await asyncio.sleep(0.5)
        print()

    print("=" * 60)
    if any_difference:
        print("✅ Member pricing confirmed — worth implementing in scraper.")
    else:
        print("— No member price differences found across tested venues.")


if __name__ == "__main__":
    asyncio.run(main())
