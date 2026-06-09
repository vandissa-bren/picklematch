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
    scrape_pbp_venue, supabase_upsert,
    PBP_SLUG_MAP, VENUE_NAMES, DAYS_AHEAD,
    _load_cookies, SUPABASE_URL, SUPABASE_KEY
)

console = Console()

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

    # Fetch existing data to preserve by_date/court_prices/other venue sessions
    async with httpx.AsyncClient(timeout=30.0) as client:
        existing_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/availability_cache",
            params={"platform": "eq.playbypoint", "select": "id,data"},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        )
        existing_records = {row["id"]: row["data"] for row in existing_resp.json()}

    r = await scrape_pbp_venue(cookies, user_id, facility_id, name, slug, dates)

    if not isinstance(r, dict):
        console.print(f"[red]Scrape failed for {name}[/red]")
        return

    rec_id = f"pbp-{facility_id}"
    existing = existing_records.get(rec_id, {})
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

    await supabase_upsert(record)
    console.print(f"[green]✓ {name}: {len(r['sessions'])} sessions pushed[/green]")
    console.print(f"[green]✓ {name}: {len(r['sessions'])} sessions pushed[/green]")

if __name__ == "__main__":
    asyncio.run(main())
