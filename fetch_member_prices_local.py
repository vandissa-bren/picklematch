"""
fetch_member_prices_local.py
Run on 170.64.187.117: python3 /app/fetch_member_prices_local.py
Uses PlayByPointAPI (headless Chromium) to bypass Cloudflare.
Fetches D&D member prices using Esta's credentials and saves to Supabase.
"""
import asyncio
import json
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, '/app')
from extract_thejar import PlayByPointAPI
import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Load from .env if not set
if not SUPABASE_URL:
    SUPABASE_URL = "https://stwohmddmdwttasbyblt.supabase.co"
if not SUPABASE_KEY:
    for line in open('/app/.env'):
        line = line.strip()
        if line.startswith('SUPABASE_SERVICE_KEY=') or line.startswith('SUPABASE_KEY='):
            SUPABASE_KEY = line.split('=', 1)[1]

ESTA_SUPABASE_USER_ID = 'f2338f3c-2cb2-444d-a134-47139c17a769'

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

FACILITY_ID = 1557
SLUG = "dinkndrivepickleballclub"

def get_shift(sec: int) -> str:
    hour = sec // 3600
    if hour >= 17: return "primetime"
    elif hour >= 12: return "day"
    return "lowtime"


async def get_esta_credentials():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/pbp_credentials",
            params={"user_id": f"eq.{ESTA_SUPABASE_USER_ID}", "select": "pbp_cookies,pbp_user_id"},
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


async def main():
    print("Fetching Esta's credentials...")
    esta_cookies, esta_pbp_id = await get_esta_credentials()
    print(f"Esta PBP user_id: {esta_pbp_id}, cookies: {len(esta_cookies)}")

    # Load existing court_prices from Supabase to get known court IDs
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/availability_cache",
            params={"id": f"eq.pbp-{FACILITY_ID}", "select": "data"},
            headers=SB_HEADERS,
        )
        existing_data = r.json()[0]["data"]

    court_prices = existing_data.get("court_prices", {})
    member_prices = dict(existing_data.get("member_court_prices", {}))
    print(f"Known court/shift combos: {len(court_prices)}")
    print(f"Existing member prices: {len(member_prices)}")

    # Extract unique court_ids and shifts
    combos = {}
    for key in court_prices:
        parts = key.rsplit("_", 1)
        if len(parts) == 2:
            court_id, shift = parts
            combos.setdefault(court_id, set()).add(shift)

    shift_times = {"lowtime": 8 * 3600, "day": 11 * 3600, "primetime": 18 * 3600}
    target_date = date.today() + timedelta(days=1)

    print(f"\nFetching member prices for {sum(len(v) for v in combos.items())} combos on {target_date}...")

    async with PlayByPointAPI(cookies=esta_cookies, club_slug=SLUG) as api:
        api._user_id = esta_pbp_id
        for court_id, shifts in combos.items():
            for shift in shifts:
                cache_key = f"{court_id}_{shift}"
                if cache_key in member_prices:
                    print(f"  {cache_key}: already cached (${member_prices[cache_key]})")
                    continue
                target_sec = shift_times.get(shift, 11 * 3600)
                try:
                    price_data = await api.court_price(
                        int(court_id), target_date, target_sec, target_sec + 3600,
                        user_id=esta_pbp_id
                    )
                    fare = (price_data or {}).get("total", {}).get("original_reservation_fare")
                    price = round(float(fare), 2) if fare is not None else None
                    # Check affiliation
                    affiliation = None
                    try:
                        affiliation = price_data["prices_per_user"][0]["price"]["affiliation"]
                    except (KeyError, IndexError, TypeError):
                        pass
                    print(f"  {cache_key}: ${price} ({affiliation})")
                    if price is not None:
                        member_prices[cache_key] = price
                except Exception as e:
                    print(f"  {cache_key}: error — {e}")
                await asyncio.sleep(0.4)

    print(f"\nTotal member prices fetched: {len(member_prices)}")
    print(json.dumps(member_prices, indent=2))

    if not member_prices:
        print("Nothing to save.")
        return

    # Save to Supabase
    existing_data["member_court_prices"] = member_prices
    async with httpx.AsyncClient() as client:
        r = await client.patch(
            f"{SUPABASE_URL}/rest/v1/availability_cache",
            params={"id": f"eq.pbp-{FACILITY_ID}"},
            headers=SB_HEADERS,
            json={"data": existing_data},
        )
        if r.status_code in (200, 204):
            print(f"✅ Saved {len(member_prices)} member prices to Supabase")
        else:
            print(f"❌ Supabase update failed: {r.status_code} {r.text[:100]}")


if __name__ == "__main__":
    asyncio.run(main())
