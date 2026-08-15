"""
test_raya_p4r.py - Test court block fetching for Raya and Pickle4Real
Run on 170.64.187.117 to verify these venues work via the droplet.
No writes to Supabase - just prints results.
"""
import asyncio
import json
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
from extract_thejar import PlayByPointAPI

TEST_VENUES = {
    1770: "rayapickleballclub",
    1783: "PICKLE4REAL",
}

def sec_to_hhmm(s): return f"{s//3600:02d}:{(s%3600)//60:02d}"

async def test_venue(cookies, user_id, facility_id, slug):
    today = datetime.now(ZoneInfo('Australia/Melbourne')).date()
    dates = [today + timedelta(days=i) for i in range(7)]
    
    async with PlayByPointAPI(cookies=cookies, club_slug=slug) as api:
        api._user_id = user_id
        for target in dates:
            try:
                h = await api.available_hours(facility_id, target, surface='pickleball')
                slots = (h or {}).get('available_hours', [])
                available = [s for s in slots if s.get('available')]
                if available:
                    # Try getting courts for first available slot
                    sec = int(available[0]['seconds_from_midnight'])
                    courts = await api.available_courts(facility_id, target, sec, sec+1800, surface='pickleball')
                    print(f"  {target}: {len(available)} slots, {len(courts or [])} courts at {sec_to_hhmm(sec)}")
                else:
                    print(f"  {target}: no availability")
            except Exception as e:
                print(f"  {target}: error — {e}")
            await asyncio.sleep(0.5)

async def main():
    d = json.loads(open('/app/.pbp_cookies.json').read())
    cookies, user_id = d['cookies'], d['user_id']
    print(f"Using cookies for user_id={user_id}\n")
    
    for fid, slug in TEST_VENUES.items():
        print(f"── {slug} (facility_id={fid}) ──")
        await test_venue(cookies, user_id, fid, slug)
        print()

if __name__ == "__main__":
    asyncio.run(main())
