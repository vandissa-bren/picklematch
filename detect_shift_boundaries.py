"""
detect_shift_boundaries.py — Probe PBP court pricing across a full day to
find the REAL shift transition times per venue, instead of guessing 12pm/5pm.

This is a diagnostic/calibration tool. It does NOT touch production data.
It prints a report and writes it to shift_boundary_report.json for review.

Usage:
    python3 detect_shift_boundaries.py                    # all venues
    python3 detect_shift_boundaries.py --venues 1557,885  # just these facility_ids
    python3 detect_shift_boundaries.py --step 60          # probe every 60 min (default 60)

Run this manually, once, from /app on the DO server (needs PBP_COOKIES_JSON env
var / the same credentials the other scripts use). Not intended to be scheduled.
"""
import asyncio
import argparse
import json
import os
from datetime import date, timedelta

from pathlib import Path
from extract_thejar import PlayByPointAPI, _load_cached_session


def load_session():
    """Mirrors api_server.py's _load_session_with_env_fallback (minus the
    in-memory runtime store, which only exists inside the live server process).
    Order: env var -> /app/.pbp_cookies.json -> browser session cache."""
    raw = os.environ.get("PBP_COOKIES_JSON", "")
    if raw:
        try:
            data = json.loads(raw)
            cookies = data.get("cookies", {})
            if cookies:
                return cookies, data.get("user_id", 0)
        except Exception as e:
            print(f"Failed to parse PBP_COOKIES_JSON: {e}")

    p = Path("/app/.pbp_cookies.json")
    if p.exists():
        try:
            data = json.loads(p.read_text())
            cookies = data.get("cookies", {})
            if cookies:
                import datetime as _dt
                mtime = _dt.datetime.fromtimestamp(p.stat().st_mtime)
                age_days = (_dt.datetime.now() - mtime).days
                print(f"Using cookies from {p} (last modified {mtime:%Y-%m-%d}, {age_days} days ago)")
                if age_days > 14:
                    print(f"  WARNING: this session is {age_days} days old and may be expired. "
                          f"If requests below fail with auth errors, that's why.")
                return cookies, data.get("user_id", 0)
        except Exception as e:
            print(f"Failed to read {p}: {e}")

    cookies, user_id, _email = _load_cached_session()
    if cookies:
        print("Using cookies from browser session cache (USER_DATA_DIR)")
        return cookies, user_id

    raise SystemExit(
        "No valid PBP session found (checked env var, .pbp_cookies.json, and "
        "browser cache). Cannot continue."
    )

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
    597:  "The Jar | South Melbourne", 1009: "Eastern Indoor Pickleball Club",
    1379: "PICKLEHOLIC", 1355: "State Pickleball Centre", 1383: "Melbourne Pickle Club",
    1485: "Pickle Haus", 755:  "Level Up Pickleball Knox City", 1584: "The Room Pickleball",
    1461: "The Real Dill | Ravenhall", 1532: "PicklePlex", 1557: "Dink & Drive Pickleball Club",
    1119: "Swing & Serve", 1487: "Pickle Playground", 1664: "The Rally Pickleball | Altona",
    1714: "Runway Pickleball", 1733: "Pickleball Powerhouse", 1696: "Picklezone",
    1770: "Raya Pickleball Club", 1783: "Pickle4Real", 1883: "The Jar HQ | Maidstone",
}

SLEEP_BETWEEN_CALLS = 0.5  # be gentle, matches existing scraper pacing
SLEEP_BETWEEN_VENUES = 2.0


def sec_to_hhmm(sec: int) -> str:
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h:02d}:{m:02d}"


def next_weekday(start: date) -> date:
    d = start
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def next_saturday(start: date) -> date:
    d = start
    while d.weekday() != 5:
        d += timedelta(days=1)
    return d


