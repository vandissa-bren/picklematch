"""
test_price_stamping.py -- a block's price may only come from pricing
applicable to THAT block.

The bug: _get_pbp_availability fetched ONE price -- for court_blocks[0], on
courts_data[0], over a fixed one-hour window -- and assigned it to every block
at the venue for the whole day.

Observed live at The Rally on 2026-08-20: browse showed $43.48/hr at 4pm, 5pm,
7pm, 8pm and 9pm while the 9pm primetime slot actually costs $60. $43.48 is
none of that venue's cached prices (25 lowtime / 60 day / 60 primetime) -- it
was whatever the first available hour on an arbitrary court happened to cost.

Run: python3 test_price_stamping.py
"""
import re, sys

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"  {detail}"))
    if not cond: failures.append(label)

src = open("api_server.py").read()
code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))

print("\n-- the stamping heuristic is gone --")
check("no 'apply same price to all blocks' loop",
      "Apply same price to all blocks" not in code)
check("no price taken from court_blocks[0]",
      'result["court_blocks"][0].get("price")' not in code)
check("no courts_data[0] price fetch", "courts_data[0]" not in code)
check("prices are not fetched with a user_id in this builder",
      "_extract_price_string" not in code)

print("\n-- each block is priced from its own court and shift --")
check("lookup is keyed by court_id and shift", '_court_prices.get(f"{cid}_{shift}")' in code)
check("a block without both stays unpriced", "if cid and shift else None" in code)
check("blocks carry their shift", '"shift": run_shift' in code)
check("runs split at shift boundaries", "s_shift == run_shift" in code)
check("weekend shifts are suffixed", '_weekend" if _is_weekend' in code)

# ---- the invariant, exercised directly ----
def price_blocks(blocks, court_prices):
    """Mirrors the production loop."""
    for b in blocks:
        cid, shift = b.get("court_id"), b.get("shift")
        b["price"] = court_prices.get(f"{cid}_{shift}") if cid and shift else None
    return blocks

# The Rally's real cached table.
RALLY = {"16078_lowtime": 25.0, "16078_day": 60.0, "16078_primetime": 60.0,
         "16078_lowtime_weekend": 30.0, "16078_day_weekend": 30.0,
         "16078_primetime_weekend": 30.0}

print("\n-- The Rally keeps its shift distinctions --")
out = price_blocks([
    {"court_id": "16078", "shift": "lowtime", "start": "08:00"},
    {"court_id": "16078", "shift": "day", "start": "12:00"},
    {"court_id": "16078", "shift": "primetime", "start": "21:00"},
], dict(RALLY))
check("lowtime -> 25", out[0]["price"] == 25.0, out[0])
check("day -> 60", out[1]["price"] == 60.0, out[1])
check("primetime -> 60", out[2]["price"] == 60.0, out[2])
check("three blocks, two distinct prices",
      len({b["price"] for b in out}) == 2, {b["price"] for b in out})

print("\n-- the original failure mode: block 0 must not propagate --")
# Block 0 is a cheap lowtime slot; the rest are primetime. Under the old code
# every block showed block 0's price.
out = price_blocks([
    {"court_id": "16078", "shift": "lowtime"},
    {"court_id": "16078", "shift": "primetime"},
    {"court_id": "16078", "shift": "primetime"},
], dict(RALLY))
check("block 0 stays 25", out[0]["price"] == 25.0)
check("block 1 is 60, NOT 25", out[1]["price"] == 60.0, out[1])
check("block 2 is 60, NOT 25", out[2]["price"] == 60.0, out[2])
check("no block inherited block 0's price",
      [b["price"] for b in out] == [25.0, 60.0, 60.0])

print("\n-- a block with no cached price stays unpriced --")
out = price_blocks([
    {"court_id": "16078", "shift": "primetime"},
    {"court_id": "99999", "shift": "primetime"},   # court not in the table
    {"court_id": "16078", "shift": None},          # shift unknown
    {"court_id": None, "shift": "day"},            # id unknown
], dict(RALLY))
check("known block priced", out[0]["price"] == 60.0)
check("unknown court -> None, not a neighbour's price", out[1]["price"] is None, out[1])
check("unknown shift -> None", out[2]["price"] is None, out[2])
check("unknown court_id -> None", out[3]["price"] is None, out[3])
check("no unpriced block borrowed the priced one's value",
      out[1]["price"] != 60.0 and out[2]["price"] != 60.0)

print("\n-- weekday and weekend differ for the same shift --")
out = price_blocks([{"court_id": "16078", "shift": "day"},
                    {"court_id": "16078", "shift": "day_weekend"}], dict(RALLY))
check("weekday day -> 60", out[0]["price"] == 60.0)
check("weekend day -> 30", out[1]["price"] == 30.0, out[1])

print("\n-- an empty price table prices nothing rather than guessing --")
out = price_blocks([{"court_id": "16078", "shift": "day"}], {})
check("no cache -> None", out[0]["price"] is None)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}"); sys.exit(1)
print("all checks passed")
