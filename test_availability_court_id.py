"""
test_availability_court_id.py -- both availability builders must carry
court_id.

The frontend identifies a court by id and falls back to the name when no id
is present. /api/live_courts (dates 14+ days out) included court_id;
_get_pbp_availability (dates under 14 days, i.e. most real traffic) computed
the id, used it as a dict key, and dropped it. Live logs showed exactly that
split: facility 1783 resolved by id on a far date while The Rally, Melbourne
Pickle Club and The Jar HQ resolved by NAME on near ones.

Run: python3 test_availability_court_id.py
"""
import re, sys

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"  {detail}"))
    if not cond: failures.append(label)

src = open("api_server.py").read()
code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))

print("\n-- every court_blocks entry carries an id --")
appends = re.findall(r'court_blocks"\]\.append\(\{(.*?)\}\)', code, re.S)
check("both append sites found", len(appends) == 2, len(appends))
for i, a in enumerate(appends):
    check(f"append site {i+1} includes court_id", '"court_id"' in a, a[:120])
    check(f"append site {i+1} includes the name too", '"court"' in a)

print("\n-- the id is taken from the key, not recomputed --")
check("the court key is unpacked into id and name",
      "ccid, cname = court_key.split" in code)
check("the key is still built from id and name",
      'key = f"{cid}|{cname}"' in code)

print("\n-- key round-trip --")
# The key is f"{cid}|{cname}"; names can contain '|' (e.g. 'The Jar | South
# Melbourne' style), so the split must be bounded to the first separator.
for cid, cname in (("16090", "Court 3"),
                   ("18395", "Court 3 - Showcourt"),
                   ("13498", "Court 3"),
                   ("999", "Court | Odd | Name")):
    key = f"{cid}|{cname}"
    got_id, got_name = key.split("|", 1)
    check(f"{cid} / {cname!r} round-trips", (got_id, got_name) == (cid, cname),
          (got_id, got_name))

print("\n-- live_courts still carries it --")
lc = code[code.index("async def live_courts"):]
check("live_courts blocks include court_id", '"court_id": court_id' in lc)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}"); sys.exit(1)
print("all checks passed")
