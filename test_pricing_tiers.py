"""
Tests for _pricing_tiers, using the shapes the props probe actually returned.
"""
import ast, os

_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "push_to_supabase.py")).read()
_nodes = [n for n in ast.parse(_src).body
          if isinstance(n, ast.FunctionDef) and n.name == "_pricing_tiers"]
_ns = {}
exec(compile(ast.Module(body=_nodes, type_ignores=[]), "<s>", "exec"), _ns)
tiers = _ns["_pricing_tiers"]

fails = []
def check(name, got, want):
    if got != want:
        fails.append(f"  FAIL {name}\n       got  {got}\n       want {want}")
    else:
        print(f"  ok   {name}")


print("--- The Jar: the reported bug ---")
jar = tiers([
    {"price": 30, "player_category": "non_member", "allowed_affiliations": "non_member"},
    {"price": 25, "player_category": "member", "allowed_affiliations": "member"},
])
check("both tiers kept", len(jar), 2)
check("member $25 no longer discarded",
      [t for t in jar if t["player_category"] == "member"][0]["price"], 25.0)
check("non-member $30 kept",
      [t for t in jar if t["player_category"] == "non_member"][0]["price"], 30.0)

print("\n--- The Rally: two member tiers ---")
rally = tiers([
    {"price": 25, "player_category": "non_member", "allowed_affiliations": "non_member"},
    {"price": 20, "player_category": "member",
     "allowed_affiliations": "Club Membership,V2 - Rally Member,ahm"},
    {"price": 12.50, "player_category": "member", "allowed_affiliations": "VIP Rally"},
])
check("all three kept", len(rally), 3)
mem = [t for t in rally if t["player_category"] == "member"]
check("two member tiers preserved separately", sorted(t["price"] for t in mem), [12.5, 20.0])
check("affiliation discriminator retained",
      [t["allowed_affiliations"] for t in mem if t["price"] == 12.5], ["VIP Rally"])

print("\n--- PicklePlex: Essentials vs Community+ ---")
plex = tiers([
    {"price": 25, "player_category": "non_member", "allowed_affiliations": "non_member"},
    {"price": 20, "player_category": "member",
     "allowed_affiliations": "Essentials,Foundation 2026,Friends,Month,member"},
    {"price": 18.75, "player_category": "member",
     "allowed_affiliations": "Community 26,Community+"},
])
check("both member tiers distinguishable",
      sorted(t["price"] for t in plex if t["player_category"] == "member"), [18.75, 20.0])

print("\n--- range fields retained where present ---")
ranged = tiers([{"price": 20, "player_category": "member", "time_unit": "week",
                 "time_range_start": 0, "time_range_end": 3}])
check("time_unit kept", ranged[0].get("time_unit"), "week")
check("range bounds kept",
      (ranged[0].get("time_range_start"), ranged[0].get("time_range_end")), (0, 3))
check("absent range fields omitted, not null",
      "time_unit" in tiers([{"price": 10, "player_category": "member"}])[0], False)

print("\n--- exclusions ---")
check("hidden records dropped",
      tiers([{"price": 10, "player_category": "member", "hidden": True}]), [])
check("null price dropped", tiers([{"price": None, "player_category": "member"}]), [])
check("non-numeric price dropped",
      tiers([{"price": "call us", "player_category": "member"}]), [])
check("bool price dropped (True is not 1.0)",
      tiers([{"price": True, "player_category": "member"}]), [])
check("zero is VALID and kept",
      tiers([{"price": 0, "player_category": "member"}])[0]["price"], 0.0)
check("non-dict entries skipped", tiers(["junk", 5, None]), [])
check("None input", tiers(None), [])
check("empty input", tiers([]), [])

print("\n--- string prices coerced ---")
check("'18.75' -> 18.75",
      tiers([{"price": "18.75", "player_category": "member"}])[0]["price"], 18.75)

print("\n--- NOT flattened to a two-field model ---")
check("output is a list of records, not price_member/price_non_member",
      isinstance(plex, list) and all(isinstance(t, dict) for t in plex), True)
check("no price_member key invented",
      any("price_member" in t for t in plex), False)

print()
if fails:
    print(f"{len(fails)} FAILURES:")
    for f in fails: print(f)
    raise SystemExit(1)
print("ALL PASS")
