"""
fetch_court_blocks.py — Fetch court availability from PBP and push to Supabase.
Run via GitHub Actions every 15 min for today/tomorrow, every 60 min for days 3-7.
Prices are fetched once per court/shift and cached in Supabase — not re-fetched every run.
"""
import asyncio
import json
import os
from datetime import date, timedelta, datetime, timezone
from zoneinfo import ZoneInfo

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
PBP_COOKIES_JSON = os.environ["PBP_COOKIES_JSON"]
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "2"))
DAYS_START = int(os.environ.get("DAYS_START", "0"))
# How old a cached price can get before we refetch it, instead of trusting it
# forever. Catches rate changes and promos starting/ending automatically.
PRICE_REFRESH_HOURS = int(os.environ.get("PRICE_REFRESH_HOURS", "24"))

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
    1770: "rayapickleballclub",
    1783: "PICKLE4REAL",
    1883: "TheJarHQ",
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
    1770: "Raya Pickleball Club",
    1783: "Pickle4Real",
    1883: "The Jar HQ | Maidstone",
}

# Venues that use non-pickleball surface names for court hire
# PBP_SLUG_MAP and VENUE_NAMES above are superseded by venue_registry and
# have no remaining consumers here. Kept until the frontend migrates too,
# then deleted. Do not add to them.

def _venue_name(facility_id) -> str:
    import venue_registry
    v = venue_registry.resolve(facility_id)
    return v.name if isinstance(v, venue_registry.Venue) else str(facility_id)


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
    """
    DEPRECATED — was a hardcoded 12pm/5pm guess applied to every venue,
    which is wrong for venues whose real shift boundaries differ (e.g.
    SportsWell's weekday primetime actually starts at 4pm, not 5pm).
    PBP returns the real shift directly on each available_hours slot now
    (see fetch_blocks_for_surface), so this is no longer used for pricing.
    Kept only as a last-resort fallback if PBP ever omits the shift field.
    """
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


async def fetch_blocks_for_surface(api, facility_id: int, target_date: date, surface: str) -> tuple:
    """Fetch court_slots for one surface type. Returns ({court_key: [secs]}, {sec: real_pbp_shift})."""
    court_slots = {}
    sec_shift_map = {}
    try:
        hours_data = await api.available_hours(facility_id, target_date, surface=surface)
        all_slots = (hours_data or {}).get("available_hours", []) if isinstance(hours_data, dict) else []

        valid_secs = []
        for s in all_slots:
            if not (isinstance(s, dict) and s.get("available")
                    and isinstance(s.get("seconds_from_midnight"), (int, float))):
                continue
            sec = int(s["seconds_from_midnight"])
            valid_secs.append(sec)
            # PBP tells us the real shift for this slot directly — no guessing needed.
            # But PBP can reuse the SAME shift label (e.g. "primetime") for both
            # weekday and weekend even when the real price genuinely differs
            # (e.g. a weekend special). Tag with weekday/weekend so those don't
            # collide under one cache key.
            real_shift = s.get("shift")
            if real_shift:
                if target_date.weekday() >= 5:
                    real_shift = f"{real_shift}_weekend"
                sec_shift_map[sec] = real_shift

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
    return court_slots, sec_shift_map


