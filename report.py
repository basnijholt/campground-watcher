#!/usr/bin/env python3
"""Print currently-bookable campgrounds from last_state.json, ranked by distance."""
import json
from pathlib import Path

HERE = Path(__file__).parent
state = json.loads((HERE / "last_state.json").read_text())
cfg = json.loads((HERE / "watch_config.json").read_text())
progress_path = HERE / "scan_progress.json"

if progress_path.exists():
    try:
        progress = json.loads(progress_path.read_text())
    except (OSError, json.JSONDecodeError):
        progress = {}
    if progress.get("status") in {"running", "failed"}:
        completed = progress.get("completed", "?")
        total = progress.get("total", "?")
        status = "is in progress" if progress.get("status") == "running" else "stopped early"
        print(
            f"NOTE: the latest scan {status} ({completed}/{total}); "
            "unprocessed campgrounds show their previous results.\n"
        )

look = {}
for c in cfg["recdotgov"]:
    look["rg:" + str(c["id"])] = (c["name"], c.get("dist_mi"), c.get("rating"), True)
for c in cfg["going_to_camp"]:
    look["wa:" + str(c["id"])] = (c["name"], c.get("est_drive_hrs"), None, False)

rows = []
for key, runs in state.items():
    if not runs:
        continue
    name, dist, rating, isrg = look.get(key, (key, None, None, key.startswith("rg:")))
    starts = sorted({r.split("|")[1] for r in runs})
    maxn = max(int(r.split("|")[2]) for r in runs)
    nsites = len({r.split("|")[0] for r in runs})
    rows.append((dist if dist is not None else 99, name, isrg, rating, nsites, maxn, starts[0], starts[-1]))

rows.sort()
print(f"{len(rows)} campgrounds bookable in the next 90 days\n")
hdr = f"{'NAME':34} {'DIST':>7} {'SITES':>5} {'MAXNT':>5}  EARLIEST..LATEST"
print(hdr)
print("-" * len(hdr))
for d, name, isrg, rating, nsites, maxn, e, l in rows:
    tag = "rg" if isrg else "wa"
    dd = (f"{d}mi" if isrg else f"{d}h") if d != 99 else "?"
    rt = f" {rating:.1f}*" if rating else ""
    print(f"[{tag}] {name[:30]:30}{rt:6} {dd:>7} {nsites:>5} {maxn:>5}  {e}..{l}")
