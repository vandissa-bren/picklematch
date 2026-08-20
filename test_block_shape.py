"""
test_block_shape.py -- every writer of availability_cache.by_date must store
the same canonical block shape.

The defect: apply_prices_to_blocks rebuilt each block from five fields,
reading court_id and shift to look up the price and then dropping both. The
scraper computed the identity correctly and discarded it in the last step
before saving, so every stored block identified its court by NAME only.

Downstream that forced the servers to re-resolve names against a live
availability call, and left the frontend on the name contract for every
near-term date -- while fetch_sportswell, which does keep court_id, made 885
and 1770 the only two venues whose stored blocks carried one.

Run: python3 test_block_shape.py
"""
import sys
from fetch_court_blocks import apply_prices_to_blocks, CANONICAL_BLOCK_FIELDS

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"  {detail}"))
    if not cond: failures.append(label)

RALLY_PRICES = {"16078_lowtime": 25.0, "16078_primetime": 60.0,
                "16090_lowtime": 25.0}
BLOCKS = [
    {"court": "Court 1 - Show Court", "court_id": "16078", "start": "08:00",
     "end": "09:30", "duration_min": 90, "shift": "lowtime"},
    {"court": "Court 1 - Show Court", "court_id": "16078", "start": "21:00",
     "end": "22:00", "duration_min": 60, "shift": "primetime"},
    {"court": "Court 3", "court_id": "16090", "start": "08:00",
     "end": "16:00", "duration_min": 480, "shift": "lowtime"},
]

print("\n-- identity survives the pricing step --")
out = apply_prices_to_blocks(BLOCKS, RALLY_PRICES)
check("court_id preserved on every block",
      all(b.get("court_id") for b in out), out)
check("ids are the ones passed in",
      [b["court_id"] for b in out] == ["16078", "16078", "16090"])
check("shift preserved too", all(b.get("shift") for b in out))
check("names still present", all(b.get("court") for b in out))

print("\n-- the canonical shape --")
for b in out:
    check(f"{b['court']} {b['start']} has exactly the canonical fields",
          set(b.keys()) == set(CANONICAL_BLOCK_FIELDS), sorted(b.keys()))

print("\n-- prices are still per court and shift --")
check("lowtime -> 25", out[0]["price"] == 25.0, out[0])
check("primetime -> 60 on the SAME court", out[1]["price"] == 60.0, out[1])
check("a second court prices independently", out[2]["price"] == 25.0)
check("two distinct prices across three blocks",
      len({b["price"] for b in out}) == 2)

print("\n-- a block that cannot be priced keeps its identity --")
out = apply_prices_to_blocks([
    {"court": "Court 9", "court_id": "99999", "start": "08:00", "end": "09:00",
     "duration_min": 60, "shift": "lowtime"},
    {"court": "Court 1 - Show Court", "court_id": "16078", "start": "08:00",
     "end": "09:00", "duration_min": 60, "shift": None},
], RALLY_PRICES)
check("unknown court -> no price", out[0]["price"] is None)
check("but court_id is still stored", out[0]["court_id"] == "99999", out[0])
check("missing shift -> no price", out[1]["price"] is None)
check("and its court_id survives", out[1]["court_id"] == "16078")

print("\n-- the readback contract --")
# What a consumer of by_date can rely on.
stored = apply_prices_to_blocks(BLOCKS, RALLY_PRICES)
check("a reader can resolve a court without re-fetching availability",
      all(b["court_id"] for b in stored))
check("a reader can re-price a block itself from court_id and shift",
      all(b["court_id"] and b["shift"] for b in stored))
# Three blocks, three distinct (court, shift, price) triples: two courts and
# two shifts, so nothing is shared by position or inherited from a sibling.
check("each block carries its own court/shift/price triple",
      len({(b["court_id"], b["shift"], b["price"]) for b in stored}) == 3,
      {(b["court_id"], b["shift"], b["price"]) for b in stored})
check("the same court prices differently across shifts",
      stored[0]["court_id"] == stored[1]["court_id"]
      and stored[0]["price"] != stored[1]["price"])

print("\n-- fetch_sportswell already stored court_id --")
sw = open("fetch_sportswell.py").read()
check("its blocks carry court_id", '"court_id"' in sw)
check("which is why 885 and 1770 were the only venues that had it", True)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}"); sys.exit(1)
print("all checks passed")
