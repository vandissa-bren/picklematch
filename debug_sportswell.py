"""Debug SportsWell available_hours response format."""
import asyncio, json
from datetime import date, timedelta
from extract_thejar import PlayByPointAPI

async def main():
    d = json.loads(open("/app/.pbp_cookies.json").read())
    cookies, user_id = d["cookies"], d["user_id"]
    target = date.today() + timedelta(days=1)

    async with PlayByPointAPI(cookies=cookies, club_slug="sportswellpickleballpalace") as api:
        api._user_id = user_id
        h = await api.available_hours(885, target, surface="pickleball")
        print("Type:", type(h))
        print("Keys:" if isinstance(h, dict) else "Length:", list(h.keys()) if isinstance(h, dict) else len(h))
        print("Raw:", json.dumps(h)[:1000])

asyncio.run(main())
