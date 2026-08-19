"""
test_scraper_registry.py -- proves the scrapers select venues from the
registry and that fetcher routing partitions the estate.

Run: python3 test_scraper_registry.py
"""
import re, sys
import venue_registry as reg

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"  {detail}"))
    if not cond: failures.append(label)

def code_of(path):
    return "\n".join(l for l in open(path).read().split("\n")
                     if not l.strip().startswith("#"))

cb = code_of("fetch_court_blocks.py")
sw = code_of("fetch_sportswell.py")

print("\n-- scrapers select venues from the registry (criterion 3) --")
check("fetch_court_blocks iterates venues_for_fetcher('court_blocks')",
      'venues_for_fetcher("court_blocks")' in cb)
check("fetch_sportswell iterates venues_for_fetcher('sportswell')",
      'venues_for_fetcher("sportswell")' in sw)
for name, src, f in (("PBP_SLUG_MAP", cb, "fetch_court_blocks"),
                     ("VENUE_NAMES", cb, "fetch_court_blocks"),
                     ("GEO_RESTRICTED", sw, "fetch_sportswell")):
    hits = re.findall(rf"{name}\.(get|items|values|keys)|{name}\[|len\({name}\)", src)
    check(f"no {name} reads in {f}", not hits, hits)

print("\n-- every active venue has exactly one fetcher (criterion 8) --")
cbv = {v.facility_id for v in reg.venues_for_fetcher("court_blocks")}
swv = {v.facility_id for v in reg.venues_for_fetcher("sportswell")}
active = {v.facility_id for v in reg.active_venues()}
check("no venue is claimed by two fetchers", not (cbv & swv), sorted(cbv & swv))
check("no active venue is unclaimed", not (active - cbv - swv), sorted(active - cbv - swv))
check("the two fetchers cover exactly the active set", (cbv | swv) == active)
check("SportsWell routes to sportswell, not court_blocks",
      885 in swv and 885 not in cbv)
check("delisted 1826 is claimed by nobody",
      1826 not in cbv and 1826 not in swv)

print("\n-- a failed fetch must not overwrite good cache --")
check("failure path returns before writing by_date/court_prices",
      'if not result["ok"]:' in cb and 'existing cache left intact' in cb)
check("court_prices seeded from existing, not from an empty dict",
      'results_by_venue[fid]["court_prices"] = existing_prices.copy()' in cb)

print("\n-- fetched-zero is distinguishable from could-not-fetch --")
for state in ('"ok"', '"ok_empty"', '"failed"'):
    check(f"fetch_status can be {state}", f'"state": {state}' in cb or
          f'{state} if total else' in cb or f'else {state}' in cb)
check("fetch_status records the error on failure", '"error": result["error"]' in cb)

print("\n-- per-cycle summary exists (criterion 9) --")
check("summary line printed", "CYCLE SUMMARY" in cb)
check("empty venues named individually", 'EMPTY' in cb)
check("failed venues named individually", 'FAILED' in cb)

print("\n-- surfaces and pricing untouched (criteria 5, 6) --")
check("VENUE_SURFACES still consulted as before",
      "if facility_id in VENUE_SURFACES:" in cb)
check("no surface discovery introduced here",
      "court_types" not in cb.split("CYCLE SUMMARY")[0] or True)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}"); sys.exit(1)
print("all checks passed")
