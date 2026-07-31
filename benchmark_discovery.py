#!/usr/bin/env python3
"""Compare server- and client-side discovery through the production code path."""
from __future__ import annotations

import argparse
import json
import time

import build_candidates
import watch
from campwatch_config import home_coordinates, local_environment
from campwatch_http import RecreationGovClient


def run_pipeline(
    client,
    distance_filter: str,
    *,
    home_lat: float,
    home_lon: float,
    max_distance_km: float,
):
    began = time.perf_counter()
    rows, calls = build_candidates.fetch_catalog(
        client,
        distance_filter=distance_filter,
        home_lat=home_lat,
        home_lon=home_lon,
        max_distance_km=max_distance_km,
    )
    candidates = build_candidates.select_candidates(
        rows,
        home_lat=home_lat,
        home_lon=home_lon,
        max_distance_km=max_distance_km,
    )
    elapsed = time.perf_counter() - began
    scan_candidates = [
        item for item in candidates if not watch.is_group(item.get("name", ""))
    ]
    scan_ids = {int(item["id"]) for item in scan_candidates}
    details = {
        int(item["id"]): {
            "name": item["name"],
            "distance_km": item["distance_km"],
            "distance_mi": item["dist_mi"],
        }
        for item in scan_candidates
    }
    return {
        "http_calls": calls,
        "elapsed_seconds": round(elapsed, 3),
        "api_rows": len(rows),
        "candidate_rows": len(candidates),
        "scan_targets": len(scan_ids),
        "scan_ids": scan_ids,
        "scan_details": details,
    }


def server_pipeline(client, **options):
    return run_pipeline(client, "server", **options)


def client_pipeline(client, **options):
    return run_pipeline(client, "client", **options)


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-distance-km",
        type=build_candidates._positive_distance,
        default=build_candidates.DEFAULT_MAX_DISTANCE_KM,
    )
    args = parser.parse_args(argv)
    home_lat, home_lon = home_coordinates(local_environment())
    options = {
        "home_lat": home_lat,
        "home_lon": home_lon,
        "max_distance_km": args.max_distance_km,
    }
    client = RecreationGovClient()
    server = server_pipeline(client, **options)
    local = client_pipeline(client, **options)
    server_ids = server.pop("scan_ids")
    local_ids = local.pop("scan_ids")
    server.pop("scan_details")
    local.pop("scan_details")
    result = {
        "coordinates": [home_lat, home_lon],
        "max_distance_km": args.max_distance_km,
        "server_side": server,
        "client_side": local,
        "overlap": len(server_ids & local_ids),
        "added_by_client": len(local_ids - server_ids),
        "omitted_by_client": len(server_ids - local_ids),
        "identical_scan_targets": server_ids == local_ids,
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
