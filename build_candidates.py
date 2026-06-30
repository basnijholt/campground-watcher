#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///
"""Build a curated list of recreation.gov campgrounds within ~2hr drive of home,
rated >= 4 stars. Writes candidates.json.

Strategy:
- Pull recreation.gov campground facilities in WA (FacilityTypeDescription=Campground).
- Keep those within a straight-line radius (proxy for ~2hr drive; configurable).
- Fetch each facility's average rating via the recreation.gov ratings endpoint.
- Keep rating >= MIN_RATING.
"""
from __future__ import annotations

import json
import os
import math
import sys
import time
from pathlib import Path

import requests

HOME_LAT = float(os.environ.get("CAMPWATCH_HOME_LAT", "47.6062"))
HOME_LON = float(os.environ.get("CAMPWATCH_HOME_LON", "-122.3321"))
# Straight-line km as a proxy for ~2hr drive. ~2hr at mixed mountain/highway
# speeds ~ 120-160 km road distance -> use ~130 km crow-flies as a generous-but-
# sane cutoff; drive time is verified later for finalists.
MAX_CROW_KM = 130.0
MIN_RATING = 4.0
STATE = "WA"

API = "https://www.recreation.gov/api/search"
RIDB_FACILITY = "https://www.recreation.gov/api/search/recommendationcontent"
HERE = Path(__file__).parent
OUT = HERE / "candidates.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (campground-watcher; personal use)",
    "Accept": "application/json",
}


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


MAX_RADIUS_MI = 90  # recreation.gov search radius (miles); crow-flies cutoff applied after


def fetch_campgrounds():
    """Page through recreation.gov search near home, keep only campgrounds."""
    results = []
    start = 0
    size = 50
    while True:
        params = {
            "q": "",
            "entity_type": "campground",
            "size": size,
            "start": start,
            "lat": HOME_LAT,
            "lng": HOME_LON,
            "radius": MAX_RADIUS_MI,
            "sort": "distance",
        }
        resp = requests.get(API, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("results", [])
        if not batch:
            break
        # keep only actual campgrounds (search returns tours/ticketfacilities too)
        results.extend([r for r in batch if r.get("entity_type") == "campground"])
        total = int(data.get("total", 0) or 0)
        start += size
        if start >= total or len(batch) < size:
            break
        time.sleep(0.4)
    return results


def main():
    print(f"Fetching {STATE} campgrounds near home ({HOME_LAT},{HOME_LON})...")
    raw = fetch_campgrounds()
    print(f"  got {len(raw)} raw results")
    candidates = []
    seen = set()
    for r in raw:
        eid = r.get("entity_id") or r.get("id")
        if eid in seen:
            continue
        seen.add(eid)
        try:
            lat = float(r.get("latitude"))
            lon = float(r.get("longitude"))
        except (TypeError, ValueError):
            lat = lon = None
        try:
            dist_mi = float(r.get("distance"))
        except (TypeError, ValueError):
            dist_mi = haversine_km(HOME_LAT, HOME_LON, lat, lon) / 1.609 if lat else None
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
                "dist_mi": round(dist_mi, 1) if dist_mi is not None else None,
                "rating": rating,
                "num_ratings": r.get("number_of_ratings"),
                "parent": r.get("parent_name"),
                "reservable": r.get("reservable"),
            }
        )
    candidates.sort(key=lambda c: (c["dist_mi"] if c["dist_mi"] is not None else 9999))
    OUT.write_text(json.dumps(candidates, indent=2))
    print(f"Wrote {len(candidates)} candidates (rating>={MIN_RATING} or unrated) -> {OUT}")
    for c in candidates:
        rt = f"{c['rating']}*({c['num_ratings']})" if c["rating"] is not None else "unrated"
        dm = f"{c['dist_mi']}mi" if c["dist_mi"] is not None else "?mi"
        print(f"  #{str(c['id']):>8}  {dm:>7}  {rt:>10}  {c['name']}")


if __name__ == "__main__":
    sys.exit(main())