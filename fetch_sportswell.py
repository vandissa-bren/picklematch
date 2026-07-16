"""Fetch court blocks for geo-restricted venues and push to Supabase.

These venues (SportsWell, Raya, Pickle4Real) are geo-restricted by PBP and
can only be reliably fetched from an Australian IP -- this DO server, not
GitHub Actions runners. Hence a separate script from fetch_court_blocks.py.
"""
import asyncio, json, os, httpx
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

from extract_thejar import PlayByPointAPI

# URL is not sensitive (just a project ref); the service key is, so no
# hardcoded fallback for that one.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://stwohmddmdwttasbyblt.supabase.co")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

# How old a cached price can get before we refetch it. Matches
# fetch_court_blocks.py's PRICE_REFRESH_HOURS behaviour.
PRICE_REFRESH_HOURS = int(os.environ.get("PRICE_REFRESH_HOURS", "168"))

GEO_RESTRICTED = {885: "sportswellpickleballpalace", 1770: "rayapickleballclub", 1783: "PICKLE4REAL"}


def sec_to_hhmm(s): return f"{s//3600:02d}:{(s%3600)//60:02d}"


def get_shift(sec, target_date=None):
    """
    DEPRECATED -- last-resort fallback only, if PBP ever omits the real shift
    field for a slot. PBP's own shift label (captured in fetch_blocks_and_prices)
    is used everywhere else now, same as fetch_court_blocks.py.
    """
    hour = sec // 3600
    if hour >= 17: shift = "primetime"
    elif hour >= 12: shift = "day"
    else: shift = "lowtime"
    if target_date and target_date.weekday() >= 5:
        shift = f"{shift}_weekend"
    return shift


async def fetch_blocks_and_prices(api, fid, target, existing_prices, existing_fetched_at):
    """
    SportsWell-style venues use session-style available_hours (one block per
    hour) -- we deliberately build one block per available hour slot rather
    than merging consecutive hours, since these venues book hourly not in
    30-min increments (a different, still-open issue for the main pipeline).
    """
    blocks = []
    new_prices = dict(existing_prices)
    new_fetched_at = dict(existing_fetched_at)
    now = datetime.utcnow()

    try:
        h = await api.available_hours(fid, target, surface="pickleball")
        slots = (h or {}).get("available_hours", [])

        # Capture PBP's real shift label per slot, tagged with weekday/weekend
        # so a shared label (e.g. "primetime") on both a weekday and a
        # weekend with genuinely different prices doesn't collide.
        sec_shift_map = {}
        valid = []
        for s in slots:
            if not (s.get("available") and isinstance(s.get("seconds_from_midnight"), (int, float))):
                continue
            sec = int(s["seconds_from_midnight"])
            valid.append(sec)
            real_shift = s.get("shift")
            if real_shift:
                if target.weekday() >= 5:
                    real_shift = f"{real_shift}_weekend"
                sec_shift_map[sec] = real_shift

        def shift_for(sec):
            return sec_shift_map.get(sec) or get_shift(sec, target)

        # Sample one representative court per distinct real shift present today.
        sample_courts = {}  # {shift: court_id}
        distinct_shifts = set(shift_for(s) for s in valid)
        for shift in distinct_shifts:
            shift_sec = next((s for s in valid if shift_for(s) == shift), None)
            if shift_sec is None:
                continue
            try:
                courts = await api.available_courts(fid, target, shift_sec, shift_sec + 1800, surface="pickleball")
                if courts:
                    sample_courts[shift] = courts[0].get("id")
                await asyncio.sleep(0.2)
            except Exception:
                pass

        # Fetch missing or stale prices using sample courts.
        for shift, court_id in sample_courts.items():
            if not court_id:
                continue
            cache_key = f"{court_id}_{shift}"
            needs_fetch = cache_key not in new_prices
            if not needs_fetch:
                last = new_fetched_at.get(cache_key)
                if last:
                    try:
                        age_hours = (now - datetime.fromisoformat(last)).total_seconds() / 3600
                        needs_fetch = age_hours >= PRICE_REFRESH_HOURS
                    except Exception:
                        needs_fetch = True
                else:
                    needs_fetch = True  # pre-existing entry from before this feature
            if not needs_fetch:
                continue
            shift_sec = next((s for s in valid if shift_for(s) == shift), None)
            if shift_sec is None:
                continue
            try:
                price_data = await api.court_price(int(court_id), target, shift_sec, shift_sec + 3600, user_id=api._user_id)
                fare = (price_data or {}).get("total", {}).get("original_reservation_fare")
                new_prices[cache_key] = round(float(fare), 2) if fare is not None else None
                new_fetched_at[cache_key] = now.isoformat()
                print(f"    Price {shift}: ${new_prices[cache_key]}")
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"    Price error {shift}: {e}")

        # Build price lookup by shift (use first court_id per shift as representative).
        shift_price = {}
        for shift, court_id in sample_courts.items():
            cache_key = f"{court_id}_{shift}"
            if cache_key in new_prices:
                shift_price[shift] = new_prices[cache_key]

        # Build one block per court per available hour.
        for sec in valid:
            shift = shift_for(sec)
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
                    "shift": shift,
                    "pricingTier": shift.replace("_weekend", ""),
                })
            await asyncio.sleep(0.2)

    except Exception as e:
        print(f"  Error: {e}")

    return blocks, new_prices, new_fetched_at


async def main():
    d = json.loads(open("/app/.pbp_cookies.json").read())
    cookies, user_id = d["cookies"], d["user_id"]
    today = datetime.now(ZoneInfo('Australia/Melbourne')).date()
    dates = [today + timedelta(days=i) for i in range(14)]  # match main pipeline's 14-day coverage
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
            existing_fetched_at = data.get("court_prices_fetched_at", {})

            async with PlayByPointAPI(cookies=cookies, club_slug=slug) as api:
                api._user_id = user_id
                updated_prices = existing_prices.copy()
                updated_fetched_at = existing_fetched_at.copy()
                for target in dates:
                    blocks, updated_prices, updated_fetched_at = await fetch_blocks_and_prices(
                        api, fid, target, updated_prices, updated_fetched_at
                    )
                    by_date[target.isoformat()] = blocks
                    print(f"  {slug} {target}: {len(blocks)} blocks")
                    await asyncio.sleep(1)

            data["by_date"] = by_date
            data["court_prices"] = updated_prices
            data["court_prices_fetched_at"] = updated_fetched_at
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/availability_cache",
                params={"id": f"eq.pbp-{fid}"},
                headers=headers,
                json={"data": data},
            )
            print(f"Saved {slug} ({len(updated_prices)} prices cached)")

asyncio.run(main())
