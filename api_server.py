"""
api_server.py  —  PickleMatch FastAPI backend

Wraps extract_thejar.py
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
import venue_registry
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
import sys

from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException, Request
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
import stripe

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
# The connected-accounts destination signs with its own secret.
WEBHOOK_SECRET_CONNECT = os.environ.get("STRIPE_WEBHOOK_SECRET_CONNECT", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "pk_live_51TrY95K2PC6iTJ2oGj2s58hhTGMtSllc8hwCMyfX55jY8oWz8QeEVm4DpVRQDgWt1zI5Lg09kcSbK1cQdnHW0iZk00Pw42nNZn")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

from extract_thejar import (
    PlayByPointAPI,
    _load_cached_session,
    _extract_react_props_from_html,
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

    # Local cache file at /app/.pbp_cookies.json
    try:
        from pathlib import Path
        p = Path('/app/.pbp_cookies.json')
        if p.exists():
            data = json.loads(p.read_text())
            cookies = data.get('cookies', {})
            if cookies:
                return cookies, data.get('user_id', 0), data.get('email', '')
    except Exception:
        pass
    # Fallback to USER_DATA_DIR cache
    cookies, user_id, email = _load_cached_session()
    if cookies:
        return cookies, user_id, email

    return {}, 0, ""

# ── App setup ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="PickleMatch API",
    description="Real-time pickleball court availability aggregator",
    version="1.0.0",
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
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", SUPABASE_KEY)
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
# In-memory cache for live court fetches (facility_id+date -> {blocks, expires})
_live_courts_cache: dict[str, dict] = {}

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
    1696: "picklezone",
    1770: "rayapickleballclub",
    1783: "PICKLE4REAL",
    1883: "TheJarHQ",
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
    1696: "Picklezone",
    1770: "Raya Pickleball Club",
    1783: "PICKLE4REAL",
    1883: "The Jar HQ | Maidstone",
}

VENUE_SURFACES: dict[int, list[str]] = {
    885: ["pickleball"],
    1557: ["standard_courts", "championship_courts"],
    1379: ["main_courts"],
    # 1783 removed: it held the bare string "Pickle4Real" where a list of
    # surfaces was required. The consumer below iterates this value, so
    # Pickle4Real requested thirteen single-character surfaces -- "P", "i",
    # "c", "k"... -- each answered by PBP with an empty 200 that is
    # indistinguishable from "fully booked". Not replaced with the correct
    # surfaces: patching this map venue-by-venue is the defect class Phase 2
    # removes. With no entry it falls through to discovery like the other 17
    # venues, which is strictly better than iterating a venue name.
}

# ── Venue identity ──────────────────────────────────────────────────────────
#
# PBP_SLUG_MAP / VENUE_NAMES above are superseded by venue_registry and have
# no remaining consumers. They are kept only until the scrapers and frontend
# have migrated too, then deleted. Do not add to them.

def registry_slug(facility_id) -> str | None:
    """
    Slug for a FETCH path -- availability, court blocks, anything that goes
    out to PBP on a venue's behalf.

    Returns None for a facility the registry does not know AND for one that
    is delisted, so callers skip it. Never a default that would address
    another venue.

    Delisted must be excluded here specifically. venue_registry.resolve()
    resolves delisted venues on purpose, so historical favourites and past
    bookings still render a real name -- but resolving for DISPLAY and
    resolving for FETCHING are different questions. Using resolve() for both
    made /api/pbp/venue/1826 serve Pickleball Paradise with a live slug and
    fetch its availability, which is exactly what delisting is meant to
    stop. Use registry_name() when the caller only needs to label a
    historical reference.
    """
    v = venue_registry.resolve(facility_id)
    if isinstance(v, venue_registry.Venue) and v.status == "active":
        return v.slug
    return None


def registry_name(facility_id) -> str:
    """
    Display name for ANY registered facility, including delisted ones, so a
    historical reference reads 'Pickleball Paradise' rather than a bare id.
    Naming a venue is not the same as offering it.
    """
    v = venue_registry.resolve(facility_id)
    return v.name if isinstance(v, venue_registry.Venue) else f"Venue {facility_id}"


def active_slug_map() -> dict[int, str]:
    """{facility_id: slug} for every active venue -- replaces iterating
    PBP_SLUG_MAP, which was missing SportsWell and Pickleball Paradise."""
    return {v.facility_id: v.slug for v in venue_registry.active_venues()}


def bookable_slug(facility_id) -> str:
    """Slug for a booking path. Fails closed."""
    try:
        return venue_registry.get_bookable_venue(facility_id).slug
    except venue_registry.RegistryError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
                slug = registry_slug(fid) or ""
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

            # Reviewed classification, not a name heuristic. Takes the
            # first court surface where a single one is needed; the old code
            # took the first surface merely CONTAINING "pickle", which is a
            # different and wrong test.
            import court_surfaces
            surface = None
            try:
                ct = await api.court_types(facility_id, kind=None)
                res = court_surfaces.resolve_surfaces(facility_id, ct or [])
                if res.unknown:
                    logger.warning(res.diagnostic())
                surface = res.court[0] if res.court else None
            except Exception as e:
                logger.warning("facility %s: surface resolution failed (%s)",
                               facility_id, e)
            if surface is None:
                # No "pickleball" default: PBP answers an unknown surface
                # with an empty 200 that is indistinguishable from a fully
                # booked venue.
                logger.warning("facility %s: no court surface, skipping",
                               facility_id)
                return []

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

async def _warm_single_date(target):
    from datetime import date as date_type
    date_str = target.isoformat()
    cache_key = f"availability:{date_str}:00:00:23:30:all"
    if _cache_get(cache_key):
        return
    cookies, user_id, _ = _load_session_with_env_fallback()
    if not cookies:
        return
    try:
        from_sec, to_sec = _hhmm_to_sec("00:00"), _hhmm_to_sec("23:30")
        results = await asyncio.gather(*[
            _get_pbp_availability(fid, registry_name(fid), slug, target, from_sec, to_sec)
            for fid, slug in active_slug_map().items()
        ], return_exceptions=True)
        court_blocks_by_id = {r["id"]: r.get("court_blocks", []) for r in results if isinstance(r, dict)}
        supabase_data = await _read_from_supabase("playbypoint")
        output = []
        for r in supabase_data:
            vid = r.get("id")
            filtered_sessions = r.get("sessions", [])
            output.append({"id": vid, "name": r.get("name"), "slug": r.get("slug"), "platform": "playbypoint", "court_blocks": court_blocks_by_id.get(vid, []), "sessions": filtered_sessions, "error": None})
        response = {"date": date_str, "from": "00:00", "to": "23:30", "venues": output, "total_court_blocks": sum(len(v["court_blocks"]) for v in output), "total_sessions": sum(len(v["sessions"]) for v in output), "source": "live", "cached_count": len(output)}
        _cache_set(cache_key, response)
        logger.info(f"Cache warmed for {date_str}: {response['total_court_blocks']} blocks, {response['total_sessions']} sessions")
    except Exception as e:
        logger.error(f"Warm error for {target}: {e}")


async def _warm_cache():
    from datetime import date as date_type
    await asyncio.sleep(5)
    targets = [date_type.fromordinal(date_type.today().toordinal() + i) for i in range(7)]
    for t in targets:
        await _warm_single_date(t)
        await asyncio.sleep(3)

async def _cache_refresh_loop():
    """Refresh cache: 60 min overnight (10pm-6am), 10 min otherwise. Skips :00-:10 on even hours to avoid cron collision."""
    from datetime import datetime
    import zoneinfo
    mel = zoneinfo.ZoneInfo("Australia/Melbourne")
    while True:
        now = datetime.now(mel)
        if now.minute < 10 and now.hour % 2 == 0:
            await asyncio.sleep(60)
            continue
        interval = 3600 if (now.hour >= 22 or now.hour < 6) else 600
        await asyncio.sleep(interval)
        await _warm_cache()


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(_warm_cache())
    asyncio.create_task(_cache_refresh_loop())


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
            {"id": fid, "name": registry_name(fid), "slug": slug, "platform": "playbypoint"}
            for fid, slug in active_slug_map().items()
        ]
    return {"venues": venues, "count": len(venues)}


BOOKING_SERVER_URL = os.environ.get("BOOKING_SERVER_URL", "https://booking.picklematch.com.au")


async def _personalise_prices(response, user_id):
    """
    Overlay `resolved_price` on sessions priced differently for this member.

    Pricing authority stays in booking_server, which owns the resolver and
    the affiliation data. Copying the matching rule here would recreate the
    divergence this work removed, where two endpoints held two versions of
    the same pricing logic and silently drifted apart.

    ONE batched call for the whole page, not one per session: forty cards
    cost a single request. It makes no PBP calls -- everything resolves
    from cached tiers and the member's stored affiliation.

    Enhancement, never a dependency. Any failure returns the response
    untouched, so the sessions page keeps working with public prices if the
    booking server is slow, down, or unreachable. Discovery must not break
    because personalisation did.

    Returns a COPY. The shared cache object is never mutated, so one user's
    price cannot leak into another's response.
    """
    if not user_id or not isinstance(response, dict):
        return response

    venues = response.get("venues") or []
    wanted = []
    for v in venues:
        fid = v.get("id")
        for s in (v.get("sessions") or []):
            if fid and s.get("lesson_id"):
                wanted.append({"lesson_id": s["lesson_id"], "facility_id": fid})
    if not wanted:
        return response

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.post(
                f"{BOOKING_SERVER_URL}/api/resolve-prices",
                json={"user_id": user_id, "sessions": wanted},
                headers={"X-Internal-Key": SUPABASE_SERVICE_KEY or ""},
            )
        if r.status_code != 200:
            print(f"personalised pricing HTTP {r.status_code}: {r.text[:200]}", flush=True)
            return response
        resolved = (r.json() or {}).get("resolved") or {}
    except Exception as e:
        print(f"personalised pricing unavailable: {e}", flush=True)
        return response

    if not resolved:
        return response

    # Copy only what is modified; the rest is shared by reference.
    out = dict(response)
    new_venues = []
    for v in venues:
        sessions = v.get("sessions") or []
        if not any(str(s.get("lesson_id")) in resolved for s in sessions):
            new_venues.append(v)
            continue
        nv = dict(v)
        nv["sessions"] = [
            {**s, "resolved_price": resolved[str(s.get("lesson_id"))]}
            if str(s.get("lesson_id")) in resolved else s
            for s in sessions
        ]
        new_venues.append(nv)
    out["venues"] = new_venues
    return out


@app.get("/api/pbp/availability")
async def pbp_availability(
    date: Optional[str] = Query(None, description="YYYY-MM-DD, default today"),
    from_time: str = Query("16:00", alias="from"),
    to_time: str = Query("23:00", alias="to"),
    venue_ids: Optional[str] = Query(None, description="Comma-separated IDs, default all saved"),
    # Named pm_user_id, not the obvious alternative:
    # _load_session_with_env_fallback() below rebinds that shorter name to
    # the PBP scraper id, which silently replaced the parameter on the
    # cache-miss path. Personalisation therefore worked on a cache hit and
    # failed on a miss, which is why it appeared intermittent.
    pm_user_id: Optional[str] = Query(None, alias="user_id",
                                      description="PickleMatch user, for personalised pricing"),
):
    """
    Get court blocks + sessions for PBP venues via live API calls through residential proxy.
    Falls back to Supabase cache if proxy calls fail.

    When `user_id` is supplied, sessions where that member's price differs
    gain a `resolved_price`. `price` is never modified, so callers read
    `resolved_price ?? price` and anonymous responses are untouched.

    `user_id` is an identity SELECTOR, not an authenticated claim -- anyone
    could pass another user's id. Acceptable here because the endpoint
    performs no writes and exposes no credentials or payment data; the only
    consequence is seeing which cached price another account would be
    shown. Matching the existing /api/pbp/connect pattern. Hardening API
    identity is a separate change, not one to make inside a pricing fix.
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
        # AFTER the cache read, never before: the cache key has no user in
        # it, so personalising anything that gets stored would serve one
        # member's price to everyone.
        return await _personalise_prices(cached_result, pm_user_id)

    slug_map = {k: v for k, v in active_slug_map().items() if not ids_filter or k in ids_filter}

    cookies, user_id, _ = _load_session_with_env_fallback()

    # Read court blocks from Supabase by_date (populated by GitHub Actions)
    court_blocks_by_id = {}

    # Fetch sessions from Supabase cache
    supabase_data = await _read_from_supabase("playbypoint")
    for r in supabase_data:
        vid = r.get("id")
        by_date_data = r.get("by_date", {})
        court_blocks_by_id[vid] = by_date_data.get(date_str, [])
    if ids_filter:
        supabase_data = [r for r in supabase_data if r.get("id") in ids_filter]

    output = []
    for r in supabase_data:
        vid = r.get("id")
        all_sessions = r.get("sessions", [])
        # Return all dates' sessions, frontend filters by date
        filtered_sessions = all_sessions
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
    # Personalise only the copy being returned. The object handed to
    # _cache_set above stays public.
    return await _personalise_prices(response, pm_user_id)


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
        "name": registry_name(facility_id),
        "slug": registry_slug(facility_id) or "",
        "platform": "playbypoint",
        "court_blocks": [],
        "sessions": [],
        "error": "not_in_cache",
    }


