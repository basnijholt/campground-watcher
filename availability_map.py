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
    return {
        "key": key,
        "name": campground.get("name") or metadata.get("name") or key,
        "provider": provider,
        "lat": coordinates[0],
        "lon": coordinates[1],
        "rating": campground.get("rating"),
        "distance_km": campground.get("distance_km") or metadata.get("distance_km"),
        "distance_mi": campground.get("dist_mi") or metadata.get("dist_mi"),
        "est_drive_hrs": campground.get("est_drive_hrs"),
        "available_sites": len(sites),
        "available_runs": len(runs),
        "earliest": min(run["start"] for run in runs),
        "latest_night": max(run["last_night"] for run in runs),
        "max_nights": max(run["nights"] for run in runs),
        "booking_url": booking_url,
        "runs": runs[:12],
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
    stable = {
        "locations": locations,
        "bounds": bounds,
        "progress": progress,
        "missing_coordinates": missing_coordinates,
    }
    fingerprint = hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return {
        **stable,
        "fingerprint": fingerprint,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


MAP_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Campground availability map</title>
  <style>
    :root { color-scheme: light; font-family: ui-sans-serif, system-ui, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; color: #17211a; background: #f4f1e8; }
    header { padding: 14px 18px; background: #193c2b; color: white; }
    header h1 { margin: 0 0 4px; font-size: 20px; }
    #status { font-size: 13px; opacity: .9; }
    #layout { display: grid; grid-template-columns: minmax(250px, 340px) 1fr; height: calc(100vh - 72px); }
    #sidebar { overflow: auto; padding: 12px; border-right: 1px solid #c9c5b8; background: #fffdf7; }
    #summary { font-size: 14px; margin: 2px 2px 12px; }
    #locations { display: grid; gap: 8px; }
    .location { width: 100%; text-align: left; border: 1px solid #d4d0c4; border-radius: 9px; padding: 10px;
      background: white; color: inherit; cursor: pointer; }
    .location:hover, .location:focus-visible { border-color: #23704a; outline: 2px solid #a9d8bd; }
    .location strong { display: block; font-size: 14px; }
    .meta { display: block; margin-top: 4px; color: #536158; font-size: 12px; }
    #map { position: relative; overflow: hidden; background: #dce6dc; min-height: 420px; }
    #tiles, #markers { position: absolute; inset: 0; overflow: hidden; }
    #tiles img { position: absolute; width: 256px; height: 256px; user-select: none; }
    .marker { position: absolute; transform: translate(-50%, -100%); width: 30px; height: 38px; border: 0;
      clip-path: polygon(50% 100%, 5% 42%, 7% 23%, 19% 8%, 36% 1%, 64% 1%, 81% 8%, 93% 23%, 95% 42%);
      color: white; font-weight: 700; cursor: pointer; filter: drop-shadow(0 2px 2px #0007); }
    .marker.rg { background: #1769aa; }
    .marker.wa { background: #b44725; }
    .marker:hover, .marker:focus-visible { z-index: 3; scale: 1.14; outline: none; }
    #controls { position: absolute; z-index: 5; top: 12px; right: 12px; display: grid; gap: 6px; }
    #controls button { border: 1px solid #777; background: white; border-radius: 6px; min-width: 38px; min-height: 36px;
      font-size: 18px; cursor: pointer; }
    #popup { position: absolute; z-index: 6; left: 16px; bottom: 26px; max-width: 390px; padding: 14px;
      border-radius: 10px; background: #fffffff2; box-shadow: 0 5px 25px #0004; display: none; }
    #popup h2 { margin: 0 24px 5px 0; font-size: 17px; }
    #popup p { margin: 5px 0; font-size: 13px; }
    #popup a { color: #075b36; font-weight: 650; }
    #popup-close { position: absolute; top: 6px; right: 7px; border: 0; background: none; font-size: 20px; cursor: pointer; }
    #attribution { position: absolute; z-index: 5; right: 5px; bottom: 3px; padding: 2px 5px; background: #ffffffe8;
      font-size: 11px; }
    #attribution a { color: #17472e; }
    .empty { padding: 20px 8px; color: #536158; }
    @media (max-width: 760px) {
      #layout { grid-template-columns: 1fr; grid-template-rows: 38vh 1fr; height: calc(100vh - 72px); }
      #sidebar { border-right: 0; border-bottom: 1px solid #c9c5b8; }
      #map { min-height: 340px; }
    }
  </style>
</head>
<body>
  <header><h1>Campground availability</h1><div id="status">Loading local results…</div></header>
  <main id="layout">
    <aside id="sidebar"><div id="summary"></div><div id="locations"></div></aside>
    <section id="map" aria-label="Map of campgrounds with availability">
      <div id="tiles"></div><div id="markers"></div>
      <div id="controls"><button id="zoom-in" title="Zoom in">+</button><button id="zoom-out" title="Zoom out">−</button>
        <button id="fit" title="Fit all watched campgrounds" style="font-size:12px">Fit</button></div>
      <article id="popup"><button id="popup-close" aria-label="Close">×</button><div id="popup-content"></div></article>
      <div id="attribution"><a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap contributors</a>
        · <a href="https://www.openstreetmap.org/fixthemap" target="_blank" rel="noopener">Report a map issue</a></div>
    </section>
  </main>
<script>
"use strict";
const TILE_SIZE = 256;
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
let mapData = null;
let zoom = 7;
let center = {lat: 47.6, lon: -122.2};
let initialized = false;

const map = document.getElementById("map");
const tiles = document.getElementById("tiles");
const markers = document.getElementById("markers");
const popup = document.getElementById("popup");
const popupContent = document.getElementById("popup-content");

function project(lat, lon, z) {
  const size = TILE_SIZE * (2 ** z);
  const boundedLat = Math.max(-85.0511, Math.min(85.0511, lat));
  const sin = Math.sin(boundedLat * Math.PI / 180);
  return {
    x: (lon + 180) / 360 * size,
    y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size
  };
}

function unproject(x, y, z) {
  const size = TILE_SIZE * (2 ** z);
  const lon = x / size * 360 - 180;
  const n = Math.PI - 2 * Math.PI * y / size;
  return {lat: 180 / Math.PI * Math.atan(Math.sinh(n)), lon};
}

function viewportOrigin() {
  const c = project(center.lat, center.lon, zoom);
  return {x: c.x - map.clientWidth / 2, y: c.y - map.clientHeight / 2};
}

function renderTiles() {
  tiles.replaceChildren();
  const origin = viewportOrigin();
  const count = 2 ** zoom;
  const minX = Math.floor(origin.x / TILE_SIZE);
  const maxX = Math.floor((origin.x + map.clientWidth) / TILE_SIZE);
  const minY = Math.max(0, Math.floor(origin.y / TILE_SIZE));
  const maxY = Math.min(count - 1, Math.floor((origin.y + map.clientHeight) / TILE_SIZE));
  for (let x = minX; x <= maxX; x += 1) {
    for (let y = minY; y <= maxY; y += 1) {
      const wrappedX = ((x % count) + count) % count;
      const image = document.createElement("img");
      image.alt = "";
      image.decoding = "async";
      image.draggable = false;
      image.src = TILE_URL.replace("{z}", zoom).replace("{x}", wrappedX).replace("{y}", y);
      image.style.left = `${x * TILE_SIZE - origin.x}px`;
      image.style.top = `${y * TILE_SIZE - origin.y}px`;
      tiles.appendChild(image);
    }
  }
}

function showLocation(location) {
  popupContent.replaceChildren();
  const title = document.createElement("h2");
  title.textContent = location.name;
  const provider = document.createElement("p");
  provider.textContent = location.provider;
  const availability = document.createElement("p");
  availability.textContent = `${location.available_sites} site(s), ${location.available_runs} run(s), ${location.earliest} through ${location.latest_night}`;
  const distance = document.createElement("p");
  const distanceParts = [];
  if (location.distance_km != null) distanceParts.push(`${location.distance_km} km`);
  if (location.distance_mi != null) distanceParts.push(`${location.distance_mi} mi`);
  if (location.est_drive_hrs != null) distanceParts.push(`~${location.est_drive_hrs} h drive`);
  distance.textContent = distanceParts.join(" · ");
  const link = document.createElement("a");
  link.href = location.booking_url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "Open booking page";
  const osm = document.createElement("a");
  osm.href = `https://www.openstreetmap.org/?mlat=${location.lat}&mlon=${location.lon}#map=13/${location.lat}/${location.lon}`;
  osm.target = "_blank";
  osm.rel = "noopener";
  osm.textContent = "Open location in OpenStreetMap";
  const links = document.createElement("p");
  links.append(link, document.createTextNode(" · "), osm);
  popupContent.append(title, provider, availability, distance, links);
  popup.style.display = "block";
}

function renderMarkers() {
  markers.replaceChildren();
  if (!mapData) return;
  const origin = viewportOrigin();
  mapData.locations.forEach((location, index) => {
    const point = project(location.lat, location.lon, zoom);
    const button = document.createElement("button");
    button.className = `marker ${location.key.startsWith("rg:") ? "rg" : "wa"}`;
    button.textContent = String(index + 1);
    button.title = location.name;
    button.setAttribute("aria-label", location.name);
    button.style.left = `${point.x - origin.x}px`;
    button.style.top = `${point.y - origin.y}px`;
    button.addEventListener("click", () => showLocation(location));
    markers.appendChild(button);
  });
}

function renderMap() { renderTiles(); renderMarkers(); }

function fitAll() {
  if (!mapData) return;
  const b = mapData.bounds;
  for (let candidate = 12; candidate >= 4; candidate -= 1) {
    const nw = project(b.north, b.west, candidate);
    const se = project(b.south, b.east, candidate);
    if (Math.abs(se.x - nw.x) <= map.clientWidth - 90 && Math.abs(se.y - nw.y) <= map.clientHeight - 90) {
      zoom = candidate;
      const midpoint = unproject((nw.x + se.x) / 2, (nw.y + se.y) / 2, candidate);
      center = midpoint;
      break;
    }
  }
  renderMap();
}

function focusLocation(location) {
  center = {lat: location.lat, lon: location.lon};
  zoom = Math.max(zoom, 10);
  renderMap();
  showLocation(location);
}

function renderSidebar() {
  const list = document.getElementById("locations");
  const summary = document.getElementById("summary");
  list.replaceChildren();
  summary.textContent = `${mapData.locations.length} campground(s) currently have qualifying availability.`;
  if (!mapData.locations.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No qualifying availability is present in last_state.json yet.";
    list.appendChild(empty);
    return;
  }
  mapData.locations.forEach((location, index) => {
    const button = document.createElement("button");
    button.className = "location";
    const name = document.createElement("strong");
    name.textContent = `${index + 1}. ${location.name}`;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${location.available_sites} site(s) · ${location.earliest} → ${location.latest_night} · ${location.provider}`;
    button.append(name, meta);
    button.addEventListener("click", () => focusLocation(location));
    list.appendChild(button);
  });
}

function renderStatus() {
  const progress = mapData.progress || {};
  const parts = [`Updated ${mapData.generated_at}`];
  if (progress.status === "running" || progress.status === "failed") {
    parts.push(`scan ${progress.status}: ${progress.completed ?? "?"}/${progress.total ?? "?"}`);
  }
  if (mapData.missing_coordinates.length) parts.push(`${mapData.missing_coordinates.length} result(s) lack coordinates`);
  document.getElementById("status").textContent = parts.join(" · ");
}

async function refreshData() {
  try {
    const response = await fetch("/data.json", {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const fresh = await response.json();
    const changed = !mapData || fresh.fingerprint !== mapData.fingerprint;
    mapData = fresh;
    renderStatus();
    if (changed) {
      renderSidebar();
      if (!initialized) { initialized = true; fitAll(); }
      else renderMarkers();
    }
  } catch (error) {
    document.getElementById("status").textContent = `Map data unavailable: ${error.name}`;
  }
}

document.getElementById("zoom-in").addEventListener("click", () => { zoom = Math.min(14, zoom + 1); renderMap(); });
document.getElementById("zoom-out").addEventListener("click", () => { zoom = Math.max(4, zoom - 1); renderMap(); });
document.getElementById("fit").addEventListener("click", fitAll);
document.getElementById("popup-close").addEventListener("click", () => { popup.style.display = "none"; });
window.addEventListener("resize", () => { if (initialized) renderMap(); });
refreshData();
setInterval(refreshData, 5000);
</script>
</body>
</html>
"""


def _port(value: str) -> int:
    port = int(value)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def _handler():
    class MapHandler(BaseHTTPRequestHandler):
        server_version = "CampwatchMap/1.0"

        def _allowed_host(self) -> bool:
            host = self.headers.get("Host", "")
            port = self.server.server_address[1]
            return host in {"127.0.0.1", f"127.0.0.1:{port}", "localhost", f"localhost:{port}"}

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src https://tile.openstreetmap.org; "
                "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

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
