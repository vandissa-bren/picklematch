"""
fetch_member_prices.py
Fetches member court hire prices using Esta's PBP account and stores them
in availability_cache as 'member_court_prices' alongside normal 'court_prices'.
Only runs for venues where we have a member account.

Run via GitHub Actions (workflow_dispatch + scheduled).
"""
import asyncio
import json
import os
import time
from datetime import date, timedelta

import httpx
from curl_cffi.requests import AsyncSession

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
ESTA_SUPABASE_USER_ID = os.environ["ESTA_SUPABASE_USER_ID"]

# Venues where Esta is a member — expand this list as we get more member accounts
MEMBER_VENUES = [
    {
        "name": "Dink & Drive",
        "facility_id": 1557,
        "slug": "dinkndrivepickleballclub",
        "surfaces": ["standard_courts", "championship_courts"],
        "member_supabase_user_id": os.environ["ESTA_SUPABASE_USER_ID"],
    },
]

DAYS_AHEAD = 7
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}
PBP_HEADERS = {
    "Accept": "*/*",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "X-Requested-With": "XMLHttpRequest",
}


def get_shift(sec: int) -> str:
    hour = sec // 3600
    if hour >= 17:
        return "primetime"
    elif hour >= 12:
        return "day"
    return "lowtime"


async def get_credentials(supabase_user_id: str, client: httpx.AsyncClient):
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/pbp_credentials",
        params={"user_id": f"eq.{supabase_user_id}", "select": "pbp_cookies,pbp_user_id"},
        headers=SB_HEADERS,
    )
    row = r.json()[0]
    cookies_raw = row["pbp_cookies"]
    pbp_user_id = int(row["pbp_user_id"])
    if isinstance(cookies_raw, str):
        cookies_raw = json.loads(cookies_raw)
    if isinstance(cookies_raw, list):
        cookies = {c["name"]: c["value"] for c in cookies_raw}
    else:
        cookies = cookies_raw
    return cookies, pbp_user_id


async def get_available_courts(cookies: dict, facility_id: int, surface: str,
                                target_date: date, target_sec: int) -> list:
    date_ts = int(time.mktime(target_date.timetuple()))
    async with AsyncSession(impersonate="chrome", cookies=cookies) as client:
        r = await client.get(
            f"https://app.playbypoint.com/api/facilities/{facility_id}/available_courts",
            params={
                "date": date_ts,
                "surface": surface,
                "start_hour": target_sec,
                "hour_end": target_sec + 3600,
                "kind": "reservation",
            },
            headers=PBP_HEADERS,
            timeout=10,
        )
        if r.status_code == 200:
            result = r.json() or []
            print(f"      available_courts [{surface}] {target_date} {target_sec//3600}:00 -> {len(result)} courts")
            return result
        print(f"      available_courts [{surface}] {target_date} {target_sec//3600}:00 -> status {r.status_code}")
        return []


