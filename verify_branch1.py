"""
verify_branch1.py -- READ-ONLY. Two open questions after the Branch 1 scrape.

Q1  The Jar HQ lesson 4324041 moved $30 -> $15. Its price_tiers is empty,
    so the cache cannot distinguish "the venue changed the price" from
    "Branch 1 changed the selection". Applies the ORIGINAL pre-Branch-1
    rule, verbatim, to today's props: $15 means the venue changed it, $30
    means the selection diverged and Branch 1 is at fault.

Q2  The tier spot-check used a clinic with ONE member record, so it did not
    prove two member tiers survive. Checks the two programs known to have
    them. Collapsing those is the exact failure the record-based storage
    exists to prevent.

No writes, no bookings.
Run:  cd /app && /app/venv/bin/python3 verify_branch1.py
"""
import asyncio, json, os, sys
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, "/app")
from extract_thejar import PlayByPointAPI, _extract_react_props_from_html
import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://stwohmddmdwttasbyblt.supabase.co")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HDRS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


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


def original_rule(props, lesson):
    """Pre-Branch-1 selection, copied verbatim. Both loops skip member."""
    price = ""
    for pl in (props.get("prices") or props.get("packages") or []):
        if not pl.get("hidden") and pl.get("price") and pl.get("player_category") != "member":
            p = float(pl["price"])
            price = f"${p:.0f}" if p == int(p) else f"${p:.2f}"
            break
    lp = price
    for ip in (lesson.get("individual_prices") or []):
        if ip.get("price") and ip.get("player_category") != "member":
            p = float(ip["price"])
            lp = f"${p:.0f}" if p == int(p) else f"${p:.2f}"
            break
    return lp


async def main():
    cookies, uid = load_cookies()

    print("=" * 74)
    print("Q1  The Jar HQ lesson 4324041: venue change, or selection change?")
    print("=" * 74)
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/availability_cache",
                  params={"id": "eq.pbp-1883", "select": "data"}, headers=HDRS, timeout=30)
    d = r.json()[0]["data"]
    sess = next((s for s in (d.get("sessions") or []) if s.get("lesson_id") == 4324041), None)
    if not sess:
        print("  session no longer in cache -- likely rolled out of the date range")
    else:
        slug = sess.get("program_slug")
        print(f"  cached price={sess.get('price')}  slug={slug}")
        async with PlayByPointAPI(cookies=cookies, club_slug="TheJarHQ") as api:
            api._user_id = uid
            html = await api.program_detail_html(slug)
            props = _extract_react_props_from_html(html) or {}
        lessons = props.get("sessions") or props.get("clinic_lessons") or []
        lesson = next((l for l in lessons if l.get("id") == 4324041), None)
        print(f"  props prices     : {json.dumps(props.get('prices'))}")
        print(f"  props packages   : {json.dumps(props.get('packages'))}")
        if lesson:
            print(f"  individual_prices: {json.dumps(lesson.get('individual_prices'))}")
            verdict = original_rule(props, lesson)
            print(f"\n  ORIGINAL rule on today's data -> {verdict}")
            if verdict == sess.get("price"):
                print("  => matches cache. The VENUE changed it; selection unaffected.")
            else:
                print(f"  => DIVERGENCE: original={verdict}, cache={sess.get('price')}")
                print("     Branch 1 altered the selection. Investigate before proceeding.")
        else:
            print("  lesson not in today's props (may have passed)")

    print("\n" + "=" * 74)
    print("Q2  Do the known MULTI-TIER member programs survive?")
    print("=" * 74)
    for fid, label, want in (
        (1532, "PicklePlex", "drill-session-advanced-beginner-to-intermediate"),
        (1664, "The Rally", "advanced-social-fixed-partners"),
    ):
        d = httpx.get(f"{SUPABASE_URL}/rest/v1/availability_cache",
                      params={"id": f"eq.pbp-{fid}", "select": "data"},
                      headers=HDRS, timeout=30).json()[0]["data"]
        hits = [s for s in (d.get("sessions") or []) if s.get("program_slug") == want]
        print(f"\n  {label} / {want}")
        if not hits:
            print("    not in cache -- no upcoming sessions in range")
            multi = [s for s in (d.get("sessions") or [])
                     if len([t for t in (s.get("price_tiers") or [])
                             if str(t.get("player_category")).lower() == "member"]) >= 2]
            if multi:
                s = multi[0]
                print(f"    but another multi-tier clinic here: {str(s.get('title'))[:40]}")
                for t in s["price_tiers"]:
                    print("      " + json.dumps(t))
                print("    => two member tiers preserved.")
            continue
        s = hits[0]
        tiers = s.get("price_tiers") or []
        members = [t for t in tiers if str(t.get("player_category")).lower() == "member"]
        print(f"    price={s.get('price')}  tiers={len(tiers)}  member tiers={len(members)}")
        for t in tiers:
            print("      " + json.dumps(t))
        print("    => BOTH member tiers preserved." if len(members) >= 2
              else "    => single member tier here")


if __name__ == "__main__":
    asyncio.run(main())
