"""Fetch court blocks for geo-restricted venues and push to Supabase."""
import asyncio, json, httpx
from datetime import date, datetime, timedelta
from extract_thejar import PlayByPointAPI
from datetime import datetime
from zoneinfo import ZoneInfo

SUPABASE_URL = "https://stwohmddmdwttasbyblt.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN0d29obWRkbWR3dHRhc2J5Ymx0Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3ODcyNDc5MywiZXhwIjoyMDk0MzAwNzkzfQ.zrsXJVxX4OZv0Eb5qycQF3_33NFyAFJfPlvK_xCzi-E"

GEO_RESTRICTED = {885: "sportswellpickleballpalace", 1770: "rayapickleballclub", 1783: "PICKLE4REAL"}

def sec_to_hhmm(s): return f"{s//3600:02d}:{(s%3600)//60:02d}"

def get_shift(sec):
    hour = sec // 3600
    if hour >= 17: return "primetime"
    elif hour >= 12: return "day"
    return "lowtime"

async def fetch_blocks_and_prices(api, fid, target, existing_prices):
    """
    SportsWell uses session-style available_hours (one block per hour).
    We build one block per available hour slot and fetch prices per shift.
    """
    blocks = []
    new_prices = dict(existing_prices)

    try:
        h = await api.available_hours(fid, target, surface="pickleball")
        slots = (h or {}).get("available_hours", [])
        valid = [
            int(s["seconds_from_midnight"])
            for s in slots
            if s.get("available") and isinstance(s.get("seconds_from_midnight"), (int, float))
        ]

        # Get court IDs once for price fetching (just need one representative court per shift)
        sample_courts = {}  # {shift: court_id}
        for sec in valid[:3]:  # sample first few slots
            try:
                courts = await api.available_courts(fid, target, sec, sec + 1800, surface="pickleball")
                if courts:
                    shift = get_shift(sec)
                    if shift not in sample_courts:
                        sample_courts[shift] = courts[0].get("id")
                await asyncio.sleep(0.2)
            except Exception:
                pass

        # Fetch missing prices using sample courts
        for shift, court_id in sample_courts.items():
            if not court_id:
                continue
            cache_key = f"{court_id}_{shift}"
            if cache_key in new_prices:
                continue
            # Find a sec for this shift
            shift_sec = next((s for s in valid if get_shift(s) == shift), None)
            if shift_sec is None:
                continue
            try:
                price_data = await api.court_price(int(court_id), target, shift_sec, shift_sec + 3600, user_id=api._user_id)
                fare = (price_data or {}).get("total", {}).get("original_reservation_fare")
                new_prices[cache_key] = round(float(fare), 2) if fare is not None else None
                print(f"    Price {shift}: ${new_prices[cache_key]}")
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"    Price error {shift}: {e}")

        # Build price lookup by shift (use first court_id per shift as representative)
        shift_price = {}
        for shift, court_id in sample_courts.items():
            cache_key = f"{court_id}_{shift}"
            if cache_key in new_prices:
                shift_price[shift] = new_prices[cache_key]

        # Build one block per court per available hour
        shift_to_tier = {"lowtime": "lowtime", "day": "day", "primetime": "primetime"}
        for sec in valid:
            shift = get_shift(sec)
            try:
                courts = await api.available_courts(fid, target, sec, sec + 1800, surface="pickleball")
            except Exception:
                courts = []
            if not courts:
                courts = [{"id": None, "name": "Court"}]
            for court in courts:
                cname = court.get("name") or "Court"
                cid = court.get("id")
                blocks.append({
                    "court": cname,
                    "court_id": str(cid) if cid else None,
                    "start": sec_to_hhmm(sec),
                    "end": sec_to_hhmm(sec + 3600),
                    "duration_min": 60,
                    "price": shift_price.get(shift),
                    "pricingTier": shift_to_tier.get(shift, "day"),
                })
            await asyncio.sleep(0.2)

    except Exception as e:
        print(f"  Error: {e}")

    return blocks, new_prices


async def main():
    d = json.loads(open("/app/.pbp_cookies.json").read())
    cookies, user_id = d["cookies"], d["user_id"]
    today = datetime.now(ZoneInfo('Australia/Melbourne')).date()
    dates = [today + timedelta(days=i) for i in range(7)]
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient() as client:
        for fid, slug in GEO_RESTRICTED.items():
            # Load existing data
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/availability_cache",
                params={"id": f"eq.pbp-{fid}", "select": "id,data"},
                headers=headers,
            )
            record = resp.json()[0]
            data = record["data"]
            by_date = data.get("by_date", {})
            existing_prices = data.get("court_prices", {})

            async with PlayByPointAPI(cookies=cookies, club_slug=slug) as api:
                api._user_id = user_id
                updated_prices = existing_prices.copy()
                for target in dates:
                    blocks, updated_prices = await fetch_blocks_and_prices(api, fid, target, updated_prices)
                    by_date[target.isoformat()] = blocks
                    print(f"  {slug} {target}: {len(blocks)} blocks")
                    await asyncio.sleep(1)

            data["by_date"] = by_date
            data["court_prices"] = updated_prices
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/availability_cache",
                params={"id": f"eq.pbp-{fid}"},
                headers=headers,
                json={"data": data},
            )
            print(f"Saved {slug} ({len(updated_prices)} prices cached)")

asyncio.run(main())
