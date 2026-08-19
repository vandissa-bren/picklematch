"""
test_api_server_registry.py -- proves api_server resolves venue identity
through the registry.

Run: python3 test_api_server_registry.py

Does not import api_server (it opens network clients and needs live env), so
it checks the source for map consumers and exercises the registry helpers
directly against the same snapshot the server loads.
"""
import re, sys
import venue_registry as reg

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"  {detail}"))
    if not cond: failures.append(label)

src = open("api_server.py").read()
code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))

print("\n-- no venue map consumers remain (criterion 3) --")
for name in ("PBP_SLUG_MAP", "VENUE_NAMES"):
    hits = re.findall(rf"{name}\.(get|items|values|keys)|{name}\[", code)
    check(f"no {name} reads", not hits, hits)
    check(f"{name} still defined (deleted after ALL consumers migrate)",
          f"{name}" in code)
check("helpers are used instead",
      code.count("registry_slug(") + code.count("active_slug_map(")
      + code.count("registry_name(") + code.count("bookable_slug(") >= 8)

print("\n-- the malformed surface entry is gone (criterion 8) --")
check('VENUE_SURFACES no longer maps 1783 to a bare string',
      '1783: "Pickle4Real"' not in code)
m = re.search(r"VENUE_SURFACES[^=]*=\s*\{(.*?)\n\}", code, re.S)
vals = re.findall(r"(\d+):\s*(\[[^\]]*\]|\"[^\"]*\")", m.group(1))
bad = [(f, v) for f, v in vals if not v.startswith("[")]
check("every VENUE_SURFACES value is a list", not bad, bad)

print("\n-- clinic/facility type confusion removed --")
check("no facility map is keyed by clinic_id",
      "PBP_SLUG_MAP.get(req.clinic_id" not in code)

print("\n-- registry helpers behave (criteria 4, 5) --")
check("active_slug_map covers 21 venues", len(reg.active_venues()) == 21)
check("SportsWell is included (absent from the scraper's own map)",
      885 in {v.facility_id for v in reg.active_venues()})
check("delisted 1826 excluded from active",
      1826 not in {v.facility_id for v in reg.active_venues()})
for fid, slug in ((1696, "picklezone"), (1783, "PICKLE4REAL"), (1883, "TheJarHQ")):
    v = reg.resolve(fid)
    check(f"{fid} -> {slug!r}", v.slug == slug, v)
u = reg.resolve(99999)
check("unknown facility -> Unresolved (caller skips, no default slug)",
      isinstance(u, reg.Unresolved))

print("\n-- names are real names, not 'Venue {id}' placeholders --")
check("1883 has a real name",
      reg.resolve(1883).name == "The Jar HQ | Maidstone", reg.resolve(1883).name)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}"); sys.exit(1)
print("all checks passed")
