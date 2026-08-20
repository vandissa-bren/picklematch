"""
test_court_surfaces.py -- the 2B classification invariant:

  every PBP surface is COURT, NON_COURT, SPECIAL or UNKNOWN before it can
  contribute inventory, and only COURT ever does

Run: python3 test_court_surfaces.py
"""
import json, sys
import court_surfaces as cs

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"  {detail}"))
    if not cond: failures.append(label)

data = json.load(open(cs.CLASSIFICATION_PATH))

print("\n-- every observed surface is classified --")
observed, classified = cs.census(), set(data["classifications"])
check("22 surfaces in the census baseline", len(observed) == 22, len(observed))
check("no census surface is unclassified", not (observed - classified),
      sorted(observed - classified))
check("no classification without a census entry", not (classified - observed),
      sorted(classified - observed))
check("every entry carries its evidence",
      all(e.get("evidence") for e in data["classifications"].values()),
      [s for s, e in data["classifications"].items() if not e.get("evidence")])
check("every class is one of the three storable",
      all(e["class"] in (cs.COURT, cs.NON_COURT, cs.ALTERNATE)
          for e in data["classifications"].values()))
mo = data["classifications"]["members_only"]
check("members_only records it is not inventory-bearing",
      mo.get("inventory_bearing") is False)
check("members_only records the RESOLVED pairing (children_court_ids)",
      "children_court_ids" in mo.get("pairing", ""))
check("members_only records that its purpose is unproven", bool(mo.get("open_purpose")))
check("UNKNOWN is never stored, only returned",
      not any(e["class"] == cs.UNKNOWN for e in data["classifications"].values()))

print("\n-- the seed matches the reviewed decisions --")
check("8 COURT surfaces", len(cs.all_of(cs.COURT)) == 8, cs.all_of(cs.COURT))
check("13 NON_COURT surfaces", len(cs.all_of(cs.NON_COURT)) == 13)
# function_room was found via /courts, not court_types -- the two endpoints
# expose different resource sets.
check("function_room -> NON_COURT", cs.classify("function_room") == cs.NON_COURT)
check("the census records that court_types is incomplete",
      "INCOMPLETE" in data["census"]["note"])
check("members_only is the only ALTERNATE", cs.all_of(cs.ALTERNATE) == ["members_only"])
for s in ("pickleball", "indoor_pickleball", "show_court", "standard_courts",
          "championship_courts", "main_courts", "drill_skill_court", "training_court"):
    check(f"{s} -> COURT", cs.classify(s) == cs.COURT, cs.classify(s))
for s in ("sauna", "ball_machine", "compression", "cold_plunge", "massage_chair",
          "private_office", "private_room", "futsal", "cricket_net",
          "hydrotherapy_spa", "infrared_sauna", "reformer_pilates"):
    check(f"{s} -> NON_COURT", cs.classify(s) == cs.NON_COURT, cs.classify(s))

print("\n-- futsal is excluded despite being named 'Court' --")
# 'Futsal/Netball Court 1' would pass any name-based court test.
check("futsal is NON_COURT", cs.classify("futsal") == cs.NON_COURT)

print("\n-- members_only contributes nothing while unresolved --")
check("members_only -> SPECIAL", cs.classify("members_only") == cs.ALTERNATE)
rally = [{"surface": "indoor_pickleball"}, {"surface": "members_only"}]
res = cs.resolve_surfaces(1664, rally)
check("The Rally resolves to indoor_pickleball only", res.court == ["indoor_pickleball"], res.court)
check("members_only lands in alternate", res.alternate == ["members_only"])
check("members_only is NOT in court inventory", "members_only" not in res.court)
check("a known ALTERNATE does NOT trigger a review alert", not res.needs_review)
check("no diagnostic for an understood surface", res.diagnostic() is None, res.diagnostic())

print("\n-- unknown surfaces contribute nothing and are loud --")
future = [{"surface": "pickleball"}, {"surface": "covered_pickleball"}]
res = cs.resolve_surfaces(597, future)
check("unknown surface -> UNKNOWN", cs.classify("covered_pickleball") == cs.UNKNOWN)
check("unknown is excluded from inventory", res.court == ["pickleball"], res.court)
check("unknown is reported, not dropped", res.unknown == ["covered_pickleball"])
check("diagnostic says UNKNOWN_SURFACE", "UNKNOWN_SURFACE" in (res.diagnostic() or ""))
check("unknown is not silently treated as NON_COURT",
      cs.classify("covered_pickleball") != cs.NON_COURT)

print("\n-- no fallback to 'pickleball' --")
try:
    cs.court_surfaces(885, [{"surface": "sauna"}, {"surface": "compression"}])
    check("a facility with only non-courts refuses", False, "returned surfaces")
except cs.SurfaceError as e:
    check("only non-court surfaces -> SurfaceError", "refusing to guess" in str(e))
try:
    cs.court_surfaces(1664, [])
    check("empty court_types refuses", False, "returned surfaces")
except cs.SurfaceError:
    check("empty court_types -> SurfaceError, not ['pickleball']", True)
try:
    cs.court_surfaces(999, [{"surface": "brand_new_thing"}])
    check("all-unknown facility refuses", False, "returned surfaces")
except cs.SurfaceError as e:
    check("all-unknown -> SurfaceError naming the unknown", "brand_new_thing" in str(e))

print("\n-- one physical court, one unit of inventory --")
# The Rally: 6 indoor_pickleball courts + 6 members_only representations of
# the SAME courts. Inventory must be 6, not 12.
r = cs.resolve_surfaces(1664, [{"surface": "indoor_pickleball"}, {"surface": "members_only"}])
check("The Rally contributes ONE surface of capacity", len(r.court) == 1, r.court)
p = cs.resolve_surfaces(1532, [{"surface": "indoor_pickleball"}, {"surface": "members_only"}])
check("Pickleplex likewise", p.court == ["indoor_pickleball"], p.court)

print("\n-- real payloads from the estate --")
dink = [{"surface": "standard_courts"}, {"surface": "championship_courts"}]
check("Dink & Drive gets BOTH surfaces",
      cs.court_surfaces(1557, dink) == ["championship_courts", "standard_courts"])
holic = [{"surface": "main_courts"}, {"surface": "drill_skill_court"}]
check("Pickleholic gets both, neither named 'pickle'",
      cs.court_surfaces(1379, holic) == ["drill_skill_court", "main_courts"])
sw = [{"surface": "pickleball"}, {"surface": "sauna"}, {"surface": "compression"},
      {"surface": "cold_plunge"}, {"surface": "massage_chair"},
      {"surface": "infrared_sauna"}, {"surface": "hydrotherapy_spa"},
      {"surface": "reformer_pilates"}]
check("SportsWell yields only pickleball from 8 surfaces",
      cs.court_surfaces(885, sw) == ["pickleball"])
p4r = [{"surface": "pickleball"}, {"surface": "ball_machine"},
       {"surface": "private_office"}, {"surface": "private_room"}]
check("Pickle4Real excludes ball_machine and rooms",
      cs.court_surfaces(1783, p4r) == ["pickleball"])
check("malformed entries are skipped, not crashed on",
      cs.resolve_surfaces(1, [{"surface": None}, {}, {"surface": "pickleball"}]).court
      == ["pickleball"])

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}"); sys.exit(1)
print("all checks passed")
