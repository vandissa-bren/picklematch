"""
fetch_court_blocks.py — Fetch court availability from PBP and push to Supabase.
Run via GitHub Actions every 15 min for today/tomorrow, every 60 min for days 3-7.
Prices are fetched once per court/shift and cached in Supabase — not re-fetched every run.
"""
import asyncio
import json
import os
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
PBP_COOKIES_JSON = os.environ["PBP_COOKIES_JSON"]
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "2"))

PBP_SLUG_MAP = {
    597:  "nplpickleball",
    1009: "easternindoorpickleballclub",
    1379: "pickleholic",
    1355: "statepickleballcentre",
    1383: "MelbournePickleClub",
    1485: "picklehaus",
    755:  "leveluppickleballknoxcity",
    1584: "theroompickleball",
    1461: "therealdill",
    1532: "pickleplex",
    1557: "dinkndrivepickleballclub",
    1119: "swingandserve",
    1487: "Pickle-Playground",
    1664: "TheRallyPickleball",
    1714: "RunwayPickleball",
    1733: "pickleballpowerhouse",
    1696: "picklezone",
}

VENUE_NAMES = {
    597:  "The Jar | South Melbourne",
    1009: "Eastern Indoor Pickleball Club",
    1379: "PICKLEHOLIC",
    1355: "State Pickleball Centre",
    1383: "Melbourne Pickle Club",
    1485: "Pickle Haus",
    755:  "Level Up Pickleball Knox City",
    1584: "The Room Pickleball",
    1461: "The Real Dill | Ravenhall",
    1532: "PicklePlex",
    1557: "Dink & Drive Pickleball Club",
    1119: "Swing & Serve",
    1487: "Pickle Playground",
    1664: "The Rally Pickleball | Altona",
    1714: "Runway Pickleball",
    1733: "Pickleball Powerhouse",
    1696: "Picklezone",
}

# Venues that use non-pickleball surface names for court hire
VENUE_SURFACES = {
    885: ["pickleball"],
    1557: ["standard_courts", "championship_courts"],
    1379: ["main_courts"],
}


def sec_to_hhmm(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h:02d}:{m:02d}"


def get_shift(sec: int, target_date: date = None) -> str:
    """Derive pricing shift from time of day, with weekend suffix if applicable."""
    hour = sec // 3600
    if hour >= 17:
        shift = "primetime"
    elif hour >= 12:
        shift = "day"
    else:
        shift = "lowtime"
    if target_date and target_date.weekday() >= 5:
        shift = f"{shift}_weekend"
    return shift


async def fetch_blocks_for_surface(api, facility_id: int, target_date: date, surface: str) -> dict:
    """Fetch court_slots for one surface type. Returns {court_key: [secs]}"""
    court_slots = {}
    try:
        hours_data = await api.available_hours(facility_id, target_date, surface=surface)
        all_slots = (hours_data or {}).get("available_hours", []) if isinstance(hours_data, dict) else []

        valid_secs = [
            int(s["seconds_from_midnight"])
            for s in all_slots
            if isinstance(s, dict) and s.get("available")
            and isinstance(s.get("seconds_from_midnight"), (int, float))
        ]

        for sec in valid_secs:
            try:
                courts = await api.available_courts(facility_id, target_date, sec, sec + 1800, surface=surface)
                for court in (courts or []):
                    cid = court.get("id") or court.get("name") or "?"
                    cname = court.get("name") or str(cid)
                    key = f"{cid}|{cname}"
                    court_slots.setdefault(key, []).append(sec)
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"    slot {sec_to_hhmm(sec)} error: {e}")
    except Exception as e:
        print(f"    surface {surface} error: {e}")
    return court_slots


