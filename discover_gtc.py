#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["camply"]
# ///
"""Discover GoingToCamp (Washington State Parks + Tacoma Power) campground IDs,
working around camply's buggy _process_facilities_responses by calling the
provider's low-level API directly with its authenticated session.

Writes gtc_campgrounds.json: [{rec_area_id, rec_area, facility_id, map_id, name}]
"""
from __future__ import annotations

import json
from pathlib import Path

from camply.providers.going_to_camp.going_to_camp_provider import (
    ENDPOINTS,
    GoingToCamp,
)

HERE = Path(__file__).parent
OUT = HERE / "gtc_campgrounds.json"

# rec-area IDs from `camply recreation-areas --provider GoingToCamp`
WA_REC_AREAS = {
    3: "Washington State Parks",
    6: "Tacoma Power Parks",
}


def main():
    gtc = GoingToCamp()
    out = []
    for rec_area_id, label in WA_REC_AREAS.items():
        print(f"=== rec-area {rec_area_id}: {label} ===")
        # LIST_CAMPGROUNDS returns resource locations (campgrounds) for the area.
        try:
            resp = gtc._api_request(rec_area_id, "LIST_CAMPGROUNDS")
        except Exception as e:  # noqa: BLE001
            print(f"  list error: {e}")
            continue
        # Also fetch CAMP_DETAILS (maps) to map resource_location_id -> mapId.
        try:
            maps = gtc._api_request(rec_area_id, "CAMP_DETAILS")
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

    OUT.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {len(out)} GoingToCamp campgrounds -> {OUT}")


if __name__ == "__main__":
    main()