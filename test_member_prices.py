"""
test_member_prices.py — Manual test only, no writes to Supabase.
Compares member vs non-member court hire prices using Esta's session.
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
ESTA_PBP_USER_ID = 1742850   # member
BLAKE_PBP_USER_ID = 1973346  # non-member

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


async def get_esta_cookies() -> dict:
    url = f"{SUPABASE_URL}/rest/v1/pbp_credentials"
    params = {"user_id": f"eq.{ESTA_SUPABASE_USER_ID}", "select": "pbp_cookies"}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params=params, headers=SB_HEADERS)
        print(f"Supabase status: {r.status_code}")
        body = r.text
        print(f"Supabase body: {body[:400]}")
        data = r.json()
        if not data:
            raise ValueError(f"No pbp_credentials row found for user {ESTA_SUPABASE_USER_ID}")
        cookies_raw = data[0]["pbp_cookies"]
        if isinstance(cookies_raw, str):
            cookies_raw = json.loads(cookies_raw)
        if isinstance(cookies_raw, list):
            return {c["name"]: c["value"] for c in cookies_raw}
        return cookies_raw  # already a dict


async def get_available_courts(cookies: dict, facility_id: int, surface: str) -> list:
    date_ts = int(time.mktime(TARGET_DATE.timetuple()))
    async with httpx.AsyncClient(cookies=cookies, headers=PBP_HEADERS) as client:
        r = await client.get(
            f"https://app.playbypoint.com/api/facilities/{facility_id}/available_courts",
            params={"date": date_ts, "surface": surface, "start_hour": TARGET_SEC,
                    "hour_end": TARGET_SEC + 3600, "kind": "reservation"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json() or []
        print(f"    available_courts {surface}: {r.status_code}")
        return []


async def get_court_price(cookies: dict, court_id: int, pbp_user_id: int) -> float | None:
    date_ts = int(time.mktime(TARGET_DATE.timetuple()))
    async with httpx.AsyncClient(cookies=cookies, headers=PBP_HEADERS) as client:
        r = await client.get(
            f"https://app.playbypoint.com/api/courts/{court_id}/price",
            params={"date": date_ts, "admin_book": "false", "hour_start": TARGET_SEC,
                    "hour_end": TARGET_SEC + 3600, "players": "reservation_type_1",
                    "payment_method": "credit_card", "user_id": pbp_user_id},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            fare = (data.get("total") or {}).get("original_reservation_fare")
            return float(fare) if fare is not None else None
        print(f"    price status: {r.status_code}")
        return None


async def main():
    print(f"Member vs non-member court prices on {TARGET_DATE} at 10:00 AM")
    print("=" * 60)

    cookies = await get_esta_cookies()
    print(f"Got {len(cookies)} cookies for Esta\n")

    any_difference = False

    for venue in VENUES:
        print(f"── {venue['name']} (facility_id={venue['facility_id']}) ──")
        courts = []
        for surface in venue["surfaces"]:
            courts = await get_available_courts(cookies, venue["facility_id"], surface)
            if courts:
                print(f"  Surface: {surface}, courts: {len(courts)}")
                break
        if not courts:
            print(f"  No courts available")
            print()
            continue

        for court in courts[:2]:
            court_id = court.get("id")
            court_name = court.get("name", str(court_id))
            non_member_price = await get_court_price(cookies, court_id, BLAKE_PBP_USER_ID)
            member_price     = await get_court_price(cookies, court_id, ESTA_PBP_USER_ID)
            print(f"  {court_name} (id={court_id})")
            print(f"    Non-member (user_id={BLAKE_PBP_USER_ID}): ${non_member_price}")
            print(f"    Member     (user_id={ESTA_PBP_USER_ID}):  ${member_price}")
            if non_member_price is not None and member_price is not None:
                diff = non_member_price - member_price
                if diff > 0.01:
                    print(f"    ✅ Member saves ${diff:.2f}/hr")
                    any_difference = True
                elif diff < -0.01:
                    print(f"    ⚠️  Member pays more? ${abs(diff):.2f}")
                else:
                    print(f"    — Same price")
            await asyncio.sleep(0.5)
        print()

    print("=" * 60)
    print("✅ Member pricing differs — implement in scraper." if any_difference
          else "— No member discounts found on court hire.")


if __name__ == "__main__":
    asyncio.run(main())
