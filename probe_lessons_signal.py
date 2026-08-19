"""
probe_lessons_signal.py -- READ-ONLY. Does `lessons == 1` separate an
occurrence price from a package price?

Pickle Haus prices at programme level, so every session there has
price_tiers == [] and the resolver finds nothing -- a real member with a
valid 'Level Up Pass' sees the public price even though PBP publishes
$25 -> $20 and $20 -> $15 for him.

Programme-level pricing was deliberately excluded from session resolution
because Dink & Drive's $125 league price is the cost of a whole package,
not one occurrence. That was right for Dink and wrong for Pickle Haus, so
the rule needs a semantic test rather than a structural one.

`lessons` is the candidate: 1 for a single session, more for a bundle.
This checks it against BOTH sides before anything is changed. Confirming
only the Pickle Haus side would prove half the claim.

Prints lessons, lesson_unit, lesson_details, price, category and
affiliations for every programme, and flags any case where the signal
fails to separate them.

No writes, no bookings.
"""
import asyncio, json, os, sys
from collections import Counter
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, "/app")
from extract_thejar import PlayByPointAPI, _extract_react_props_from_html
import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://stwohmddmdwttasbyblt.supabase.co")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HDRS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
SLEEP = 1.5

# Both sides of the hypothesis. Pickle Haus should be occurrence pricing;
# Dink & Drive's league should be a package.
CASES = [
    (1485, "picklehaus", "Pickle Haus", ["dupr-rated-friday", "happy-hour-social"]),
    (1557, "dinkndrivepickleballclub", "Dink & Drive", None),  # find its league
]


def load_cookies():
    raw = os.environ.get("PBP_COOKIES_JSON", "")
    if raw:
        d = json.loads(raw)
        if d.get("cookies"):
            return d["cookies"], d.get("user_id", 0)
    p = "/app/.pbp_cookies.json"
    if os.path.exists(p):
        d = json.loads(open(p).read())
        return d.get("cookies"), d.get("user_id", 0)
    raise SystemExit("No PBP session available.")


def show(rec, indent="      "):
    print(indent + json.dumps({k: rec.get(k) for k in
        ("lessons", "lesson_unit", "lesson_details", "price",
         "player_category", "allowed_affiliations", "hidden", "name")}))


async def main():
    cookies, uid = load_cookies()
    verdict = {"occurrence": [], "package": [], "ambiguous": []}

    for fid, club_slug, label, slugs in CASES:
        cache = httpx.get(f"{SUPABASE_URL}/rest/v1/availability_cache",
                          params={"id": f"eq.pbp-{fid}", "select": "data"},
                          headers=HDRS, timeout=30).json()
        data = cache[0]["data"] if cache else {}
        sessions = data.get("sessions") or []
        prog_pricing = data.get("program_pricing") or {}

        if slugs is None:
            # Find the programme whose cached price looks like a package.
            by_slug = {}
            for s in sessions:
                sl = s.get("program_slug")
                if sl:
                    by_slug.setdefault(sl, []).append(s)
            slugs = sorted(by_slug,
                           key=lambda sl: -max(
                               float(str(x.get("price") or "$0").replace("$", "") or 0)
                               for x in by_slug[sl]))[:2]

        print("=" * 76)
        print(f"{label}   (facility {fid})")
        print(f"  cached sessions: {len(sessions)}   "
              f"with price_tiers: {sum(1 for s in sessions if s.get('price_tiers'))}   "
              f"program_pricing entries: {len(prog_pricing)}")
        print("=" * 76)

        for slug in slugs:
            await asyncio.sleep(SLEEP)
            try:
                async with PlayByPointAPI(cookies=cookies, club_slug=club_slug) as api:
                    api._user_id = uid
                    html = await api.program_detail_html(slug)
                    props = _extract_react_props_from_html(html) or {}
            except Exception as e:
                print(f"\n  {slug}: fetch failed -- {str(e)[:70]}")
                continue

            in_cache = [s for s in sessions if s.get("program_slug") == slug]
            print(f"\n  --- {slug}")
            print(f"      programme name : {props.get('name')!r}")
            print(f"      sessions cached: {len(in_cache)}   "
                  f"in program_pricing: {slug in prog_pricing}")

            records = (props.get("prices") or []) + (props.get("packages") or [])
            if not records:
                print("      no programme-level pricing records")
            for r in records:
                show(r)

            lesson_counts = {r.get("lessons") for r in records
                             if not r.get("hidden") and r.get("price") is not None}
            member = [r for r in records
                      if str(r.get("player_category", "")).lower() == "member"]

            if not lesson_counts:
                bucket = "ambiguous"
            elif lesson_counts == {1}:
                bucket = "occurrence"
            elif all(isinstance(x, int) and x > 1 for x in lesson_counts if x is not None):
                bucket = "package"
            else:
                bucket = "ambiguous"
            verdict[bucket].append((label, slug, sorted(str(x) for x in lesson_counts),
                                    len(member)))
            print(f"      lessons values : {sorted(str(x) for x in lesson_counts)}"
                  f"   member records: {len(member)}   => {bucket.upper()}")

            # per-lesson prices, for contrast
            lessons_raw = props.get("sessions") or props.get("clinic_lessons") or []
            ip = [l.get("individual_prices") for l in lessons_raw[:2] if l.get("individual_prices")]
            print(f"      per-lesson individual_prices present: {bool(ip)}")

    print("\n" + "=" * 76)
    print("VERDICT")
    print("=" * 76)
    for k in ("occurrence", "package", "ambiguous"):
        print(f"  {k}: {len(verdict[k])}")
        for v in verdict[k]:
            print(f"     {v}")
    if verdict["occurrence"] and verdict["package"] and not verdict["ambiguous"]:
        print("\n  `lessons` SEPARATES the two cases. Safe to use as the semantic test.")
    elif verdict["ambiguous"]:
        print("\n  AMBIGUOUS cases exist -- `lessons` alone is NOT sufficient.")
    else:
        print("\n  Only one side observed -- the claim is half-proven. Do not rely on it.")


if __name__ == "__main__":
    asyncio.run(main())
