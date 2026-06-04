"""
push_to_supabase.py  —  Scrape availability locally and push to Supabase.

Runs on your local machine (has valid PBP cookies + real IP).
Pushes results every 30 minutes so Railway API can serve them instantly.

Setup:
    pip install supabase
    python push_to_supabase.py              # run once
    python push_to_supabase.py --watch      # run every 30 mins forever

Add to Windows Task Scheduler to run automatically.
"""

from __future__ import annotations

import asyncio
import json
import argparse
import sys
import httpx
from datetime import date, datetime, timedelta
from pathlib import Path

from supabase import create_client, Client

# ── Supabase REST helper (no supabase package needed) ─────────────────────────

async def supabase_upsert(records: list[dict]) -> None:
    """Upsert records into availability_cache via Supabase REST API."""
    if not records:
        return
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    url = f"{SUPABASE_URL}/rest/v1/availability_cache"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=records, headers=headers)
        if resp.status_code not in (200, 201):
            console.print(f"  [red]Supabase error {resp.status_code}: {resp.text[:200]}[/red]")
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent))
from extract_thejar import PlayByPointAPI, _load_cached_session, _extract_react_props_from_html
from extract_opensports import OpenSportsAPI, parse_session
from extract_sportlogic import SportLogicClient, VENUES as SL_VENUES

console = Console()

SUPABASE_URL = "https://stwohmddmdwttasbyblt.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN0d29obWRkbWR3dHRhc2J5Ymx0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg3MjQ3OTMsImV4cCI6MjA5NDMwMDc5M30.x7VcVmJZ35S1uZy9_SU5RlB_MnuLziX2v81y9l02Yy8"

import os as _os
PROXY_URL = _os.environ.get("PROXY_URL")  # e.g. http://user:pass@p.webshare.io:80

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
}

DAYS_AHEAD = 7


def _sec_to_hhmm(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h:02d}:{m:02d}"