def court_slots_to_blocks(court_slots: dict, sec_shift_map: dict = None) -> list:
    # TODO(known issue, logged 2026-07-16): this assumes every venue books in
    # 30-min increments (merge check is s == run_end + 1800). Some venues
    # (e.g. SportsWell/885) actually book hourly, so genuinely consecutive
    # available hours never merge into a >=60min block and get silently
    # dropped. Needs the adjacency step to be detected per-venue rather than
    # hardcoded. Deferred — not fixed in this pass, shift-guessing fix only.
    """Convert {court_key: [secs]} to list of bookable blocks >= 60 min.

    Runs are only merged while consecutive slots share the SAME real PBP shift —
    a run is broken at a shift change (e.g. lowtime -> primetime) even if the
    slots are otherwise back-to-back, since a merged block spanning two shifts
    can't be priced correctly with a single cache key. Each resulting block
    carries the real shift it belongs to.
    """
    sec_shift_map = sec_shift_map or {}
    blocks = []
    for court_key, secs in court_slots.items():
        parts = court_key.split("|", 1)
        court_id = parts[0]
        cname = parts[1] if len(parts) > 1 else court_key
        secs_sorted = sorted(set(secs))
        run_start = run_end = None
        run_shift = None

        def flush(rs, re_, shift):
            dur = (re_ - rs) // 60 + 30
            if dur >= 60:
                blocks.append({
                    "court": cname,
                    "court_id": court_id,
                    "start": sec_to_hhmm(rs),
                    "end": sec_to_hhmm(re_ + 1800),
                    "start_sec": rs,
                    "duration_min": dur,
                    "shift": shift,
                })

        for s in secs_sorted:
            s_shift = sec_shift_map.get(s)
            if run_start is None:
                run_start = run_end = s
                run_shift = s_shift
            elif s == run_end + 1800 and s_shift == run_shift:
                run_end = s
            else:
                flush(run_start, run_end, run_shift)
                run_start = run_end = s
                run_shift = s_shift
        if run_start is not None:
            flush(run_start, run_end, run_shift)
    return blocks


async def fetch_missing_prices(api, blocks: list, target_date: date, user_id: int, existing_prices: dict, existing_fetched_at: dict = None) -> tuple:
    """
    Fetch prices for court/shift combos that are missing OR stale (older than
    PRICE_REFRESH_HOURS). Cache key is court_id + PBP's REAL shift for that
    block (attached in court_slots_to_blocks from the available_hours response).
    Returns (updated_prices, updated_fetched_at).
    """
    new_prices = dict(existing_prices)
    new_fetched_at = dict(existing_fetched_at or {})
    now = datetime.utcnow()

    for block in blocks:
        court_id = block.get("court_id")
        start_sec = block.get("start_sec")
        if not court_id or start_sec is None:
            continue
        # Real PBP shift if we have it; last-resort fallback to the old guess
        # only if PBP omitted the shift field for this slot (shouldn't normally happen).
        shift = block.get("shift") or get_shift(start_sec, target_date)
        cache_key = f"{court_id}_{shift}"

        if cache_key in new_prices:
            last_fetched_str = new_fetched_at.get(cache_key)
            if last_fetched_str:
                try:
                    age_hours = (now - datetime.fromisoformat(last_fetched_str)).total_seconds() / 3600
                    if age_hours < PRICE_REFRESH_HOURS:
                        continue  # still fresh, skip
                except Exception:
                    pass  # unparseable timestamp -- treat as stale, refetch below
            else:
                # No timestamp recorded (pre-existing entry from before this
                # feature) -- treat as stale so it gets verified once.
                pass

        try:
            price_data = await api.court_price(
                int(court_id), target_date, start_sec, start_sec + 3600, user_id=user_id
            )
            fare = (price_data or {}).get("total", {}).get("original_reservation_fare")
            price = round(float(fare), 2) if fare is not None else None
            new_prices[cache_key] = price
            new_fetched_at[cache_key] = now.isoformat()
            print(f"    Fetched price {cache_key}: ${price}")
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"    price error {cache_key}: {e}")
            new_prices[cache_key] = None
            # Don't stamp fetched_at on failure -- retry next run instead of
            # waiting a full PRICE_REFRESH_HOURS cycle for a transient error.

    return new_prices, new_fetched_at


# The canonical stored block. Every writer of availability_cache.by_date must
# produce this shape, so a reader never has to know which job wrote a row.
CANONICAL_BLOCK_FIELDS = ("court", "court_id", "start", "end", "duration_min",
                          "price", "shift")


