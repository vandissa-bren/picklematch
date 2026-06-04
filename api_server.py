"""
api_server.py  —  PickleMatch FastAPI backend

Wraps extract_thejar.py, extract_opensports.py, and extract_sportlogic.py
into a REST API your React frontend can call.

Start:
    pip install fastapi uvicorn
    python api_server.py

Then in your React app, fetch from:
    http://localhost:8000/api/...

For production, run behind nginx or deploy to Railway/Render/Fly.io.
"""

from __future__ import annotations

import asyncio
import httpx
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
import sys

from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Scraper imports ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from extract_thejar import (
    PlayByPointAPI,
    _load_cached_session,
    _extract_react_props_from_html,
)
from extract_opensports import OpenSportsAPI, parse_session
from extract_sportlogic import (
    SportLogicClient,
    VENUES as SL_VENUES,
    _load_session as sl_load_session,
    _ensure_valid_session,
)

logger = logging.getLogger("pickleball_api")

# ── In-memory availability cache ────────────────────────────────────────────
_availability_cache: dict = {}  # key -> {"data": ..., "expires": timestamp}
CACHE_TTL = 300  # 5 minutes

def _cache_get(key: str):
    entry = _availability_cache.get(key)
    if entry and datetime.utcnow().timestamp() < entry["expires"]:
        return entry["data"]
    return None

def _cache_set(key: str, data):
    _availability_cache[key] = {
        "data": data,
        "expires": datetime.utcnow().timestamp() + CACHE_TTL,
    }


# Cookies can be refreshed at runtime via POST /api/internal/refresh-cookies
_runtime_cookies: dict = {}
_runtime_user_id: int = 0
_runtime_email: str = ""

def _load_session_with_env_fallback():
    """
    Load PBP session — checks runtime store first, then env var, then local cache.
    """
    global _runtime_cookies, _runtime_user_id, _runtime_email

    # Runtime store (pushed via /api/internal/refresh-cookies)
    if _runtime_cookies:
        return _runtime_cookies, _runtime_user_id, _runtime_email

    # Environment variable
    raw = os.environ.get("PBP_COOKIES_JSON", "")
    if raw:
        try:
            data = json.loads(raw)
            cookies = data.get("cookies", {})
            user_id = data.get("user_id", 0)
            email = data.get("email", "")
            if cookies:
                return cookies, user_id, email
        except Exception as e:
            logger.error(f"Failed to parse PBP_COOKIES_JSON: {e}")

    # Local cache file
    cookies, user_id, email = _load_cached_session()
    if cookies:
        return cookies, user_id, email

    return {}, 0, ""

# ── App setup ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="PickleMatch API",
    description="Real-time pickleball court availability aggregator",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Supabase REST helper ─────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://stwohmddmdwttasbyblt.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN0d29obWRkbWR3dHRhc2J5Ymx0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg3MjQ3OTMsImV4cCI6MjA5NDMwMDc5M30.x7VcVmJZ35S1uZy9_SU5RlB_MnuLziX2v81y9l02Yy8")
PROXY_URL = os.environ.get("PROXY_URL")  # e.g. http://user:pass@p.webshare.io:80


async def _read_from_supabase(platform: str) -> list[dict]:
    """Read cached availability data from Supabase REST API."""
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        }
        url = f"{SUPABASE_URL}/rest/v1/availability_cache?select=data&platform=eq.{platform}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            logger.info(f"Supabase read {platform}: status={resp.status_code} rows={len(resp.json()) if resp.status_code == 200 else 'err'} url={url}")
            if resp.status_code == 200:
                rows = resp.json()
                result = [row["data"] for row in rows if row.get("data")]
                logger.info(f"Supabase {platform}: {len(rows)} raw rows → {len(result)} with data")
                return result
    except Exception as e:
        logger.error(f"Supabase read error: {e}")
    return []

# ── Slug map for saved venues ───────────────────────────────────────────────
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

# ── Helpers ─────────────────────────────────────────────────────────────────

def _sec_to_hhmm(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h:02d}:{m:02d}"


