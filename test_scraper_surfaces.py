"""
test_scraper_surfaces.py -- scraper and API server on the reviewed surface
classification.

Run: python3 test_scraper_surfaces.py
"""
import re, sys
import court_surfaces as cs

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"  {detail}"))
    if not cond: failures.append(label)

def code(path):
    return "\n".join(l for l in open(path).read().split("\n")
                     if not l.strip().startswith("#"))

fcb, api, ext = code("fetch_court_blocks.py"), code("api_server.py"), code("extract_thejar.py")

print("\n-- the 'pickle' name heuristic is gone everywhere --")
for name, src in (("fetch_court_blocks", fcb), ("api_server", api)):
    check(f"no 'pickle' substring test in {name}", '"pickle" in' not in src)
check("no VENUE_SURFACES reads in fetch_court_blocks",
      not re.search(r"VENUE_SURFACES\.(get|items)|VENUE_SURFACES\[", fcb))
check("no VENUE_SURFACES reads in api_server",
      not re.search(r"VENUE_SURFACES\.(get|items)|VENUE_SURFACES\[", api))
check("both still define the map until the cleanup change",
      "VENUE_SURFACES" in fcb and "VENUE_SURFACES" in api)

print("\n-- discovery is unfiltered --")
# kind=reservation hides members_only and training_court while
# available_courts still serves courts on those surfaces.
check("court_types accepts kind=None", "kind: str | None" in ext)
check("kind is omitted when None", 'params={"kind": kind} if kind else {}' in ext)
for name, src in (("fetch_court_blocks", fcb), ("api_server", api)):
    calls = re.findall(r"court_types\(([^)]*)\)", src)
    unfiltered = [c for c in calls if "kind=None" in c]
    check(f"{name} discovers with kind=None", len(unfiltered) == len(calls),
          [c for c in calls if "kind=None" not in c])

print("\n-- no 'pickleball' fallback survives --")
for name, src in (("fetch_court_blocks", fcb), ("api_server", api)):
    check(f"{name} never defaults a surface to 'pickleball'",
          'surfaces = ["pickleball"]' not in src and 'surface = "pickleball"' not in src)
check("api_server returns no_court_surfaces rather than guessing",
      "no_court_surfaces" in api)
check("fetch_court_blocks raises rather than scraping nothing quietly",
      "no court surfaces" in fcb)

print("\n-- unknown surfaces are reported, not swallowed --")
for name, src in (("fetch_court_blocks", fcb), ("api_server", api)):
    check(f"{name} emits the unknown-surface diagnostic",
          "res.unknown" in src and "diagnostic()" in src)

print("\n-- what the estate now resolves to --")
cases = [
    (1664, [{"surface": "indoor_pickleball"}, {"surface": "members_only"}],
     ["indoor_pickleball"], "The Rally: members_only excluded, no double count"),
    (1557, [{"surface": "standard_courts"}, {"surface": "championship_courts"}],
     ["championship_courts", "standard_courts"], "Dink & Drive: both surfaces"),
    (1379, [{"surface": "main_courts"}, {"surface": "drill_skill_court"}],
     ["drill_skill_court", "main_courts"], "Pickleholic: gains drill_skill_court"),
    (1696, [{"surface": "pickleball"}, {"surface": "training_court"}],
     ["pickleball", "training_court"], "Picklezone: gains training_court"),
    (885, [{"surface": "pickleball"}, {"surface": "sauna"}, {"surface": "compression"},
           {"surface": "cold_plunge"}, {"surface": "massage_chair"}],
     ["pickleball"], "SportsWell: 5 surfaces -> 1 court surface"),
    (1783, [{"surface": "pickleball"}, {"surface": "ball_machine"},
            {"surface": "private_office"}, {"surface": "private_room"}],
     ["pickleball"], "Pickle4Real: ball_machine and rooms excluded"),
]
for fid, types, expected, label in cases:
    got = cs.court_surfaces(fid, types)
    check(label, got == expected, got)

print("\n-- the heuristic would have got these wrong --")
# Each of these is a surface the old "pickle" test misses or misclassifies.
for surface, expected, why in (
        ("championship_courts", cs.COURT, "no 'pickle' in the name"),
        ("drill_skill_court", cs.COURT, "no 'pickle' in the name"),
        ("training_court", cs.COURT, "hidden by kind=reservation too"),
        ("members_only", cs.ALTERNATE, "court-shaped but not capacity"),
        ("futsal", cs.NON_COURT, "resources named 'Futsal/Netball Court 1'")):
    check(f"{surface} -> {expected} ({why})", cs.classify(surface) == expected)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}"); sys.exit(1)
print("all checks passed")

print("\n-- scraper writes only courts the inventory recognises --")
# available_courts is an availability view and can return resources /courts
# does not list: a live cycle surfaced court 16455 at The Rally, absent from
# that facility's inventory. Caching blocks for it would offer a court the
# booking server refuses.
import court_inventory as ci
RALLY_TYPES = [{"surface": "indoor_pickleball"}, {"surface": "members_only"}]
RALLY_COURTS = [
    {"id": 16078, "name": "Court 1 - Show Court", "surface": "indoor_pickleball",
     "archived": False, "children_court_ids": [18043]},
    {"id": 16090, "name": "Court 3", "surface": "indoor_pickleball",
     "archived": False, "children_court_ids": [18046]},
    {"id": 18043, "name": "Court 1 Overlap", "surface": "members_only",
     "archived": False, "children_court_ids": []},
    {"id": 16815, "name": "The Rally Lounge", "surface": "function_room",
     "archived": False, "children_court_ids": []},
]
inv = ci.build_inventory(1664, RALLY_TYPES, RALLY_COURTS)
valid = {str(c.id) for c in inv.courts}
check("inventory ids are the physical courts only", valid == {"16078", "16090"}, valid)
check("a court absent from inventory is filtered out", "16455" not in valid)
check("the members_only child is filtered out", "18043" not in valid)
check("function_room is filtered out (NON_COURT)", "16815" not in valid)
# It is classified now, so it must NOT raise a diagnostic -- a decision
# already taken is silent. An unseen surface still must.
check("a classified NON_COURT raises no diagnostic", inv.diagnostic() is None, inv.diagnostic())
inv_new = ci.build_inventory(1664, RALLY_TYPES, RALLY_COURTS + [
    {"id": 99, "name": "Padel 1", "surface": "padel", "archived": False}])
check("an unseen surface still raises UNKNOWN_SURFACE",
      "padel" in (inv_new.diagnostic() or ""), inv_new.diagnostic())
check("and contributes no inventory", 99 not in {c.id for c in inv_new.courts})
check("fetch_court_blocks intersects blocks with inventory",
      "valid_ids" in fcb and "build_inventory" in fcb)
check("and reports what it dropped rather than dropping silently",
      "not in the facility inventory" in open("fetch_court_blocks.py").read())

print("\n-- members_only pairing is by id, not by name --")
parent = next(c for c in RALLY_COURTS if c["id"] == 16078)
check("children_court_ids gives the mapping", parent["children_court_ids"] == [18043])
check("the child declares no children of its own",
      next(c for c in RALLY_COURTS if c["id"] == 18043)["children_court_ids"] == [])

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}"); sys.exit(1)
print("all checks passed")
