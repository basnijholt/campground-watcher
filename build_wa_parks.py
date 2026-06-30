#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["camply"]
# ///
"""Build the FULL list of Washington State Parks (GoingToCamp rec-area 3 +
Tacoma Power rec-area 6) that are within ~2 hours' drive of home, with their
gpsCoordinates and rootMapId.

No paid API. Drive-time is estimated as crow-flies km * ROAD_FACTOR / AVG_KMH.
Borderline parks (within DRIVE_HRS + MARGIN) are kept and flagged so we don't
miss anything near the cutoff. Day-use-only / non-campground facilities are
dropped via a name + resource-category heuristic.

Writes wa_parks.json consumed by watch.py.
"""
from __future__ import annotations

import json
import os
import math
from pathlib import Path

from camply.providers.going_to_camp.going_to_camp_provider import GoingToCamp

HOME_LAT = float(os.environ.get("CAMPWATCH_HOME_LAT", "47.6062"))
HOME_LON = float(os.environ.get("CAMPWATCH_HOME_LON", "-122.3321"))
DRIVE_HRS = 2.0
MARGIN_HRS = 0.25  # keep borderline parks just over the line, flagged
ROAD_FACTOR = 1.35  # crow-flies -> road distance multiplier (PNW mountains)
AVG_KMH = 75.0  # mixed highway/mountain average speed

REC_AREAS = {3: "Washington State Parks", 6: "Tacoma Power Parks"}

# Names that are clearly NOT overnight campgrounds (trails, day-use, offices,
# marine/boat-in handled separately). Heuristic only; availability check is the
# final arbiter (these return zero campsites anyway).
NON_CAMP_MARKERS = (
    "TRAIL", "OFFICE", "REGION", "OBSERVATORY", "OBA", "HERITAGE",
    "INTERPRETIVE", "DAY USE", "DAY-USE", "GEYSER",
    "RETREAT CENTER", "VISITORS CENTER", "VISITOR CENTER", "FRONT DESK",
    "INFORMATION CENTER", "INTERNET", "HQ", "CAMPUS OPERATIONS", "DEPOT",
    "PROPERTY", " IC", "WESTHAVEN", "PALOUSE TO CASCADES",
)

HERE = Path(__file__).parent
OUT = HERE / "wa_parks.json"


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_gps(s):
    try:
        lat, lon = (float(x.strip()) for x in s.split(","))
        return lat, lon
    except Exception:  # noqa: BLE001
        return None, None


def main():
    g = GoingToCamp()
    kept, dropped_far, dropped_noncamp, no_coords = [], [], [], []
    for rec_area_id, label in REC_AREAS.items():
        resp = g._api_request(rec_area_id, "LIST_CAMPGROUNDS")
        for f in resp:
            loc = f.get("resourceLocationId")
            lv = (f.get("localizedValues") or [{}])[0]
            name = lv.get("fullName") or lv.get("shortName") or str(loc)
            root_map = f.get("rootMapId")
            lat, lon = parse_gps(f.get("gpsCoordinates") or "")
            up = name.upper()
            if any(m in up for m in NON_CAMP_MARKERS):
                dropped_noncamp.append(name)
                continue
            if root_map is None:
                # No reservable campsite map = day-use / trailhead / boat-in
                # non-campground. Drop (these have nothing to watch).
                dropped_noncamp.append(name + " [no map]")
                continue
            if lat is None:
                # No GPS = admin/non-campground facility in practice. Drop.
                no_coords.append(name)
                continue
            crow = haversine_km(HOME_LAT, HOME_LON, lat, lon)
            est_hrs = crow * ROAD_FACTOR / AVG_KMH
            if est_hrs > DRIVE_HRS + MARGIN_HRS:
                dropped_far.append((name, round(est_hrs, 2)))
                continue
            kept.append(
                {
                    "facility_id": loc,
                    "rec_area_id": rec_area_id,
                    "rec_area": label,
                    "name": name,
                    "root_map_id": root_map,
                    "lat": lat,
                    "lon": lon,
                    "crow_km": round(crow, 1),
                    "est_drive_hrs": round(est_hrs, 2),
                    "borderline": est_hrs > DRIVE_HRS,
                }
            )

    kept.sort(key=lambda c: (c["est_drive_hrs"] is None, c["est_drive_hrs"] or 0))
    OUT.write_text(json.dumps(kept, indent=2))
    print(f"KEPT {len(kept)} WA parks within ~{DRIVE_HRS}h (+{MARGIN_HRS}h margin)")
    print(f"  dropped (too far): {len(dropped_far)}")
    print(f"  dropped (non-campground): {len(dropped_noncamp)}")
    print(f"  kept w/o coords (flagged borderline): {len(no_coords)}")
    print()
    for c in kept:
        h = f"{c['est_drive_hrs']}h" if c["est_drive_hrs"] is not None else "  ?  "
        flag = " *borderline" if c["borderline"] else ""
        print(f"  {h:>6}  {c['name']}{flag}")


if __name__ == "__main__":
    main()