async def scrape_pbp_venue(
    cookies: dict, user_id: int,
    facility_id: int, name: str, slug: str,
    dates: list[date],
) -> dict:
    """Scrape court blocks + sessions for one PBP venue across multiple dates."""
    result = {
        "id": facility_id,
        "name": name,
        "slug": slug,
        "platform": "playbypoint",
        "by_date": {},
        "sessions": [],
    }

    if not slug:
        return result

    try:
        async with PlayByPointAPI(cookies=cookies, club_slug=slug, proxy=PROXY_URL) as api:
            api._user_id = user_id

            # Surface detection.
            surface = "pickleball"
            try:
                ct = await api.court_types(facility_id)
                ps = [s for s in (ct or []) if "pickle" in (s.get("surface") or "").lower()]
                if ps:
                    surface = ps[0]["surface"]
            except Exception:
                pass

            # Court blocks are served live from api_server.py — skip slow per-slot scraping.
            for target_date in dates:
                result["by_date"][target_date.isoformat()] = []



            # Sessions (across all dates).
            try:
                programs_resp = await api._get_json(
                    "/api/public/clinics",
                    params={"search": "", "facility_id": facility_id, "per_page": 50},
                )
                # Response is {"clinics": [...]} not a bare list
                if isinstance(programs_resp, dict):
                    clinic_stubs = programs_resp.get("clinics") or []
                elif isinstance(programs_resp, list):
                    clinic_stubs = programs_resp
                else:
                    clinic_stubs = []

                date_strs = {d.isoformat() for d in dates}

                for stub in clinic_stubs:
                    program_name = stub.get("name", "Session")
                    program_url = stub.get("url") or ""
                    # Extract slug from url like /programs/social-open-play-a2af7d
                    program_slug = program_url.split("/programs/")[-1] if "/programs/" in program_url else ""

                    # Fetch individual session dates via React props.
                    lessons = []
                    program_price = ""
                    description = ""
                    skill_level = ""
                    if program_slug:
                        try:
                            html = await api.program_detail_html(program_slug)
                            props = _extract_react_props_from_html(html)
                            lessons = (props.get("sessions")
                                       or props.get("clinic_lessons") or [])
                            program_name = props.get("name") or props.get("clinic_name") or program_name

                            # Extract price from props["prices"] (non-member shown rate)
                            all_prices = []
                            for price_list in (props.get("prices") or [], props.get("packages") or []):
                                for p in price_list:
                                    if not p.get("hidden") and p.get("available_for_players") and p.get("price"):
                                        all_prices.append(float(p["price"]))
                            if all_prices:
                                program_price = f"${min(all_prices):.0f}"

                            # Fetch description + skill level from clinic API.
                            clinic_id = stub.get("id") or props.get("clinic_id")
                            if clinic_id:
                                try:
                                    clinic_data = await api._get_json(f"/api/public/clinics/{clinic_id}")
                                    clinic = (clinic_data or {}).get("clinic", {})
                                    raw_desc = clinic.get("description") or ""
                                    # Strip HTML tags for clean text.
                                    import re as _re
                                    description = _re.sub(r"<[^>]+>", " ", raw_desc).strip()
                                    description = _re.sub(r"\s+", " ", description)[:500]
                                    # Skill level from ntrp_str or rating range.
                                    ntrp = stub.get("ntrp_str") or ""
                                    min_r = clinic.get("min_rating")
                                    max_r = clinic.get("max_rating")
                                    if ntrp:
                                        skill_level = ntrp
                                    elif min_r and max_r:
                                        skill_level = f"DUPR {min_r}–{max_r}"
                                    elif min_r:
                                        skill_level = f"DUPR {min_r}+"
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    for lesson in lessons:
                        if lesson.get("lesson_date") not in date_strs:
                            continue
                        hs = lesson.get("hour_start", 0)
                        he = lesson.get("hour_end", hs + 3600)
                        capacity = lesson.get("capacity") or stub.get("capacity") or 0
                        player_count = lesson.get("player_count", 0)
                        spots_left = max(0, capacity - player_count) if capacity else None
                        if spots_left == 0:
                            continue
                        price = ""
                        ind = lesson.get("individual_prices") or []
                        if ind:
                            amounts = [p.get("price", 0) for p in ind if p.get("price")]
                            if amounts:
                                price = f"${min(amounts):.0f}"
                        if not price:
                            pv = lesson.get("price") or lesson.get("fee")
                            if pv:
                                price = f"${float(pv):.0f}"
                        if not price:
                            price = program_price  # from props["prices"]
                        if not price:
                            pv = stub.get("price") or stub.get("fee")
                            if pv:
                                price = f"${float(pv):.0f}"

                        # Fetch roster.
                        roster = []
                        lesson_id = lesson.get("id")
                        if lesson_id:
                            try:
                                roster_data = await api._get_json(
                                    "/api/public/clinics/lesson_players",
                                    params={"lesson_id": lesson_id},
                                )
                                roster = [
                                    {
                                        "id": u.get("id"),
                                        "name": u.get("name"),
                                        "initials": u.get("name_initials"),
                                        "avatar": u.get("avatar") or "",
                                        "rating": u.get("rating"),
                                    }
                                    for u in (roster_data or {}).get("users", [])
                                ]
                            except Exception:
                                pass

                        result["sessions"].append({
                            "title": program_name,
                            "type": stub.get("category") or "Session",
                            "date": lesson.get("lesson_date"),
                            "start": _sec_to_hhmm(hs),
                            "end": _sec_to_hhmm(he),
                            "price": price,
                            "spots_left": spots_left,
                            "capacity": capacity,
                            "description": description,
                            "skill_level": skill_level,
                            "roster": roster,
                            "lesson_id": lesson_id,
                        })
            except Exception as e:
                console.print(f"    [yellow]sessions error for {name}: {e}[/yellow]")

    except Exception as e:
        console.print(f"    [red]error for {name}: {e}[/red]")

    return result


async def scrape_opensports(dates: list[date]) -> list[dict]:
    """Scrape OpenSports sessions."""
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
    """Scrape SportLogic availability."""
    results = []
    for vk, v in SL_VENUES.items():
        try:
            async with SportLogicClient(vk) as client:
                by_date = {}
                for d in dates:
                    slots = await client.get_availability(d)
                    by_date[d.isoformat()] = [
                        {"court": s["court_name"], "time": s["time"]}
                        for s in slots
                    ]
                results.append({
                    "key": vk,
                    "name": v["name"],
                    "platform": "sportlogic",
                    "by_date": by_date,
                })
        except Exception as e:
            console.print(f"  [yellow]SportLogic {vk} error: {e}[/yellow]")
    return results


def push_to_supabase(sb, records: list[dict]) -> None:
    """Upsert records into availability_cache table."""
    asyncio.get_event_loop().run_until_complete(supabase_upsert(records))