def _hhmm_to_sec(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 3600 + int(m) * 60


async def _get_pbp_venues() -> list[dict]:
    """Get all saved PBP venues with slugs resolved."""
    cookies, user_id, _ = _load_session_with_env_fallback()
    if not cookies:
        return []

    venues = []
    try:
        async with PlayByPointAPI(cookies=cookies, club_slug="nplpickleball", proxy=PROXY_URL) as api:
            api._user_id = user_id
            facs = await api.preferred_facilities(user_id)
            seen = set()
            for f in facs:
                fid = f.get("id")
                if not fid or fid in seen:
                    continue
                seen.add(fid)
                slug = PBP_SLUG_MAP.get(fid, "")
                if not slug:
                    # Try to resolve from API
                    try:
                        data = await api._get_json(f"/api/facilities/{fid}")
                        if isinstance(data, dict):
                            facility = data.get("facility") or data
                            book_url = facility.get("book_url") or ""
                            slug = book_url.rstrip("/").split("/")[-1]
                    except Exception:
                        pass
                venues.append({
                    "id": fid,
                    "name": f.get("name", ""),
                    "slug": slug,
                    "platform": "playbypoint",
                })
    except Exception as e:
        logger.error(f"get_pbp_venues failed: {e}")

    return venues


async def _get_pbp_availability(
    facility_id: int,
    name: str,
    slug: str,
    target_date: date,
    from_sec: int,
    to_sec: int,
) -> dict:
    """Court slots + sessions for one PBP venue."""
    result = {
        "id": facility_id,
        "name": name,
        "slug": slug,
        "platform": "playbypoint",
        "court_blocks": [],
        "sessions": [],
        "error": None,
    }

    if not slug:
        result["error"] = "no_slug"
        return result

    cookies, user_id, _ = _load_session_with_env_fallback()
    if not cookies:
        result["error"] = "no_session"
        return result

    try:
        async with PlayByPointAPI(cookies=cookies, club_slug=slug, proxy=PROXY_URL) as api:
            api._user_id = user_id

            # Auto-detect surface.
            surface = "pickleball"
            try:
                ct = await api.court_types(facility_id)
                ps = [s for s in (ct or [])
                      if "pickle" in (s.get("surface") or "").lower()]
                if ps:
                    surface = ps[0]["surface"]
            except Exception:
                pass

            # Available hours.
            try:
                hours_data = await api.available_hours(
                    facility_id, target_date, surface=surface,
                )
                all_slots = (hours_data or {}).get("available_hours") or [] \
                    if isinstance(hours_data, dict) else (hours_data or [])

                # Build per-court slot map.
                court_slots: dict[str, list[int]] = {}
                for slot in all_slots:
                    if not isinstance(slot, dict) or not slot.get("available"):
                        continue
                    sec = slot.get("seconds_from_midnight")
                    if not isinstance(sec, (int, float)):
                        continue
                    if not (from_sec <= int(sec) < to_sec):
                        continue
                    try:
                        courts = await api.available_courts(
                            facility_id, target_date,
                            int(sec), int(sec) + 1800, surface=surface,
                        )
                        for court in (courts or []):
                            cid = court.get("id") or court.get("name") or "?"
                            cname = court.get("name") or str(cid)
                            key = f"{cid}|{cname}"
                            court_slots.setdefault(key, []).append(int(sec))
                    except Exception:
                        pass

                # Find bookable blocks ≥60 min.
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
                                result["court_blocks"].append({
                                    "court": cname,
                                    "start": _sec_to_hhmm(run_start),
                                    "end": _sec_to_hhmm(run_end + 1800),
                                    "duration_min": dur,
                                })
                            run_start = run_end = s
                    if run_start is not None:
                        dur = (run_end - run_start) // 60 + 30
                        if dur >= 60:
                            result["court_blocks"].append({
                                "court": cname,
                                "start": _sec_to_hhmm(run_start),
                                "end": _sec_to_hhmm(run_end + 1800),
                                "duration_min": dur,
                            })

                result["court_blocks"].sort(key=lambda x: (x["start"], x["court"]))

                # Add indicative price from venue static data if API didn't return one.
                from extract_thejar import _extract_price_string
                if result["court_blocks"] and not result["court_blocks"][0].get("price"):
                    # Try to get price for the first available block only (fast).
                    try:
                        first = result["court_blocks"][0]
                        fstart = _hhmm_to_sec(first["start"])
                        fend = _hhmm_to_sec(first["end"])
                        # Use min 1hr for pricing.
                        fend_price = fstart + 3600
                        courts_data = await api.available_courts(
                            facility_id, target_date, fstart, fend_price,
                            surface=surface,
                        )
                        if courts_data:
                            court_id = courts_data[0].get("id")
                            price_data = await api.court_price(
                                court_id, target_date, fstart, fend_price,
                                user_id=user_id,
                            )
                            price_str = _extract_price_string(price_data)
                            if price_str:
                                # Apply same price to all blocks at this venue.
                                for b in result["court_blocks"]:
                                    b["price"] = price_str
                    except Exception:
                        pass

            except Exception as e:
                logger.debug(f"court slots failed for {slug}: {e}")

            # Sessions / programs.
            try:
                programs = await api._get_json(
                    "/api/public/clinics",
                    params={"search": "", "facility_id": facility_id},
                )
                clinic_stubs = (programs if isinstance(programs, list)
                                else (programs or {}).get("clinics") or [])
                today_str = target_date.isoformat()

                for stub in clinic_stubs:
                    program_slug = stub.get("slug") or ""
                    program_name = stub.get("name", "Session")
                    lessons = stub.get("sessions") or stub.get("clinic_lessons") or []

                    # The catalog may not include lesson dates — enrich first.
                    if program_slug and not any(l.get("lesson_date") for l in lessons):
                        try:
                            html = await api.program_detail_html(program_slug)
                            props = _extract_react_props_from_html(html)
                            enriched = (props.get("sessions")
                                        or props.get("clinic_lessons") or [])
                            if enriched:
                                lessons = enriched
                                program_name = props.get("name") or program_name
                        except Exception:
                            pass

                    today_lessons = [l for l in lessons
                                     if l.get("lesson_date") == today_str]

                    if not today_lessons:
                        continue

                    for lesson in today_lessons:
                        hs = lesson.get("hour_start", 0)
                        if not (from_sec <= hs < to_sec):
                            continue
                        capacity = lesson.get("capacity") or stub.get("capacity") or 0
                        player_count = lesson.get("player_count", 0)
                        spots_left = max(0, capacity - player_count) if capacity else None
                        if spots_left == 0:
                            continue
                        he = lesson.get("hour_end", hs + 3600)

                        price = ""
                        ind = lesson.get("individual_prices") or []
                        if ind:
                            amounts = [p.get("price", 0) for p in ind if p.get("price")]
                            if amounts:
                                price = f"${min(amounts):.0f}"
                        if not price:
                            pv = stub.get("price") or stub.get("fee")
                            if pv:
                                price = f"${float(pv):.0f}"

                        result["sessions"].append({
                            "title": program_name,
                            "type": stub.get("category") or "Session",
                            "start": _sec_to_hhmm(hs),
                            "end": _sec_to_hhmm(he),
                            "price": price,
                            "spots_left": spots_left,
                            "capacity": capacity,
                        })
            except Exception as e:
                logger.debug(f"sessions failed for {slug}: {e}")

    except Exception as e:
        result["error"] = str(e)[:80]

    return result


# ── Routes ──────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def _warm_cache():
    """Pre-warm availability cache for today and tomorrow."""
    from datetime import date as date_type
    await asyncio.sleep(5)  # Wait for server to fully start
    for days_ahead in [0, 1]:
        try:
            target = date_type.today()
            if days_ahead:
                target = date_type.fromordinal(target.toordinal() + 1)
            date_str = target.isoformat()
            cache_key = f"availability:{date_str}:08:00:23:00:all"
            if not _cache_get(cache_key):
                cookies, user_id, _ = _load_session_with_env_fallback()
                if cookies:
                    results = await asyncio.gather(*[
                        _get_pbp_availability(fid, VENUE_NAMES.get(fid, f"Venue {fid}"), slug, target, _hhmm_to_sec("08:00"), _hhmm_to_sec("23:00"))
                        for fid, slug in PBP_SLUG_MAP.items()
                    ], return_exceptions=True)
                    court_blocks_by_id = {r["id"]: r.get("court_blocks", []) for r in results if isinstance(r, dict)}
                    supabase_data = await _read_from_supabase("playbypoint")
                    output = []
                    for r in supabase_data:
                        vid = r.get("id")
                        filtered_sessions = [s for s in r.get("sessions", []) if s.get("date") == date_str and (s.get("start", "00:00") == "00:00" or _hhmm_to_sec("08:00") <= _hhmm_to_sec(s["start"]) < _hhmm_to_sec("23:00"))]
                        output.append({"id": vid, "name": r.get("name"), "slug": r.get("slug"), "platform": "playbypoint", "court_blocks": court_blocks_by_id.get(vid, []), "sessions": filtered_sessions, "error": None})
                    response = {"date": date_str, "from": "08:00", "to": "23:00", "venues": output, "total_court_blocks": sum(len(v["court_blocks"]) for v in output), "total_sessions": sum(len(v["sessions"]) for v in output), "source": "live", "cached_count": len(output)}
                    _cache_set(cache_key, response)
                    logger.info(f"Cache warmed for {date_str}: {response['total_court_blocks']} blocks, {response['total_sessions']} sessions")
        except Exception as e:
            logger.error(f"Cache warm error: {e}")


async def _cache_refresh_loop():
    """Refresh cache every 4 minutes in the background."""
    while True:
        await asyncio.sleep(240)  # 4 minutes
        await _warm_cache()


@asynccontextmanager
async def lifespan(app):
    asyncio.create_task(_warm_cache())
    asyncio.create_task(_cache_refresh_loop())
    yield


@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


class RefreshCookiesRequest(BaseModel):
    pbp_cookies_json: str
    secret: str = ""


@app.post("/api/internal/refresh-cookies")
async def refresh_cookies(req: RefreshCookiesRequest):
    """Accept fresh PBP cookies pushed from the Windows machine."""
    global _runtime_cookies, _runtime_user_id, _runtime_email
    try:
        data = json.loads(req.pbp_cookies_json)
        cookies = data.get("cookies", {})
        user_id = data.get("user_id", 0)
        email = data.get("email", "")
        if not cookies:
            raise HTTPException(status_code=400, detail="No cookies in payload")
        _runtime_cookies = cookies
        _runtime_user_id = user_id
        _runtime_email = email
        logger.info(f"Runtime cookies refreshed for {email} ({len(cookies)} cookies)")
        return {"status": "ok", "email": email, "cookie_count": len(cookies)}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")


@app.get("/api/debug/supabase")
async def debug_supabase():
    """Debug endpoint to check Supabase connectivity and data."""
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        }
        url = f"{SUPABASE_URL}/rest/v1/availability_cache?select=id,platform,venue_name&limit=5"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            return {
                "status_code": resp.status_code,
                "supabase_url": SUPABASE_URL,
                "rows": resp.json() if resp.status_code == 200 else [],
                "error": resp.text if resp.status_code != 200 else None,
            }
    except Exception as e:
        return {"error": str(e)}


