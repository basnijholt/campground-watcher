#!/usr/bin/env python3
"""Show campgrounds with a run covering specific nights (default: next Fri+Sat)."""
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
state = json.loads((HERE / "last_state.json").read_text())
cfg = json.loads((HERE / "watch_config.json").read_text())

# target nights: args = list of YYYY-MM-DD, else next Fri+Sat
if len(sys.argv) > 1:
    want = [dt.date.fromisoformat(a) for a in sys.argv[1:]]
else:
    today = dt.date.today()
    fri = today + dt.timedelta((4 - today.weekday()) % 7)
    if fri == today:
        fri += dt.timedelta(7)
    want = [fri, fri + dt.timedelta(1)]  # Fri + Sat nights

want_set = set(want)
print(f"Looking for sites open ALL of: {[d.isoformat() for d in want]}\n")

look = {}
for c in cfg["recdotgov"]:
    look["rg:" + str(c["id"])] = (c["name"], c.get("dist_mi"), c.get("rating"), True)
for c in cfg["going_to_camp"]:
    look["wa:" + str(c["id"])] = (c["name"], c.get("est_drive_hrs"), None, False)

hits = []
for key, runs in state.items():
    if not runs:
        continue
    name, dist, rating, isrg = look.get(key, (key, None, None, key.startswith("rg:")))
    sites_ok = []
    for r in runs:
        site, start_s, nights_s = r.split("|")
        start = dt.date.fromisoformat(start_s)
        nights = int(nights_s)
        covered = {start + dt.timedelta(i) for i in range(nights)}
        if want_set <= covered:
            sites_ok.append(site)
    if sites_ok:
        hits.append((dist if dist is not None else 99, name, isrg, rating, len(sites_ok)))

hits.sort()
if not hits:
    print("No sites cover that whole weekend.")
else:
    print(f"{len(hits)} campgrounds with sites open that weekend:\n")
    for d, name, isrg, rating, n in hits:
        tag = "rg" if isrg else "wa"
        dd = (f"{d}mi" if isrg else f"{d}h") if d != 99 else "?"
        rt = f" {rating:.1f}*" if rating else ""
        print(f"[{tag}] {name[:32]:32}{rt:6} {dd:>7}  {n} site(s)")