def prune_past_dates(by_date: dict, today_str: str) -> dict:
    """
    Drop by_date keys strictly before today. Returns a new dict.

    Filtered against TODAY, never against the writer's own scrape window.
    Three jobs write this key -- today+1 at :00, days 3-8 at :05, days 8-14 at
    :07 -- so a writer pruning to its own range would delete the other jobs'
    coverage every quarter hour and they would restore it minutes later. That
    churn would be worse than the leak it fixes.

    Only court blocks are pruned. sessions and other historical venue data are
    untouched: nothing reads a past date's court_blocks (the one caller that
    passes a past date, the roster lookup in MySessionsPage, reads
    venueData.sessions), but that is not true of the rest of the record.

    Malformed keys are kept rather than dropped -- deleting something we
    cannot parse is the wrong default when the alternative is a little unused
    data.
    """
    kept = {}
    for date_str, blocks in (by_date or {}).items():
        try:
            if str(date_str) >= today_str:      # ISO dates sort lexically
                kept[date_str] = blocks
        except Exception:
            kept[date_str] = blocks
    return kept


def apply_prices_to_blocks(blocks: list, court_prices: dict) -> list:
    """
    Map stored prices onto blocks using each block's real PBP shift.

    Preserves court_id and shift. This function previously rebuilt each block
    from five fields, reading court_id and shift to look up the price and then
    dropping both -- so the scraper computed the identity correctly and
    discarded it in the last step before saving. Every stored block therefore
    identified its court by NAME only, which is what forced the servers to
    re-resolve names against a live availability call, and what made the
    frontend fall back to the name contract for every near-term date.

    shift is kept for the same reason: a reader that has the shift can price a
    block itself instead of trusting a price frozen at scrape time.
    """
    result = []
    for block in blocks:
        court_id = block.get("court_id")
        shift = block.get("shift")
        price = court_prices.get(f"{court_id}_{shift}") if court_id and shift else None

        result.append({
            "court": block["court"],
            "court_id": court_id,
            "start": block["start"],
            "end": block["end"],
            "duration_min": block["duration_min"],
            "price": price,
            "shift": shift,
        })
    return result


async def fetch_court_blocks_for_venue(api, facility_id: int, target_date: date, user_id: int, existing_prices: dict, existing_fetched_at: dict = None) -> tuple:
    """
    Fetch available court blocks for one venue on one date.
    Returns (blocks_with_prices, updated_court_prices, updated_fetched_at).
    """
    try:
        # Surfaces come from the reviewed classification, not from
        # VENUE_SURFACES (3 of 21 venues) or the "pickle" name heuristic.
        # That heuristic took the FIRST surface whose name contained
        # "pickle", so it missed Dink & Drive's championship_courts,
        # Pickleholic's drill_skill_court and Picklezone's training_court --
        # while it would have accepted a futsal surface had one been named
        # differently. Unfiltered on purpose: kind=reservation hides real
        # court surfaces.
        import court_surfaces
        ct = await api.court_types(facility_id, kind=None)
        res = court_surfaces.resolve_surfaces(facility_id, ct or [])
        if res.unknown:
            print(f"  !! {res.diagnostic()}")
        if not res.court:
            # No classified court surface. Raising beats scraping nothing
            # quietly: an empty result here would look exactly like a venue
            # with no availability, which is how The Rally stayed broken.
            raise RuntimeError(
                f"facility {facility_id}: no court surfaces "
                f"(non_court={res.non_court}, alternate={res.alternate}, "
                f"unknown={res.unknown})")
        surfaces = res.court

        # Constrain to the authoritative inventory. available_courts is a
        # availability view and can return resources that /courts does not
        # list as active courts -- a live cycle surfaced court 16455 at The
        # Rally, which is absent from that facility's inventory entirely.
        # Without this intersection the scraper caches blocks and prices for
        # courts the booking server will refuse, so browse would offer a
        # court that cannot be booked.
        import court_inventory
        inv = court_inventory.build_inventory(
            facility_id, ct or [], await api.courts(facility_id))
        valid_ids = {str(c.id) for c in inv.courts}

        combined_slots: dict = {}
        combined_shift_map: dict = {}
        for surface in surfaces:
            slots, sec_shift_map = await fetch_blocks_for_surface(api, facility_id, target_date, surface)
            for k, v in slots.items():
                combined_slots.setdefault(k, []).extend(v)
            combined_shift_map.update(sec_shift_map)
            await asyncio.sleep(0.5)

        blocks = court_slots_to_blocks(combined_slots, combined_shift_map)
        before = len(blocks)
        blocks = [b for b in blocks if str(b.get("court_id")) in valid_ids]
        if len(blocks) != before:
            dropped = {str(b.get("court_id")) for b in
                       court_slots_to_blocks(combined_slots, combined_shift_map)
                       if str(b.get("court_id")) not in valid_ids}
            print(f"  !! facility {facility_id}: dropped {before - len(blocks)} "
                  f"block(s) for court ids {sorted(dropped)} -- present in "
                  f"available_courts but not in the facility inventory")
        updated_prices, updated_fetched_at = await fetch_missing_prices(
            api, blocks, target_date, user_id, existing_prices, existing_fetched_at
        )
        blocks_with_prices = apply_prices_to_blocks(blocks, updated_prices)
        return blocks_with_prices, updated_prices, updated_fetched_at

    except Exception as e:
        print(f"  Error fetching {facility_id} for {target_date}: {e}")
        return [], existing_prices, (existing_fetched_at or {})


