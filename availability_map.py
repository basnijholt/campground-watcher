#!/usr/bin/env python3
"""Serve a live, dependency-free map of campgrounds with availability.

The server binds only to 127.0.0.1. Its HTML uses small first-party JavaScript
and loads visible raster tiles directly from OpenStreetMap; no mapping SDK or
third-party Python package is installed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from campwatch_http import GTC_HOSTS


HERE = Path(__file__).resolve().parent
CONFIG = HERE / "watch_config.json"
STATE = HERE / "last_state.json"
PROGRESS = HERE / "scan_progress.json"
CANDIDATES = HERE / "candidates.json"
WA_PARKS = HERE / "wa_parks.json"
MAP_HTML = (HERE / "availability_map.html").read_text(encoding="utf-8")
MAP_JS = (HERE / "availability_map.js").read_text(encoding="utf-8")

# The map only watches its local, atomically-written source files while a
# browser has an event-stream connection open.  This avoids a background timer
# repeatedly transferring the full availability payload to every open map tab.
EVENT_POLL_SECONDS = 0.25
EVENT_DEBOUNCE_SECONDS = 0.10
EVENT_HEARTBEAT_SECONDS = 15.0
EVENT_RETRY_MILLISECONDS = 2_000


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _file_revision(path: Path) -> tuple[int, int, int] | None:
    """Return a cheap change token without reading a watched file."""
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


def _source_revisions() -> tuple[tuple[int, int, int] | None, ...]:
    """Tokenize every local input that could alter map data."""
    return tuple(
        _file_revision(path)
        for path in (CONFIG, STATE, PROGRESS, CANDIDATES, WA_PARKS)
    )


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _coordinates(latitude, longitude) -> tuple[float, float] | None:
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return lat, lon


def _parse_runs(values) -> list[dict]:
    runs = []
    for value in values if isinstance(values, list) else []:
        try:
            site, start_text, nights_text = str(value).rsplit("|", 2)
            start = dt.date.fromisoformat(start_text)
            nights = int(nights_text)
            if nights <= 0:
                continue
        except (TypeError, ValueError):
            continue
        runs.append(
            {
                "site": site,
                "start": start.isoformat(),
                "nights": nights,
                "last_night": (start + dt.timedelta(days=nights - 1)).isoformat(),
            }
        )
    return sorted(runs, key=lambda run: (run["start"], run["site"]))


def _gtc_booking_url(campground: dict, run: dict) -> str:
    rec_area = int(campground["rec_area"])
    domain = GTC_HOSTS[rec_area]
    start = dt.date.fromisoformat(run["start"])
    end = start + dt.timedelta(days=run["nights"])
    params = {
        "mapId": campground.get("root_map_id"),
        "bookingCategoryId": 0,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "isReserving": "true",
        "equipmentId": -32768,
        "subEquipmentId": -32768,
        "partySize": 1,
        "resourceLocationId": campground["id"],
    }
    return f"https://{domain}/create-booking/results?{urllib.parse.urlencode(params)}"


def _location_record(
    *,
    key: str,
    campground: dict,
    metadata: dict,
    provider: str,
    coordinates: tuple[float, float],
    runs: list[dict],
    booking_url: str,
) -> dict:
    sites = {run["site"] for run in runs}
    osrm_duration_seconds = campground.get("osrm_duration_seconds")
    if osrm_duration_seconds is None:
        osrm_duration_seconds = metadata.get("osrm_duration_seconds")
    osrm_route_unavailable = campground.get("osrm_route_unavailable")
    if osrm_route_unavailable is None:
        osrm_route_unavailable = metadata.get("osrm_route_unavailable")
    return {
        "key": key,
        "name": campground.get("name") or metadata.get("name") or key,
        "provider": provider,
        "lat": coordinates[0],
        "lon": coordinates[1],
        "rating": campground.get("rating"),
        "distance_km": campground.get("distance_km") or metadata.get("distance_km"),
        "distance_mi": campground.get("dist_mi") or metadata.get("dist_mi"),
        "est_drive_hrs": campground.get("est_drive_hrs") or metadata.get("est_drive_hrs"),
        "osrm_duration_seconds": osrm_duration_seconds,
        "osrm_route_unavailable": osrm_route_unavailable is True,
        "available_sites": len(sites),
        "available_runs": len(runs),
        "earliest": min(run["start"] for run in runs),
        "latest_night": max(run["last_night"] for run in runs),
        "max_nights": max(run["nights"] for run in runs),
        "booking_url": booking_url,
        # The browser filters these locally. Keep every run so a short date
        # window cannot accidentally hide availability after the first page.
        "runs": runs,
    }


def build_map_data(
    *,
    config_path: Path = CONFIG,
    state_path: Path = STATE,
    progress_path: Path = PROGRESS,
    candidates_path: Path = CANDIDATES,
    wa_parks_path: Path = WA_PARKS,
) -> dict:
    """Join incremental availability with local campground coordinates."""
    config = _load_json(config_path, {"recdotgov": [], "going_to_camp": []})
    state = _load_json(state_path, {})
    raw_progress = _load_json(progress_path, {})
    candidates = _load_json(candidates_path, [])
    wa_parks = _load_json(wa_parks_path, [])

    candidate_by_id = {str(item.get("id")): item for item in candidates}
    park_by_id = {str(item.get("facility_id")): item for item in wa_parks}
    locations = []
    all_coordinates = []
    missing_coordinates = []

    for campground in config.get("recdotgov", []):
        campground_id = str(campground.get("id"))
        metadata = candidate_by_id.get(campground_id, {})
        coordinates = _coordinates(
            campground.get("lat", metadata.get("lat")),
            campground.get("lon", metadata.get("lon")),
        )
        if coordinates is not None:
            all_coordinates.append(coordinates)
        runs = _parse_runs(state.get(f"rg:{campground_id}", []))
        if not runs:
            continue
        if coordinates is None:
            missing_coordinates.append(campground.get("name") or campground_id)
            continue
        locations.append(
            _location_record(
                key=f"rg:{campground_id}",
                campground=campground,
                metadata=metadata,
                provider="recreation.gov",
                coordinates=coordinates,
                runs=runs,
                booking_url=(
                    f"https://www.recreation.gov/camping/campgrounds/"
                    f"{campground_id}/availability"
                ),
            )
        )

    for campground in config.get("going_to_camp", []):
        campground_id = str(campground.get("id"))
        metadata = park_by_id.get(campground_id, {})
        coordinates = _coordinates(
            campground.get("lat", metadata.get("lat")),
            campground.get("lon", metadata.get("lon")),
        )
        if coordinates is not None:
            all_coordinates.append(coordinates)
        runs = _parse_runs(state.get(f"wa:{campground_id}", []))
        if not runs:
            continue
        if coordinates is None:
            missing_coordinates.append(campground.get("name") or campground_id)
            continue
        try:
            booking_url = _gtc_booking_url(campground, runs[0])
        except (KeyError, TypeError, ValueError):
            booking_url = "https://washington.goingtocamp.com/"
        locations.append(
            _location_record(
                key=f"wa:{campground_id}",
                campground=campground,
                metadata=metadata,
                provider=campground.get("rec_area_name") or "WA State Parks",
                coordinates=coordinates,
                runs=runs,
                booking_url=booking_url,
            )
        )

    locations.sort(key=lambda item: (item["earliest"], item["name"]))
    if all_coordinates:
        bounds = {
            "south": min(item[0] for item in all_coordinates),
            "west": min(item[1] for item in all_coordinates),
            "north": max(item[0] for item in all_coordinates),
            "east": max(item[1] for item in all_coordinates),
        }
    else:
        bounds = {"south": 46.8, "west": -123.3, "north": 48.5, "east": -120.8}

    progress = {
        key: raw_progress.get(key)
        for key in ("status", "completed", "total", "updated_at", "last_target")
        if key in raw_progress
    }
    data_updated_at = raw_progress.get("updated_at")
    if not isinstance(data_updated_at, str):
        try:
            data_updated_at = dt.datetime.fromtimestamp(
                state_path.stat().st_mtime, tz=dt.timezone.utc
            ).astimezone().isoformat(timespec="seconds")
        except OSError:
            data_updated_at = None
    availability = {
        "locations": locations,
        "bounds": bounds,
        "missing_coordinates": missing_coordinates,
    }
    availability_fingerprint = _fingerprint(availability)
    progress_snapshot = {
        "progress": progress,
        "data_updated_at": data_updated_at,
    }
    progress_fingerprint = _fingerprint(progress_snapshot)
    stable = {**availability, **progress_snapshot}
    return {
        **stable,
        # ``fingerprint`` remains the complete payload revision for callers
        # that only need one value.  The event stream uses the two component
        # revisions to avoid fetching the full payload for checkpoint-only
        # updates.
        "fingerprint": _fingerprint(stable),
        "availability_fingerprint": availability_fingerprint,
        "progress_fingerprint": progress_fingerprint,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _event_payload(data: dict) -> dict:
    """Return the tiny event-stream message for a full map-data snapshot."""
    return {
        "availability_fingerprint": data["availability_fingerprint"],
        "progress_fingerprint": data["progress_fingerprint"],
        "progress": data["progress"],
        "data_updated_at": data["data_updated_at"],
    }


def _port(value: str) -> int:
    port = int(value)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def _handler():
    class MapHandler(BaseHTTPRequestHandler):
        server_version = "CampwatchMap/1.0"
        protocol_version = "HTTP/1.1"

        def _allowed_host(self) -> bool:
            host = self.headers.get("Host", "")
            port = self.server.server_address[1]
            return host in {"127.0.0.1", f"127.0.0.1:{port}", "localhost", f"localhost:{port}"}

        def _send_security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src https://tile.openstreetmap.org; "
                "style-src 'unsafe-inline'; script-src 'self'; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'",
            )

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_event_headers(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(f"retry: {EVENT_RETRY_MILLISECONDS}\n\n".encode())
            self.wfile.flush()

        def _write_event(self, event: str, payload: dict) -> None:
            encoded = json.dumps(payload, separators=(",", ":"))
            self.wfile.write(f"event: {event}\ndata: {encoded}\n\n".encode())
            self.wfile.flush()

        def _stream_events(self) -> None:
            """Send an initial snapshot, then only semantic local changes."""
            revisions = _source_revisions()
            data = build_map_data()
            availability_fingerprint = data["availability_fingerprint"]
            progress_fingerprint = data["progress_fingerprint"]
            try:
                self._send_event_headers()
                self._write_event("map-update", _event_payload(data))
                last_heartbeat = time.monotonic()
                while True:
                    time.sleep(EVENT_POLL_SECONDS)
                    current_revisions = _source_revisions()
                    if current_revisions != revisions:
                        # ``watch.py`` writes state and progress as two atomic
                        # replaces.  A tiny debounce coalesces them into one
                        # map event whenever they land together.
                        time.sleep(EVENT_DEBOUNCE_SECONDS)
                        before = _source_revisions()
                        fresh = build_map_data()
                        after = _source_revisions()
                        if before != after:
                            # A writer completed between the two reads; wait
                            # for a stable snapshot instead of announcing a
                            # mixed revision.
                            continue
                        revisions = after
                        availability_changed = (
                            fresh["availability_fingerprint"]
                            != availability_fingerprint
                        )
                        progress_changed = (
                            fresh["progress_fingerprint"] != progress_fingerprint
                        )
                        availability_fingerprint = fresh["availability_fingerprint"]
                        progress_fingerprint = fresh["progress_fingerprint"]
                        if availability_changed or progress_changed:
                            self._write_event("map-update", _event_payload(fresh))
                            last_heartbeat = time.monotonic()
                    elif time.monotonic() - last_heartbeat >= EVENT_HEARTBEAT_SECONDS:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_heartbeat = time.monotonic()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

        def do_GET(self) -> None:  # noqa: N802
            if not self._allowed_host():
                self._send(421, "text/plain; charset=utf-8", b"invalid Host header\n")
                return
            path = urllib.parse.urlsplit(self.path).path
            if path == "/":
                self._send(200, "text/html; charset=utf-8", MAP_HTML.encode())
            elif path == "/data.json":
                body = json.dumps(build_map_data(), separators=(",", ":")).encode()
                self._send(200, "application/json; charset=utf-8", body)
            elif path == "/events":
                self._stream_events()
            elif path == "/availability_map.js":
                self._send(200, "application/javascript; charset=utf-8", MAP_JS.encode())
            elif path == "/favicon.ico":
                self._send(204, "image/x-icon", b"")
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def log_message(self, format: str, *args) -> None:
            return

    return MapHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=_port, default=8765, help="loopback port (default: 8765)")
    parser.add_argument("--open", action="store_true", help="open the map in the default browser")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), _handler())
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Availability map: {url}")
    print("Press Ctrl-C to stop. Map tiles are requested from OpenStreetMap while viewed.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nMap server stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