def court_slots_to_blocks(court_slots: dict) -> list:
    """Convert {court_key: [secs]} to list of bookable blocks >= 60 min."""
    blocks = []
    for court_key, secs in court_slots.items():
        parts = court_key.split("|", 1)
        court_id = parts[0]
        cname = parts[1] if len(parts) > 1 else court_key
        secs_sorted = sorted(set(secs))
        run_start = run_end = None
        for s in secs_sorted:
            if run_start is None:
                run_start = run_end = s
            elif s == run_end + 1800:
                run_end = s
            else:
                dur = (run_end - run_start) // 60 + 30
                if dur >= 60:
                    blocks.append({
                        "court": cname,
                        "court_id": court_id,
                        "start": sec_to_hhmm(run_start),
                        "end": sec_to_hhmm(run_end + 1800),
                        "start_sec": run_start,
                        "duration_min": dur,
                    })
                run_start = run_end = s
        if run_start is not None:
            dur = (run_end - run_start) // 60 + 30
            if dur >= 60:
                blocks.append({
                    "court": cname,
                    "court_id": court_id,
                    "start": sec_to_hhmm(run_start),
                    "end": sec_to_hhmm(run_end + 1800),
                    "start_sec": run_start,
                    "duration_min": dur,
                })
    return blocks


async def fetch_missing_prices(api, blocks: list, target_date: date, user_id: int, existing_prices: dict) -> tuple[dict, dict]:
    """
    Fetch prices only for court/shift combos not already in existing_prices.
    Uses PBP's actual shift name as cache key, stores a mapping of
    derived_shift -> pbp_shift for use in apply_prices_to_blocks.
    Returns (updated_prices, shift_map) where shift_map is {court_id: {derived_shift: pbp_shift}}
    """
    new_prices = dict(existing_prices)
    shift_map = {}  # {court_id: {derived_shift: pbp_shift}}

    for block in blocks:
        court_id = block.get("court_id")
        start_sec = block.get("start_sec")
        if not court_id or start_sec is None:
            continue
        shift = get_shift(start_sec, target_date)
        cache_key = f"{court_id}_{shift}"

        # Check if we already have price under derived key or any existing key for this court
        if cache_key in new_prices:
            continue
        # Also skip if we already fetched PBP shift for this court/time
        if court_id in shift_map and shift in shift_map[court_id]:
            pbp_shift = shift_map[court_id][shift]
            if f"{court_id}_{pbp_shift}" in new_prices:
                continue

        try:
            price_data = await api.court_price(
                int(court_id), target_date, start_sec, start_sec + 3600, user_id=user_id
            )
            fare = (price_data or {}).get("total", {}).get("original_reservation_fare")
            price = round(float(fare), 2) if fare is not None else None

            # Use PBP's actual shift name if available
            try:
                pbp_shift = price_data["prices_per_user"][0]["price"]["shift_prices"][0]["shift"]
                pbp_key = f"{court_id}_{pbp_shift}"
                new_prices[pbp_key] = price
                # Also store under derived key so lookup works
                new_prices[cache_key] = price
                # Record mapping for this court
                shift_map.setdefault(court_id, {})[shift] = pbp_shift
                print(f"    Fetched price {pbp_key} (pbp shift '{pbp_shift}'): ${price}")
            except (KeyError, IndexError, TypeError):
                new_prices[cache_key] = price
                print(f"    Fetched price {cache_key}: ${price}")

            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"    price error {cache_key}: {e}")
            new_prices[cache_key] = None

    return new_prices, shift_map


def apply_prices_to_blocks(blocks: list, court_prices: dict, shift_map: dict, target_date: date = None) -> list:
    """
    Map stored prices onto blocks.
    Tries PBP shift key first, falls back to derived shift key.
    """
    result = []
    for block in blocks:
        court_id = block.get("court_id")
        start_sec = block.get("start_sec")
        shift = get_shift(start_sec, target_date) if start_sec is not None else None
        price = None

        if court_id and shift:
            # Try PBP shift name first
            pbp_shift = (shift_map.get(court_id) or {}).get(shift)
            if pbp_shift:
                price = court_prices.get(f"{court_id}_{pbp_shift}")
            # Fall back to derived shift key
            if price is None:
                price = court_prices.get(f"{court_id}_{shift}")

        result.append({
            "court": block["court"],
            "start": block["start"],
            "end": block["end"],
            "duration_min": block["duration_min"],
            "price": price,
        })
    return result


