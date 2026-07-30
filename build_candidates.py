#!/usr/bin/env python3
"""Build a curated list of recreation.gov campgrounds within a local radius.

Strategy:
- Fetch metadata using either a statewide query (private default) or the
  provider's faster server-side proximity query.
- Apply one common local validation and kilometer-distance pipeline either way.
- Keep reservable overnight campgrounds rated >= 4 stars (or unrated).

Writes candidates.json.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Callable

from campwatch_config import local_environment
from campwatch_http import RecreationGovClient, atomic_write_json

LOCAL_ENV = local_environment()
HOME_LAT = float(LOCAL_ENV.get("CAMPWATCH_HOME_LAT", "47.6062"))
HOME_LON = float(LOCAL_ENV.get("CAMPWATCH_HOME_LON", "-122.3321"))
DEFAULT_MAX_DISTANCE_KM = 90.0
KM_PER_MILE = 1.609344
MIN_RATING = 4.0
STATE = "WA"
STATE_NAME = "Washington"

HERE = Path(__file__).parent
OUT = HERE / "candidates.json"

HTTP = RecreationGovClient()


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_catalog(
    client: RecreationGovClient,
    *,
    distance_filter: str,
    home_lat: float,
    home_lon: float,
    max_distance_km: float,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[list[dict], int]:
    """Fetch source rows; only this query shape differs between the two modes."""
    if distance_filter == "client":
        base_params = {"q": STATE_NAME, "sort": "name"}
    elif distance_filter == "server":
        # recreation.gov interprets both radius and returned distance as km.
        base_params = {
            "q": "",
            "entity_type": "campground",
            "lat": home_lat,
            "lng": home_lon,
            "radius": max_distance_km,
            "sort": "distance",
        }
    else:
        raise ValueError("distance_filter must be 'client' or 'server'")

    results = []
    calls = 0
    start = 0
    size = 50
    while True:
        params = dict(base_params, size=size, start=start)
        data = client.search(params)
        calls += 1
        batch = data.get("results", [])
        if not batch:
            break
        results.extend(batch)
        total = int(data.get("total", 0) or 0)
        start += size
        if start >= total or len(batch) < size:
            break
        sleeper(0.4)
    return results, calls


def select_candidates(
    rows: list[dict],
    *,
    home_lat: float,
    home_lon: float,
    max_distance_km: float,
) -> list[dict]:
    """Apply the exact same local rules to server- or client-fetched rows."""
    candidates = []
    seen = set()
    for r in rows:
        if (
            r.get("entity_type") != "campground"
            or r.get("state_code") != STATE_NAME
        ):
            continue
        eid = r.get("entity_id") or r.get("id")
        if eid is None or eid in seen:
            continue
        seen.add(eid)
        try:
            lat = float(r.get("latitude"))
            lon = float(r.get("longitude"))
        except (TypeError, ValueError):
            lat = lon = None
        if lat is None or lon is None:
            continue
        distance_km = haversine_km(home_lat, home_lon, lat, lon)
        if distance_km > max_distance_km:
            continue
        try:
            campsite_count = int(r.get("campsites_count") or 0)
        except (TypeError, ValueError):
            campsite_count = 0
        if (
            r.get("reservable") is not True
            or campsite_count <= 0
            or "Overnight" not in (r.get("campsite_type_of_use") or [])
        ):
            continue
        rating = r.get("average_rating")
        try:
            rating = float(rating) if rating not in (None, "") else None
        except (TypeError, ValueError):
            rating = None
        if rating is not None and rating < MIN_RATING:
            continue
        candidates.append(
            {
                "id": eid,
                "name": r.get("name"),
                "lat": lat,
                "lon": lon,
                "distance_km": round(distance_km, 1),
                "dist_mi": round(distance_km / KM_PER_MILE, 1),
                "rating": rating,
                "num_ratings": r.get("number_of_ratings"),
                "parent": r.get("parent_name"),
                "reservable": r.get("reservable"),
                "campsites_count": campsite_count,
            }
        )
    candidates.sort(key=lambda c: (c["distance_km"], str(c["name"])))
    return candidates


def _positive_distance(value: str) -> float:
    distance = float(value)
    if not 0 < distance <= 1000:
        raise argparse.ArgumentTypeError("distance must be between 0 and 1000 km")
    return distance


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--distance-filter",
        choices=("client", "server"),
        default="client",
        help=(
            "client keeps coordinates local (default); server sends coordinates "
            "to recreation.gov for a faster proximity query"
        ),
    )
    parser.add_argument(
        "--max-distance-km",
        type=_positive_distance,
        default=DEFAULT_MAX_DISTANCE_KM,
        help=f"straight-line cutoff in kilometers (default: {DEFAULT_MAX_DISTANCE_KM:g})",
    )
    args = parser.parse_args(argv)

    if args.distance_filter == "server":
        print("Server filtering selected: sending coordinates to recreation.gov.")
    else:
        print("Client filtering selected: coordinates remain local.")
    print(
        f"Fetching {STATE} campground metadata; cutoff "
        f"{args.max_distance_km:g} km ({args.max_distance_km / KM_PER_MILE:.1f} mi)..."
    )
    raw, calls = fetch_catalog(
        HTTP,
        distance_filter=args.distance_filter,
        home_lat=HOME_LAT,
        home_lon=HOME_LON,
        max_distance_km=args.max_distance_km,
    )
    candidates = select_candidates(
        raw,
        home_lat=HOME_LAT,
        home_lon=HOME_LON,
        max_distance_km=args.max_distance_km,
    )
    atomic_write_json(OUT, candidates, mode=0o644)
    print(
        f"  fetched {len(raw)} rows in {calls} request(s); "
        f"wrote {len(candidates)} candidates -> {OUT}"
    )
    for c in candidates:
        rt = f"{c['rating']}*({c['num_ratings']})" if c["rating"] is not None else "unrated"
        distance = f"{c['distance_km']}km/{c['dist_mi']}mi"
        print(f"  #{str(c['id']):>8}  {distance:>15}  {rt:>10}  {c['name']}")


if __name__ == "__main__":
    sys.exit(main())