async def probe_day(api, facility_id, court_id, target_date, user_id, step_sec):
    """Probe court_price every `step_sec` across the operating day. Returns list of
    {start_sec, hhmm, fare, pbp_shift} sorted by time."""
    hours_data = await api.available_hours(facility_id, target_date)
    all_slots = (hours_data or {}).get("available_hours", []) if isinstance(hours_data, dict) else []
    valid_secs = sorted(set(
        int(s["seconds_from_midnight"]) for s in all_slots
        if isinstance(s, dict) and s.get("available")
        and isinstance(s.get("seconds_from_midnight"), (int, float))
    ))
    if not valid_secs:
        return []

    day_start, day_end = valid_secs[0], valid_secs[-1]
    probe_points = list(range(day_start, day_end + 1, step_sec))
    if probe_points[-1] != day_end:
        probe_points.append(day_end)

    results = []
    for sec in probe_points:
        try:
            price_data = await api.court_price(
                court_id, target_date, sec, sec + 1800, user_id=user_id
            )
            fare = (price_data or {}).get("total", {}).get("original_reservation_fare")
            pbp_shift = None
            try:
                pbp_shift = price_data["prices_per_user"][0]["price"]["shift_prices"][0]["shift"]
            except (KeyError, IndexError, TypeError):
                pass
            results.append({
                "start_sec": sec, "hhmm": sec_to_hhmm(sec),
                "fare": round(float(fare), 2) if fare is not None else None,
                "pbp_shift": pbp_shift,
            })
        except Exception as e:
            results.append({"start_sec": sec, "hhmm": sec_to_hhmm(sec), "fare": None, "pbp_shift": None, "error": str(e)})
        await asyncio.sleep(SLEEP_BETWEEN_CALLS)
    return results


def find_transitions(probes):
    """Given sorted probes, return list of {at, from, to} where fare or pbp_shift changes."""
    transitions = []
    prev = None
    for p in probes:
        key = (p["fare"], p["pbp_shift"])
        if prev is not None and key != prev[1] and p["fare"] is not None:
            transitions.append({
                "at": p["hhmm"],
                "from_fare": prev[1][0], "from_shift": prev[1][1],
                "to_fare": key[0], "to_shift": key[1],
            })
        if p["fare"] is not None:
            prev = (p["hhmm"], key)
    return transitions


async def pick_representative_court(api, facility_id, target_date):
    """Find any bookable court on target_date to use as the probe court."""
    hours_data = await api.available_hours(facility_id, target_date)
    all_slots = (hours_data or {}).get("available_hours", []) if isinstance(hours_data, dict) else []
    valid_secs = [int(s["seconds_from_midnight"]) for s in all_slots if isinstance(s, dict) and s.get("available")]
    if not valid_secs:
        return None
    mid = sorted(valid_secs)[len(valid_secs) // 2]
    courts = await api.available_courts(facility_id, target_date, mid, mid + 1800)
    if not courts:
        return None
    c = courts[0]
    return c.get("id"), (c.get("name") or str(c.get("id")))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", type=str, default="")
    parser.add_argument("--step", type=int, default=60, help="probe interval in minutes")
    args = parser.parse_args()

    step_sec = args.step * 60
    wanted = set(int(x) for x in args.venues.split(",") if x.strip()) if args.venues else None

    cookies, user_id = load_session()

    weekday_date = next_weekday(date.today() + timedelta(days=1))
    weekend_date = next_saturday(date.today())

    report = {}

    for fid, slug in PBP_SLUG_MAP.items():
        if wanted and fid not in wanted:
            continue
        name = VENUE_NAMES.get(fid, str(fid))
        print(f"\n=== {name} (facility_id={fid}) ===")
        venue_report = {"name": name, "weekday": None, "weekend": None}

        try:
            async with PlayByPointAPI(cookies=cookies, club_slug=slug) as api:
                api._user_id = user_id

                for label, target_date in [("weekday", weekday_date), ("weekend", weekend_date)]:
                    court = await pick_representative_court(api, fid, target_date)
                    if not court:
                        print(f"  {label}: no bookable court found on {target_date}, skipping")
                        continue
                    court_id, court_name = court
                    print(f"  {label} ({target_date}), probing court '{court_name}' every {args.step} min...")
                    probes = await probe_day(api, fid, court_id, target_date, user_id, step_sec)
                    transitions = find_transitions(probes)
                    for t in transitions:
                        print(f"    transition at {t['at']}: ${t['from_fare']} ({t['from_shift']}) -> ${t['to_fare']} ({t['to_shift']})")
                    if not transitions:
                        print(f"    no price transitions detected across the day")
                    venue_report[label] = {"court": court_name, "probes": probes, "transitions": transitions}
                    await asyncio.sleep(SLEEP_BETWEEN_CALLS)
        except Exception as e:
            print(f"  FAILED: {e}")
            venue_report["error"] = str(e)

        report[fid] = venue_report
        await asyncio.sleep(SLEEP_BETWEEN_VENUES)

    with open("shift_boundary_report.json", "w") as f:
        json.dump(report, f, indent=2)
    print("\n\nFull report written to shift_boundary_report.json")


if __name__ == "__main__":
    asyncio.run(main())
