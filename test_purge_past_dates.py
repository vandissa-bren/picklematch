"""
test_purge_past_dates.py -- by_date holds today and every future date, and
nothing before today.

availability_cache.by_date accumulated indefinitely: The Rally held 17 dates
including 15-17 August, days past and never rewritten, because no writer ever
removed a key.

The filter is against TODAY, never the writer's own scrape window. Three jobs
write this key -- today+1 at :00, days 3-8 at :05, days 8-14 at :07 -- so a
writer pruning to its own range would delete the others' coverage every
quarter hour and they would restore it minutes later.

Run: python3 test_purge_past_dates.py
"""
import sys
from datetime import date, timedelta
from fetch_court_blocks import prune_past_dates

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"  {detail}"))
    if not cond: failures.append(label)

TODAY = date(2026, 8, 20)
T = TODAY.isoformat()
def d(n): return (TODAY + timedelta(days=n)).isoformat()
BLOCK = [{"court": "Court 1", "court_id": "16078", "start": "08:00",
          "end": "09:00", "duration_min": 60, "price": 25.0, "shift": "lowtime"}]

print("\n-- the retention boundary --")
full = {d(n): list(BLOCK) for n in (-14, -5, -1, 0, 1, 7, 14)}
kept = prune_past_dates(full, T)
check("yesterday removed", d(-1) not in kept)
check("5 days ago removed", d(-5) not in kept)
check("14 days ago removed", d(-14) not in kept)
check("today retained", d(0) in kept)
check("tomorrow retained", d(1) in kept)
check("day +7 retained", d(7) in kept)
check("day +14 retained", d(14) in kept)
check("exactly the forward dates remain",
      sorted(kept) == sorted([d(0), d(1), d(7), d(14)]), sorted(kept))

print("\n-- a writer never deletes another writer's coverage --")
# The :00 job writes today+1 only. It must not prune days 3-14 written by the
# :05 and :07 jobs merely because its own range is shorter.
estate = {d(n): list(BLOCK) for n in (-3, 0, 1, 3, 5, 8, 11, 14)}
after_near = prune_past_dates(estate, T)
after_near[d(0)] = ["fresh"]; after_near[d(1)] = ["fresh"]
check("days 3-14 survive the :00 writer",
      all(d(n) in after_near for n in (3, 5, 8, 11, 14)), sorted(after_near))
check("its own dates are written", after_near[d(0)] == ["fresh"])
check("the past date is gone", d(-3) not in after_near)

# and the :07 job, writing days 8-14, must not drop today+1
after_far = prune_past_dates(after_near, T)
after_far[d(8)] = ["fresh-far"]
check("today and tomorrow survive the :07 writer",
      d(0) in after_far and d(1) in after_far)
check("no writer's output is lost across the cycle",
      sorted(after_far) == sorted([d(n) for n in (0, 1, 3, 5, 8, 11, 14)]),
      sorted(after_far))

print("\n-- idempotence --")
once = prune_past_dates(full, T)
twice = prune_past_dates(once, T)
check("pruning twice changes nothing", once == twice)
check("pruning an already-clean dict is a no-op",
      prune_past_dates(twice, T) == twice)

print("\n-- it returns a new dict rather than mutating --")
src = {d(-1): list(BLOCK), d(1): list(BLOCK)}
out = prune_past_dates(src, T)
check("input still holds the past date", d(-1) in src)
check("output does not", d(-1) not in out)

print("\n-- block contents are untouched --")
out = prune_past_dates({d(1): list(BLOCK)}, T)
check("blocks pass through unchanged", out[d(1)] == BLOCK, out[d(1)])
check("court_id survives the prune", out[d(1)][0]["court_id"] == "16078")

print("\n-- edge cases --")
check("empty dict", prune_past_dates({}, T) == {})
check("None", prune_past_dates(None, T) == {})
check("an empty future date is retained, not treated as absent",
      prune_past_dates({d(2): []}, T) == {d(2): []})
# Deleting something unparseable is the wrong default when the alternative is
# a little unused data.
weird = prune_past_dates({"not-a-date": list(BLOCK), d(-1): list(BLOCK)}, T)
check("a malformed key is kept", "not-a-date" in weird)
check("while a real past date still goes", d(-1) not in weird)

print("\n-- only by_date is pruned --")
src = open("fetch_court_blocks.py").read()
# Behaviour, not wording: the call site passes only data["by_date"], so no
# other key on the record can be reached by it.
check("the prune is passed by_date and nothing else",
      'prune_past_dates(data.get("by_date", {}),' in src)
check("prune runs before the merge, so this writer's dates are safe",
      src.index("prune_past_dates(data.get") < src.index('for date_str, blocks in result["by_date"]'))
# A record carrying sessions and past-dated blocks: only the blocks go.
record = {"sessions": [{"lesson_id": 1, "date": d(-30)}],
          "program_pricing": {"x": 1},
          "by_date": {d(-30): list(BLOCK), d(1): list(BLOCK)}}
record["by_date"] = prune_past_dates(record["by_date"], T)
check("a 30-day-old session record survives", len(record["sessions"]) == 1)
check("program_pricing survives", record["program_pricing"] == {"x": 1})
check("but its 30-day-old court blocks are gone", d(-30) not in record["by_date"])
check("and the future date remains", d(1) in record["by_date"])

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}"); sys.exit(1)
print("all checks passed")
