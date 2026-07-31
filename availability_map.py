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
    stable = {
        "locations": locations,
        "bounds": bounds,
        "progress": progress,
        "missing_coordinates": missing_coordinates,
        "data_updated_at": data_updated_at,
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
    #freshness { display: inline-flex; align-items: center; min-height: 24px; margin-top: 7px; padding: 3px 8px;
      border: 1px solid #94c7a4; border-radius: 999px; background: #16412d; color: #edf8ef; font-size: 12px; font-weight: 650; }
    #freshness.stale { border-color: #f7cf83; background: #5b3b12; color: #fff4dc; }
    #layout { display: grid; grid-template-columns: minmax(250px, 340px) 1fr; height: calc(100vh - 72px); }
    #sidebar { overflow: auto; padding: 12px; border-right: 1px solid #c9c5b8; background: #fffdf7; }
    #filters { margin: 0 0 12px; padding: 10px; border: 1px solid #d4d0c4; border-radius: 9px; background: #f8f6ef; }
    #filters legend { padding: 0 4px; font-size: 13px; font-weight: 700; }
    .date-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .date-fields label { display: grid; gap: 3px; color: #536158; font-size: 11px; font-weight: 650; }
    .date-fields input { width: 100%; min-width: 0; padding: 6px; border: 1px solid #aaa69b; border-radius: 6px;
      background: white; color: #17211a; font: inherit; font-size: 12px; }
    #presets { display: flex; gap: 5px; margin-top: 8px; }
    #presets button { flex: 1; padding: 5px 3px; border: 1px solid #aaa69b; border-radius: 6px; background: white;
      color: #284034; cursor: pointer; font-size: 11px; }
    #presets button:hover, #presets button:focus-visible { border-color: #23704a; outline: 1px solid #23704a; }
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
      max-height: min(520px, calc(100% - 48px)); overflow: auto; border-radius: 10px; background: #fffffff2;
      box-shadow: 0 5px 25px #0004; display: none; }
    #popup h2 { margin: 0 24px 5px 0; font-size: 17px; }
    #popup p { margin: 5px 0; font-size: 13px; }
    #popup a { color: #075b36; font-weight: 650; }
    .run-table-shell { max-height: min(350px, 52vh); margin: 8px 0 5px; overflow: auto; overscroll-behavior: contain;
      border: 1px solid #cbc7bb; border-radius: 8px; background: #fffefa; box-shadow: inset 0 1px #fff; }
    .availability-table-title { position: sticky; inset-block-start: 0; z-index: 3; overflow: hidden; padding: 8px 9px; border-bottom: 1px solid #cbc7bb;
      background: #fffefa; color: #203c2c; font-size: 12px; font-weight: 750; line-height: 17px; text-align: left; text-overflow: ellipsis; white-space: nowrap; }
    .run-table { width: 100%; margin: 0; border-collapse: separate; border-spacing: 0; font-size: 12px; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
    .run-table th, .run-table td { padding: 7px 8px; text-align: left; vertical-align: middle; border-bottom: 1px solid #e3dfd4; }
    .run-table th { position: sticky; inset-block-start: 34px; z-index: 2; background: #e0ebe3; color: #294536; box-shadow: 0 1px #bccdc1;
      font-size: 10px; letter-spacing: .04em; text-transform: uppercase; }
    .run-table th:last-child, .run-table td:last-child { width: 1%; text-align: center; white-space: nowrap; }
    .run-table tbody tr:nth-child(even) { background: #f7f5ef; }
    .run-table tbody tr:hover { background: #eaf4ed; }
    .run-table tbody tr:last-child td { border-bottom: 0; }
    .book-link { display: inline-flex; align-items: center; justify-content: center; min-height: 28px; padding: 4px 9px; border: 1px solid #28714c;
      border-radius: 6px; background: #eaf5ed; color: #075b36; font-weight: 750; line-height: 1; text-decoration: none; }
    .book-link:hover, .book-link:focus-visible { border-color: #075b36; background: #cfe8d6; outline: 2px solid #a9d8bd; outline-offset: 1px; }
    .more-runs { margin: 5px 1px 8px; color: #536158; font-size: 11px; }
    #hover-card { position: absolute; z-index: 8; width: min(430px, calc(100% - 24px)); padding: 9px 10px;
      border: 1px solid #52645a; border-radius: 8px; background: #fffffff7; box-shadow: 0 4px 16px #0005;
      transform: translate(-50%, -100%); font-size: 12px; }
    .hover-card-header { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 3px; }
    .hover-card-header strong { min-width: 0; font-size: 13px; }
    .card-actions { display: flex; flex: none; gap: 4px; }
    .card-action { display: grid; place-items: center; width: 28px; height: 28px; padding: 0; border: 1px solid #aaa69b;
      border-radius: 6px; background: white; color: #294536; cursor: pointer; }
    .card-action:hover, .card-action:focus-visible { border-color: #23704a; outline: 2px solid #a9d8bd; }
    .card-action[aria-pressed="true"] { border-color: #193c2b; background: #193c2b; color: white; }
    .card-action svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 2;
      stroke-linecap: round; stroke-linejoin: round; }
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
  <header><h1>Campground availability</h1><div id="status">Loading local results…</div><div id="freshness" aria-live="polite"></div></header>
  <main id="layout">
    <aside id="sidebar">
      <fieldset id="filters">
        <legend>Dates to display</legend>
        <div class="date-fields">
          <label>From <input id="date-from" type="date"></label>
          <label>Through <input id="date-through" type="date"></label>
        </div>
        <div id="presets" aria-label="Date range presets">
          <button type="button" data-days="7">Next 7 days</button>
          <button type="button" data-days="30">Next 30 days</button>
          <button type="button" data-days="all">All results</button>
        </div>
      </fieldset>
      <div id="summary" aria-live="polite"></div><div id="locations"></div>
    </aside>
    <section id="map" aria-label="Map of campgrounds with availability">
      <div id="tiles"></div><div id="markers"></div>
      <div id="controls"><button id="zoom-in" title="Zoom in">+</button><button id="zoom-out" title="Zoom out">−</button>
        <button id="fit" title="Fit displayed campgrounds" style="font-size:12px">Fit</button></div>
      <aside id="hover-card" role="dialog" aria-label="Campground availability preview" hidden></aside>
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
let filtersInitialized = false;
let usingAllDates = true;
let visibleLocations = [];
let hoverPinned = false;
let hoverLocationKey = null;
let hoverMarker = null;
let hoverHideTimer = null;

const map = document.getElementById("map");
const tiles = document.getElementById("tiles");
const markers = document.getElementById("markers");
const popup = document.getElementById("popup");
const popupContent = document.getElementById("popup-content");
const hoverCard = document.getElementById("hover-card");
const dateFrom = document.getElementById("date-from");
const dateThrough = document.getElementById("date-through");

function addIsoDays(value, days) {
  const date = new Date(`${value}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function dayCount(start, end) {
  return Math.round((Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86400000) + 1;
}

function formatDate(value) {
  return new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric", year: "numeric", timeZone: "UTC"})
    .format(new Date(`${value}T00:00:00Z`));
}

function formatDateRange(run) {
  return run.display_start === run.display_end
    ? formatDate(run.display_start)
    : `${formatDate(run.display_start)} – ${formatDate(run.display_end)}`;
}

function availabilityRows(location) {
  if (!location.key.startsWith("wa:")) {
    return location.runs.map(run => ({...run, site_count: 1}));
  }
  const grouped = new Map();
  location.runs.forEach(run => {
    const key = `${run.display_start}|${run.display_end}`;
    if (!grouped.has(key)) {
      grouped.set(key, {
        display_start: run.display_start,
        display_end: run.display_end,
        sites: new Set()
      });
    }
    grouped.get(key).sites.add(run.site);
  });
  return [...grouped.values()]
    .map(row => ({...row, site_count: row.sites.size}))
    .sort((left, right) => left.display_start.localeCompare(right.display_start)
      || left.display_end.localeCompare(right.display_end));
}

function bookingUrlFor(location, run) {
  if (!run || !location.key.startsWith("wa:")) return location.booking_url;
  try {
    const url = new URL(location.booking_url);
    if (url.searchParams.has("startDate")) {
      url.searchParams.set("startDate", run.display_start);
      url.searchParams.set("endDate", addIsoDays(run.display_end, 1));
    }
    return url.href;
  } catch (error) {
    return location.booking_url;
  }
}

function makeBookingLink(location, run) {
  const link = document.createElement("a");
  link.className = "book-link";
  link.href = bookingUrlFor(location, run);
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "Book";
  link.setAttribute("aria-label", `Book ${location.name}, ${formatDateRange(run)}`);
  return link;
}

function makeAvailabilityTable(location, limit, moreText) {
  const rows = availabilityRows(location);
  const aggregateSites = location.key.startsWith("wa:");
  const container = document.createElement("div");
  const table = document.createElement("table");
  table.className = "run-table";
  table.setAttribute("aria-label", aggregateSites ? "Available date windows" : "Available site dates");
  const caption = document.createElement("caption");
  caption.className = "sr-only";
  caption.textContent = `${location.name} availability`;
  table.appendChild(caption);
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  const headings = aggregateSites
    ? ["Available dates", "Nights", "Sites", "Book"]
    : ["Site", "Available dates", "Nights", "Book"];
  headings.forEach(label => {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = label;
    headRow.appendChild(cell);
  });
  head.appendChild(headRow);
  const body = document.createElement("tbody");
  rows.slice(0, limit).forEach(run => {
    const row = document.createElement("tr");
    const dates = document.createElement("td");
    const nights = document.createElement("td");
    dates.textContent = formatDateRange(run);
    nights.textContent = String(dayCount(run.display_start, run.display_end));
    if (aggregateSites) {
      const sites = document.createElement("td");
      const book = document.createElement("td");
      sites.textContent = String(run.site_count);
      book.appendChild(makeBookingLink(location, run));
      row.append(dates, nights, sites, book);
    } else {
      const site = document.createElement("td");
      const book = document.createElement("td");
      site.textContent = run.site;
      book.appendChild(makeBookingLink(location, run));
      row.append(site, dates, nights, book);
    }
    body.appendChild(row);
  });
  table.append(head, body);
  const shell = document.createElement("div");
  shell.className = "run-table-shell";
  shell.tabIndex = 0;
  shell.setAttribute("aria-label", `${location.name} availability table; scroll for more dates`);
  const tableTitle = document.createElement("div");
  tableTitle.className = "availability-table-title";
  tableTitle.textContent = `${location.name} availability`;
  shell.append(tableTitle, table);
  container.appendChild(shell);
  if (rows.length > limit) {
    const more = document.createElement("p");
    more.className = "more-runs";
    const label = aggregateSites ? "date window(s)" : "run(s)";
    more.textContent = `+${rows.length - limit} more ${label}${moreText}`;
    container.appendChild(more);
  }
  return container;
}

function makeIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const paths = name === "pin"
    ? ["M9 3h6l-1 5 3 3v2H7v-2l3-3-1-5Z", "M12 13v8"]
    : ["M6 6l12 12", "M18 6 6 18"];
  paths.forEach(value => {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", value);
    svg.appendChild(path);
  });
  return svg;
}

function makeCardAction(label, iconName) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "card-action";
  button.setAttribute("aria-label", label);
  button.title = label;
  button.appendChild(makeIcon(iconName));
  return button;
}

function availableBounds() {
  const runs = (mapData?.locations || []).flatMap(location => location.runs);
  if (!runs.length) return null;
  return {
    first: runs.reduce((value, run) => run.start < value ? run.start : value, runs[0].start),
    last: runs.reduce((value, run) => run.last_night > value ? run.last_night : value, runs[0].last_night)
  };
}

function syncDateControls() {
  const bounds = availableBounds();
  if (!bounds) return;
  dateFrom.min = bounds.first;
  dateFrom.max = bounds.last;
  dateThrough.min = bounds.first;
  dateThrough.max = bounds.last;
  if (!filtersInitialized || usingAllDates) {
    dateFrom.value = bounds.first;
    dateThrough.value = bounds.last;
    filtersInitialized = true;
  }
}

function filterLocation(location) {
  const first = dateFrom.value;
  const last = dateThrough.value;
  const runs = location.runs
    .filter(run => (!first || run.last_night >= first) && (!last || run.start <= last))
    .map(run => ({
      ...run,
      display_start: first && run.start < first ? first : run.start,
      display_end: last && run.last_night > last ? last : run.last_night
    }));
  if (!runs.length) return null;
  return {
    ...location,
    runs,
    available_sites: new Set(runs.map(run => run.site)).size,
    available_runs: runs.length,
    earliest: runs.reduce((value, run) => run.display_start < value ? run.display_start : value, runs[0].display_start),
    latest_night: runs.reduce((value, run) => run.display_end > value ? run.display_end : value, runs[0].display_end)
  };
}

function filteredLocationList() {
  return mapData.locations
    .map(filterLocation)
    .filter(Boolean)
    .sort((left, right) => left.earliest.localeCompare(right.earliest) || left.name.localeCompare(right.name));
}

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
  const rowCount = availabilityRows(location).length;
  availability.textContent = location.key.startsWith("wa:")
    ? `${location.available_sites} site(s) across ${rowCount} date window(s), ${location.earliest} through ${location.latest_night}`
    : `${location.available_sites} site(s), ${location.available_runs} run(s), ${location.earliest} through ${location.latest_night}`;
  const runHeading = document.createElement("strong");
  runHeading.textContent = "Available dates";
  const runTable = makeAvailabilityTable(location, Number.POSITIVE_INFINITY, "");
  const distance = document.createElement("p");
  const distanceParts = [];
  if (location.distance_km != null) distanceParts.push(`${location.distance_km} km`);
  if (location.distance_mi != null) distanceParts.push(`${location.distance_mi} mi`);
  if (location.est_drive_hrs != null) distanceParts.push(`~${location.est_drive_hrs} h drive`);
  distance.textContent = distanceParts.join(" · ");
  const link = document.createElement("a");
  link.href = bookingUrlFor(location, location.runs[0]);
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
  popupContent.append(title, provider, availability, runHeading, runTable, distance, links);
  popup.style.display = "block";
}

function cancelHoverHide() {
  if (hoverHideTimer != null) {
    clearTimeout(hoverHideTimer);
    hoverHideTimer = null;
  }
}

function closeHover() {
  cancelHoverHide();
  hoverPinned = false;
  if (hoverMarker) hoverMarker.setAttribute("aria-expanded", "false");
  hoverLocationKey = null;
  hoverMarker = null;
  hoverCard.hidden = true;
  hoverCard.replaceChildren();
}

function scheduleHoverHide() {
  cancelHoverHide();
  if (hoverPinned) return;
  hoverHideTimer = setTimeout(() => {
    hoverHideTimer = null;
    const markerActive = hoverMarker
      && (hoverMarker.matches(":hover") || document.activeElement === hoverMarker);
    const cardActive = hoverCard.matches(":hover") || hoverCard.contains(document.activeElement);
    if (!hoverPinned && !markerActive && !cardActive) closeHover();
  }, 180);
}

function updatePinAction(button) {
  const label = hoverPinned ? "Unpin availability card" : "Pin availability card";
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-pressed", String(hoverPinned));
  button.title = label;
}

function showHover(location, button, restorePinned = false) {
  cancelHoverHide();
  if (hoverPinned && hoverLocationKey !== location.key && !restorePinned) return;
  if (hoverMarker && hoverMarker !== button) hoverMarker.setAttribute("aria-expanded", "false");
  hoverLocationKey = location.key;
  hoverMarker = button;
  button.setAttribute("aria-expanded", "true");
  hoverCard.replaceChildren();
  hoverCard.setAttribute("aria-label", `${location.name} availability preview`);
  const header = document.createElement("div");
  header.className = "hover-card-header";
  const title = document.createElement("strong");
  title.textContent = location.name;
  const actions = document.createElement("div");
  actions.className = "card-actions";
  const pinButton = makeCardAction("Pin availability card", "pin");
  updatePinAction(pinButton);
  pinButton.addEventListener("click", event => {
    event.stopPropagation();
    hoverPinned = !hoverPinned;
    updatePinAction(pinButton);
    if (!hoverPinned) scheduleHoverHide();
  });
  const closeButton = makeCardAction("Close availability card", "close");
  closeButton.addEventListener("click", event => {
    event.stopPropagation();
    closeHover();
  });
  actions.append(pinButton, closeButton);
  header.append(title, actions);
  hoverCard.append(header, makeAvailabilityTable(location, 5, "; click the marker for details"));
  const markerX = Number.parseFloat(button.style.left);
  const markerY = Number.parseFloat(button.style.top);
  hoverCard.hidden = false;
  const halfWidth = hoverCard.offsetWidth / 2;
  hoverCard.style.left = `${Math.max(halfWidth + 12, Math.min(map.clientWidth - halfWidth - 12, markerX))}px`;
  hoverCard.style.top = `${Math.max(hoverCard.offsetHeight + 12, markerY - 44)}px`;
}

function renderMarkers() {
  const pinnedKey = hoverPinned ? hoverLocationKey : null;
  if (!pinnedKey) closeHover();
  markers.replaceChildren();
  if (!mapData) return;
  const origin = viewportOrigin();
  let pinnedTarget = null;
  visibleLocations.forEach((location, index) => {
    const point = project(location.lat, location.lon, zoom);
    const button = document.createElement("button");
    button.className = `marker ${location.key.startsWith("rg:") ? "rg" : "wa"}`;
    button.textContent = String(index + 1);
    button.setAttribute("aria-label", `${location.name}: ${location.available_sites} sites, ${location.earliest} through ${location.latest_night}`);
    button.setAttribute("aria-haspopup", "dialog");
    button.setAttribute("aria-expanded", "false");
    button.style.left = `${point.x - origin.x}px`;
    button.style.top = `${point.y - origin.y}px`;
    button.addEventListener("click", () => {
      closeHover();
      showLocation(location);
    });
    button.addEventListener("pointerenter", () => showHover(location, button));
    button.addEventListener("pointerleave", scheduleHoverHide);
    button.addEventListener("focus", () => showHover(location, button));
    button.addEventListener("blur", scheduleHoverHide);
    markers.appendChild(button);
    if (location.key === pinnedKey) pinnedTarget = {location, button};
  });
  if (pinnedKey && pinnedTarget) showHover(pinnedTarget.location, pinnedTarget.button, true);
  else if (pinnedKey) closeHover();
}

function renderMap() { renderTiles(); renderMarkers(); }

function fitAll() {
  if (!mapData) return;
  const b = visibleLocations.length ? {
    south: Math.min(...visibleLocations.map(location => location.lat)),
    west: Math.min(...visibleLocations.map(location => location.lon)),
    north: Math.max(...visibleLocations.map(location => location.lat)),
    east: Math.max(...visibleLocations.map(location => location.lon))
  } : mapData.bounds;
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
  const filterDescription = dateFrom.value && dateThrough.value
    ? ` from ${formatDate(dateFrom.value)} through ${formatDate(dateThrough.value)}`
    : "";
  summary.textContent = `${visibleLocations.length} of ${mapData.locations.length} campground(s) have availability${filterDescription}.`;
  if (!visibleLocations.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No qualifying availability overlaps this date range.";
    list.appendChild(empty);
    return;
  }
  visibleLocations.forEach((location, index) => {
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

function renderResults() {
  if (!mapData) return;
  visibleLocations = filteredLocationList();
  popup.style.display = "none";
  renderSidebar();
  renderMarkers();
}

function applyDateFilter(changedInput) {
  if (dateFrom.value && dateThrough.value && dateFrom.value > dateThrough.value) {
    if (changedInput === dateFrom) dateThrough.value = dateFrom.value;
    else dateFrom.value = dateThrough.value;
  }
  usingAllDates = false;
  renderResults();
}

function applyPreset(days) {
  const bounds = availableBounds();
  if (!bounds) return;
  if (days === "all") {
    dateFrom.value = bounds.first;
    dateThrough.value = bounds.last;
    usingAllDates = true;
  } else {
    const today = new Date();
    const localToday = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
    const start = localToday >= bounds.first && localToday <= bounds.last ? localToday : bounds.first;
    dateFrom.value = start;
    dateThrough.value = addIsoDays(start, Number(days) - 1) > bounds.last
      ? bounds.last
      : addIsoDays(start, Number(days) - 1);
    usingAllDates = false;
  }
  renderResults();
}

function renderStatus() {
  const progress = mapData.progress || {};
  const parts = [];
  const updated = mapData.data_updated_at ? new Date(mapData.data_updated_at) : null;
  if (updated && !Number.isNaN(updated.getTime())) {
    parts.push(`Data updated ${updated.toLocaleString()}`);
  } else {
    parts.push("Data update time unavailable");
  }
  if (progress.status === "running" || progress.status === "failed") {
    parts.push(`scan ${progress.status}: ${progress.completed ?? "?"}/${progress.total ?? "?"}`);
  }
  if (mapData.missing_coordinates.length) parts.push(`${mapData.missing_coordinates.length} result(s) lack coordinates`);
  document.getElementById("status").textContent = parts.join(" · ");
  const freshness = document.getElementById("freshness");
  const ageMinutes = updated ? Math.floor((Date.now() - updated.getTime()) / 60000) : null;
  if (ageMinutes == null || Number.isNaN(ageMinutes)) {
    freshness.textContent = "Availability freshness is unknown";
    freshness.classList.add("stale");
  } else if (ageMinutes > 60) {
    const hours = Math.floor(ageMinutes / 60);
    const minutes = ageMinutes % 60;
    freshness.textContent = `Stale availability data: last updated ${hours}h ${minutes}m ago`;
    freshness.classList.add("stale");
  } else {
    freshness.textContent = `Availability data is current (${ageMinutes}m old)`;
    freshness.classList.remove("stale");
  }
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
      syncDateControls();
      visibleLocations = filteredLocationList();
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
dateFrom.addEventListener("input", () => applyDateFilter(dateFrom));
dateThrough.addEventListener("input", () => applyDateFilter(dateThrough));
document.querySelectorAll("#presets button").forEach(button => {
  button.addEventListener("click", () => applyPreset(button.dataset.days));
});
hoverCard.addEventListener("pointerenter", cancelHoverHide);
hoverCard.addEventListener("pointerleave", scheduleHoverHide);
hoverCard.addEventListener("focusin", cancelHoverHide);
hoverCard.addEventListener("focusout", scheduleHoverHide);
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !hoverCard.hidden) closeHover();
});
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
