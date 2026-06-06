"""
fetch_court_blocks.py — Fetch court availability from PBP and push to Supabase.
Run via GitHub Actions every 15 min for today/tomorrow, every 60 min for days 3-7.
"""
import asyncio
import json
import os
import sys
from datetime import date, timedelta

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
PBP_COOKIES_JSON = os.environ["PBP_COOKIES_JSON"]
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "2"))  # How many days to fetch (2=today+tomorrow, 7=all)

PBP_SLUG_MAP = {
    597:  "nplpickleball",
    885:  "sportswellpickleballpalace",
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
}

VENUE_NAMES = {
    597:  "The Jar | South Melbourne",
    885:  "SportsWell | Pickleball Palace",
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
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def sec_to_hhmm(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h:02d}:{m:02d}"


async def fetch_court_blocks_for_venue(api, facility_id: int, target_date: date) -> list:
    """Fetch available court blocks for one venue on one date."""
    blocks = []
    try:
        surface = "pickleball"
        try:
            ct = await api.court_types(facility_id)
            ps = [s for s in (ct or []) if "pickle" in (s.get("surface") or "").lower()]
            if ps:
                surface = ps[0]["surface"]
        except Exception:
            pass

        hours_data = await api.available_hours(facility_id, target_date, surface=surface)
        all_slots = (hours_data or {}).get("available_hours", []) if isinstance(hours_data, dict) else []

        valid_secs = [
            int(s["seconds_from_midnight"])
            for s in all_slots
            if isinstance(s, dict) and s.get("available")
            and isinstance(s.get("seconds_from_midnight"), (int, float))
        ]

        async def fetch_slot(sec):
            try:
                courts = await api.available_courts(facility_id, target_date, sec, sec + 1800, surface=surface)
                return sec, courts or []
            except Exception:
                return sec, []

        slot_results = await asyncio.gather(*[fetch_slot(s) for s in valid_secs])

        court_slots: dict[str, list[int]] = {}
        for sec, courts in slot_results:
            for court in courts:
                cid = court.get("id") or court.get("name") or "?"
                cname = court.get("name") or str(cid)
                key = f"{cid}|{cname}"
                court_slots.setdefault(key, []).append(sec)

        for court_key, secs in court_slots.items():
            cname = court_key.split("|", 1)[1]
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
                            "start": sec_to_hhmm(run_start),
                            "end": sec_to_hhmm(run_end + 1800),
                            "duration_min": dur,
                        })
                    run_start = run_end = s
            if run_start is not None:
                dur = (run_end - run_start) // 60 + 30
                if dur >= 60:
                    blocks.append({
                        "court": cname,
                        "start": sec_to_hhmm(run_start),
                        "end": sec_to_hhmm(run_end + 1800),
                        "duration_min": dur,
                    })
    except Exception as e:
        print(f"  Error fetching {facility_id} for {target_date}: {e}")

    return blocks


async def main():
    from extract_thejar import PlayByPointAPI
    import httpx

    # Parse cookies
    cookie_data = json.loads(PBP_COOKIES_JSON)
    cookies = cookie_data["cookies"]
    user_id = cookie_data["user_id"]

    today = date.today()
    dates = [today + timedelta(days=i) for i in range(DAYS_AHEAD)]

    print(f"Fetching court blocks for {len(dates)} dates × {len(PBP_SLUG_MAP)} venues...")

    # Fetch all venues for all dates
    results_by_venue = {}  # fid -> {date_str -> [blocks]}

    for fid, slug in PBP_SLUG_MAP.items():
        results_by_venue[fid] = {}
        async with PlayByPointAPI(cookies=cookies, club_slug=slug) as api:
            api._user_id = user_id
            for target_date in dates:
                date_str = target_date.isoformat()
                blocks = await fetch_court_blocks_for_venue(api, fid, target_date)
                results_by_venue[fid][date_str] = blocks
                print(f"  {VENUE_NAMES.get(fid, fid)} {date_str}: {len(blocks)} blocks")
                await asyncio.sleep(1)  # Small delay between dates

    # Push to Supabase — update by_date field only
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    async with httpx.AsyncClient() as client:
        # First read existing records
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/availability_cache",
            params={"platform": "eq.playbypoint", "select": "id,data"},
            headers=headers,
        )
        records = resp.json()

        for record in records:
            row_id = record["id"]
            data = record["data"]
            fid = data.get("id")
            if fid not in results_by_venue:
                continue

            # Update by_date with new blocks
            by_date = data.get("by_date", {})
            for date_str, blocks in results_by_venue[fid].items():
                by_date[date_str] = blocks
            data["by_date"] = by_date

            # Upsert back
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/availability_cache",
                params={"id": f"eq.{row_id}"},
                headers=headers,
                json={"data": data},
            )
            print(f"  Updated {data.get('name', row_id)} in Supabase")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
