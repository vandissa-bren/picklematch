"""
probe_program_semantics.py -- READ-ONLY. What do programme-level price
records actually mean, across the whole estate?

`lessons == 1` was proposed as the test for "prices one occurrence" and is
DISPROVEN: Dink & Drive's league is lessons=1 with
lesson_unit="per_week_per_next_sessions", lesson_details="1 session x per
week per total program", at $100/$125 -- a programme commitment, not an
occurrence.

lesson_unit looks like the real discriminator ("session" vs everything
else), but four programmes is not evidence. This surveys every programme
across every venue and reports:

  * all distinct lesson_unit values, with example prices
  * all distinct lesson_details patterns
  * records classified OCCURRENCE / PACKAGE / UNKNOWN by lesson_unit
  * programmes mixing both kinds, since Pickle Haus does exactly that:
      1 session  $20 member / $25 non-member   (visible)
      2 sessions $20                            (hidden)
      6 sessions $120                           (hidden)
    so classification must be per RECORD, not per programme
  * package records that carry a genuine member discount -- a real member
    benefit that must not be shown as a session price, and must not simply
    be discarded either
  * per venue: how many sessions would gain a member price if occurrence
    records at programme level were resolved

Written because "0 price_tiers" was previously read as "this venue has no
member pricing". For Pickle Haus that was wrong -- the tiers exist, at
programme level -- so Raya and Melbourne Pickle Club need rechecking on
the same basis rather than trusting the earlier classification.

No writes, no bookings.
"""
import asyncio, json, os, sys
from collections import Counter, defaultdict
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, "/app")
from extract_thejar import PlayByPointAPI, _extract_react_props_from_html
import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://stwohmddmdwttasbyblt.supabase.co")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HDRS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
SLEEP = 1.6
MAX_PROGRAMS_PER_VENUE = int(os.environ.get("MAX_PROGRAMS", "4"))

SLUGS = {
    597: "nplpickleball", 1009: "easternindoorpickleballclub", 1379: "pickleholic",
    1355: "statepickleballcentre", 1383: "MelbournePickleClub", 1485: "picklehaus",
    755: "leveluppickleballknoxcity", 1584: "theroompickleball", 1461: "therealdill",
    1532: "pickleplex", 1557: "dinkndrivepickleballclub", 1119: "swingandserve",
    1487: "Pickle-Playground", 1664: "TheRallyPickleball", 1714: "RunwayPickleball",
    1733: "pickleballpowerhouse", 1696: "picklezone", 1770: "rayapickleballclub",
    1783: "PICKLE4REAL", 1883: "TheJarHQ",
}


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


def classify(rec):
    unit = str(rec.get("lesson_unit") or "").strip().lower()
    if unit == "session":
        return "OCCURRENCE"
    if unit:
        return "PACKAGE"
    return "UNKNOWN"


async def main():
    cookies, uid = load_cookies()

    rows = httpx.get(f"{SUPABASE_URL}/rest/v1/availability_cache",
                     params={"platform": "eq.playbypoint", "select": "data"},
                     headers=HDRS, timeout=60).json()

    units = Counter()
    details = Counter()
    by_class = Counter()
    mixed_programmes = []
    package_with_member_discount = []
    venue_gain = {}
    unknown_examples = []

    for row in rows:
        d = row.get("data") or {}
        fid = d.get("id")
        slug_club = SLUGS.get(fid)
        if not slug_club:
            continue
        sessions = d.get("sessions") or []
        prog_slugs, seen = [], set()
        for s in sessions:
            sl = s.get("program_slug")
            if sl and sl not in seen:
                seen.add(sl); prog_slugs.append(sl)
            if len(prog_slugs) >= MAX_PROGRAMS_PER_VENUE:
                break
        if not prog_slugs:
            continue

        gained = 0
        print(f"\n{'='*76}\n{d.get('name')}  (fid={fid})   "
              f"sessions={len(sessions)}  with price_tiers="
              f"{sum(1 for s in sessions if s.get('price_tiers'))}\n{'='*76}")

        for slug in prog_slugs:
            await asyncio.sleep(SLEEP)
            try:
                async with PlayByPointAPI(cookies=cookies, club_slug=slug_club) as api:
                    api._user_id = uid
                    html = await api.program_detail_html(slug)
                    props = _extract_react_props_from_html(html) or {}
            except Exception as e:
                print(f"  {slug[:44]}: {str(e)[:50]}")
                continue

            records = (props.get("prices") or []) + (props.get("packages") or [])
            if not records:
                continue

            kinds = set()
            occ_member = [r for r in records
                          if classify(r) == "OCCURRENCE" and not r.get("hidden")
                          and str(r.get("player_category", "")).lower() == "member"]
            pkg_member = [r for r in records
                          if classify(r) == "PACKAGE" and not r.get("hidden")
                          and str(r.get("player_category", "")).lower() == "member"]

            print(f"\n  {slug[:52]}")
            for r in records:
                k = classify(r)
                kinds.add(k)
                by_class[k] += 1
                units[str(r.get("lesson_unit"))] += 1
                details[str(r.get("lesson_details"))] += 1
                flag = "" if not r.get("hidden") else "  (hidden)"
                print(f"    {k:10} lessons={str(r.get('lessons')):4} "
                      f"unit={str(r.get('lesson_unit'))[:28]:28} "
                      f"${str(r.get('price')):8} {str(r.get('player_category'))[:11]:11}{flag}")
                if k == "UNKNOWN" and len(unknown_examples) < 8:
                    unknown_examples.append((d.get("name"), slug, r))

            if len(kinds) > 1:
                mixed_programmes.append((d.get("name"), slug, sorted(kinds)))
            if occ_member:
                gained += sum(1 for s in sessions if s.get("program_slug") == slug)
            if pkg_member:
                package_with_member_discount.append(
                    (d.get("name"), slug,
                     [(r.get("lessons"), r.get("lesson_unit"), r.get("price")) for r in pkg_member]))

        venue_gain[d.get("name")] = gained

    print("\n" + "=" * 76)
    print("SUMMARY")
    print("=" * 76)
    print(f"\nrecords by class: {dict(by_class)}")
    print("\nlesson_unit values:")
    for u, n in units.most_common():
        print(f"  {u[:44]:44} {n}")
    print("\nlesson_details patterns:")
    for t, n in details.most_common(12):
        print(f"  {t[:52]:52} {n}")

    print(f"\nprogrammes mixing OCCURRENCE and PACKAGE records: {len(mixed_programmes)}")
    for m in mixed_programmes[:10]:
        print("   ", m)
    print("  (classification must therefore be per RECORD, not per programme)")

    print(f"\nUNKNOWN lesson_unit records: {by_class['UNKNOWN']}")
    for u in unknown_examples:
        print("   ", u[0], u[1], json.dumps({k: u[2].get(k) for k in
              ('lessons', 'lesson_unit', 'lesson_details', 'price', 'player_category')}))

    print(f"\nPACKAGE records with a member discount: {len(package_with_member_discount)}")
    for p in package_with_member_discount[:10]:
        print("   ", p)
    print("  (a real member benefit -- must not be shown as a session price,")
    print("   and must not simply be discarded either)")

    print("\nsessions that would gain a member price from occurrence records:")
    for v, n in sorted(venue_gain.items(), key=lambda x: -x[1]):
        if n:
            print(f"  {str(v)[:36]:36} {n}")


if __name__ == "__main__":
    asyncio.run(main())