async def _pbp_login_for_user(email: str, password: str) -> tuple[dict, int, str]:
    """
    Log in to PlayByPoint as a specific user using harvest_cookies_via_playwright.
    Returns (cookies, user_id, email) or ({}, 0, '') on failure.
    """
    try:
        from extract_thejar import harvest_cookies_via_playwright
        cookies, user_id = await harvest_cookies_via_playwright(
            email=email,
            password=password,
            headless=True,
        )
        if cookies:
            return cookies, user_id or 0, email
        return {}, 0, ""
    except Exception as e:
        console.print(f"    [red]Login failed for {email}: {e}[/red]")
        return {}, 0, ""


async def refresh_user_sessions() -> None:
    """Read all users from pbp_credentials, log in for each, store fresh cookies."""
    console.print("Refreshing user PBP sessions…")
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

    # Read all connected credentials.
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/pbp_credentials?select=user_id,pbp_email,pbp_password_encrypted,pbp_cookies,session_valid_until",
            headers=headers,
        )
        if resp.status_code != 200:
            console.print(f"  [yellow]Could not read pbp_credentials: {resp.status_code}[/yellow]")
            return
        users = resp.json()

    if not users:
        console.print("  [dim]No connected users found[/dim]")
        return

    console.print(f"  Found {len(users)} connected user(s)")

    from datetime import timezone as _tz
    for u in users:
        user_id = u.get("user_id")
        email = u.get("pbp_email", "")
        encoded_pw = u.get("pbp_password_encrypted", "")
        if not email or not encoded_pw:
            continue

        # Skip if cookies already exist and haven't expired.
        existing_cookies = u.get("pbp_cookies")
        valid_until = u.get("session_valid_until")
        if existing_cookies and valid_until:
            try:
                expiry = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
                if expiry > datetime.now(_tz.utc):
                    console.print(f"  [dim]Skipping {email} — cookies still valid until {expiry.strftime('%Y-%m-%d')}[/dim]")
                    continue
            except Exception:
                pass

        try:
            import base64
            password = base64.b64decode(encoded_pw).decode("utf-8")
        except Exception:
            console.print(f"  [yellow]Could not decode password for {email}[/yellow]")
            continue

        console.print(f"  Logging in for {email}…")
        try:
            cookies, pbp_uid, _ = await _pbp_login_for_user(email, password)
            if not cookies:
                console.print(f"  [red]Login failed for {email}[/red]")
                # Mark as disconnected.
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.patch(
                        f"{SUPABASE_URL}/rest/v1/pbp_credentials?user_id=eq.{user_id}",
                        json={"is_connected": False, "updated_at": datetime.utcnow().isoformat()},
                        headers=headers,
                    )
                continue

            # Store fresh cookies back to Supabase.
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.patch(
                    f"{SUPABASE_URL}/rest/v1/pbp_credentials?user_id=eq.{user_id}",
                    json={
                        "pbp_cookies": cookies,
                        "pbp_user_id": pbp_uid,
                        "is_connected": True,
                        "session_valid_until": (datetime.utcnow() + timedelta(days=7)).isoformat(),
                        "last_synced_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat(),
                    },
                    headers=headers,
                )
                if r.status_code in (200, 204):
                    console.print(f"  [green]✓[/green] Session refreshed for {email} (PBP ID: {pbp_uid})")
                else:
                    console.print(f"  [yellow]Could not save session for {email}: {r.status_code}[/yellow]")

        except Exception as e:
            console.print(f"  [red]Error refreshing session for {email}: {e}[/red]")

    console.print()