async def fetch_court_blocks_for_venue(api, facility_id: int, target_date: date, user_id: int, existing_prices: dict, existing_shift_map: dict) -> tuple:
    """
    Fetch available court blocks for one venue on one date.
    Returns (blocks_with_prices, updated_court_prices, updated_shift_map).
    """
    try:
        if facility_id in VENUE_SURFACES:
            surfaces = VENUE_SURFACES[facility_id]
        else:
            surface = "pickleball"
            try:
                ct = await api.court_types(facility_id)
                ps = [s for s in (ct or []) if "pickle" in (s.get("surface") or "").lower()]
                if ps:
                    surface = ps[0]["surface"]
            except Exception:
                pass
            surfaces = [surface]

        combined_slots: dict = {}
        for surface in surfaces:
            slots = await fetch_blocks_for_surface(api, facility_id, target_date, surface)
            for k, v in slots.items():
                combined_slots.setdefault(k, []).extend(v)
            await asyncio.sleep(0.5)

        blocks = court_slots_to_blocks(combined_slots)
        updated_prices, new_shift_map = await fetch_missing_prices(api, blocks, target_date, user_id, existing_prices)

        # Merge shift maps
        for court_id, mapping in new_shift_map.items():
            existing_shift_map.setdefault(court_id, {}).update(mapping)

        blocks_with_prices = apply_prices_to_blocks(blocks, updated_prices, existing_shift_map, target_date)
        return blocks_with_prices, updated_prices, existing_shift_map

    except Exception as e:
        print(f"  Error fetching {facility_id} for {target_date}: {e}")
        return [], existing_prices, existing_shift_map


async def main():
    from extract_thejar import PlayByPointAPI
    import httpx

    cookie_data = json.loads(PBP_COOKIES_JSON)
    cookies = cookie_data["cookies"]
    user_id = cookie_data["user_id"]

    today = datetime.now(ZoneInfo('Australia/Melbourne')).date()
    dates = [today + timedelta(days=i) for i in range(DAYS_AHEAD)]

    print(f"Fetching court blocks for {len(dates)} dates x {len(PBP_SLUG_MAP)} venues...")

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/availability_cache",
            params={"platform": "eq.playbypoint", "select": "id,data"},
            headers=headers,
        )
        records = resp.json()

    records_by_fid = {}
    for record in records:
        fid = record["data"].get("id")
        if fid:
            records_by_fid[fid] = record

    results_by_venue = {}

    for fid, slug in PBP_SLUG_MAP.items():
        name = VENUE_NAMES.get(fid, str(fid))
        results_by_venue[fid] = {"by_date": {}, "court_prices": {}, "shift_map": {}}

        existing_record = records_by_fid.get(fid, {})
        existing_data = existing_record.get("data", {})
        existing_prices = existing_data.get("court_prices", {})
        existing_shift_map = existing_data.get("shift_map", {})

        try:
            async with PlayByPointAPI(cookies=cookies, club_slug=slug) as api:
                api._user_id = user_id
                updated_prices = existing_prices.copy()
                updated_shift_map = existing_shift_map.copy()
                for target_date in dates:
                    date_str = target_date.isoformat()
                    blocks, updated_prices, updated_shift_map = await fetch_court_blocks_for_venue(
                        api, fid, target_date, user_id, updated_prices, updated_shift_map
                    )
                    results_by_venue[fid]["by_date"][date_str] = blocks
                    results_by_venue[fid]["court_prices"] = updated_prices
                    results_by_venue[fid]["shift_map"] = updated_shift_map
                    print(f"  {name} {date_str}: {len(blocks)} blocks")
                    await asyncio.sleep(1)
        except Exception as e:
            print(f"  {name} failed: {e}")
        await asyncio.sleep(2)

    # Push to Supabase
    async with httpx.AsyncClient() as client:
        async def patch_venue(record):
            row_id = record["id"]
            data = record["data"]
            fid = data.get("id")
            if fid not in results_by_venue:
                return
            by_date = data.get("by_date", {})
            for date_str, blocks in results_by_venue[fid]["by_date"].items():
                by_date[date_str] = blocks
            data["by_date"] = by_date
            data["court_prices"] = results_by_venue[fid]["court_prices"]
            data["shift_map"] = results_by_venue[fid]["shift_map"]
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/availability_cache",
                params={"id": f"eq.{row_id}"},
                headers=headers,
                json={"data": data},
            )
            total = sum(len(v) for v in by_date.values())
            n_prices = len(results_by_venue[fid]["court_prices"])
            print(f"  Saved {data.get('name', row_id)}: {total} total blocks, {n_prices} prices cached")

        await asyncio.gather(*[patch_venue(record) for record in records])

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