# ── PlayByPoint ─────────────────────────────────────────────────────────────

@app.get("/api/debug/session")
async def debug_session():
    cookies, user_id, email = _load_session_with_env_fallback()
    raw = os.environ.get("PBP_COOKIES_JSON", "")
    return {
        "has_cookies": bool(cookies),
        "cookie_count": len(cookies),
        "user_id": user_id,
        "email": email,
        "env_var_length": len(raw),
        "proxy_url": os.environ.get("PROXY_URL", "not set")[:30] if os.environ.get("PROXY_URL") else "not set",
    }



async def pbp_venues():
    """List all saved PBP venues from Supabase cache."""
    cached = await _read_from_supabase("playbypoint")
    venues = [
        {"id": r["id"], "name": r["name"], "slug": r["slug"], "platform": "playbypoint"}
        for r in cached if r.get("id")
    ]
    # Fallback to slug map if cache empty
    if not venues:
        venues = [
            {"id": fid, "name": f"Venue {fid}", "slug": slug, "platform": "playbypoint"}
            for fid, slug in PBP_SLUG_MAP.items()
        ]
    return {"venues": venues, "count": len(venues)}


@app.get("/api/pbp/availability")
async def pbp_availability(
    date: Optional[str] = Query(None, description="YYYY-MM-DD, default today"),
    from_time: str = Query("16:00", alias="from"),
    to_time: str = Query("23:00", alias="to"),
    venue_ids: Optional[str] = Query(None, description="Comma-separated IDs, default all saved"),
):
    """
    Get court blocks + sessions for PBP venues via live API calls through residential proxy.
    Falls back to Supabase cache if proxy calls fail.
    """
    target_date = (datetime.strptime(date, "%Y-%m-%d").date()
                   if date else datetime.today().date())
    date_str = target_date.isoformat()
    from_sec = _hhmm_to_sec(from_time)
    to_sec = _hhmm_to_sec(to_time)

    ids_filter = {int(i.strip()) for i in venue_ids.split(",")} if venue_ids else None
    cache_key = f"availability:{date_str}:{from_time}:{to_time}:{venue_ids or 'all'}"

    # Check in-memory cache first
    cached_result = _cache_get(cache_key)
    if cached_result:
        return cached_result

    slug_map = {k: v for k, v in PBP_SLUG_MAP.items() if not ids_filter or k in ids_filter}

    cookies, user_id, _ = _load_session_with_env_fallback()

    # Fetch court blocks live (fast from DO server)
    if cookies:
        results = await asyncio.gather(*[
            _get_pbp_availability(fid, VENUE_NAMES.get(fid, f"Venue {fid}"), slug, target_date, from_sec, to_sec)
            for fid, slug in slug_map.items()
        ], return_exceptions=True)
        court_blocks_by_id = {}
        for r in results:
            if isinstance(r, dict):
                court_blocks_by_id[r["id"]] = r.get("court_blocks", [])
    else:
        court_blocks_by_id = {}

    # Fetch sessions from Supabase cache
    supabase_data = await _read_from_supabase("playbypoint")
    if ids_filter:
        supabase_data = [r for r in supabase_data if r.get("id") in ids_filter]

    output = []
    for r in supabase_data:
        vid = r.get("id")
        all_sessions = r.get("sessions", [])
        filtered_sessions = [
            s for s in all_sessions
            if s.get("date") == date_str
            and (s.get("start", "00:00") == "00:00" or from_sec <= _hhmm_to_sec(s["start"]) < to_sec)
        ]
        output.append({
            "id": vid,
            "name": r.get("name"),
            "slug": r.get("slug"),
            "platform": "playbypoint",
            "court_blocks": court_blocks_by_id.get(vid, []),
            "sessions": filtered_sessions,
            "error": None,
        })

    response = {
        "date": date_str,
        "from": from_time,
        "to": to_time,
        "venues": output,
        "total_court_blocks": sum(len(v["court_blocks"]) for v in output),
        "total_sessions": sum(len(v["sessions"]) for v in output),
        "source": "live",
        "cached_count": len(output),
    }

    _cache_set(cache_key, response)
    return response


