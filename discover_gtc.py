#!/usr/bin/env python3
"""Discover GoingToCamp (Washington State Parks + Tacoma Power) campground IDs
by calling the public JSON endpoints directly.

Writes gtc_campgrounds.json: [{rec_area_id, rec_area, facility_id, map_id, name}]
"""
from __future__ import annotations

from pathlib import Path

from campwatch_config import load_provider_rules
from campwatch_http import GoingToCampClient, atomic_write_json

HERE = Path(__file__).parent
OUT = HERE / "gtc_campgrounds.json"

WA_REC_AREAS = load_provider_rules()["going_to_camp"]["rec_areas"]


def main():
    gtc = GoingToCampClient()
    out = []
    for rec_area_id, label in WA_REC_AREAS.items():
        print(f"=== rec-area {rec_area_id}: {label} ===")
        # LIST_CAMPGROUNDS returns resource locations (campgrounds) for the area.
        try:
            resp = gtc.request_json(rec_area_id, "LIST_CAMPGROUNDS")
        except Exception as e:  # noqa: BLE001
            print(f"  list error: {e}")
            continue
        # Also fetch CAMP_DETAILS (maps) to map resource_location_id -> mapId.
        try:
            maps = gtc.request_json(rec_area_id, "CAMP_DETAILS")
        except Exception as e:  # noqa: BLE001
            print(f"  maps error: {e}")
            maps = []
        map_by_loc = {}
        for m in maps if isinstance(maps, list) else []:
            loc = m.get("resourceLocationId")
            if loc is not None:
                map_by_loc[loc] = m.get("mapId")

        items = resp if isinstance(resp, list) else resp.get("resourceLocations", [])
        for f in items:
            loc_id = f.get("resourceLocationId") or f.get("id")
            names = f.get("localizedValues") or []
            name = names[0].get("fullName") if names else f.get("resourceLocationName")
            out.append(
                {
                    "rec_area_id": rec_area_id,
                    "rec_area": label,
                    "facility_id": loc_id,
                    "map_id": map_by_loc.get(loc_id),
                    "name": name,
                    "lat": f.get("latitude"),
                    "lon": f.get("longitude"),
                }
            )
            print(f"  #{loc_id}  map={map_by_loc.get(loc_id)}  {name}")

    atomic_write_json(OUT, out, mode=0o644)
    print(f"\nWrote {len(out)} GoingToCamp campgrounds -> {OUT}")


if __name__ == "__main__":
    main()
