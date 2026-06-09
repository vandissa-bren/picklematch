"""
fetch_venue.py — Scrape a single PBP venue and push to Supabase.
Triggered manually via GitHub Actions workflow_dispatch.
Usage: FACILITY_ID=1557 python fetch_venue.py
"""
from __future__ import annotations
import asyncio, json, os
from datetime import date, datetime, timedelta
from pathlib import Path
import httpx
from rich.console import Console

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from push_to_supabase import (
    scrape_pbp_venue,
    PBP_SLUG_MAP, VENUE_NAMES, DAYS_AHEAD,
    _load_cookies, SUPABASE_URL,
)

console = Console()

SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", SUPABASE_KEY)


async def main():
    facility_id = int(os.environ.get("FACILITY_ID", "0"))
    if not facility_id or facility_id not in PBP_SLUG_MAP:
        console.print(f"[red]Invalid or missing FACILITY_ID. Valid IDs: {list(PBP_SLUG_MAP.keys())}[/red]")
        return

    slug = PBP_SLUG_MAP[facility_id]
    name = VENUE_NAMES.get(facility_id, str(facility_id))
    dates = [date.today() + timedelta(days=i) for i in range(DAYS_AHEAD)]
    cookies, user_id = _load_cookies()

    if not cookies:
        console.print("[red]No PBP cookies.[/red]")
        return

    console.print(f"Scraping {name} ({facility_id})…")

    # Fetch existing data to preserve by_date/court_prices
    read_headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        existing_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/availability_cache",
            params={"platform": "eq.playbypoint", "select": "id,data"},
            headers=read_headers,
        )
        existing_records = {row["id"]: row["data"] for row in existing_resp.json()}

    r = await scrape_pbp_venue(cookies, user_id, facility_id, name, slug, dates)

    if not isinstance(r, dict):
        console.print(f"[red]Scrape failed for {name}[/red]")
        return

    rec_id = f"pbp-{facility_id}"
    existing = existing_records.get(rec_id, {})

    # Preserve court blocks and prices
    r["by_date"] = existing.get("by_date", {})
    r["court_prices"] = existing.get("court_prices", {})

    if len(r.get("sessions", [])) == 0:
        console.print(f"[red]0 sessions found for {name} — aborting to avoid data loss[/red]")
        return

    record = [{
        "id": rec_id,
        "venue_name": name,
        "platform": "playbypoint",
        "date": date.today().isoformat(),
        "data": r,
        "updated_at": datetime.utcnow().isoformat(),
    }]

    # Write with service key
    write_headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/availability_cache",
            json=record,
            headers=write_headers,
        )
        if resp.status_code in (200, 201):
            console.print(f"[green]✓ {name}: {len(r['sessions'])} sessions written to Supabase[/green]")
        else:
            console.print(f"[red]Supabase error {resp.status_code}: {resp.text[:200]}[/red]")


if __name__ == "__main__":
    asyncio.run(main())