@app.get("/api/pbp/venue/{facility_id}")
async def pbp_single_venue(
    facility_id: int,
    date: Optional[str] = Query(None),
    from_time: str = Query("00:00", alias="from"),
    to_time: str = Query("23:30", alias="to"),
):
    """Get availability for a single PBP venue from Supabase cache."""
    target_date = (datetime.strptime(date, "%Y-%m-%d").date()
                   if date else datetime.today().date())
    date_str = target_date.isoformat()
    from_sec = _hhmm_to_sec(from_time)
    to_sec = _hhmm_to_sec(to_time)

    cached = await _read_from_supabase("playbypoint")
    for r in cached:
        if r.get("id") == facility_id:
            by_date = r.get("by_date", {})
            all_blocks = by_date.get(date_str, [])
            filtered_blocks = [
                b for b in all_blocks
                if from_sec <= _hhmm_to_sec(b["start"]) < to_sec
            ]
            filtered_sessions = [
                s for s in r.get("sessions", [])
                if s.get("date") == date_str
                and from_sec <= _hhmm_to_sec(s["start"]) < to_sec
            ]
            return {
                "id": facility_id,
                "name": r.get("name"),
                "slug": r.get("slug"),
                "platform": "playbypoint",
                "court_blocks": filtered_blocks,
                "sessions": filtered_sessions,
                "error": None,
            }

    return {
        "id": facility_id,
        "name": f"Venue {facility_id}",
        "slug": PBP_SLUG_MAP.get(facility_id, ""),
        "platform": "playbypoint",
        "court_blocks": [],
        "sessions": [],
        "error": "not_in_cache",
    }