async def run_once():
    console.print(f"\n[bold]🏓 PickleMatch → Supabase sync[/bold] · {datetime.now().strftime('%H:%M:%S')}\n")

    dates = [date.today() + timedelta(days=i) for i in range(DAYS_AHEAD)]

    # ── Refresh user PBP sessions ────────────────────────────────────────────
    await refresh_user_sessions()

    # ── PlayByPoint ─────────────────────────────────────────────────────────
    cookies, user_id, email = _load_cached_session()
    if not cookies:
        console.print("[red]No PBP session. Run the scraper first.[/red]")
    else:
        console.print(f"[dim]PBP session: {email}[/dim]")
        console.print(f"Scraping {len(PBP_SLUG_MAP)} PBP venues × {DAYS_AHEAD} days…")

        pbp_results = []
        for fid, slug in PBP_SLUG_MAP.items():
            result = await scrape_pbp_venue(cookies, user_id, fid, VENUE_NAMES.get(fid, f"Venue {fid}"), slug, dates)
            pbp_results.append(result)

        records = []
        for r in pbp_results:
            if not isinstance(r, dict):
                continue
            records.append({
                "id": f"pbp-{r['id']}",
                "venue_name": VENUE_NAMES.get(r["id"], r["name"]),
                "platform": "playbypoint",
                "date": date.today().isoformat(),
                "data": r,
                "updated_at": datetime.utcnow().isoformat(),
            })
            console.print(f"  [green]✓[/green] {r['name']} · {sum(len(v) for v in r['by_date'].values())} blocks · {len(r['sessions'])} sessions")

        await supabase_upsert(records)
        console.print(f"[green]✓ Pushed {len(records)} PBP venues to Supabase[/green]\n")

        # Cache CSRF tokens per venue slug so Railway can book courts.
        console.print("Caching CSRF tokens…")
        csrf_records = []
        async with PlayByPointAPI(cookies=cookies, club_slug="nplpickleball", proxy=PROXY_URL) as api:
            api._user_id = user_id
            for fid, slug in PBP_SLUG_MAP.items():
                try:
                    api.club_slug = slug
                    token = await api._get_csrf_token()
                    if token:
                        csrf_records.append({
                            "slug": slug,
                            "token": token,
                            "updated_at": datetime.utcnow().isoformat(),
                        })
                    await asyncio.sleep(1)
                except Exception as e:
                    console.print(f"  [yellow]CSRF error for {slug}: {e}[/yellow]")

        if csrf_records:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"{SUPABASE_URL}/rest/v1/csrf_tokens",
                    headers={
                        "apikey": SUPABASE_KEY,
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates",
                    },
                    json=csrf_records,
                )
            console.print(f"[green]✓ Cached {len(csrf_records)} CSRF tokens[/green]\n")
        else:
            console.print("[yellow]No CSRF tokens cached[/yellow]\n")
        console.print("Caching court IDs…")
        court_id_records = []
        seen_courts = set()
        for r in pbp_results:
            if not isinstance(r, dict):
                continue
            fid = r["id"]
            for date_str, blocks in r.get("by_date", {}).items():
                for b in blocks:
                    cid = b.get("court_id")
                    cname = b.get("court")
                    key = (fid, cname)
                    if cid and cname and key not in seen_courts:
                        seen_courts.add(key)
                        court_id_records.append({
                            "facility_id": fid,
                            "court_name": cname,
                            "court_id": cid,
                            "updated_at": datetime.utcnow().isoformat(),
                        })

        if court_id_records:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"{SUPABASE_URL}/rest/v1/court_ids",
                    headers={
                        "apikey": SUPABASE_KEY,
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates",
                    },
                    json=court_id_records,
                )
            console.print(f"[green]✓ Cached {len(court_id_records)} court IDs[/green]\n")
        else:
            console.print("[yellow]No court IDs cached[/yellow]\n")

    # ── OpenSports ───────────────────────────────────────────────────────────
    console.print("Scraping OpenSports…")
    os_sessions = await scrape_opensports(dates)
    await supabase_upsert([{
        "id": "opensports-melbourne",
        "venue_name": "OpenSports Melbourne",
        "platform": "opensports",
        "date": date.today().isoformat(),
        "data": {"sessions": os_sessions},
        "updated_at": datetime.utcnow().isoformat(),
    }])
    console.print(f"[green]✓ Pushed {len(os_sessions)} OpenSports sessions[/green]\n")

    # ── SportLogic ───────────────────────────────────────────────────────────
    console.print("Scraping SportLogic…")
    sl_results = await scrape_sportlogic(dates)
    records = []
    for r in sl_results:
        records.append({
            "id": f"sportlogic-{r['key']}",
            "venue_name": r["name"],
            "platform": "sportlogic",
            "date": date.today().isoformat(),
            "data": r,
            "updated_at": datetime.utcnow().isoformat(),
        })
        console.print(f"  [green]✓[/green] {r['name']}")
    await supabase_upsert(records)
    console.print(f"[green]✓ Pushed {len(records)} SportLogic venues[/green]\n")

    console.print(f"[bold green]Sync complete[/bold green] · {datetime.now().strftime('%H:%M:%S')}\n")


async def watch(interval_minutes: int = 30):
    while True:
        await run_once()
        console.print(f"[dim]Next sync in {interval_minutes} minutes…[/dim]")
        await asyncio.sleep(interval_minutes * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="Run continuously every 30 mins")
    parser.add_argument("--interval", type=int, default=30, help="Interval in minutes (default: 30)")
    args = parser.parse_args()

    if args.watch:
        asyncio.run(watch(args.interval))
    else:
        asyncio.run(run_once())
