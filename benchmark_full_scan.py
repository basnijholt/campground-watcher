#!/usr/bin/env python3
"""Measure complete availability workload for the controlled discovery A/B.

The Washington phase is identical for both federal discovery modes, so it is
measured once and included in both totals.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time

import benchmark_discovery
import watch
from campwatch_config import home_coordinates, local_environment
from campwatch_http import GoingToCampClient, RecreationGovClient


def measured_call(function, *args, **kwargs):
    began = time.perf_counter()
    try:
        result = function(*args, **kwargs)
        return time.perf_counter() - began, result, None
    except Exception as exc:  # noqa: BLE001
        return time.perf_counter() - began, None, type(exc).__name__


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-distance-km",
        type=benchmark_discovery.build_candidates._positive_distance,
        default=benchmark_discovery.build_candidates.DEFAULT_MAX_DISTANCE_KM,
    )
    args = parser.parse_args(argv)
    home_lat, home_lon = home_coordinates(local_environment())
    discovery_options = {
        "home_lat": home_lat,
        "home_lon": home_lon,
        "max_distance_km": args.max_distance_km,
    }
    start = dt.date.today()
    end = start + dt.timedelta(days=watch.WINDOW_DAYS)

    discovery_client = RecreationGovClient()
    server_discovery = benchmark_discovery.server_pipeline(
        discovery_client, **discovery_options
    )
    client_discovery = benchmark_discovery.client_pipeline(
        discovery_client, **discovery_options
    )
    server_ids = server_discovery["scan_ids"]
    client_ids = client_discovery["scan_ids"]
    union_ids = sorted(server_ids | client_ids)

    print(
        f"Federal availability: {len(union_ids)} unique targets, "
        f"window {start}..{end}",
        flush=True,
    )
    federal_client = RecreationGovClient()
    federal_times = {}
    federal_errors = {}
    federal_available = {}
    for index, campground_id in enumerate(union_ids, 1):
        elapsed, result, error = measured_call(
            watch.recgov_available_nights,
            federal_client,
            campground_id,
            start,
            end,
        )
        federal_times[campground_id] = elapsed
        federal_errors[campground_id] = error
        federal_available[campground_id] = bool(result)
        print(
            f"  federal {index}/{len(union_ids)} #{campground_id}: "
            f"{elapsed:.3f}s {error or 'ok'}",
            flush=True,
        )

    current_config = json.loads(watch.CONFIG.read_text())
    wa_targets = current_config["going_to_camp"]
    print(f"Washington availability: {len(wa_targets)} shared targets", flush=True)
    watch._booking_categories_cache.clear()
    watch._sub_equipment_cache.clear()
    gtc_client = GoingToCampClient()
    wa_began = time.perf_counter()
    wa_errors = 0
    wa_available = 0
    for index, campground in enumerate(wa_targets, 1):
        elapsed, result, error = measured_call(
            watch.gtc_available_nights,
            campground["rec_area"],
            campground["id"],
            start,
            end,
            root_map_id=campground.get("root_map_id"),
            client=gtc_client,
        )
        wa_errors += error is not None
        wa_available += bool(result)
        print(
            f"  Washington {index}/{len(wa_targets)}: "
            f"{elapsed:.3f}s {error or 'ok'}",
            flush=True,
        )
    wa_elapsed = time.perf_counter() - wa_began

    def federal_summary(ids):
        return {
            "targets": len(ids),
            "elapsed_seconds": round(sum(federal_times[item] for item in ids), 3),
            "errors": sum(federal_errors[item] is not None for item in ids),
            "with_availability": sum(federal_available[item] for item in ids),
        }

    server_federal = federal_summary(server_ids)
    client_federal = federal_summary(client_ids)
    server_total = server_federal["elapsed_seconds"] + wa_elapsed
    client_total = client_federal["elapsed_seconds"] + wa_elapsed
    result = {
        "window": [start.isoformat(), end.isoformat()],
        "server_side_federal": server_federal,
        "client_side_federal": client_federal,
        "shared_washington": {
            "targets": len(wa_targets),
            "elapsed_seconds": round(wa_elapsed, 3),
            "errors": wa_errors,
            "with_availability": wa_available,
        },
        "server_full_availability_seconds": round(server_total, 3),
        "client_full_availability_seconds": round(client_total, 3),
        "difference_seconds": round(client_total - server_total, 3),
        "client_over_server_ratio": round(client_total / server_total, 6),
        "discovery_seconds": {
            "server": server_discovery["elapsed_seconds"],
            "client": client_discovery["elapsed_seconds"],
        },
    }
    print("RESULT " + json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
