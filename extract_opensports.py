#!/usr/bin/env python3
"""
extract_opensports.py  —  OpenSports pickleball session aggregator.

Scrapes upcoming pickleball sessions from OpenSports.net's public API.
No login or Cloudflare bypass required for discovery/read operations.

API base: https://osapi.opensports.ca
Required headers: app-id, buildnumber, source (all static, no auth)

Discovered endpoints (HAR-verified 2026-05-15):
  GET /app/posts/listFiltered  — session search by location + sport
  GET /groups/listOne           — club/group detail by alias
  GET /groups/dashboard         — nearby groups/clubs

Usage:
  python extract_opensports.py search                          # Melbourne pickleball (default)
  python extract_opensports.py search --location "Sydney"      # Different city
  python extract_opensports.py search --lat -37.81 --lng 144.96 --radius 50
  python extract_opensports.py search --days 14                # Next 14 days
  python extract_opensports.py search --group "sports-point-indoor-pickleball"
  python extract_opensports.py clubs                           # List nearby clubs
  python extract_opensports.py club sports-point-indoor-pickleball  # Club detail
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
import typer
from rich.console import Console
from rich.table import Table

# ─── Constants ─────────────────────────────────────────────────
API_BASE = "https://osapi.opensports.ca"
WEBSITE_BASE = "https://opensports.net"

# Static headers required by the OpenSports API (no auth needed).
API_HEADERS = {
    "app-id": "8c5b94f6-fe1f-4651-9d3a-95a396ec3589",
    "buildnumber": "202202",
    "source": "oswebsite",
    "Accept": "*/*",
    "Origin": WEBSITE_BASE,
    "Referer": f"{WEBSITE_BASE}/",
}

# Sport IDs
SPORT_PICKLEBALL = 28

# Default location: Melbourne CBD
DEFAULT_LAT = -37.81502914428711
DEFAULT_LNG = 144.96633911132812
DEFAULT_RADIUS_KM = 25

# Well-known locations for --location shorthand
LOCATIONS = {
    "melbourne": (-37.8150, 144.9663),
    "sydney": (-33.8688, 151.2093),
    "brisbane": (-27.4698, 153.0251),
    "perth": (-31.9505, 115.8605),
    "adelaide": (-34.9285, 138.6007),
    "canberra": (-35.2809, 149.1300),
    "hobart": (-42.8821, 147.3272),
    "gold coast": (-28.0167, 153.4000),
    "geelong": (-38.1499, 144.3617),
}


# ─── API Client ────────────────────────────────────────────────
class OpenSportsAPI:
    """Async client for the OpenSports public API."""

    def __init__(self):
        self._client = httpx.AsyncClient(
            headers=API_HEADERS,
            timeout=30.0,
            follow_redirects=True,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._client.aclose()

    async def search_sessions(
        self,
        latitude: float = DEFAULT_LAT,
        longitude: float = DEFAULT_LNG,
        radius_km: float = DEFAULT_RADIUS_KM,
        sport_id: int = SPORT_PICKLEBALL,
        limit: int = 100,
        upcoming: bool = True,
        include_waitlisted: bool = True,
    ) -> list[dict]:
        """
        Search for upcoming sessions near a location.

        Endpoint: GET /app/posts/listFiltered
        Params:
          - latitude, longitude: center point
          - distance: radius in meters
          - limit: max results (API default 16, we use 100)
          - upcoming: only future events
          - sportIDs[]: sport filter (28 = pickleball)
          - hasWaitlistUsers: include sessions with waitlists
        """
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "distance": int(radius_km * 1000),
            "limit": limit,
            "upcoming": str(upcoming).lower(),
            "sportIDs[]": sport_id,
        }
        if not include_waitlisted:
            params["hasWaitlistUsers"] = "false"

        all_results = []
        page_limit = min(limit, 50)  # Fetch in pages of 50
        params["limit"] = page_limit

        while len(all_results) < limit:
            resp = await self._client.get(
                f"{API_BASE}/app/posts/listFiltered",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            results = data.get("result") or []
            if not results:
                break

            all_results.extend(results)

            # If we got fewer than the page limit, no more pages.
            if len(results) < page_limit:
                break

            # Use the last result's start time as the cursor for next page.
            # The API returns results ordered by start time.
            last_start = results[-1].get("start")
            if last_start:
                params["startAfter"] = last_start
            else:
                break

        return all_results[:limit]

    async def get_group(self, alias_id: str) -> Optional[dict]:
        """
        Get club/group detail by alias.

        Endpoint: GET /groups/listOne?aliasID=<slug>
        """
        resp = await self._client.get(
            f"{API_BASE}/groups/listOne",
            params={"aliasID": alias_id},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result") or data

    async def list_nearby_groups(
        self,
        latitude: float = DEFAULT_LAT,
        longitude: float = DEFAULT_LNG,
    ) -> list[dict]:
        """
        List nearby groups/clubs.

        Endpoint: GET /groups/dashboard
        """
        resp = await self._client.get(
            f"{API_BASE}/groups/dashboard",
            params={"latitude": latitude, "longitude": longitude},
        )
        resp.raise_for_status()
        data = resp.json()
        # Response may have multiple sections (featured, nearby, etc.)
        if isinstance(data, dict):
            result = data.get("result")
            if isinstance(result, list):
                return result
            # Might be nested: {result: {nearby: [...], featured: [...]}}
            if isinstance(result, dict):
                groups = []
                for key, val in result.items():
                    if isinstance(val, list):
                        groups.extend(val)
                return groups
        return []


# ─── Data Parsing ──────────────────────────────────────────────
def parse_session(raw: dict) -> dict:
    """
    Parse a raw OpenSports session into a clean dict.
    
    Converts UTC timestamps to local time using the session's timeZone.
    Extracts pricing from ticketsSummary.
    Computes spots available.
    """
    tz_name = raw.get("timeZone", "Australia/Melbourne")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Australia/Melbourne")

    # Parse times.
    start_utc = raw.get("start", "")
    end_utc = raw.get("end", "")

    start_local = None
    end_local = None
    date_str = ""
    start_time = ""
    end_time = ""

    if start_utc:
        try:
            dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
            start_local = dt.astimezone(tz)
            date_str = start_local.strftime("%Y-%m-%d")
            start_time = start_local.strftime("%H:%M")
        except Exception:
            pass

    if end_utc:
        try:
            dt = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
            end_local = dt.astimezone(tz)
            end_time = end_local.strftime("%H:%M")
        except Exception:
            pass

    # Parse tickets/pricing.
    tickets = raw.get("ticketsSummary") or []
    prices = []
    total_sold = 0
    total_capacity = 0
    has_unlimited = False

    for t in tickets:
        price = t.get("price", 0)
        currency = t.get("currency", "AUD")
        title = t.get("title", "")
        sold = t.get("quantitySold", 0) or 0
        capacity = t.get("quantityTotal", 0)

        total_sold += sold
        if capacity == -1:
            has_unlimited = True
        elif capacity > 0:
            total_capacity += capacity

        prices.append({
            "title": title,
            "price": price,
            "currency": currency,
            "sold": sold,
            "capacity": capacity if capacity != -1 else None,
        })

    # Availability.
    registered = raw.get("registeredAttendees", 0) or 0
    max_attendees = raw.get("maxAttendees")

    if max_attendees and max_attendees > 0:
        spots_left = max(0, max_attendees - registered)
        capacity_str = f"{registered}/{max_attendees}"
    elif has_unlimited:
        spots_left = None  # unlimited
        capacity_str = f"{registered}/∞"
    elif total_capacity > 0:
        spots_left = max(0, total_capacity - total_sold)
        capacity_str = f"{total_sold}/{total_capacity}"
    else:
        spots_left = None
        capacity_str = str(registered)

    # Price display.
    if len(prices) == 1:
        p = prices[0]
        price_str = f"${p['price']:.2f}" if p["price"] > 0 else "Free"
    elif len(prices) > 1:
        price_strs = []
        for p in prices:
            label = p["title"]
            val = f"${p['price']:.2f}" if p["price"] > 0 else "Free"
            price_strs.append(f"{label}: {val}")
        price_str = " / ".join(price_strs)
    else:
        price_str = "—"

    # Cheapest price for sorting.
    min_price = min((p["price"] for p in prices), default=0)

    # Status.
    if spots_left == 0:
        status = "Full"
    elif raw.get("waitlistUserCount") and raw["waitlistUserCount"] > 0:
        status = f"Waitlist ({raw['waitlistUserCount']})"
    else:
        status = "Available"

    # Level.
    level = ""
    data = raw.get("data") or {}
    level_data = data.get("level") or {}
    if level_data.get("title"):
        level = level_data["title"].strip()

    # Place.
    place = raw.get("place") or {}

    return {
        "id": raw.get("id"),
        "title": (raw.get("title") or "").strip(),
        "date": date_str,
        "day": start_local.strftime("%A") if start_local else "",
        "start_time": start_time,
        "end_time": end_time,
        "venue": (place.get("title") or "").strip(),
        "address": place.get("address", ""),
        "city": place.get("city", ""),
        "group_name": (raw.get("groupName") or "").strip(),
        "group_id": raw.get("creatorGroupID"),
        "level": level,
        "price_str": price_str,
        "min_price": min_price,
        "prices": prices,
        "spots": capacity_str,
        "spots_left": spots_left,
        "status": status,
        "registered": registered,
        "max_attendees": max_attendees,
        "tags": raw.get("tags") or [],
        "alias_id": raw.get("aliasID", ""),
        "url": f"{WEBSITE_BASE}/{raw.get('aliasID', '')}",
        "payment_type": raw.get("paymentType", ""),
        "require_payment": raw.get("requireInAppPayment", False),
        "waitlist_count": raw.get("waitlistUserCount"),
        "raw": raw,
    }


# ─── CLI ───────────────────────────────────────────────────────
cli = typer.Typer(
    name="extract_opensports",
    help="OpenSports pickleball session aggregator.",
    add_completion=False,
)


def _resolve_location(
    location: Optional[str], lat: Optional[float], lng: Optional[float],
) -> tuple[float, float, str]:
    """Resolve location from --location name or --lat/--lng coords."""
    if lat is not None and lng is not None:
        return lat, lng, f"{lat:.4f}, {lng:.4f}"

    if location:
        key = location.lower().strip()
        if key in LOCATIONS:
            la, lo = LOCATIONS[key]
            return la, lo, location.title()

        # Try a loose match.
        for name, (la, lo) in LOCATIONS.items():
            if key in name or name in key:
                return la, lo, name.title()

        typer.echo(
            f"Unknown location '{location}'. "
            f"Known: {', '.join(LOCATIONS.keys())}. "
            f"Use --lat/--lng for custom coordinates."
        )
        raise typer.Exit(1)

    return DEFAULT_LAT, DEFAULT_LNG, "Melbourne"


@cli.command(name="search")
def search_cmd(
    location: Optional[str] = typer.Option(
        None, "--location", "-l",
        help="City name (melbourne, sydney, brisbane, etc.)",
    ),
    lat: Optional[float] = typer.Option(None, "--lat"),
    lng: Optional[float] = typer.Option(None, "--lng"),
    radius: float = typer.Option(
        DEFAULT_RADIUS_KM, "--radius", "-r",
        help="Search radius in km (default: 25)",
    ),
    days: int = typer.Option(
        7, "--days", "-d",
        help="Show sessions within this many days",
    ),
    limit: int = typer.Option(
        100, "--limit",
        help="Max sessions to fetch",
    ),
    group: Optional[str] = typer.Option(
        None, "--group", "-g",
        help="Filter by group/club name (partial match)",
    ),
    level: Optional[str] = typer.Option(
        None, "--level",
        help="Filter by level (partial match, e.g. 'intermediate')",
    ),
    free_only: bool = typer.Option(
        False, "--free", help="Show only free sessions",
    ),
    available_only: bool = typer.Option(
        True, "--available/--all", help="Show only available (not full)",
    ),
    output: Optional[str] = typer.Option(
        None, "--output", "-o",
        help="Save results to JSON file",
    ),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", "-v"),
) -> None:
    """
    Search for upcoming pickleball sessions.

    Examples:
        python extract_opensports.py search
        python extract_opensports.py search --location sydney --radius 30
        python extract_opensports.py search --group "sports point" --days 14
        python extract_opensports.py search --level intermediate --free
    """
    latitude, longitude, loc_name = _resolve_location(location, lat, lng)
    console = Console()

    async def _search():
        async with OpenSportsAPI() as api:
            raw_sessions = await api.search_sessions(
                latitude=latitude,
                longitude=longitude,
                radius_km=radius,
                limit=limit,
            )
        return raw_sessions

    raw = asyncio.run(_search())

    if not raw:
        console.print(f"[yellow]No sessions found near {loc_name}.[/yellow]")
        return

    # Parse all sessions.
    sessions = [parse_session(s) for s in raw]

    # Filter by date range.
    now = datetime.now()
    cutoff = (now + timedelta(days=days)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    sessions = [s for s in sessions if today <= s["date"] <= cutoff]

    # Filter by group.
    if group:
        g = group.lower()
        sessions = [
            s for s in sessions
            if g in s["group_name"].lower() or g in s["venue"].lower()
        ]

    # Filter by level.
    if level:
        lv = level.lower()
        sessions = [s for s in sessions if lv in s["level"].lower()]

    # Filter free only.
    if free_only:
        sessions = [s for s in sessions if s["min_price"] == 0]

    # Filter available only.
    if available_only:
        sessions = [s for s in sessions if s["status"] != "Full"]

    # Sort by date, then start time.
    sessions.sort(key=lambda s: (s["date"], s["start_time"]))

    if not sessions:
        console.print(
            f"[yellow]No matching sessions found "
            f"({len(raw)} total fetched, all filtered out).[/yellow]"
        )
        return

    # Save JSON if requested.
    if output:
        out_data = {
            "location": loc_name,
            "latitude": latitude,
            "longitude": longitude,
            "radius_km": radius,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "sessions": [
                {k: v for k, v in s.items() if k != "raw"}
                for s in sessions
            ],
        }
        Path(output).write_text(
            json.dumps(out_data, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        console.print(f"[dim]Saved {len(sessions)} sessions to {output}[/dim]")

    # Display table.
    table = Table(
        title=f"Pickleball Sessions · {loc_name} · {radius}km · Next {days} days",
        show_lines=False,
        padding=(0, 1),
    )
    table.add_column("Date", style="bold", width=12)
    table.add_column("Day", width=5)
    table.add_column("Time", width=13)
    table.add_column("Title", max_width=32)
    table.add_column("Venue", max_width=24)
    table.add_column("Group", max_width=22)
    table.add_column("Level", max_width=16)
    table.add_column("Price", justify="right", width=10)
    table.add_column("Spots", justify="right", width=8)
    table.add_column("Status", width=10)

    for s in sessions:
        status_style = {
            "Available": "green",
            "Full": "red",
        }.get(s["status"], "yellow")

        # Shorten day name.
        day_short = s["day"][:3] if s["day"] else ""

        # Simplify price for single-ticket sessions.
        price_display = (
            f"${s['min_price']:.0f}"
            if s["min_price"] > 0 and len(s["prices"]) == 1
            else "Free" if s["min_price"] == 0 and len(s["prices"]) <= 1
            else s["price_str"][:10]
        )

        table.add_row(
            s["date"],
            day_short,
            f"{s['start_time']}–{s['end_time']}",
            s["title"][:32],
            s["venue"][:24],
            s["group_name"][:22],
            s["level"][:16],
            price_display,
            s["spots"],
            f"[{status_style}]{s['status']}[/{status_style}]",
        )

    console.print(table)
    console.print(
        f"\n[dim]{len(sessions)} session(s) shown · "
        f"{len(raw)} fetched from API[/dim]"
    )

    # Summary: unique venues and groups.
    venues = sorted(set(s["venue"] for s in sessions if s["venue"]))
    groups = sorted(set(s["group_name"] for s in sessions if s["group_name"]))
    if venues:
        console.print(f"[dim]Venues: {', '.join(venues[:10])}")
    if groups:
        console.print(f"[dim]Groups: {', '.join(groups[:10])}")


@cli.command(name="clubs")
def clubs_cmd(
    location: Optional[str] = typer.Option(
        None, "--location", "-l",
        help="City name",
    ),
    lat: Optional[float] = typer.Option(None, "--lat"),
    lng: Optional[float] = typer.Option(None, "--lng"),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", "-v"),
) -> None:
    """
    List nearby pickleball clubs/groups.

    Examples:
        python extract_opensports.py clubs
        python extract_opensports.py clubs --location sydney
    """
    latitude, longitude, loc_name = _resolve_location(location, lat, lng)
    console = Console()

    async def _clubs():
        async with OpenSportsAPI() as api:
            return await api.list_nearby_groups(latitude, longitude)

    groups = asyncio.run(_clubs())

    if not groups:
        console.print(f"[yellow]No clubs found near {loc_name}.[/yellow]")
        return

    table = Table(title=f"Pickleball Clubs · {loc_name}")
    table.add_column("Name", style="bold")
    table.add_column("Alias")
    table.add_column("Members")
    table.add_column("Sport")

    for g in groups:
        name = g.get("name") or g.get("title") or "—"
        alias = g.get("aliasID") or g.get("alias") or "—"
        members = str(g.get("memberCount") or g.get("numMembers") or "—")
        sport = g.get("sportName") or "—"

        table.add_row(name, alias, members, sport)

    console.print(table)
    console.print(f"\n[dim]{len(groups)} club(s) found[/dim]")


@cli.command(name="club")
def club_cmd(
    alias: str = typer.Argument(
        ..., help="Club alias/slug (e.g. 'sports-point-indoor-pickleball')",
    ),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", "-v"),
) -> None:
    """
    Show detail for a specific club/group.

    Examples:
        python extract_opensports.py club sports-point-indoor-pickleball
        python extract_opensports.py club Easternindoor
    """
    console = Console()

    async def _club():
        async with OpenSportsAPI() as api:
            return await api.get_group(alias)

    data = asyncio.run(_club())

    if not data:
        console.print(f"[red]Club '{alias}' not found.[/red]")
        return

    # The response structure varies, try to extract useful info.
    if isinstance(data, dict):
        name = data.get("name") or data.get("title") or alias
        desc = data.get("description") or ""
        members = data.get("memberCount") or data.get("numMembers") or "—"
        sport = data.get("sportName") or "—"
        place = data.get("place") or {}
        address = place.get("address") or "—"
        city = place.get("city") or ""
        alias_id = data.get("aliasID") or alias
        url = f"{WEBSITE_BASE}/{alias_id}"

        console.print(f"\n[bold]{name}[/bold]")
        console.print(f"  Alias:    {alias_id}")
        console.print(f"  URL:      {url}")
        console.print(f"  Members:  {members}")
        console.print(f"  Sport:    {sport}")
        console.print(f"  Address:  {address}")
        if city:
            console.print(f"  City:     {city}")
        if desc:
            console.print(f"\n  {desc[:500]}")
        console.print("")

        # Show full JSON if verbose.
        if verbose:
            console.print("[dim]Raw JSON:[/dim]")
            console.print(
                json.dumps(data, indent=2, default=str)[:3000]
            )
    else:
        console.print(json.dumps(data, indent=2, default=str)[:3000])


if __name__ == "__main__":
    cli()
