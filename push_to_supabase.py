"""
push_to_supabase.py — Scrape PBP/OpenSports/SportLogic and push to Supabase cache.
Runs on the DO server every hour via cron.
Requires .pbp_cookies.json (uploaded by refresh_cookies.py from Windows machine).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from rich.console import Console

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from extract_thejar import PlayByPointAPI, _extract_react_props_from_html
from extract_opensports import OpenSportsAPI, parse_session
from extract_sportlogic import SportLogicClient, VENUES as SL_VENUES

console = Console()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://stwohmddmdwttasbyblt.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN0d29obWRkbWR3dHRhc2J5Ymx0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg3MjQ3OTMsImV4cCI6MjA5NDMwMDc5M30.x7VcVmJZ35S1uZy9_SU5RlB_MnuLziX2v81y9l02Yy8")
PROXY_URL = os.environ.get("PROXY_URL") or None
DAYS_AHEAD = 14

PBP_SLUG_MAP: dict[int, str] = {
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
    1733: "pickleballpowerhouse",
    1770: "rayapickleballclub",
}

VENUE_NAMES: dict[int, str] = {
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
    1733: "Pickleball Powerhouse",
    1770: "Raya Pickleball Club",
}


def _sec_to_hhmm(sec: int) -> str:
    h = int(sec) // 3600
    m = (int(sec) % 3600) // 60
    return f"{h:02d}:{m:02d}"


def _load_cookies() -> tuple[dict, int]:
    """Load PBP cookies from env var or local cache file."""
    raw = os.environ.get("PBP_COOKIES_JSON", "")
    if raw:
        try:
            data = json.loads(raw)
            cookies = data.get("cookies", {})
            if cookies:
                return cookies, data.get("user_id", 0)
        except Exception:
            pass
    for cache_path in [
        Path(__file__).parent / ".pbp_cookies.json",
        Path.home() / ".pbp_cookies.json",
    ]:
        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text())
                cookies = data.get("cookies", {})
                if cookies:
                    return cookies, data.get("user_id", 0)
            except Exception:
                pass
    return {}, 0


async def supabase_upsert(records: list[dict]) -> None:
    if not records:
        return
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/availability_cache",
            json=records,
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            console.print(f"  [red]Supabase error {resp.status_code}: {resp.text[:200]}[/red]")


async def scrape_pbp_venue(
    cookies: dict,
    user_id: int,
    facility_id: int,
    name: str,
    slug: str,
    dates: list[date],
) -> dict:
    result = {
        "id": facility_id,
        "name": name,
        "slug": slug,
        "platform": "playbypoint",
        "by_date": {d.isoformat(): [] for d in dates},
        "sessions": [],
    }

    date_strs = {d.isoformat() for d in dates}

    try:
        async with PlayByPointAPI(cookies=cookies, club_slug=slug, proxy=PROXY_URL) as api:
            api._user_id = user_id

            # Get clinic list
            try:
                resp = await api._get_json(
                    "/api/public/clinics",
                    params={"search": "", "facility_id": facility_id, "per_page": 50},
                )
                stubs = (resp or {}).get("clinics") or [] if isinstance(resp, dict) else (resp or [])
            except Exception as e:
                console.print(f"    [yellow]clinic list error for {name}: {e}[/yellow]")
                return result

            for stub in stubs:
                clinic_id = stub.get("id")
                program_url = stub.get("url") or ""
                program_slug = program_url.split("/programs/")[-1] if "/programs/" in program_url else ""
                if not clinic_id or not program_slug:
                    continue

                # Skip clinics with no upcoming sessions in our date range
                week_days = stub.get("future_week_days") or []
                has_upcoming = any(((d.weekday() + 1) % 7) in week_days for d in dates)
                if not has_upcoming:
                    continue

                try:
                    # Fetch HTML page — only source for lesson dates/times
                    html = await api.program_detail_html(program_slug)
                    props = _extract_react_props_from_html(html)
                    lessons_raw = props.get("sessions") or props.get("clinic_lessons") or []

                    # Metadata
                    raw_desc = props.get("description") or ""
                    desc = re.sub(r"<[^>]+>", " ", raw_desc).strip()[:500]
                    desc = re.sub(r"\s+", " ", desc)
                    sl = stub.get("ntrp_str") or ""
                    if not sl:
                        mn = props.get("min_rating")
                        mx = props.get("max_rating")
                        if mn and mx:
                            sl = f"{mn} / {mx}"
                        elif mn:
                            sl = f"{mn}+"
                    price = ""
                    for pl in (props.get("prices") or props.get("packages") or []):
                        if not pl.get("hidden") and pl.get("price"):
                            price = f"${float(pl['price']):.0f}"
                            break

                    for lesson in lessons_raw:
                        ld = lesson.get("lesson_date")
                        if ld not in date_strs:
                            continue
                        lid = lesson.get("id")
                        cap = lesson.get("capacity") or stub.get("capacity") or 0
                        pc = lesson.get("player_count", 0)
                        spots = max(0, cap - pc) if cap else None
                        if spots == 0:
                            continue
                        hs = lesson.get("hour_start", 0)
                        he = lesson.get("hour_end", hs + 3600)

                        lp = price
                        for ip in (lesson.get("individual_prices") or []):
                            if ip.get("price"):
                                lp = f"${float(ip['price']):.0f}"
                                break

                        # Roster
                        roster = []
                        if lid:
                            try:
                                rd = await api._get_json(
                                    "/api/public/clinics/lesson_players",
                                    params={"lesson_id": lid},
                                )
                                roster = [
                                    {
                                        "id": u.get("id"),
                                        "name": u.get("name"),
                                        "initials": u.get("name_initials"),
                                        "avatar": u.get("avatar") or "",
                                        "rating": u.get("rating"),
                                    }
                                    for u in (rd or {}).get("users", [])
                                ]
                            except Exception:
                                pass

                        result["sessions"].append({
                            "title": stub.get("name", "Session"),
                            "type": stub.get("category") or "Session",
                            "date": ld,
                            "start": _sec_to_hhmm(hs),
                            "end": _sec_to_hhmm(he),
                            "price": lp,
                            "spots_left": spots,
                            "capacity": cap,
                            "description": desc,
                            "skill_level": sl,
                            "roster": roster,
                            "lesson_id": lid, "program_slug": program_slug,
                        })
                except Exception as e:
                    console.print(f"    [yellow]clinic {clinic_id} error for {name}: {e}[/yellow]")

    except Exception as e:
        console.print(f"    [red]error for {name}: {e}[/red]")

    return result


async def scrape_opensports(dates: list[date]) -> list[dict]:
    try:
        async with OpenSportsAPI() as api:
            raw = await api.search_sessions(
                latitude=-37.815, longitude=144.966, radius_km=35, limit=200
            )
        sessions = [parse_session(s) for s in raw]
        date_strs = {d.isoformat() for d in dates}
        sessions = [s for s in sessions if s["date"] in date_strs and s["status"] != "Full"]
        for s in sessions:
            s.pop("raw", None)
        return sessions
    except Exception as e:
        console.print(f"  [yellow]OpenSports error: {e}[/yellow]")
        return []


async def scrape_sportlogic(dates: list[date]) -> list[dict]:
    results = []
    for vk, v in SL_VENUES.items():
        try:
            async with SportLogicClient(vk) as client:
                by_date = {}
                for d in dates:
                    try:
                        slots = await client.get_availability(d)
                        by_date[d.isoformat()] = slots
                    except Exception:
                        by_date[d.isoformat()] = []
                results.append({
                    "id": f"sportlogic-{vk}",
                    "name": v.get("name", vk),
                    "platform": "sportlogic",
                    "by_date": by_date,
                    "sessions": [],
                })
                console.print(f"  [green]✓[/green] {v.get('name', vk)}")
        except Exception as e:
            console.print(f"  [yellow]SportLogic error for {vk}: {e}[/yellow]")
    return results


async def run_once():
    console.print(f"\n[bold]🏓 PickleMatch → Supabase sync[/bold] · {datetime.now().strftime('%H:%M:%S')}\n")
    dates = [date.today() + timedelta(days=i) for i in range(DAYS_AHEAD)]
    cookies, user_id = _load_cookies()

    # ── PlayByPoint ───────────────────────────────────────────────────────────
    if not cookies:
        console.print("[red]No PBP cookies. Run refresh_cookies.py first.[/red]")
    else:
        console.print(f"Scraping {len(PBP_SLUG_MAP)} PBP venues × {DAYS_AHEAD} days…")

        # Fetch existing Supabase data first to preserve by_date/court_prices
        existing_records = {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            existing_resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/availability_cache",
                params={"platform": "eq.playbypoint", "select": "id,data"},
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            )
            for row in existing_resp.json():
                existing_records[row["id"]] = row["data"]

        pbp_results = []
        for fid, slug in PBP_SLUG_MAP.items():
            r = await scrape_pbp_venue(cookies, user_id, fid, VENUE_NAMES.get(fid, f"Venue {fid}"), slug, dates)
            pbp_results.append(r)
            await asyncio.sleep(3)

        records = []
        for r in pbp_results:
            if not isinstance(r, dict):
                continue
            rec_id = f"pbp-{r['id']}"
            existing = existing_records.get(rec_id, {})
            # Preserve court blocks and prices from previous court blocks run
            r["by_date"] = existing.get("by_date", {})
            r["court_prices"] = existing.get("court_prices", {})
            records.append({
                "id": rec_id,
                "venue_name": VENUE_NAMES.get(r["id"], r["name"]),
                "platform": "playbypoint",
                "date": date.today().isoformat(),
                "data": r,
                "updated_at": datetime.utcnow().isoformat(),
            })
            console.print(f"  [green]✓[/green] {r['name']} · {sum(len(v) for v in r['by_date'].values())} blocks · {len(r['sessions'])} sessions")

        await supabase_upsert(records)
        console.print(f"[green]✓ Pushed {len(records)} PBP venues to Supabase[/green]\n")

    # ── OpenSports ────────────────────────────────────────────────────────────
    console.print("Scraping OpenSports…")
    os_sessions = await scrape_opensports(dates)
    if os_sessions:
        os_record = [{
            "id": "opensports-melbourne",
            "venue_name": "OpenSports Melbourne",
            "platform": "opensports",
            "date": date.today().isoformat(),
            "data": {"sessions": os_sessions},
            "updated_at": datetime.utcnow().isoformat(),
        }]
        await supabase_upsert(os_record)
        console.print(f"[green]✓ Pushed {len(os_sessions)} OpenSports sessions[/green]\n")

    # ── SportLogic ────────────────────────────────────────────────────────────
    console.print("Scraping SportLogic…")
    sl_results = await scrape_sportlogic(dates)
    if sl_results:
        sl_records = [{
            "id": f"sportlogic-{r['id']}",
            "venue_name": r["name"],
            "platform": "sportlogic",
            "date": date.today().isoformat(),
            "data": r,
            "updated_at": datetime.utcnow().isoformat(),
        } for r in sl_results]
        await supabase_upsert(sl_records)
        console.print(f"[green]✓ Pushed {len(sl_results)} SportLogic venues[/green]\n")

    console.print(f"Sync complete · {datetime.now().strftime('%H:%M:%S')}")

async def watch(interval_minutes: int = 60):
    while True:
        await run_once()
        console.print(f"[dim]Next sync in {interval_minutes} minutes…[/dim]")
        await asyncio.sleep(interval_minutes * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if args.watch:
        asyncio.run(watch(args.interval))
    else:
        asyncio.run(run_once())