# ── OpenSports ──────────────────────────────────────────────────────────────

@app.get("/api/opensports/sessions")
async def opensports_sessions(
    lat: float = Query(-37.815, description="Latitude"),
    lng: float = Query(144.966, description="Longitude"),
    radius: float = Query(25, description="Radius in km"),
    days: int = Query(7, description="Days ahead to fetch"),
    date: Optional[str] = Query(None, description="Filter to specific date YYYY-MM-DD"),
):
    """
    Get upcoming OpenSports pickleball sessions near a location.

    Example:
        GET /api/opensports/sessions?lat=-37.815&lng=144.966&radius=30&days=7
    """
    async with OpenSportsAPI() as api:
        raw = await api.search_sessions(
            latitude=lat,
            longitude=lng,
            radius_km=radius,
            limit=200,
        )

    sessions = [parse_session(s) for s in raw]

    # Filter by date range.
    now = datetime.now()
    cutoff = (now + timedelta(days=days)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")

    if date:
        sessions = [s for s in sessions if s["date"] == date]
    else:
        sessions = [s for s in sessions if today <= s["date"] <= cutoff]

    sessions = [s for s in sessions if s["status"] != "Full"]
    sessions.sort(key=lambda s: (s["date"], s["start_time"]))

    # Slim down for API response (remove raw).
    slim = []
    for s in sessions:
        s.pop("raw", None)
        slim.append(s)

    return {
        "sessions": slim,
        "count": len(slim),
        "location": {"lat": lat, "lng": lng, "radius_km": radius},
    }


# ── SportLogic ──────────────────────────────────────────────────────────────

@app.get("/api/sportlogic/venues")
async def sportlogic_venues():
    """List known SportLogic venues with session status."""
    venues = []
    for key, v in SL_VENUES.items():
        has_session = bool(sl_load_session(key))
        venues.append({
            "key": key,
            "name": v["name"],
            "base_url": v["base_url"],
            "has_session": has_session,
        })
    return {"venues": venues}


@app.get("/api/sportlogic/availability")
async def sportlogic_availability(
    venues: str = Query("picklepark,pickleplay", description="Comma-separated venue keys"),
    date: Optional[str] = Query(None),
    from_time: str = Query("16:00", alias="from"),
    to_time: str = Query("23:00", alias="to"),
    pricing: bool = Query(False, description="Fetch prices (requires login session)"),
):
    """
    Get court availability for SportLogic venues.

    Example:
        GET /api/sportlogic/availability?venues=picklepark,pickleplay&from=18:00&to=22:00
        GET /api/sportlogic/availability?venues=picklepark&pricing=true
    """
    target_date = (datetime.strptime(date, "%Y-%m-%d").date()
                   if date else datetime.today().date())
    from_sec = _hhmm_to_sec(from_time)
    to_sec = _hhmm_to_sec(to_time)
    venue_keys = [v.strip() for v in venues.split(",") if v.strip() in SL_VENUES]

    results = []
    for vk in venue_keys:
        jsid = None
        if pricing:
            jsid = await _ensure_valid_session(vk)

        async with SportLogicClient(vk, jsessionid=jsid) as client:
            slots = await client.get_availability(target_date)
            slots = [s for s in slots if from_sec <= s["hour"] * 3600 < to_sec]

            if pricing and jsid:
                slots = await client.enrich_with_prices(slots, auto_refresh=False)

            # Group into bookable blocks per court.
            court_slots: dict[str, list[dict]] = {}
            for s in slots:
                court_slots.setdefault(s["court_id"], []).append(s)

            court_blocks = []
            for court_id, cs in court_slots.items():
                cs_sorted = sorted(cs, key=lambda x: x["time"])
                run_start = run_end = None
                run_slots = []
                for s in cs_sorted:
                    h, m = s["time"].split(":")
                    sec = int(h) * 3600 + int(m) * 60
                    if run_start is None:
                        run_start = run_end = sec
                        run_slots = [s]
                    elif sec == run_end + 3600:
                        run_end = sec
                        run_slots.append(s)
                    else:
                        dur = (run_end - run_start) // 60 + 60
                        if dur >= 60:
                            court_blocks.append({
                                "court": cs_sorted[0]["court_name"],
                                "start": _sec_to_hhmm(run_start),
                                "end": _sec_to_hhmm(run_end + 3600),
                                "duration_min": dur,
                                "price": run_slots[0].get("price"),
                                "full_price": run_slots[0].get("full_price"),
                            })
                        run_start = run_end = sec
                        run_slots = [s]
                if run_start is not None:
                    dur = (run_end - run_start) // 60 + 60
                    if dur >= 60:
                        court_blocks.append({
                            "court": cs_sorted[0]["court_name"],
                            "start": _sec_to_hhmm(run_start),
                            "end": _sec_to_hhmm(run_end + 3600),
                            "duration_min": dur,
                            "price": run_slots[0].get("price"),
                            "full_price": run_slots[0].get("full_price"),
                        })

            results.append({
                "key": vk,
                "name": SL_VENUES[vk]["name"],
                "platform": "sportlogic",
                "date": target_date.isoformat(),
                "court_blocks": sorted(court_blocks, key=lambda x: x["start"]),
                "pricing_included": pricing and bool(jsid),
            })

    return {
        "date": target_date.isoformat(),
        "from": from_time,
        "to": to_time,
        "venues": results,
    }


# ── Combined ─────────────────────────────────────────────────────────────────

@app.get("/api/all")
async def all_availability(
    date: Optional[str] = Query(None),
    from_time: str = Query("16:00", alias="from"),
    to_time: str = Query("23:00", alias="to"),
    include_opensports: bool = Query(True),
    include_sportlogic: bool = Query(True),
):
    """
    Get everything — PBP + OpenSports + SportLogic in one call.

    This is the main endpoint for the dashboard.

    Example:
        GET /api/all?from=18:00&to=22:00
        GET /api/all?date=2026-05-20&from=09:00&to=23:00
    """
    target_date = (datetime.strptime(date, "%Y-%m-%d").date()
                   if date else datetime.today().date())
    from_sec = _hhmm_to_sec(from_time)
    to_sec = _hhmm_to_sec(to_time)

    # Run all platform fetches concurrently.
    pbp_task = asyncio.create_task(_fetch_all_pbp(target_date, from_sec, to_sec))

    os_task = (
        asyncio.create_task(_fetch_opensports(target_date))
        if include_opensports else asyncio.create_task(asyncio.coroutine(lambda: [])())
    )

    sl_task = (
        asyncio.create_task(_fetch_sportlogic(target_date, from_sec, to_sec))
        if include_sportlogic else asyncio.create_task(asyncio.coroutine(lambda: [])())
    )

    pbp_results, os_results, sl_results = await asyncio.gather(
        pbp_task, os_task, sl_task, return_exceptions=True
    )

    return {
        "date": target_date.isoformat(),
        "from": from_time,
        "to": to_time,
        "playbypoint": pbp_results if not isinstance(pbp_results, Exception) else [],
        "opensports": os_results if not isinstance(os_results, Exception) else [],
        "sportlogic": sl_results if not isinstance(sl_results, Exception) else [],
    }


async def _fetch_all_pbp(target_date: date, from_sec: int, to_sec: int) -> list:
    venues = await _get_pbp_venues()
    tasks = [
        _get_pbp_availability(
            v["id"], v["name"], v["slug"], target_date, from_sec, to_sec
        )
        for v in venues
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if not isinstance(r, Exception)]


async def _fetch_opensports(target_date: date) -> list:
    async with OpenSportsAPI() as api:
        raw = await api.search_sessions(
            latitude=-37.815, longitude=144.966, radius_km=35, limit=200
        )
    sessions = [parse_session(s) for s in raw]
    today_str = target_date.isoformat()
    sessions = [s for s in sessions
                if s["date"] == today_str and s["status"] != "Full"]
    for s in sessions:
        s.pop("raw", None)
    return sessions


async def _fetch_sportlogic(target_date: date, from_sec: int, to_sec: int) -> list:
    results = []
    for vk in SL_VENUES:
        try:
            async with SportLogicClient(vk) as client:
                slots = await client.get_availability(target_date)
                slots = [s for s in slots if from_sec <= s["hour"] * 3600 < to_sec]
                results.append({
                    "key": vk,
                    "name": SL_VENUES[vk]["name"],
                    "platform": "sportlogic",
                    "slots": [{"court": s["court_name"], "time": s["time"]}
                               for s in slots],
                })
        except Exception:
            pass
    return results


# ── PBP Booking ───────────────────────────────────────────────────────────────

class ValidateCredentialsRequest(BaseModel):
    email: str
    password: str


@app.post("/api/pbp/validate")
async def pbp_validate(req: ValidateCredentialsRequest):
    """
    Validate PBP credentials by attempting to fetch the user profile.
    Returns user_id and email on success, raises 401 on failure.
    This endpoint is called when a user connects their PBP account.
    """
    try:
        import base64
        # Try logging in via the API using email/password.
        # PBP uses a Rails session login — we POST to /users/sign_in.
        login_url = "https://app.playbypoint.com/users/sign_in"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        payload = {
            "user": {
                "email": req.email,
                "password": req.password,
            }
        }
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.post(login_url, json=payload, headers=headers)

            if resp.status_code in (200, 201):
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                user_id = data.get("id") or data.get("user_id")
                cookies = dict(resp.cookies)
                if cookies or user_id:
                    return {
                        "valid": True,
                        "user_id": user_id,
                        "email": req.email,
                        "cookies": cookies,
                    }

            # Try alternate endpoint.
            resp2 = await client.post(
                "https://app.playbypoint.com/api/users/sign_in",
                json=payload,
                headers=headers,
            )
            if resp2.status_code in (200, 201):
                data2 = resp2.json() if resp2.headers.get("content-type", "").startswith("application/json") else {}
                user_id = data2.get("id") or data2.get("user_id")
                cookies = dict(resp2.cookies)
                return {
                    "valid": True,
                    "user_id": user_id,
                    "email": req.email,
                    "cookies": cookies,
                }

            raise HTTPException(status_code=401, detail="Invalid email or password. Please check your PlayByPoint credentials.")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not verify credentials: {e}")


class CourtBookingRequest(BaseModel):
    user_id: str
    facility_id: int
    date: str           # YYYY-MM-DD
    start_time: str     # HH:MM
    end_time: str       # HH:MM
    court_name: str     # e.g. "Court 1"
    payment_method: str = "card"


def _hhmm_to_sec(hhmm: str) -> int:
    """Convert HH:MM to seconds from midnight."""
    h, m = map(int, hhmm.split(':'))
    return h * 3600 + m * 60


@app.post("/api/pbp/book_court")
async def pbp_book_court(req: CourtBookingRequest):
    """
    Book a court hire slot on PBP using stored user session cookies.
    """
    # Fetch user's stored PBP cookies from Supabase.
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        }
        url = f"{SUPABASE_URL}/rest/v1/pbp_credentials?user_id=eq.{req.user_id}&select=pbp_cookies,pbp_user_id,is_connected"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            rows = resp.json() if resp.status_code == 200 else []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not fetch credentials: {e}")

    if not rows:
        raise HTTPException(status_code=401, detail="PBP account not connected. Please connect in Profile settings.")

    row = rows[0]
    cookies = row.get("pbp_cookies")
    pbp_user_id = row.get("pbp_user_id")
    if not cookies or not pbp_user_id:
        raise HTTPException(status_code=401, detail="PBP session not yet active. Please run the sync script or wait a few minutes.")

    try:
        slug = PBP_SLUG_MAP.get(req.facility_id, "")
        target_date = datetime.strptime(req.date, "%Y-%m-%d").date()
        start_sec = _hhmm_to_sec(req.start_time)
        end_sec = _hhmm_to_sec(req.end_time)

        async with PlayByPointAPI(cookies=cookies, club_slug=slug, proxy=PROXY_URL) as api:
            api._user_id = pbp_user_id

            # Get surface type.
            surface = "pickleball"
            try:
                ct = await api.court_types(req.facility_id)
                ps = [s for s in (ct or []) if "pickle" in (s.get("surface") or "").lower()]
                if ps:
                    surface = ps[0]["surface"]
            except Exception:
                pass

            # Get court ID for the requested court name.
            courts = await api.available_courts(
                req.facility_id, target_date,
                start_sec, start_sec + 1800,
                surface=surface,
            )
            court_id = None
            for c in (courts or []):
                if c.get("name", "").lower() == req.court_name.lower():
                    court_id = c.get("id")
                    break

            if not court_id:
                raise HTTPException(
                    status_code=404,
                    detail=f"Court '{req.court_name}' not found or no longer available."
                )

            # Make the real booking — CSRF token fetched live via proxy.
            result = await api.book_court(
                court_id=court_id,
                day=target_date,
                start_seconds=start_sec,
                end_seconds=end_sec,
                user_id=pbp_user_id,
                payment_method=req.payment_method,
                dry_run=False,
            )

            return {
                "success": True,
                "message": "Court booked!",
                "booking": result,
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Booking failed: {e}")



    user_id: str
    lesson_id: int
    clinic_id: int
    plan_id: Optional[int] = None
    program_slug: Optional[str] = None


@app.post("/api/pbp/book")
async def pbp_book(req: BookingRequest):
    """
    Book a PBP session on behalf of a user using their stored session cookies.
    Requires the user to have connected their PBP account via the profile page.
    """
    # Fetch user's stored PBP cookies from Supabase.
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        }
        url = f"{SUPABASE_URL}/rest/v1/pbp_credentials?user_id=eq.{req.user_id}&select=pbp_cookies,pbp_user_id,is_connected,session_valid_until"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            rows = resp.json() if resp.status_code == 200 else []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not fetch user credentials: {e}")

    if not rows:
        raise HTTPException(status_code=401, detail="PBP account not connected. Please connect your account in Profile settings.")

    row = rows[0]
    cookies = row.get("pbp_cookies")
    pbp_user_id = row.get("pbp_user_id")
    if not cookies or not pbp_user_id:
        raise HTTPException(status_code=401, detail="PBP session not yet active. Please run the sync script or wait a few minutes.")

    # Use the stored cookies to make the booking.
    try:
        slug = req.program_slug or PBP_SLUG_MAP.get(req.clinic_id, "")
        async with PlayByPointAPI(cookies=cookies, club_slug=slug, proxy=PROXY_URL) as api:
            api._user_id = pbp_user_id

            # Get plan_id if not provided.
            plan_id = req.plan_id
            if not plan_id:
                lesson_data = await api._get_json(f"/api/public/clinics/{req.clinic_id}")
                prices = (lesson_data or {}).get("prices") or []
                packages = (lesson_data or {}).get("packages") or []
                all_plans = prices + packages
                # Pick lowest-price non-hidden single-session plan.
                single = [p for p in all_plans if p.get("lessons") == 1 and not p.get("hidden") and p.get("available_for_players")]
                if not single:
                    single = [p for p in all_plans if not p.get("hidden") and p.get("available_for_players")]
                if not single:
                    raise HTTPException(status_code=400, detail="No available booking plan found for this session.")
                plan_id = sorted(single, key=lambda p: p.get("price", 999))[0]["id"]

            # Make the real booking using the HAR-verified method.
            result = await api.book_program(
                clinic_id=req.clinic_id,
                plan_id=plan_id,
                clinic_lesson_ids=[req.lesson_id],
                program_slug=slug,
                payment_method="card",
                dry_run=False,
            )

            return {
                "success": True,
                "message": "Booking confirmed!",
                "booking": result,
            }

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Booking failed: {e}")
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Booking failed: {e}")


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "api_server:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
