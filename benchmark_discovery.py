#!/usr/bin/env python3
"""Compare server- and client-side discovery through the production code path."""
from __future__ import annotations

import json
import time

import build_candidates
import watch
from campwatch_http import RecreationGovClient


# Use the original public repository coordinates for a reproducible experiment.
LAT = 47.6062
LON = -122.3321
MAX_DISTANCE_KM = 90.0


def run_pipeline(client, distance_filter: str):
    began = time.perf_counter()
    rows, calls = build_candidates.fetch_catalog(
        client,
        distance_filter=distance_filter,
        home_lat=LAT,
        home_lon=LON,
        max_distance_km=MAX_DISTANCE_KM,
    )
    candidates = build_candidates.select_candidates(
        rows,
        home_lat=LAT,
        home_lon=LON,
        max_distance_km=MAX_DISTANCE_KM,
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


def server_pipeline(client):
    return run_pipeline(client, "server")


def client_pipeline(client):
    return run_pipeline(client, "client")


def main():
    client = RecreationGovClient()
    server = server_pipeline(client)
    local = client_pipeline(client)
    server_ids = server.pop("scan_ids")
    local_ids = local.pop("scan_ids")
    server.pop("scan_details")
    local.pop("scan_details")
    result = {
        "coordinates": [LAT, LON],
        "max_distance_km": MAX_DISTANCE_KM,
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