# ── Combined ─────────────────────────────────────────────────────────────────

@app.get("/api/all")
async def all_availability(
    date: Optional[str] = Query(None),
    from_time: str = Query("16:00", alias="from"),
    to_time: str = Query("23:00", alias="to"),
):
    """Get all PBP availability in one call."""
    target_date = (datetime.strptime(date, "%Y-%m-%d").date()
                   if date else datetime.today().date())
    from_sec = _hhmm_to_sec(from_time)
    to_sec = _hhmm_to_sec(to_time)
    pbp_results = await _fetch_all_pbp(target_date, from_sec, to_sec)
    return {
        "date": target_date.isoformat(),
        "from": from_time,
        "to": to_time,
        "playbypoint": pbp_results,
    }


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
        slug = bookable_slug(req.facility_id)
        target_date = datetime.strptime(req.date, "%Y-%m-%d").date()
        start_sec = _hhmm_to_sec(req.start_time)
        end_sec = _hhmm_to_sec(req.end_time)

        async with PlayByPointAPI(cookies=cookies, club_slug=slug, proxy=PROXY_URL) as api:
            api._user_id = pbp_user_id

            # Reviewed classification, not a name heuristic.
            import court_surfaces
            ct = await api.court_types(req.facility_id, kind=None)
            res = court_surfaces.resolve_surfaces(req.facility_id, ct or [])
            if res.unknown:
                logger.warning(res.diagnostic())
            if not res.court:
                raise HTTPException(
                    status_code=400,
                    detail=f"no court surfaces at facility {req.facility_id}")
            surface = res.court[0]

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
        # Was: req.program_slug or PBP_SLUG_MAP.get(req.clinic_id, "")
        # -- a CLINIC id looked up in a FACILITY map. Clinic ids are
        # six-figure (226121, 179528); facility ids are 597-1883, so that
        # lookup could never match and always yielded "". Behaviour is
        # unchanged; the type confusion is not carried into the registry.
        slug = req.program_slug or ""
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


