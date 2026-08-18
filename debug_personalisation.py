"""
debug_personalisation.py -- READ-ONLY. Where does personalisation stop?

Walks the chain end to end for one user and one venue, reporting the first
link that fails rather than leaving it to guesswork:

  1. fixture present in pbp_credentials?
  2. does the batch endpoint return resolved prices?
  3. does /api/pbp/availability include resolved_price?
  4. does the SHARED cache stay clean (anonymous unchanged)?

No writes, no PBP calls, no bookings.
Run on 170.64.187.117:  cd /app && /app/venv/bin/python3 debug_personalisation.py
"""
import asyncio, json, os
from dotenv import load_dotenv

load_dotenv()
import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://stwohmddmdwttasbyblt.supabase.co")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HDRS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
USER_ID = os.environ.get("FAKE_USER_ID", "8948db6f-4dbf-4ae9-989d-b560db5a617b")
BOOKING = os.environ.get("BOOKING_SERVER_URL", "https://booking.picklematch.com.au")
FID = 597


async def main():
    async with httpx.AsyncClient(timeout=30.0) as c:
        print("=" * 70)
        print("1. FIXTURE STATE")
        print("=" * 70)
        r = await c.get(f"{SUPABASE_URL}/rest/v1/pbp_credentials",
                        params={"user_id": f"eq.{USER_ID}",
                                "select": "memberships,pricing_affiliations"},
                        headers=HDRS)
        row = (r.json() or [{}])[0]
        pa = row.get("pricing_affiliations") or {}
        print(f"  pricing_affiliations: {json.dumps(pa)}")
        aff = (pa.get(str(FID)) or {}).get("affiliation")
        print(f"  affiliation at {FID}: {aff!r}")
        if not aff:
            print("  STOP: no affiliation. Fixture not applied.")
            return

        print("\n" + "=" * 70)
        print("2. TARGET SESSIONS IN CACHE")
        print("=" * 70)
        r = await c.get(f"{SUPABASE_URL}/rest/v1/availability_cache",
                        params={"id": f"eq.pbp-{FID}", "select": "data"}, headers=HDRS)
        data = (r.json() or [{}])[0].get("data") or {}
        targets = [s for s in (data.get("sessions") or [])
                   if "prosecco" in str(s.get("title", "")).lower()][:3]
        if not targets:
            targets = [s for s in (data.get("sessions") or []) if s.get("price_tiers")][:3]
        for s in targets:
            print(f"  {str(s.get('title'))[:40]:40} {s.get('date')} price={s.get('price')} "
                  f"lesson={s.get('lesson_id')}")
            for t in (s.get("price_tiers") or []):
                print(f"      {json.dumps(t)}")

        print("\n" + "=" * 70)
        print("3. BATCH ENDPOINT")
        print("=" * 70)
        payload = {"user_id": USER_ID,
                   "sessions": [{"lesson_id": s["lesson_id"], "facility_id": FID}
                                for s in targets if s.get("lesson_id")]}
        try:
            rb = await c.post(f"{BOOKING}/api/resolve-prices", json=payload,
                              headers={"X-Internal-Key": SUPABASE_SERVICE_KEY})
            print(f"  HTTP {rb.status_code}: {rb.text[:300]}")
        except Exception as e:
            print(f"  FAILED: {e}")

        print("\n" + "=" * 70)
        print("4. AVAILABILITY ENDPOINT, per session date")
        print("=" * 70)
        dates = sorted({s.get("date") for s in targets if s.get("date")})
        for d in dates[:2]:
            for label, params in (("anon", {"date": d, "from": "00:00", "to": "23:30"}),
                                  ("user", {"date": d, "from": "00:00", "to": "23:30",
                                            "user_id": USER_ID})):
                try:
                    ra = await c.get("http://localhost:8000/api/pbp/availability", params=params)
                    body = ra.json()
                except Exception as e:
                    print(f"  {d} {label}: FAILED {e}")
                    continue
                found = []
                for v in body.get("venues", []):
                    if v.get("id") != FID:
                        continue
                    for s in (v.get("sessions") or []):
                        if s.get("lesson_id") in {t.get("lesson_id") for t in targets}:
                            found.append((s.get("title"), s.get("price"), s.get("resolved_price")))
                print(f"  {d} {label:4}: {len(found)} target session(s)")
                for f in found[:3]:
                    mark = "  <-- personalised" if f[2] else ""
                    print(f"      {str(f[0])[:34]:34} price={f[1]} resolved={f[2]}{mark}")


asyncio.run(main())
