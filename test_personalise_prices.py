"""
Tests for _personalise_prices -- the 3B overlay.

The resolver is frozen and tested in booking_server. These cover this
layer's own guarantees: the shared cache is never mutated, price is never
overwritten, and failure degrades to the public experience.
"""
import ast, asyncio, copy, os, sys, types

_here = os.path.dirname(os.path.abspath(__file__))
_src = open(os.path.join(_here, "api_server.py")).read()
_fn = [n for n in ast.parse(_src).body
       if isinstance(n, ast.AsyncFunctionDef) and n.name == "_personalise_prices"][0]

fails = []
def check(name, got, want):
    if got != want:
        fails.append(f"  FAIL {name}\n       got  {got!r}\n       want {want!r}")
    else:
        print(f"  ok   {name}")

# Stub httpx so nothing leaves the process.
class FakeResp:
    def __init__(self, status, payload): self.status_code, self._p = status, payload
    def json(self): return self._p

class FakeClient:
    def __init__(self, behaviour): self.b = behaviour
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, json=None, headers=None):
        if callable(self.b): return self.b(json)
        return self.b

def run(response, user_id, behaviour):
    ns = {
        "httpx": types.SimpleNamespace(AsyncClient=lambda **kw: FakeClient(behaviour)),
        "os": os, "print": lambda *a, **k: None,
        "BOOKING_SERVER_URL": "http://x", "SUPABASE_SERVICE_KEY": "k",
    }
    exec(compile(ast.Module(body=[_fn], type_ignores=[]), "<t>", "exec"), ns)
    return asyncio.get_event_loop().run_until_complete(
        ns["_personalise_prices"](response, user_id))


def RESP():
    return {"date": "2026-08-25", "venues": [
        {"id": 597, "name": "The Jar", "sessions": [
            {"lesson_id": 1, "title": "A", "price": "$30"},
            {"lesson_id": 2, "title": "B", "price": "$30"}]},
        {"id": 1664, "name": "The Rally", "sessions": [
            {"lesson_id": 3, "title": "C", "price": "$25"}]}]}


print("--- overlays resolved_price without touching price ---")
out = run(RESP(), "u1", FakeResp(200, {"resolved": {"1": "$25", "3": "$20"}}))
s = out["venues"][0]["sessions"]
check("Jar session 1 gains resolved_price", s[0].get("resolved_price"), "$25")
check("public price untouched", s[0].get("price"), "$30")
check("unresolved session has NO resolved_price", "resolved_price" in s[1], False)
check("Rally resolved", out["venues"][1]["sessions"][0].get("resolved_price"), "$20")
check("other fields preserved", s[0].get("title"), "A")

print("\n--- the shared cache object is never mutated (acceptance #9) ---")
shared = RESP()
before = copy.deepcopy(shared)
_ = run(shared, "userA", FakeResp(200, {"resolved": {"1": "$25"}}))
check("input object unchanged after personalising for user A", shared, before)
outB = run(shared, "userB", FakeResp(200, {"resolved": {}}))
check("user B sees no resolved_price from A's request",
      "resolved_price" in outB["venues"][0]["sessions"][0], False)

print("\n--- anonymous and no-op cases return the response unchanged ---")
r = RESP()
check("no user_id returns the same object", run(r, None, FakeResp(200, {})) is r, True)
check("empty user_id", run(r, "", FakeResp(200, {})) is r, True)
check("empty resolved map returns same object",
      run(r, "u", FakeResp(200, {"resolved": {}})) is r, True)
check("no sessions at all", run({"venues": []}, "u", FakeResp(200, {})), {"venues": []})
check("non-dict response passed through", run(None, "u", FakeResp(200, {})), None)

print("\n--- failure degrades to public prices, never breaks discovery ---")
r = RESP()
check("booking server 500 -> unchanged", run(r, "u", FakeResp(500, {})) is r, True)
check("booking server 401 -> unchanged", run(r, "u", FakeResp(401, {})) is r, True)
def boom(_): raise RuntimeError("connection refused")
check("connection error -> unchanged", run(r, "u", boom) is r, True)
def bad(_): return FakeResp(200, None)
check("malformed body -> unchanged", run(r, "u", bad) is r, True)

print("\n--- batching: one call for the whole page (acceptance #10) ---")
seen = {}
def capture(payload):
    seen["payload"] = payload
    return FakeResp(200, {"resolved": {}})
run(RESP(), "u1", capture)
check("one request carries every session", len(seen["payload"]["sessions"]), 3)
check("user id forwarded", seen["payload"]["user_id"], "u1")
check("sends only ids, not tiers or prices",
      sorted(seen["payload"]["sessions"][0].keys()), ["facility_id", "lesson_id"])

print("\n--- source guarantees ---")
seg = ast.get_source_segment(_src, _fn)
check("no PBP call in this layer",
      "playbypoint" in seg.lower() or "AsyncSession" in seg, False)
check("no pricing logic duplicated here",
      any(t in seg for t in ("allowed_affiliations", "player_category", "price_tiers")), False)
check("returns a copy, not the mutated input", "out = dict(response)" in seg, True)

full = _src[_src.index("async def pbp_availability"):]
full = full[:full.index("\n@app.")] if "\n@app." in full else full
check("personalisation happens AFTER _cache_set",
      full.index("_cache_set") < full.rindex("_personalise_prices"), True)
check("cache key does not include the user", "user_id" not in
      full[full.index("cache_key ="):full.index("cache_key =") + 120], True)

print("\n--- the PickleMatch user id must not be shadowed ---")
# Both endpoints bind `user_id` locally to the PBP scraper id -- one by
# assignment, one by tuple unpacking. When the query parameter shared that
# name it was silently replaced, and the pricing service received PBP's
# numeric id. It returned 422 and the response fell back to public prices,
# which is indistinguishable from a member having no discount.
import re as _re
for _name in ("pbp_availability", "live_sessions"):
    _f = [n for n in ast.parse(_src).body
          if isinstance(n, ast.AsyncFunctionDef) and n.name == _name][0]
    _s = ast.get_source_segment(_src, _f)
    check(f"{_name}: parameter is pm_user_id", "pm_user_id" in _s, True)
    check(f"{_name}: public query name preserved", 'alias="user_id"' in _s, True)
    # Substring matching would be wrong here: "user_id: Optional[str] =
    # Query" is contained in "pm_user_id: Optional[str] = Query", so the
    # correct code matches the pattern meant to detect the bug. Check the
    # parsed argument names instead.
    _args = [a.arg for a in _f.args.args + _f.args.kwonlyargs]
    check(f"{_name}: no argument literally named user_id",
          "user_id" in _args, False)
    check(f"{_name}: pm_user_id is an argument", "pm_user_id" in _args, True)
    # Every call into personalisation must pass the parameter, never the
    # local PBP id.
    _calls = _re.findall(r"_personalise\w*\([^)]*\)", _s)
    check(f"{_name}: all personalisation calls use pm_user_id",
          all("pm_user_id" in c for c in _calls) and len(_calls) > 0, True)

print()
if fails:
    print(f"{len(fails)} FAILURES:")
    for f in fails: print(f)
    raise SystemExit(1)
print("ALL PASS")
