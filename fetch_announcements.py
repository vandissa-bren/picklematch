"""
fetch_announcements.py
Two-pronged announcement capture:
1. Poll /api/users/{user_id}/notifications for new unread announcements
2. Forward probe 60 IDs beyond max known to catch any gaps

Run hourly via cron. Push notifications must be disabled on phone so
web notifications stay unread until this poller catches them.
"""
import asyncio
import json
import os
import re
import httpx
from datetime import datetime
from pathlib import Path
from extract_thejar import PlayByPointAPI

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://stwohmddmdwttasbyblt.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN0d29obWRkbWR3dHRhc2J5Ymx0Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3ODcyNDc5MywiZXhwIjoyMDk0MzAwNzkzfQ.zrsXJVxX4OZv0Eb5qycQF3_33NFyAFJfPlvK_xCzi-E")

FACILITY_NAMES = {
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
    1664: "The Rally Pickleball",
    1714: "Runway Pickleball",
    1733: "Pickleball Powerhouse",
    1770: "Raya Pickleball Club",
    1783: "PICKLE4REAL",
    1696: "picklezone",
}

VENUE_FRAGMENTS = {name.lower() for name in FACILITY_NAMES.values()}


def _load_cookies():
    for p in [Path(__file__).parent / ".pbp_cookies.json", Path.home() / ".pbp_cookies.json"]:
        if p.exists():
            try:
                data = json.loads(p.read_text())
                return data.get("cookies", {}), data.get("user_id", 0)
            except Exception:
                pass
    return {}, 0


def parse_announcement_html(html: str, announcement_id: int):
    title = re.search(r"class='mb0 text semi bold'>([^<]+)<", html or '')
    date_str = re.search(r'class="ui text small grey">\s*([^<]+)<', html or '')
    body = re.search(
        r'<div class="ui divider"></div>\s*<div>(.*?)</div>\s*</div>\s*</div>\s*</div>',
        html or '', re.S
    )

    if not title or not date_str:
        return None

    parts = date_str.group(1).strip().split(' - ', 1)
    facility_name = parts[0].strip()
    clean_date = parts[1].strip() if len(parts) > 1 else ''

    facility_id = None
    for fid, name in FACILITY_NAMES.items():
        if name.lower() in facility_name.lower() or facility_name.lower() in name.lower():
            facility_id = fid
            break

    if not facility_id:
        print(f"  Unknown facility: '{facility_name}' — skipping {announcement_id}")
        return None

    body_html = body.group(1).strip() if body else ''
    body_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body_html)).strip()

    return {
        "id": f"pbp-announcement-{announcement_id}",
        "announcement_id": announcement_id,
        "facility_id": facility_id,
        "facility_name": facility_name,
        "title": title.group(1).strip(),
        "date_str": clean_date,
        "body_html": body_html[:5000],
        "body_text": body_text[:2000],
        "url": f"https://app.playbypoint.com/announcements/{announcement_id}",
        "fetched_at": datetime.utcnow().isoformat(),
    }


async def get_known_ids(client: httpx.AsyncClient) -> set:
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/announcements",
        params={"select": "announcement_id"},
        headers=headers,
    )
    if resp.status_code == 200:
        return {r["announcement_id"] for r in resp.json()}
    return set()


async def upsert_announcement(client: httpx.AsyncClient, record: dict):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    resp = await client.post(
        f"{SUPABASE_URL}/rest/v1/announcements",
        json=record,
        headers=headers,
    )
    return resp.status_code


async def main():
    cookies, user_id = _load_cookies()
    if not cookies or not user_id:
        print("No cookies found")
        return

    async with httpx.AsyncClient() as http:
        known_ids = await get_known_ids(http)

        async with PlayByPointAPI(cookies=cookies, club_slug='nplpickleball') as api:
            api._user_id = user_id

            # 1. Notification polling
            try:
                notifications = await api._get_json(
                    f'/api/users/{user_id}/notifications', params={}
                )
            except Exception as e:
                print(f"Notifications error: {e}")
                notifications = []

            if notifications:
                print(f"Found {len(notifications)} notification(s)")
                print("RAW:", json.dumps(notifications[0], indent=2))

            notif_count = 0
            for notif in (notifications or []):
                aid = (
                    notif.get('announcement_id') or
                    notif.get('announceable_id') or
                    notif.get('notifiable_id') or
                    notif.get('resource_id')
                )
                url = notif.get('url') or notif.get('path') or notif.get('link') or ''
                if not aid and url:
                    m = re.search(r'/announcements/(\d+)', url)
                    if m:
                        aid = int(m.group(1))
                if not aid:
                    print("Unknown notification structure:", json.dumps(notif))
                    continue
                aid = int(aid)
                if aid in known_ids:
                    continue
                try:
                    html = await api._get_raw(f'/announcements/{aid}')
                    record = parse_announcement_html(html, aid)
                    if record:
                        await upsert_announcement(http, record)
                        print(f"[notif] Stored {aid}: {record['title'][:50]} ({record['facility_name']})")
                        known_ids.add(aid)
                        notif_count += 1
                except Exception as e:
                    print(f"Error fetching announcement {aid}: {e}")

            print(f"Notifications: {notif_count} new stored.")

            # 2. Forward probe
            if known_ids:
                max_known = max(known_ids)
            else:
                max_known = 134900

            probe_count = 0
            for aid in range(max_known + 1, max_known + 201):
                if aid in known_ids:
                    continue
                try:
                    html = await api._get_raw(f'/announcements/{aid}')
                    date_str = re.search(r'class="ui text small grey">\s*([^<]+)<', html or '')
                    if not date_str:
                        await asyncio.sleep(0.1)
                        continue
                    parts = date_str.group(1).strip().split(' - ', 1)
                    facility = parts[0].strip().lower()
                    if any(v in facility or facility in v for v in VENUE_FRAGMENTS):
                        record = parse_announcement_html(html, aid)
                        if record:
                            await upsert_announcement(http, record)
                            print(f"[probe] Stored {aid}: {record['facility_name']} - {record['title'][:40]}")
                            probe_count += 1
                            known_ids.add(aid)
                    await asyncio.sleep(0.1)
                except Exception:
                    pass

            print(f"Probe: {probe_count} new from forward scan.")


if __name__ == "__main__":
    asyncio.run(main())
