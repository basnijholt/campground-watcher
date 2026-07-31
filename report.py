#!/usr/bin/env python3
"""Print campgrounds with recorded availability from last_state.json, ranked by distance."""
import json
from pathlib import Path

from campwatch_config import load_provider_rules

HERE = Path(__file__).parent
state = json.loads((HERE / "last_state.json").read_text())
cfg = json.loads((HERE / "watch_config.json").read_text())
stay_limits = load_provider_rules()["going_to_camp"]["stay_limits"]
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
    failed_keys = progress.get("failed_keys")
    if isinstance(failed_keys, list) and failed_keys:
        print(
            f"NOTE: the latest scan could not check {len(failed_keys)} campground(s); "
            "their saved observations may be old and current availability is unknown.\n"
        )

look = {}
for c in cfg["recdotgov"]:
    look["rg:" + str(c["id"])] = (
        c["name"], c.get("dist_mi"), c.get("rating"), True, None
    )
for c in cfg["going_to_camp"]:
    stay_limit = stay_limits.get(int(c["rec_area"]))
    look["wa:" + str(c["id"])] = (
        c["name"],
        c.get("est_drive_hrs"),
        None,
        False,
        (stay_limit or {}).get("max_nights"),
    )

rows = []
for key, runs in state.items():
    if not runs:
        continue
    name, dist, rating, isrg, max_stay_nights = look.get(
        key, (key, None, None, key.startswith("rg:"), None)
    )
    starts = sorted({r.split("|")[1] for r in runs})
    max_observed_nights = max(int(r.split("|")[2]) for r in runs)
    nsites = len({r.split("|")[0] for r in runs})
    rows.append((
        dist if dist is not None else 99,
        name,
        isrg,
        rating,
        nsites,
        max_observed_nights,
        max_stay_nights,
        starts[0],
        starts[-1],
    ))

rows.sort()
print(f"{len(rows)} campgrounds with recorded availability in the next 90 days\n")
hdr = f"{'NAME':34} {'DIST':>7} {'SITES':>5} {'OBS MAX':>7} {'KNOWN CAP':>9}  EARLIEST..LATEST"
print(hdr)
print("-" * len(hdr))
for d, name, isrg, rating, nsites, max_observed, known_cap, e, l in rows:
    tag = "rg" if isrg else "wa"
    dd = (f"{d}mi" if isrg else f"{d}h") if d != 99 else "?"
    rt = f" {rating:.1f}*" if rating else ""
    cap = str(known_cap) if known_cap else "unknown"
    print(f"[{tag}] {name[:30]:30}{rt:6} {dd:>7} {nsites:>5} {max_observed:>7} {cap:>9}  {e}..{l}")