async def get_member_price(cookies: dict, court_id: int, pbp_user_id: int,
                            slug: str, target_date: date, target_sec: int) -> float | None:
    date_ts = int(time.mktime(target_date.timetuple()))
    headers = {**PBP_HEADERS, "Referer": f"https://app.playbypoint.com/book/{slug}"}
    async with AsyncSession(impersonate="chrome", cookies=cookies) as client:
        r = await client.get(
            f"https://app.playbypoint.com/api/courts/{court_id}/price",
            params={
                "date": date_ts,
                "admin_book": "false",
                "hour_start": target_sec,
                "hour_end": target_sec + 3600,
                "players_reservation_type": "1",
                "user_ids[]": pbp_user_id,
                "user_who_is_paying": pbp_user_id,
                "kind": "reservation",
                "coupon_code": "",
                "booking_package_purchase_id": "",
            },
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            # Check affiliation to confirm member pricing was applied
            affiliation = None
            try:
                affiliation = data["prices_per_user"][0]["price"]["affiliation"]
            except (KeyError, IndexError):
                pass
            fare = (data.get("total") or {}).get("original_reservation_fare")
            price = round(float(fare), 2) if fare is not None else None
            print(f"      court {court_id} shift — affiliation: {affiliation}, price: ${price}")
            return price
        print(f"    price endpoint status: {r.status_code} body: {r.text[:300]}")
        return None


async def fetch_member_prices_for_venue(venue: dict, cookies: dict, pbp_user_id: int) -> dict:
    """
    Returns member_court_prices dict: {"court_id_shift": price}
    Uses known court IDs from existing court_prices in Supabase — avoids available_courts call.
    """
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/availability_cache",
            params={"id": f"eq.pbp-{venue['facility_id']}", "select": "data"},
            headers=SB_HEADERS,
        )
        records = r.json()

    existing_data = records[0]["data"] if records else {}
    member_prices = dict(existing_data.get("member_court_prices", {}))
    court_prices = existing_data.get("court_prices", {})
    print(f"  Existing member prices cached: {len(member_prices)}")

    # Extract unique court_id+shift combos from existing court_prices
    # Keys are like "15020_day", "15020_primetime" etc.
    combos = {}
    for key in court_prices:
        parts = key.rsplit("_", 1)
        if len(parts) == 2:
            court_id, shift = parts
            combos.setdefault(court_id, set()).add(shift)

    print(f"  Court/shift combos to fetch: {sum(len(v) for v in combos.values())}")

    # Use tomorrow as target date
    target_date = date.today() + timedelta(days=1)
    shift_times = {"lowtime": 8 * 3600, "day": 11 * 3600, "primetime": 18 * 3600}

    for court_id, shifts in combos.items():
        for shift in shifts:
            cache_key = f"{court_id}_{shift}"
            if cache_key in member_prices:
                print(f"    {cache_key}: already cached (${member_prices[cache_key]})")
                continue
            target_sec = shift_times.get(shift, 11 * 3600)
            price = await get_member_price(
                cookies, int(court_id), pbp_user_id,
                venue["slug"], target_date, target_sec
            )
            if price is not None:
                member_prices[cache_key] = price
            await asyncio.sleep(0.4)

    return member_prices

async def main():
    print(f"Fetching member court prices for {len(MEMBER_VENUES)} venue(s)...")

    async with httpx.AsyncClient() as sb_client:
        esta_cookies, esta_pbp_id = await get_credentials(ESTA_SUPABASE_USER_ID, sb_client)
    print(f"Esta PBP user_id: {esta_pbp_id}, cookies: {len(esta_cookies)}\n")

    for venue in MEMBER_VENUES:
        print(f"── {venue['name']} (facility_id={venue['facility_id']}) ──")
        member_prices = await fetch_member_prices_for_venue(venue, esta_cookies, esta_pbp_id)
        print(f"  Total member prices: {len(member_prices)}")
        print(f"  Prices: {json.dumps(member_prices, indent=2)}")

        if not member_prices:
            print("  No prices fetched — skipping update")
            continue

        # Update Supabase — only update member_court_prices field
        async with httpx.AsyncClient() as client:
            # Get current data first to avoid overwriting other fields
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/availability_cache",
                params={"id": f"eq.pbp-{venue['facility_id']}", "select": "data"},
                headers=SB_HEADERS,
            )
            current_data = r.json()[0]["data"]
            current_data["member_court_prices"] = member_prices

            r2 = await client.patch(
                f"{SUPABASE_URL}/rest/v1/availability_cache",
                params={"id": f"eq.pbp-{venue['facility_id']}"},
                headers=SB_HEADERS,
                json={"data": current_data},
            )
            if r2.status_code in (200, 204):
                print(f"  ✅ Saved {len(member_prices)} member prices to Supabase")
            else:
                print(f"  ❌ Supabase update failed: {r2.status_code} {r2.text[:100]}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