# ─── PBP Account Connect ──────────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    user_id: str
    email: str
    password: str


@app.post("/api/pbp/connect")
async def pbp_connect(req: ConnectRequest):
    import re
    from curl_cffi.requests import AsyncSession
    from playwright.async_api import async_playwright

    try:
        # Step 1: curl_cffi login
        session = AsyncSession(impersonate="chrome124")
        r = await session.get("https://app.playbypoint.com/users/sign_in")
        csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
        if not csrf_match:
            raise HTTPException(status_code=500, detail="Could not load PBP login page.")
        token = csrf_match.group(1)
        get_cookies = dict(r.cookies)

        r2 = await session.post(
            "https://app.playbypoint.com/users/sign_in",
            json={"user": {"email": req.email, "password": req.password, "remember_me": "1"}},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-CSRF-Token": token,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://app.playbypoint.com/users/sign_in",
            },
            cookies=get_cookies,
        )
        resp_json = r2.json() if r2.headers.get("content-type", "").startswith("application/json") else {}
        if not resp_json.get("success"):
            raise HTTPException(status_code=401, detail="Invalid PBP email or password.")

        session_cookie = dict(r2.cookies).get("_paybycourt_session") or get_cookies.get("_paybycourt_session")
        if not session_cookie:
            raise HTTPException(status_code=401, detail="Login failed — no session cookie returned.")

        # Step 2: Playwright loads home to get user_id
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            )
            await context.add_cookies([{
                "name": "_paybycourt_session",
                "value": session_cookie,
                "domain": "app.playbypoint.com",
                "path": "/",
            }])
            page = await context.new_page()
            await page.goto("https://app.playbypoint.com/home", timeout=30000)
            for _ in range(10):
                await page.wait_for_timeout(2000)
                title = (await page.title()).lower()
                if "just a moment" not in title and "cloudflare" not in title:
                    break
            html = await page.content()
            uid_match = re.search(r'"user_id"\s*:\s*(\d+)', html)
            pbp_user_id = int(uid_match.group(1)) if uid_match else 0
            pw_cookies = await context.cookies()
            all_cookies = {c["name"]: c["value"] for c in pw_cookies}
            await browser.close()

        # Step 3: Upsert to Supabase
        from datetime import datetime, timedelta
        svc_headers = {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        payload = {
            "user_id": req.user_id,
            "pbp_email": req.email,
            "pbp_cookies": all_cookies,
            "pbp_user_id": pbp_user_id,
            "is_connected": True,
            "last_synced_at": datetime.utcnow().isoformat(),
            "session_valid_until": (datetime.utcnow() + timedelta(days=30)).isoformat(),
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try PATCH first
            resp = await client.patch(
                f"{SUPABASE_URL}/rest/v1/pbp_credentials?user_id=eq.{req.user_id}",
                headers=svc_headers,
                json=payload,
            )
            # If no existing row, INSERT
            if resp.status_code == 200 and resp.json() == []:
                resp = await client.post(
                    f"{SUPABASE_URL}/rest/v1/pbp_credentials",
                    headers={**svc_headers, "Prefer": "return=minimal"},
                    json=payload,
                )
            if resp.status_code not in (200, 201, 204):
                raise HTTPException(status_code=500, detail=f"Failed to save credentials: {resp.text}")

        # Step 4: Fetch DUPR ratings
        dupr_rating = None
        dupr_rating_doubles = None
        dupr_rating_name = None
        try:
            from curl_cffi.requests import AsyncSession as _Session
            _sess = _Session(impersonate="chrome124")
            _r = await _sess.get(
                f"https://app.playbypoint.com/api/users/{pbp_user_id}/ratings",
                cookies=all_cookies,
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
            if _r.status_code == 200:
                _ratings = _r.json().get("ratings", [])
                _dupr = next((x for x in _ratings if x.get("provider") == "dupr"), None)
                _nrp = next((x for x in _ratings if x.get("provider") == "ntrp_default"), None)
                _best = _dupr or _nrp
                if _best:
                    dupr_rating = _best.get("single")
                    dupr_rating_doubles = _best.get("double")
                    dupr_rating_name = "DUPR" if _dupr else "NRP"
            if dupr_rating is not None or dupr_rating_doubles is not None:
                _upd = {}
                if dupr_rating is not None: _upd["dupr_rating"] = dupr_rating
                if dupr_rating_doubles is not None: _upd["dupr_rating_doubles"] = dupr_rating_doubles
                if dupr_rating_name: _upd["dupr_rating_name"] = dupr_rating_name
                async with httpx.AsyncClient(timeout=10.0) as _c:
                    await _c.patch(
                        f"{SUPABASE_URL}/rest/v1/pbp_credentials?user_id=eq.{req.user_id}",
                        headers=svc_headers,
                        json=_upd,
                    )
        except Exception:
            pass
        # Step 5: Sync memberships and rating via booking server (fire and forget).
        # The inline DUPR fetch above (Step 4) uses cookies still mid-connection
        # and can silently fail (broad except/pass, no logging). This call
        # re-reads the now-persisted session cookies instead, which testing
        # confirmed reliably works even when Step 4 doesn't -- so it's not
        # just a duplicate, it's the actual fix for new users never getting
        # a rating synced.
        try:
            async with httpx.AsyncClient(timeout=15.0) as _mc:
                await _mc.post(f"https://booking.picklematch.com.au/api/sync_memberships/{req.user_id}")
                await _mc.post(f"https://booking.picklematch.com.au/api/sync_rating/{req.user_id}")
        except Exception:
            pass

        return {
            "success": True,
            "pbp_email": req.email,
            "pbp_user_id": pbp_user_id,
            "dupr_rating": dupr_rating,
            "dupr_rating_doubles": dupr_rating_doubles,
            "dupr_rating_name": dupr_rating_name,
            "message": "PlayByPoint account connected successfully.",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection failed: {e}")

# ── Live session fetch for extended dates ─────────────────────────────────────
def _extract_tiers(records):
    """
    Shape PBP pricing records for the resolver.

    Field selection only -- no pricing decision is made here. Which tier
    applies to a user is decided solely by the resolver in booking_server,
    and this endpoint deliberately holds no part of that rule.

    Mirrors the scraper's shaping in push_to_supabase.py. The two live in
    separate repositories on separate hosts and cannot share code; keeping
    them consistent is a small, visible cost, and far preferable to a second
    copy of the matching logic.
    """
    out = []
    for r in (records or []):
        if not isinstance(r, dict) or r.get("hidden"):
            continue
        raw = r.get("price")
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        tier = {"price": value, "player_category": r.get("player_category")}
        # lesson_unit must be carried: the resolver uses it to decide whether
        # a programme-level price may be shown as a session price, and
        # excludes anything unlabelled. Omitting it here meant live-scraped
        # programme pricing was silently ineligible.
        for key in ("allowed_affiliations", "lessons", "lesson_unit",
                    "lesson_details", "time_unit",
                    "time_range_start", "time_range_end"):
            if r.get(key) is not None:
                tier[key] = r.get(key)
        out.append(tier)
    return out


async def _personalise_live_sessions(sessions, user_id):
    """
    Overlay `resolved_price` on live-scraped sessions.

    Sends the tiers inline: these sessions were scraped just now and need
    not be in availability_cache, so the resolver could not look them up.

    Same batch endpoint and same frozen resolver as cached discovery, so
    both paths price identically. An enhancement, never a dependency --
    any failure returns the sessions untouched, because "search my member
    venues" must keep working even when personalisation cannot.
    """
    if not user_id or not sessions:
        return sessions

    payload = [{"lesson_id": s["lesson_id"], "facility_id": s["facility_id"],
                "price": s.get("price"), "price_tiers": s.get("price_tiers") or []}
               for s in sessions if s.get("lesson_id") and s.get("facility_id")]
    if not payload:
        return sessions

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.post(
                f"{BOOKING_SERVER_URL}/api/resolve-prices",
                json={"user_id": user_id, "sessions": payload},
                headers={"X-Internal-Key": SUPABASE_SERVICE_KEY or ""},
            )
        if r.status_code != 200:
            # Logged, not silent: a non-2xx here previously produced exactly
            # the same observable result as "this member has no discount",
            # which made a broken call indistinguishable from correct
            # behaviour.
            print(f"live_sessions personalisation HTTP {r.status_code}: {r.text[:200]}", flush=True)
            return sessions
        resolved = (r.json() or {}).get("resolved") or {}
    except Exception as e:
        print(f"live_sessions personalisation unavailable: {e}", flush=True)
        return sessions

    for s in sessions:
        hit = resolved.get(str(s.get("lesson_id")))
        if hit:
            s["resolved_price"] = hit
    return sessions


@app.get("/api/live_sessions")
async def live_sessions(
    facility_ids: str = Query(...),  # comma-separated facility IDs
    date: str = Query(...),          # YYYY-MM-DD
    # Named pm_user_id, not the obvious alternative: this function already
    # binds that shorter name to the PBP scraper id from the cookie file
    # below, which silently overwrote the parameter and sent PBP's numeric
    # id to the pricing service instead of the caller's UUID.
    pm_user_id: Optional[str] = Query(None, alias="user_id",
                                      description="PickleMatch user, for personalised pricing"),
):
    """
    Scrape sessions live from PBP for specific venues on a specific date.

    This backs the "search my member venues" feature, so it is precisely
    where a member is most likely to be looking -- and it filtered member
    pricing out entirely. With `user_id`, sessions priced differently for
    that member gain `resolved_price`; `price` stays the public figure.
    """
    import re
    from extract_thejar import _extract_react_props_from_html

    fids = [int(x.strip()) for x in facility_ids.split(",") if x.strip().isdigit()]
    target_date = datetime.strptime(date, "%Y-%m-%d").date()
    date_str = target_date.isoformat()

    # Load system cookies
    try:
        with open("/app/.pbp_cookies.json") as f:
            cookie_data = json.load(f)
        cookies = cookie_data.get("cookies", {})
        user_id = cookie_data.get("user_id", 0)
    except Exception:
        cookies = {}
        user_id = 0

    all_sessions = []

    for fid in fids:
        slug = registry_slug(fid)
        if not slug:
            continue
        try:
            async with PlayByPointAPI(cookies=cookies, club_slug=slug, proxy=PROXY_URL) as api:
                api._user_id = user_id
                resp = await api._get_json(
                    "/api/public/clinics",
                    params={"search": "", "facility_id": fid, "per_page": 50},
                )
                stubs = (resp or {}).get("clinics") or [] if isinstance(resp, dict) else (resp or [])
                for stub in stubs:
                    clinic_id = stub.get("id")
                    program_url = stub.get("url") or ""
                    program_slug = program_url.split("/programs/")[-1] if "/programs/" in program_url else ""
                    if not clinic_id or not program_slug:
                        continue
                    try:
                        html = await api.program_detail_html(program_slug)
                        props = _extract_react_props_from_html(html)
                        lessons_raw = props.get("sessions") or props.get("clinic_lessons") or []
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
                            if not pl.get("hidden") and pl.get("price") and pl.get("player_category") != "member":
                                p = float(pl["price"])
                                price = f"${p:.0f}" if p == int(p) else f"${p:.2f}"
                                break
                        # The loop above skips every member record, which is
                        # why this endpoint could never show a member price.
                        # Keep them; `price` stays the public figure.
                        program_tiers = _extract_tiers(
                            (props.get("prices") or []) + (props.get("packages") or []))
                        for lesson in lessons_raw:
                            ld = lesson.get("lesson_date")
                            if ld != date_str:
                                continue
                            lid = lesson.get("id")
                            cap = lesson.get("capacity") or stub.get("capacity") or 0
                            pc = lesson.get("player_count", 0)
                            spots = max(0, cap - pc) if cap else None
                            is_full = cap > 0 and spots == 0
                            hs = lesson.get("hour_start", 0)
                            he = lesson.get("hour_end", hs + 3600)
                            lp = price
                            for ip in (lesson.get("individual_prices") or []):
                                if ip.get("price") and ip.get("player_category") != "member":
                                    p = float(ip["price"])
                                    lp = f"${p:.0f}" if p == int(p) else f"${p:.2f}"
                                    break
                            # Per-lesson tiers where the clinic prices per
                            # session; otherwise the programme-level ones.
                            lesson_tiers = _extract_tiers(
                                lesson.get("individual_prices")) or program_tiers
                            all_sessions.append({
                                "facility_id": fid,
                                "title": stub.get("name", "Session"),
                                "type": stub.get("category") or "Session",
                                "date": ld,
                                "start": _sec_to_hhmm(hs),
                                "end": _sec_to_hhmm(he),
                                "price": lp,
                                "spots_left": spots,
                                "capacity": cap,
                                "status": "Full" if is_full else "Available",
                                "description": desc,
                                "skill_level": sl,
                                "lesson_id": lid,
                                "clinic_id": clinic_id,
                                "program_slug": program_slug,
                                "price_tiers": lesson_tiers,
                            })
                    except Exception:
                        continue
        except Exception as e:
            print(f"live_sessions error for {fid}: {e}", flush=True)
            continue

    all_sessions = await _personalise_live_sessions(all_sessions, pm_user_id)
    return {"sessions": all_sessions, "date": date_str, "fetched_at": datetime.utcnow().isoformat()}

# ── Announcements ─────────────────────────────────────────────────────────────
@app.get("/api/announcements/{facility_id}")
async def get_announcements(facility_id: int):
    """Get announcements for a specific facility."""
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/announcements",
            params={
                "facility_id": f"eq.{facility_id}",
                "select": "announcement_id,title,date_str,body_html,body_text,url,fetched_at",
                "order": "announcement_id.desc",
                "limit": "20",
            },
            headers=headers,
        )
    return {"announcements": resp.json() if resp.status_code == 200 else []}

@app.get("/api/live_courts")
async def live_courts(
    facility_id: int = Query(...),
    date: str = Query(...),  # YYYY-MM-DD
):
    """Fetch court blocks live from PBP for a specific venue and date."""
    from datetime import date as date_type
    import asyncio
    target_date = datetime.strptime(date, "%Y-%m-%d").date()
    slug = registry_slug(facility_id)
    if not slug:
        return {"court_blocks": [], "date": date, "error": "Unknown facility"}

    # Check in-memory cache (30 min TTL)
    import time
    cache_key = f"{facility_id}:{date}"
    cached = _live_courts_cache.get(cache_key)
    if cached and cached["expires"] > time.time():
        return {"court_blocks": cached["blocks"], "date": date, "facility_id": facility_id, "fetched_at": cached["fetched_at"], "cached": True}

    try:
        with open("/app/.pbp_cookies.json") as f:
            cookie_data = json.load(f)
        cookies = cookie_data.get("cookies", {})
        user_id = cookie_data.get("user_id", 0)
    except Exception:
        cookies = {}
        user_id = 0
    try:
        async with PlayByPointAPI(cookies=cookies, club_slug=slug, proxy=PROXY_URL) as api:
            api._user_id = user_id
            # Get surface type
            # Reviewed classification, not VENUE_SURFACES (3 of 21 venues,
            # one entry malformed) nor the "pickle" name heuristic. Both are
            # gone. Unfiltered on purpose: kind=reservation hides
            # members_only and training_court.
            import court_surfaces
            try:
                ct = await api.court_types(facility_id, kind=None)
                res = court_surfaces.resolve_surfaces(facility_id, ct or [])
                if res.unknown:
                    logger.warning(res.diagnostic())
                surfaces = res.court
            except Exception as e:
                logger.warning("facility %s: surface resolution failed (%s)",
                               facility_id, e)
                surfaces = []
            if not surfaces:
                # No fallback to "pickleball". Most venues do not have that
                # surface, and PBP answers an unknown surface with an empty
                # 200 that reads as "fully booked" -- the failure this whole
                # layer exists to remove. Return nothing, visibly.
                return {"court_blocks": [], "date": date,
                        "facility_id": facility_id,
                        "error": "no_court_surfaces"}
            court_slots: dict = {}
            sec_shift_map: dict = {}
            for surface in surfaces:
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
                        # PBP tells us the real shift directly -- no need to guess from time of day.
                        # Same shift label can mean a different real price on weekends
                        # (e.g. a weekend special), so tag with weekday/weekend to avoid
                        # weekday and weekend prices colliding under one cache key.
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
                        except Exception:
                            pass
                except Exception:
                    pass
            # Convert to blocks -- split at real shift boundaries (a run of
            # slots is only merged while PBP's own shift label stays the same,
            # so a 3pm-5pm run spanning lowtime->primetime becomes two blocks
            # instead of being priced under a single wrong guessed shift).
            def sec_to_hhmm(sec):
                return f"{int(sec)//3600:02d}:{(int(sec)%3600)//60:02d}"
            blocks = []
            for court_key, secs in court_slots.items():
                parts = court_key.split("|", 1)
                court_id = parts[0]
                cname = parts[1] if len(parts) > 1 else court_key
                secs_sorted = sorted(set(secs))
                run_start = run_end = None
                run_shift = None
                for s in secs_sorted:
                    s_shift = sec_shift_map.get(s)
                    if run_start is None:
                        run_start = run_end = s
                        run_shift = s_shift
                    elif s == run_end + 1800 and s_shift == run_shift:
                        run_end = s
                    else:
                        dur = (run_end - run_start) // 60 + 30
                        if dur >= 60:
                            blocks.append({"court": cname, "court_id": court_id, "start": sec_to_hhmm(run_start), "end": sec_to_hhmm(run_end + 1800), "duration_min": dur, "price": None, "shift": run_shift})
                        run_start = run_end = s
                        run_shift = s_shift
                if run_start is not None:
                    dur = (run_end - run_start) // 60 + 30
                    if dur >= 60:
                        blocks.append({"court": cname, "court_id": court_id, "start": sec_to_hhmm(run_start), "end": sec_to_hhmm(run_end + 1800), "duration_min": dur, "price": None, "shift": run_shift})
            # Get prices from cache -- keyed by court_id + REAL PBP shift, same
            # scheme fetch_court_blocks.py now uses.
            try:
                supabase_data = await _read_from_supabase("playbypoint")
                cached = next((r for r in supabase_data if r.get("id") == facility_id), {})
                court_prices = cached.get("court_prices", {})
                for block in blocks:
                    cid = block["court_id"]
                    shift = block.get("shift")
                    block["price"] = court_prices.get(f"{cid}_{shift}") if shift else None
            except Exception:
                pass
            import time as _time
            _live_courts_cache[f"{facility_id}:{date}"] = {"blocks": blocks, "expires": _time.time() + 1800, "fetched_at": datetime.utcnow().isoformat()}
            return {"court_blocks": blocks, "date": date, "facility_id": facility_id, "fetched_at": datetime.utcnow().isoformat()}
    except Exception as e:
        return {"court_blocks": [], "date": date, "error": str(e)[:200]}

# ── Stripe ────────────────────────────────────────────────────────────────────

class StripeConnectRequest(BaseModel):
    user_id: str
    return_url: str = "https://picklematch.com.au/profile"
    refresh_url: str = "https://picklematch.com.au/profile"

class PaymentIntentRequest(BaseModel):
    user_id: str
    session_id: str
    host_user_id: str
    amount: int  # in cents
    currency: str = "aud"
    description: str = "PickleMatch session"
    # WHAT THE PAYMENT IS FOR, not how to take it. The server derives
    # capture_method from this and the session's own settings; a client that
    # could name the capture method could decide not to be charged.
    # Defaults to "join" so an older client keeps its existing behaviour.
    purpose: str = "join"

class CaptureRequest(BaseModel):
    session_id: str
    payment_intent_id: str

@app.get("/api/stripe/config")
async def stripe_config():
    """Return publishable key for frontend."""
    return {"publishable_key": STRIPE_PUBLISHABLE_KEY}

@app.post("/api/stripe/connect")
async def stripe_connect(req: StripeConnectRequest):
    """Create or retrieve a Stripe Connect account for a host and return onboarding link."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    svc_headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    # Check if user already has a Stripe account
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{req.user_id}&select=stripe_account_id,stripe_onboarded",
            headers=svc_headers,
        )
        profile = r.json()[0] if r.json() else {}
        account_id = profile.get("stripe_account_id")

    # Create new Express account if needed
    if not account_id:
        account = stripe.Account.create(
            type="express",
            country="AU",
            capabilities={
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
            },
        )
        account_id = account.id
        # Save to Supabase
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{req.user_id}",
                headers=svc_headers,
                json={"stripe_account_id": account_id, "stripe_onboarded": False},
            )

    # Create account link for onboarding
    account_link = stripe.AccountLink.create(
        account=account_id,
        refresh_url=req.refresh_url,
        return_url=req.return_url,
        type="account_onboarding",
    )
    return {"url": account_link.url, "account_id": account_id}

@app.get("/api/stripe/status/{user_id}")
async def stripe_status(user_id: str):
    """Check if host has completed Stripe onboarding."""
    if not STRIPE_SECRET_KEY:
        return {"onboarded": False}
    svc_headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}&select=stripe_account_id,stripe_onboarded",
            headers=svc_headers,
        )
        profile = r.json()[0] if r.json() else {}
        account_id = profile.get("stripe_account_id")
        if not account_id:
            return {"onboarded": False, "account_id": None}

    # Verify with Stripe
    try:
        account = stripe.Account.retrieve(account_id)
        onboarded = account.charges_enabled and account.payouts_enabled
        if onboarded and not profile.get("stripe_onboarded"):
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}",
                    headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "Content-Type": "application/json"},
                    json={"stripe_onboarded": True},
                )
        return {"onboarded": onboarded, "account_id": account_id}
    except Exception as e:
        return {"onboarded": False, "account_id": account_id, "error": str(e)}

@app.post("/api/stripe/payment_intent")
async def create_payment_intent(req: PaymentIntentRequest):
    """Create a PaymentIntent for a player joining a session."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    svc_headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    # Check if this is an official PickleMatch session
    is_official = False
    requires_approval = False
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/created_sessions?id=eq.{req.session_id}&select=is_official,require_approval",
            headers=svc_headers,
        )
        session_data = r.json()[0] if r.json() else {}
        is_official = session_data.get("is_official", False)
        requires_approval = session_data.get("require_approval", False)

    # ══ HOLD OR CHARGE ═══════════════════════════════════════════════════
    # AUTHORISE NOW, CAPTURE LATER wherever the place is not yet the player's:
    # the card is held, and captured when it becomes theirs — or cancelled,
    # and nothing is ever taken. A player charged for a place they never got
    # has to be refunded, which is the failure this avoids.
    #
    # TWO PATHS NEED A HOLD, NOT ONE. This read `requires_approval` alone,
    # which was correct while approval was the only reason to wait. A WAITLIST
    # join also waits — for a place to open — and on an approval-off session it
    # was therefore CHARGED at once, while the client recorded it as a hold.
    # The worker recovered those with "ALREADY CAPTURED"; the intent should not
    # have needed recovering.
    #
    #     join, approval off      automatic
    #     join, approval on       manual — captured when the host approves
    #     waitlist, either way    manual — captured when a place opens
    is_waitlist = (req.purpose or "join").strip().lower() == "waitlist"
    capture_method = "manual" if (requires_approval or is_waitlist) else "automatic"

    if is_official:
        intent = stripe.PaymentIntent.create(
            amount=req.amount,
            currency=req.currency,
            description=req.description,
            metadata={
                "session_id": req.session_id,
                "player_user_id": req.user_id,
                "host_user_id": req.host_user_id,
            },
            capture_method=capture_method,
        )
        return {
            "client_secret": intent.client_secret,
            "payment_intent_id": intent.id,
            "account_id": None,
            # The caller cannot tell a hold from a capture, and it writes
            # `paid` from this result. Told, rather than left to guess.
            "capture_method": capture_method,
        }

    # Get host's Stripe account for Connect payment
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{req.host_user_id}&select=stripe_account_id,stripe_onboarded",
            headers=svc_headers,
        )
        profile = r.json()[0] if r.json() else {}
        account_id = profile.get("stripe_account_id")
        if not account_id or not profile.get("stripe_onboarded"):
            raise HTTPException(status_code=400, detail="Host has not completed Stripe setup")

    intent = stripe.PaymentIntent.create(
        amount=req.amount,
        currency=req.currency,
        description=req.description,
        metadata={
            "session_id": req.session_id,
            "player_user_id": req.user_id,
            "host_user_id": req.host_user_id,
        },
        stripe_account=account_id,
        capture_method=capture_method,
    )
    return {
        "client_secret": intent.client_secret,
        "payment_intent_id": intent.id,
        "account_id": account_id,
        "capture_method": capture_method,
    }

async def _connected_account_for_session(session_id: str, svc_headers: dict) -> str:
    """The host's Stripe account for a session.

    The host column on `created_sessions` is `user_id`. `process_refund` reads
    `created_by`, which is neither selected nor a real column, so it resolves
    to empty and refunds run against the PLATFORM account instead of the
    host's — which is why they fail.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/created_sessions?id=eq.{session_id}&select=user_id",
            headers=svc_headers,
        )
        row = r.json()[0] if r.json() else {}
        host_id = row.get("user_id")
        if not host_id:
            raise HTTPException(status_code=404, detail="Session not found")
        r2 = await client.get(
            f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{host_id}&select=stripe_account_id",
            headers=svc_headers,
        )
        prof = r2.json()[0] if r2.json() else {}
        account_id = prof.get("stripe_account_id")
        if not account_id:
            raise HTTPException(status_code=400, detail="Host has no Stripe account")
        return account_id


@app.post("/api/stripe/capture")
async def capture_payment(req: CaptureRequest):
    """Take money that has been held.

    Called when a host approves. Everything before this point is an
    authorisation: the funds are reserved and nothing has moved.

    The database is NOT written here. Capturing fires
    `payment_intent.succeeded`, and the webhook already sets `paid`, `paid_at`
    and `amount_paid` from it — writing them here as well would be a second
    author of the same fact.

    Idempotent by Stripe's own behaviour: capturing an already-captured intent
    returns an error naming that, which is reported rather than retried.
    """
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    svc_headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    account_id = await _connected_account_for_session(req.session_id, svc_headers)
    try:
        intent = stripe.PaymentIntent.capture(
            req.payment_intent_id,
            stripe_account=account_id,
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # WRITTEN HERE, because nothing else writes it. The webhook was the
    # intended author and has never received an event: it listens on the
    # PLATFORM account while every payment is a direct charge on the host's
    # connected one, so `payment_intent.succeeded` has fired zero times since
    # the endpoint was created.
    #
    # Synchronous suits this better anyway. The host approves, the money moves,
    # and the row says so in the same round trip — rather than whenever Stripe
    # gets round to delivering. The webhook is still worth fixing, as the way
    # to hear about disputes, expired holds and failed captures; it is not the
    # right place to learn that a payment the server just made succeeded.
    amount = intent.amount_received / 100
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/session_participants"
            f"?payment_intent_id=eq.{req.payment_intent_id}",
            headers={**svc_headers, "Content-Type": "application/json"},
            json={
                "paid": True,
                "paid_at": datetime.utcnow().isoformat(),
                "amount_paid": amount,
            },
        )
    return {
        "captured": True,
        "amount": amount,
        "status": intent.status,
    }


@app.post("/api/stripe/cancel")
async def cancel_payment(req: CaptureRequest):
    """Release a hold without taking anything.

    Called when a host declines. The authorisation disappears and the player
    is never charged — so there is no refund to owe, chase or reconcile, which
    is the whole reason for holding rather than capturing at request.
    """
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    svc_headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    account_id = await _connected_account_for_session(req.session_id, svc_headers)
    try:
        intent = stripe.PaymentIntent.cancel(
            req.payment_intent_id,
            stripe_account=account_id,
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"cancelled": True, "status": intent.status}
# ══ /api/stripe/refund REMOVED — 15 Aug 2026 ═══════════════════════════
# Unreachable, never functional, and a way for a second refund path to come
# back. It resolved the host account through `created_by` — neither selected
# nor a real column — so every call ran against the platform account rather
# than the host's connected one and failed. Its only caller, a Refund button
# on the legacy join page, sent three fields the model did not accept and so
# failed validation before reaching Stripe.
#
# Refunds are now an obligation the DATABASE records the moment a paid place
# is lost, settled by `settle_refunds.py` against the host's connected
# account. One path, one ledger, and `refunds.payment_intent_id` is unique so
# a charge cannot be refunded twice.
#
# Repairing this endpoint would have reintroduced exactly that: a manual
# refund the ledger cannot see, alongside a worker settling the same debt.

@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """What Stripe tells us that we did not ask about.

    Everything else in this file is a question we put to Stripe. This is the
    reverse, and it is the only way to learn things nobody can poll for: a
    player disputing a charge, a refund failing at the bank, a payment coming
    apart after the fact.

    IT NO LONGER WRITES `paid`. `/api/stripe/capture` does that synchronously,
    at the moment the money actually moves. This used to write the same three
    fields on `payment_intent.succeeded` — which under authorise-then-capture
    fires at capture, so the same fact had two authors and one of them arrived
    seconds or minutes late. A late write lands on a row whose situation may
    have changed since, and `refund_due_on_lost_place` fires on `paid IS TRUE`.

    So the branch became a RECONCILIATION: Stripe says the money moved, and if
    the row disagrees that is worth knowing rather than worth overwriting.

    CONNECTED ACCOUNTS. Payments are direct charges on the host's account, so
    these events arrive with `event.account` set. The signature is verified the
    same way; the account id is carried through for logging because a payment
    problem belongs to a particular host.
    """
    from fastapi import Request
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    # ══ TWO DESTINATIONS, TWO SECRETS ═══════════════════════════════════
    # Stripe splits events by source: the platform account sends onboarding
    # and official-session payments, connected accounts send every host's own
    # payments, refunds and disputes. A destination's source is fixed when it
    # is created, so there must be two — and Stripe signs each with its own
    # secret.
    #
    # Verified against either. With one secret, half the events fail signature
    # verification and are rejected — silently from our side, and looking
    # exactly like a webhook that is not being delivered at all, which is the
    # fault this whole endpoint spent a month exhibiting.
    secrets = [x for x in (STRIPE_WEBHOOK_SECRET, WEBHOOK_SECRET_CONNECT) if x]
    event = None
    last_error = None
    for secret in secrets:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, secret)
            break
        except Exception as e:
            last_error = e
    if event is None:
        raise HTTPException(status_code=400, detail=str(last_error or "no webhook secret configured"))

    svc_headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    kind = event["type"]
    # `construct_event` returns a StripeObject, not a dict: subscripting works,
    # `.get` does not. Present only on connected-account events, so it must be
    # read defensively rather than indexed.
    acct = event["account"] if "account" in event else None
    obj = event["data"]["object"]

    async def notify(user_id: str, ntype: str, message: str, link=None):
        """The same notifications table Host OS reads."""
        if not user_id:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/notifications",
                headers=svc_headers,
                json={"user_id": user_id, "type": ntype,
                      "message": message, "link": link},
            )

    async def participant_by_intent(intent_id: str):
        if not intent_id:
            return None
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/session_participants"
                f"?payment_intent_id=eq.{intent_id}"
                "&select=id,session_id,user_id,full_name,paid,status",
                headers=svc_headers,
            )
            rows = r.json() if r.status_code == 200 else []
            return rows[0] if rows else None

    async def host_of(session_id: str):
        if not session_id:
            return None, None
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/created_sessions?id=eq.{session_id}"
                "&select=user_id,title",
                headers=svc_headers,
            )
            rows = r.json() if r.status_code == 200 else []
            if not rows:
                return None, None
            return rows[0].get("user_id"), rows[0].get("title")

    # ── the money moved, and the row should already say so ────────────────
    if kind == "payment_intent.succeeded":
        p = await participant_by_intent(obj["id"])
        if p and not p.get("paid"):
            # Not corrected here. Capture writes `paid`, and a row that
            # disagrees means capture succeeded at Stripe and failed to record
            # it — a real inconsistency that wants a person, not a silent
            # patch that would hide how often it happens.
            print(f"[webhook] RECONCILE {obj['id']} acct={acct} — Stripe says "
                  f"succeeded, participant {p['id']} says paid=false", flush=True)

    # ── a player has taken their money back ───────────────────────────────
    elif kind == "charge.dispute.created":
        intent_id = obj["payment_intent"] if "payment_intent" in obj else None
        p = await participant_by_intent(intent_id)
        if p:
            host_id, title = await host_of(p["session_id"])
            who = p.get("full_name") or "A player"
            # THE HOST DECIDES. A dispute is a payment event, not a ruling on
            # whether someone plays — they may be someone the host knows, and
            # a dispute can be a mistake. So the place is left alone and the
            # host is told.
            await notify(
                host_id, "payment_disputed",
                f"{who} has disputed their payment for "
                f"{title or 'a session'}. Stripe has taken the money back from "
                f"your account. They still hold their place — remove them if "
                f"you want to.",
            )
            print(f"[webhook] DISPUTE {intent_id} acct={acct} "
                  f"participant={p['id']} session={p['session_id']}", flush=True)
        else:
            print(f"[webhook] DISPUTE {intent_id} acct={acct} — no participant "
                  f"row matches this charge", flush=True)

    # ── a refund landed; the ledger should already know ───────────────────
    elif kind == "charge.refunded":
        intent_id = obj["payment_intent"] if "payment_intent" in obj else None
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/refunds"
                f"?payment_intent_id=eq.{intent_id}&select=id,status",
                headers=svc_headers,
            )
            rows = r.json() if r.status_code == 200 else []
        if not rows:
            # A refund issued outside the ledger — from the Stripe dashboard,
            # most likely. Worth seeing: the ledger is meant to be the only
            # path, and this is how you learn it is not.
            print(f"[webhook] REFUND OUTSIDE LEDGER {intent_id} acct={acct}",
                  flush=True)
        elif rows[0]["status"] != "refunded":
            print(f"[webhook] RECONCILE refund {intent_id} acct={acct} — Stripe "
                  f"refunded, ledger says {rows[0]['status']}", flush=True)

    # ── a payment came apart ──────────────────────────────────────────────
    elif kind == "payment_intent.payment_failed":
        p = await participant_by_intent(obj["id"])
        lpe = obj["last_payment_error"] if "last_payment_error" in obj else None
        err = (lpe["message"] if lpe and "message" in lpe else "")
        print(f"[webhook] PAYMENT FAILED {obj['id']} acct={acct} "
              f"participant={p['id'] if p else 'none'} — {err}", flush=True)

    # ── onboarding finished, without anyone asking ────────────────────────
    elif kind == "account.updated":
        account = obj
        if account["charges_enabled"] and account["payouts_enabled"]:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/profiles?stripe_account_id=eq.{account['id']}",
                    headers=svc_headers,
                    json={"stripe_onboarded": True},
                )

    return {"received": True}