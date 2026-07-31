#!/usr/bin/env python3
"""Build the FULL list of Washington State Parks (GoingToCamp rec-area 3 +
Tacoma Power rec-area 6) that are within ~2 hours' drive of home, with their
gpsCoordinates and rootMapId.

By default, drive time is estimated as crow-flies km * ROAD_FACTOR / AVG_KMH.
``--distance-filter drive`` adds an explicit OpenStreetMap OSRM route distance
and traffic-free route duration for every retained park. Borderline parks
(within DRIVE_HRS + MARGIN) are kept and flagged so we don't miss anything near
the cutoff. Day-use-only / non-campground facilities are dropped via a name +
resource-category heuristic.

Writes wa_parks.json consumed by watch.py.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from campwatch_config import home_coordinates, load_provider_rules, local_environment
from campwatch_http import GoingToCampClient, OsmDrivingRouter, atomic_write_json

RULES = load_provider_rules()
GOING_TO_CAMP_RULES = RULES["going_to_camp"]
DRIVE_RULES = GOING_TO_CAMP_RULES["estimated_drive"]
DRIVE_HRS = DRIVE_RULES["max_hours"]
MARGIN_HRS = DRIVE_RULES["margin_hours"]
ROAD_FACTOR = DRIVE_RULES["road_factor"]
AVG_KMH = DRIVE_RULES["average_kmh"]
REC_AREAS = GOING_TO_CAMP_RULES["rec_areas"]
NON_CAMP_MARKERS = GOING_TO_CAMP_RULES["non_camp_markers"]

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


def attach_osm_route_times(
    parks: list[dict], router: OsmDrivingRouter, *, home_lat: float, home_lon: float
) -> tuple[list[dict], int]:
    """Add OSM road duration, retaining parks that have no drivable route."""
    routes = router.driving_routes(
        home_lat, home_lon, [(park["lat"], park["lon"]) for park in parks]
    )
    if len(routes) != len(parks):
        raise RuntimeError("routing response did not match the park list")
    unavailable = 0
    for park, (distance_km, duration_seconds) in zip(parks, routes):
        if distance_km is None or duration_seconds is None:
            # Keep the campground/watch target intact. The map can explain that
            # OSRM did not return a drivable route instead of silently hiding it.
            park["osrm_route_unavailable"] = True
            unavailable += 1
            continue
        park["distance_km"] = round(distance_km, 1)
        park["dist_mi"] = round(distance_km / 1.609344, 1)
        park["osrm_duration_seconds"] = round(duration_seconds)
        park["drive_time_source"] = "openstreetmap_osrm"
    return parks, unavailable


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--distance-filter",
        choices=("client", "drive"),
        default="client",
        help=(
            "client keeps the local heuristic (default); drive sends coordinates "
            "to OpenStreetMap OSRM for route distance and duration"
        ),
    )
    args = parser.parse_args(argv)
    home_lat, home_lon = home_coordinates(local_environment())
    g = GoingToCampClient()
    kept, dropped_far, dropped_noncamp, no_coords = [], [], [], []
    for rec_area_id, label in REC_AREAS.items():
        resp = g.request_json(rec_area_id, "LIST_CAMPGROUNDS")
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
            crow = haversine_km(home_lat, home_lon, lat, lon)
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

    dropped_unrouteable = 0
    if args.distance_filter == "drive":
        kept, dropped_unrouteable = attach_osm_route_times(
            kept, OsmDrivingRouter(), home_lat=home_lat, home_lon=home_lon
        )
        kept.sort(
            key=lambda c: (
                c.get("osrm_duration_seconds") is None,
                c.get("osrm_duration_seconds") or float("inf"),
            )
        )
    else:
        kept.sort(key=lambda c: (c["est_drive_hrs"] is None, c["est_drive_hrs"] or 0))
    atomic_write_json(OUT, kept, mode=0o644)
    print(f"KEPT {len(kept)} WA parks within ~{DRIVE_HRS}h (+{MARGIN_HRS}h margin)")
    print(f"  dropped (too far): {len(dropped_far)}")
    print(f"  dropped (non-campground): {len(dropped_noncamp)}")
    print(f"  dropped (no coordinates): {len(no_coords)}")
    if args.distance_filter == "drive":
        print(f"  no OSM road route (retained and marked): {dropped_unrouteable}")
    print()
    for c in kept:
        if args.distance_filter == "drive" and c.get("osrm_duration_seconds") is not None:
            h = f"{c['osrm_duration_seconds'] / 3600:.2f}h OSM"
        elif args.distance_filter == "drive":
            h = "no OSM route"
        else:
            h = f"{c['est_drive_hrs']}h" if c["est_drive_hrs"] is not None else "  ?  "
        flag = " *borderline" if c["borderline"] else ""
        print(f"  {h:>6}  {c['name']}{flag}")


if __name__ == "__main__":
    main()