async def main():
    from extract_thejar import PlayByPointAPI
    import venue_registry
    import httpx

    cookie_data = json.loads(PBP_COOKIES_JSON)
    cookies = cookie_data["cookies"]
    user_id = cookie_data["user_id"]

    today = datetime.now(ZoneInfo('Australia/Melbourne')).date()
    dates = [today + timedelta(days=i) for i in range(DAYS_START, DAYS_AHEAD)]

    targets = venue_registry.venues_for_fetcher("court_blocks")
    print(f"Fetching court blocks for {len(dates)} dates x {len(targets)} venues "
          f"(registry fetcher=court_blocks)...")

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

    # Venue selection comes from the registry, which is also what decides
    # this scraper is responsible for them. SportsWell, Raya and Pickle4Real
    # carry fetcher='sportswell' and are handled by fetch_sportswell.py, so
    # they are correctly absent here rather than accidentally missing.
    for venue in venue_registry.venues_for_fetcher("court_blocks"):
        fid, slug, name = venue.facility_id, venue.slug, venue.name
        results_by_venue[fid] = {"by_date": {}, "court_prices": {}, "court_prices_fetched_at": {}}

        existing_record = records_by_fid.get(fid, {})
        existing_data = existing_record.get("data", {})
        existing_prices = existing_data.get("court_prices", {})
        existing_fetched_at = existing_data.get("court_prices_fetched_at", {})

        # Start from what is already cached. On failure this venue is left
        # untouched rather than written with empty results -- see the
        # `ok` flag below.
        results_by_venue[fid]["court_prices"] = existing_prices.copy()
        results_by_venue[fid]["court_prices_fetched_at"] = existing_fetched_at.copy()
        results_by_venue[fid]["ok"] = False
        results_by_venue[fid]["error"] = None
        results_by_venue[fid]["dates_ok"] = 0

        try:
            async with PlayByPointAPI(cookies=cookies, club_slug=slug) as api:
                api._user_id = user_id
                updated_prices = existing_prices.copy()
                updated_fetched_at = existing_fetched_at.copy()
                for target_date in dates:
                    date_str = target_date.isoformat()
                    blocks, updated_prices, updated_fetched_at = await fetch_court_blocks_for_venue(
                        api, fid, target_date, user_id, updated_prices, updated_fetched_at
                    )
                    results_by_venue[fid]["by_date"][date_str] = blocks
                    results_by_venue[fid]["court_prices"] = updated_prices
                    results_by_venue[fid]["court_prices_fetched_at"] = updated_fetched_at
                    results_by_venue[fid]["dates_ok"] += 1
                    print(f"  {name} {date_str}: {len(blocks)} blocks")
                    await asyncio.sleep(1)
                results_by_venue[fid]["ok"] = True
        except Exception as e:
            results_by_venue[fid]["error"] = str(e)
            print(f"  {name} FAILED: {e}")
        await asyncio.sleep(2)

    # Push to Supabase
    async with httpx.AsyncClient() as client:
        async def patch_venue(record):
            row_id = record["id"]
            data = record["data"]
            fid = data.get("id")
            if fid not in results_by_venue:
                return
            result = results_by_venue[fid]

            # A failed fetch must not overwrite good data. Previously
            # court_prices was written unconditionally from a dict that
            # stayed empty when the venue raised, so one transient failure
            # wiped that venue's cached prices entirely.
            if not result["ok"]:
                data["fetch_status"] = {
                    "state": "failed",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "error": result["error"],
                    "dates_ok": result["dates_ok"],
                }
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/availability_cache",
                    params={"id": f"eq.{row_id}"},
                    headers=headers,
                    json={"data": data},
                )
                print(f"  NOT SAVED {data.get('name', row_id)}: fetch failed, "
                      f"existing cache left intact")
                return

            # Prune before merging, so a date this writer is about to write is
            # never dropped by its own prune.
            by_date = prune_past_dates(data.get("by_date", {}),
                                       date.today().isoformat())
            for date_str, blocks in result["by_date"].items():
                by_date[date_str] = blocks
            data["by_date"] = by_date
            data["court_prices"] = result["court_prices"]
            data["court_prices_fetched_at"] = result["court_prices_fetched_at"]

            total = sum(len(v) for v in by_date.values())
            # "Fetched successfully and found nothing" and "could not fetch"
            # are different facts. A timestamp alone cannot tell them apart,
            # which is why SportsWell reading blocks=0 / error=None was
            # operationally ambiguous.
            data["fetch_status"] = {
                "state": "ok" if total else "ok_empty",
                "at": datetime.now(timezone.utc).isoformat(),
                "error": None,
                "dates_ok": result["dates_ok"],
                "blocks": total,
            }
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/availability_cache",
                params={"id": f"eq.{row_id}"},
                headers=headers,
                json={"data": data},
            )
            n_prices = len(result["court_prices"])
            print(f"  Saved {data.get('name', row_id)}: {total} total blocks, "
                  f"{n_prices} prices cached"
                  + ("  [ZERO BLOCKS -- fetch succeeded but found nothing]"
                     if not total else ""))

        await asyncio.gather(*[patch_venue(record) for record in records])

    # Per-cycle summary. Warming was previously silent on success, so a venue
    # that stopped producing blocks surfaced as a user reporting empty
    # availability rather than as anything in a log.
    ok = [f for f, r in results_by_venue.items() if r["ok"] and r["by_date"]]
    empty = [f for f in ok if not sum(len(v) for v in results_by_venue[f]["by_date"].values())]
    failed = [f for f, r in results_by_venue.items() if not r["ok"]]
    print()
    print("=" * 60)
    print(f"CYCLE SUMMARY  attempted={len(results_by_venue)}  "
          f"ok={len(ok) - len(empty)}  ok_but_empty={len(empty)}  failed={len(failed)}")
    for fid in empty:
        print(f"  EMPTY  {fid} {_venue_name(fid)} "
              f"-- fetched cleanly, zero blocks")
    for fid in failed:
        print(f"  FAILED {fid} {_venue_name(fid)} "
              f"-- {results_by_venue[fid]['error']}")
    print("=" * 60)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
