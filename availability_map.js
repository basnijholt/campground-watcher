"use strict";
const TILE_SIZE = 256;
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const FRESH_SCAN_COMMAND = "python3 watch.py --all-once";
const MIN_ZOOM = 4;
const MAX_ZOOM = 14;
let mapData = null;
let zoom = 7;
let center = {lat: 47.6, lon: -122.2};
let initialized = false;
let filtersInitialized = false;
let usingAllDates = true;
let visibleLocations = [];
let cardPinned = false;
let cardLocationKey = null;
let cardAnchor = null;
let cardHideTimer = null;
let cardDrag = null;
let mapPan = null;
let mapRenderFrame = null;
let renderedTileZoom = null;
const renderedTiles = new Map();
let wheelDelta = 0;
let wheelResetTimer = null;
let freshnessResetTimer = null;
let refreshInFlight = false;
let refreshQueued = false;
let liveUpdatesDisconnected = false;
let recoveryProbeTimer = null;

const SCAN_STALLED_AFTER_SECONDS = 5 * 60;
const RECOVERY_PROBE_MILLISECONDS = 30 * 1000;

const map = document.getElementById("map");
const tiles = document.getElementById("tiles");
const markers = document.getElementById("markers");
const availabilityCard = document.getElementById("availability-card");
const cardTitle = document.getElementById("card-title");
const cardContent = document.getElementById("card-content");
const cardHeader = document.getElementById("card-header");
const cardPin = document.getElementById("card-pin");
const dateFrom = document.getElementById("date-from");
const dateThrough = document.getElementById("date-through");
const freshness = document.getElementById("freshness");

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

function formatCheckInDate(value) {
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: "UTC"
  }).format(new Date(`${value}T00:00:00Z`));
}

function formatDateRange(run) {
  return run.display_start === run.display_end
    ? formatDate(run.display_start)
    : `${formatDate(run.display_start)} – ${formatDate(run.display_end)}`;
}

function formatDriveDuration(seconds) {
  const minutes = Math.max(1, Math.round(Number(seconds) / 60));
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return hours ? `${hours}h ${remainingMinutes}m` : `${minutes}m`;
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

function availabilityDateGroups(location) {
  const grouped = new Map();
  availabilityRows(location).forEach(run => {
    if (!grouped.has(run.display_start)) grouped.set(run.display_start, new Map());
    const stays = grouped.get(run.display_start);
    if (!stays.has(run.display_end)) {
      stays.set(run.display_end, {
        display_start: run.display_start,
        display_end: run.display_end,
        site_count: 0,
      });
    }
    stays.get(run.display_end).site_count += run.site_count;
  });
  return [...grouped.entries()]
    .map(([checkIn, runs]) => ({
      checkIn,
      runs: [...runs.values()].sort((left, right) => left.display_end.localeCompare(right.display_end)),
    }))
    .sort((left, right) => left.checkIn.localeCompare(right.checkIn));
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

function makeStayChip(location, run) {
  const nights = dayCount(run.display_start, run.display_end);
  const text = `${nights} night${nights === 1 ? "" : "s"} · ${run.site_count} site${run.site_count === 1 ? "" : "s"}`;
  const link = document.createElement("a");
  link.className = "stay-chip";
  link.href = bookingUrlFor(location, run);
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = text;
  link.setAttribute("aria-label", `${text} — book ${location.name}, checking in ${formatDate(run.display_start)}`);
  return link;
}

function makeAvailabilityTable(location, limit, moreText) {
  const groups = availabilityDateGroups(location);
  const container = document.createElement("div");
  const table = document.createElement("table");
  table.className = "run-table";
  table.setAttribute("aria-label", "Available stays by check-in date");
  const caption = document.createElement("caption");
  caption.className = "sr-only";
  caption.textContent = `${location.name} availability`;
  table.appendChild(caption);
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  const headings = ["Check in", "Available stays"];
  headings.forEach(label => {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = label;
    headRow.appendChild(cell);
  });
  head.appendChild(headRow);
  const body = document.createElement("tbody");
  groups.slice(0, limit).forEach(group => {
    const row = document.createElement("tr");
    const checkIn = document.createElement("th");
    checkIn.scope = "row";
    checkIn.textContent = formatCheckInDate(group.checkIn);
    const stays = document.createElement("td");
    const options = document.createElement("div");
    options.className = "stay-options";
    group.runs.forEach(run => options.appendChild(makeStayChip(location, run)));
    stays.appendChild(options);
    row.append(checkIn, stays);
    body.appendChild(row);
  });
  table.append(head, body);
  const shell = document.createElement("div");
  shell.className = "run-table-shell";
  shell.tabIndex = 0;
  shell.setAttribute("aria-label", `${location.name} availability table; scroll for more dates`);
  shell.appendChild(table);
  container.appendChild(shell);
  if (groups.length > limit) {
    const more = document.createElement("p");
    more.className = "more-runs";
    more.textContent = `+${groups.length - limit} more check-in day(s)${moreText}`;
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

function updatePinAction() {
  const label = cardPinned ? "Unpin availability card" : "Pin availability card";
  cardPin.setAttribute("aria-label", label);
  cardPin.setAttribute("aria-pressed", String(cardPinned));
  cardPin.title = label;
}

function cancelCardHide() {
  if (cardHideTimer != null) {
    clearTimeout(cardHideTimer);
    cardHideTimer = null;
  }
}

function scheduleCardHide() {
  cancelCardHide();
  if (cardPinned) return;
  cardHideTimer = setTimeout(() => {
    cardHideTimer = null;
    const anchorActive = cardAnchor && (cardAnchor.matches(":hover") || document.activeElement === cardAnchor);
    const cardActive = availabilityCard.matches(":hover") || availabilityCard.contains(document.activeElement);
    if (!cardPinned && !anchorActive && !cardActive) closeLocation();
  }, 180);
}

function clampCardPosition(left, top) {
  const maxLeft = Math.max(12, map.clientWidth - availabilityCard.offsetWidth - 12);
  const maxTop = Math.max(12, map.clientHeight - availabilityCard.offsetHeight - 12);
  return {left: Math.max(12, Math.min(maxLeft, left)), top: Math.max(12, Math.min(maxTop, top))};
}

function moveCard(left, top) {
  const position = clampCardPosition(left, top);
  availabilityCard.style.left = `${position.left}px`;
  availabilityCard.style.top = `${position.top}px`;
}

function placeCardNear(anchor) {
  const markerX = Number.parseFloat(anchor.style.left);
  const markerY = Number.parseFloat(anchor.style.top);
  const cardWidth = availabilityCard.offsetWidth;
  const cardHeight = availabilityCard.offsetHeight;
  let left = markerX + 20;
  if (left + cardWidth > map.clientWidth - 12) left = markerX - cardWidth - 20;
  moveCard(left, markerY - cardHeight / 2);
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

function centeredAtWorld(x, y, z) {
  const size = TILE_SIZE * (2 ** z);
  const minY = Math.min(size / 2, map.clientHeight / 2);
  const maxY = Math.max(size / 2, size - map.clientHeight / 2);
  const wrappedX = ((x % size) + size) % size;
  return unproject(wrappedX, Math.max(minY, Math.min(maxY, y)), z);
}

function viewportOrigin() {
  const c = project(center.lat, center.lon, zoom);
  return {x: c.x - map.clientWidth / 2, y: c.y - map.clientHeight / 2};
}

function renderTiles() {
  const origin = viewportOrigin();
  const count = 2 ** zoom;
  const minX = Math.floor(origin.x / TILE_SIZE);
  const maxX = Math.floor((origin.x + map.clientWidth) / TILE_SIZE);
  const minY = Math.max(0, Math.floor(origin.y / TILE_SIZE));
  const maxY = Math.min(count - 1, Math.floor((origin.y + map.clientHeight) / TILE_SIZE));
  if (renderedTileZoom !== zoom) {
    tiles.replaceChildren();
    renderedTiles.clear();
    renderedTileZoom = zoom;
  }
  const wanted = new Set();
  for (let x = minX; x <= maxX; x += 1) {
    for (let y = minY; y <= maxY; y += 1) {
      const key = `${zoom}/${x}/${y}`;
      wanted.add(key);
      const wrappedX = ((x % count) + count) % count;
      let image = renderedTiles.get(key);
      if (!image) {
        image = document.createElement("img");
        image.alt = "";
        image.decoding = "async";
        image.draggable = false;
        image.src = TILE_URL.replace("{z}", zoom).replace("{x}", wrappedX).replace("{y}", y);
        renderedTiles.set(key, image);
        tiles.appendChild(image);
      }
      image.style.left = `${x * TILE_SIZE - origin.x}px`;
      image.style.top = `${y * TILE_SIZE - origin.y}px`;
    }
  }
  renderedTiles.forEach((image, key) => {
    if (!wanted.has(key)) {
      image.remove();
      renderedTiles.delete(key);
    }
  });
}

function showLocation(location, anchor = null, pin = false) {
  cancelCardHide();
  if (cardPinned && cardLocationKey !== location.key && !pin) return;
  const shouldPlace = anchor && (availabilityCard.hidden || !cardPinned);
  cardLocationKey = location.key;
  cardAnchor = anchor;
  cardTitle.textContent = location.name;
  availabilityCard.setAttribute("aria-labelledby", "card-title");
  cardContent.replaceChildren();
  const availability = document.createElement("p");
  availability.textContent = `${location.available_sites} site${location.available_sites === 1 ? "" : "s"} · ${formatDate(location.earliest)} – ${formatDate(location.latest_night)}`;
  const runHeading = document.createElement("strong");
  runHeading.textContent = "Available stays";
  const runTable = makeAvailabilityTable(location, Number.POSITIVE_INFINITY, "");
  runTable.className = "availability-table-block";
  const distance = document.createElement("p");
  const distanceParts = [];
  if (location.distance_km != null) distanceParts.push(`${location.distance_km} km`);
  if (location.distance_mi != null) distanceParts.push(`${location.distance_mi} mi`);
  if (location.osrm_duration_seconds != null) {
    distanceParts.push(`${formatDriveDuration(location.osrm_duration_seconds)} drive`);
  } else if (location.est_drive_hrs != null) {
    distanceParts.push(`~${location.est_drive_hrs} h drive`);
  }
  distance.textContent = distanceParts.join(" · ");
  const driveNote = document.createElement("p");
  driveNote.className = "drive-note";
  if (location.osrm_duration_seconds != null) {
    driveNote.textContent = "OpenStreetMap/OSRM route time (no live traffic).";
  } else if (location.osrm_route_unavailable) {
    driveNote.textContent = "OpenStreetMap/OSRM did not return a drivable route for this campground.";
  } else if (location.est_drive_hrs != null) {
    driveNote.textContent = "WA/Tacoma Power park-list estimate, calculated locally from distance and average speed; not a routed time.";
  }
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
  const provider = document.createElement("p");
  provider.className = "card-provider";
  provider.textContent = `Provider: ${location.provider}`;
  cardContent.append(availability, runHeading, runTable, distance, driveNote, links, provider);
  availabilityCard.hidden = false;
  if (pin) cardPinned = true;
  updatePinAction();
  if (shouldPlace) placeCardNear(anchor);
  else if (!availabilityCard.style.left) {
    moveCard(16, map.clientHeight - availabilityCard.offsetHeight - 26);
  }
}

function closeLocation() {
  cancelCardHide();
  cardPinned = false;
  cardLocationKey = null;
  cardAnchor = null;
  availabilityCard.hidden = true;
  availabilityCard.removeAttribute("aria-labelledby");
  updatePinAction();
}

function refreshOpenCard() {
  if (availabilityCard.hidden || !cardLocationKey || !mapData) return;
  const location = visibleLocations.find(item => item.key === cardLocationKey);
  if (location) {
    const table = cardContent.querySelector(".run-table-shell");
    const scrollTop = table ? table.scrollTop : 0;
    showLocation(location, null, cardPinned);
    const refreshedTable = cardContent.querySelector(".run-table-shell");
    if (refreshedTable) refreshedTable.scrollTop = scrollTop;
    return;
  }
  if (!cardPinned) {
    closeLocation();
    return;
  }
  cardContent.replaceChildren();
  const message = document.createElement("p");
  message.textContent = "This campground no longer has qualifying availability in the selected date range.";
  cardContent.appendChild(message);
  updatePinAction();
}

function renderMarkers() {
  markers.replaceChildren();
  if (!mapData) return;
  const origin = viewportOrigin();
  visibleLocations.forEach((location, index) => {
    const point = project(location.lat, location.lon, zoom);
    const button = document.createElement("button");
    button.className = "marker";
    button.textContent = String(index + 1);
    button.setAttribute("aria-label", `${location.name}: ${location.available_sites} sites, ${location.earliest} through ${location.latest_night}`);
    button.setAttribute("aria-haspopup", "dialog");
    button.style.left = `${point.x - origin.x}px`;
    button.style.top = `${point.y - origin.y}px`;
    button.addEventListener("click", () => showLocation(location, button, true));
    button.addEventListener("pointerenter", () => showLocation(location, button));
    button.addEventListener("pointerleave", scheduleCardHide);
    button.addEventListener("focus", () => showLocation(location, button));
    button.addEventListener("blur", scheduleCardHide);
    markers.appendChild(button);
  });
}

function renderMap() {
  if (mapRenderFrame !== null) {
    cancelAnimationFrame(mapRenderFrame);
    mapRenderFrame = null;
  }
  renderTiles();
  renderMarkers();
}

function renderMapSoon() {
  if (mapRenderFrame !== null) return;
  mapRenderFrame = requestAnimationFrame(() => {
    mapRenderFrame = null;
    renderMap();
  });
}

function panBy(screenX, screenY) {
  const current = project(center.lat, center.lon, zoom);
  center = centeredAtWorld(current.x - screenX, current.y - screenY, zoom);
}

function zoomAt(clientX, clientY, amount) {
  const nextZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom + amount));
  if (nextZoom === zoom) return false;
  const rect = map.getBoundingClientRect();
  const pointX = clientX - rect.left;
  const pointY = clientY - rect.top;
  const origin = viewportOrigin();
  const scale = 2 ** (nextZoom - zoom);
  const focusedX = (origin.x + pointX) * scale;
  const focusedY = (origin.y + pointY) * scale;
  zoom = nextZoom;
  center = centeredAtWorld(
    focusedX - pointX + map.clientWidth / 2,
    focusedY - pointY + map.clientHeight / 2,
    zoom
  );
  return true;
}

function zoomAtMapCenter(amount) {
  const rect = map.getBoundingClientRect();
  return zoomAt(rect.left + map.clientWidth / 2, rect.top + map.clientHeight / 2, amount);
}

function isMapGestureTarget(target) {
  return !target.closest("button, a, input, select, textarea, #availability-card");
}

function fitAll() {
  if (!mapData) return;
  const b = visibleLocations.length ? {
    south: Math.min(...visibleLocations.map(location => location.lat)),
    west: Math.min(...visibleLocations.map(location => location.lon)),
    north: Math.max(...visibleLocations.map(location => location.lat)),
    east: Math.max(...visibleLocations.map(location => location.lon))
  } : mapData.bounds;
  for (let candidate = MAX_ZOOM - 2; candidate >= MIN_ZOOM; candidate -= 1) {
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
  showLocation(location, null, true);
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
  closeLocation();
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

function setFreshness(text, stale) {
  clearTimeout(freshnessResetTimer);
  freshness.textContent = stale ? `${text} · Click to copy refresh command` : text;
  freshness.classList.toggle("stale", stale);
  freshness.disabled = !stale;
  freshness.title = stale ? `Copy: ${FRESH_SCAN_COMMAND}` : "";
  freshness.setAttribute("aria-label", stale
    ? `${text}. Click to copy the refresh command: ${FRESH_SCAN_COMMAND}`
    : text);
}

function fallbackCopy(text) {
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.cssText = "position:fixed; opacity:0; pointer-events:none";
  document.body.appendChild(field);
  field.select();
  const copied = document.execCommand("copy");
  field.remove();
  if (!copied) throw new Error("clipboard unavailable");
}

async function copyFreshScanCommand() {
  if (!freshness.classList.contains("stale")) return;
  try {
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      await navigator.clipboard.writeText(FRESH_SCAN_COMMAND);
    } else {
      fallbackCopy(FRESH_SCAN_COMMAND);
    }
    freshness.textContent = "Refresh command copied";
    freshness.title = `Copied: ${FRESH_SCAN_COMMAND}`;
    freshnessResetTimer = setTimeout(renderStatus, 1800);
  } catch (_error) {
    freshness.textContent = `Could not copy — run: ${FRESH_SCAN_COMMAND}`;
    freshnessResetTimer = setTimeout(renderStatus, 4000);
  }
}

function renderStatus() {
  if (!mapData) return;
  const progress = mapData.progress || {};
  const parts = [];
  const updated = mapData.data_updated_at ? new Date(mapData.data_updated_at) : null;
  const ageSeconds = updated && !Number.isNaN(updated.getTime())
    ? Math.max(0, Math.floor((Date.now() - updated.getTime()) / 1000))
    : null;
  const scanMayBeStalled = progress.status === "running"
    && ageSeconds != null
    && ageSeconds > SCAN_STALLED_AFTER_SECONDS;
  if (updated && !Number.isNaN(updated.getTime())) {
    parts.push(`Data updated ${updated.toLocaleString()}`);
  } else {
    parts.push("Data update time unavailable");
  }
  if (progress.status === "running") {
    parts.push(`scan ${progress.status}: ${progress.completed ?? "?"}/${progress.total ?? "?"}`);
  } else if (progress.status === "failed") {
    parts.push(`scan failed: ${progress.completed ?? "?"}/${progress.total ?? "?"}`);
  }
  if (scanMayBeStalled) parts.push("scan may have stopped — no checkpoint for 5m");
  if (liveUpdatesDisconnected) parts.push("live map updates disconnected — reconnecting");
  if (mapData.missing_coordinates.length) parts.push(`${mapData.missing_coordinates.length} result(s) lack coordinates`);
  document.getElementById("status").textContent = parts.join(" · ");
  const ageMinutes = ageSeconds == null ? null : Math.floor(ageSeconds / 60);
  if (ageMinutes == null || Number.isNaN(ageMinutes)) {
    setFreshness("Availability freshness is unknown", true);
  } else if (progress.status === "failed" || scanMayBeStalled) {
    setFreshness("Availability scan needs attention", true);
  } else if (ageMinutes > 60) {
    const hours = Math.floor(ageMinutes / 60);
    const minutes = ageMinutes % 60;
    setFreshness(`Stale availability data: last updated ${hours}h ${minutes}m ago`, true);
  } else {
    setFreshness(`Availability data is current (${ageMinutes}m old)`, false);
  }
}

async function refreshData() {
  if (refreshInFlight) {
    refreshQueued = true;
    return;
  }
  refreshInFlight = true;
  try {
    const response = await fetch("/data.json", {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const fresh = await response.json();
    const availabilityChanged = !mapData
      || fresh.availability_fingerprint !== mapData.availability_fingerprint;
    mapData = fresh;
    renderStatus();
    if (availabilityChanged) {
      syncDateControls();
      visibleLocations = filteredLocationList();
      renderSidebar();
      if (!initialized) { initialized = true; fitAll(); }
      else {
        renderMarkers();
        refreshOpenCard();
      }
    }
  } catch (error) {
    liveUpdatesDisconnected = true;
    if (mapData) renderStatus();
    else document.getElementById("status").textContent = `Map server unavailable: ${error.name}`;
    scheduleRecoveryProbe();
  } finally {
    refreshInFlight = false;
    if (refreshQueued) {
      refreshQueued = false;
      void refreshData();
    }
  }
}

function applyProgressUpdate(update) {
  if (!mapData) return;
  mapData = {
    ...mapData,
    progress: update.progress || {},
    data_updated_at: update.data_updated_at || mapData.data_updated_at,
    progress_fingerprint: update.progress_fingerprint || mapData.progress_fingerprint,
  };
  renderStatus();
}

function handleMapUpdate(event) {
  try {
    const update = JSON.parse(event.data);
    if (!mapData || update.availability_fingerprint !== mapData.availability_fingerprint) {
      void refreshData();
    } else {
      applyProgressUpdate(update);
    }
  } catch (_error) {
    // The next server event or focus catch-up will restore a valid snapshot.
  }
}

function scheduleRecoveryProbe() {
  if (recoveryProbeTimer != null) return;
  recoveryProbeTimer = setTimeout(() => {
    recoveryProbeTimer = null;
    if (liveUpdatesDisconnected) void refreshData();
  }, RECOVERY_PROBE_MILLISECONDS);
}

function startLiveUpdates() {
  if (!("EventSource" in window)) {
    liveUpdatesDisconnected = true;
    if (mapData) renderStatus();
    else document.getElementById("status").textContent = "Live map updates are not supported by this browser.";
    scheduleRecoveryProbe();
    return;
  }
  const events = new EventSource("/events");
  events.addEventListener("map-update", handleMapUpdate);
  events.addEventListener("open", () => {
    liveUpdatesDisconnected = false;
    if (recoveryProbeTimer != null) {
      clearTimeout(recoveryProbeTimer);
      recoveryProbeTimer = null;
    }
    if (mapData) renderStatus();
  });
  events.addEventListener("error", () => {
    liveUpdatesDisconnected = true;
    if (mapData) renderStatus();
    scheduleRecoveryProbe();
  });
  window.addEventListener("beforeunload", () => events.close(), {once: true});
}

document.getElementById("zoom-in").addEventListener("click", () => { if (zoomAtMapCenter(1)) renderMap(); });
document.getElementById("zoom-out").addEventListener("click", () => { if (zoomAtMapCenter(-1)) renderMap(); });
document.getElementById("fit").addEventListener("click", fitAll);
freshness.addEventListener("click", copyFreshScanCommand);
map.addEventListener("pointerdown", event => {
  if (event.button !== 0 || !isMapGestureTarget(event.target)) return;
  map.focus({preventScroll: true});
  mapPan = {pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY};
  map.classList.add("panning");
  map.setPointerCapture(event.pointerId);
});
map.addEventListener("pointermove", event => {
  if (!mapPan || event.pointerId !== mapPan.pointerId) return;
  event.preventDefault();
  panBy(event.clientX - mapPan.clientX, event.clientY - mapPan.clientY);
  mapPan.clientX = event.clientX;
  mapPan.clientY = event.clientY;
  renderMapSoon();
});
function stopMapPan(event) {
  if (!mapPan || event.pointerId !== mapPan.pointerId) return;
  if (map.hasPointerCapture(event.pointerId)) map.releasePointerCapture(event.pointerId);
  mapPan = null;
  map.classList.remove("panning");
}
map.addEventListener("pointerup", stopMapPan);
map.addEventListener("pointercancel", stopMapPan);
map.addEventListener("dblclick", event => {
  if (!isMapGestureTarget(event.target)) return;
  event.preventDefault();
  map.focus({preventScroll: true});
  if (zoomAt(event.clientX, event.clientY, 1)) renderMap();
});
map.addEventListener("wheel", event => {
  if (!isMapGestureTarget(event.target)) return;
  event.preventDefault();
  const unit = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 40 : 1;
  wheelDelta += event.deltaY * unit;
  clearTimeout(wheelResetTimer);
  wheelResetTimer = setTimeout(() => { wheelDelta = 0; }, 180);
  let steps = 0;
  while (Math.abs(wheelDelta) >= 80) {
    steps += wheelDelta < 0 ? 1 : -1;
    wheelDelta += wheelDelta < 0 ? 80 : -80;
  }
  if (steps && zoomAt(event.clientX, event.clientY, steps)) renderMapSoon();
}, {passive: false});
map.addEventListener("keydown", event => {
  if (event.target !== map) return;
  let changed = false;
  if (event.key === "+" || event.key === "=") changed = zoomAtMapCenter(1);
  else if (event.key === "-" || event.key === "_") changed = zoomAtMapCenter(-1);
  else if (event.key === "ArrowLeft") { panBy(80, 0); changed = true; }
  else if (event.key === "ArrowRight") { panBy(-80, 0); changed = true; }
  else if (event.key === "ArrowUp") { panBy(0, 80); changed = true; }
  else if (event.key === "ArrowDown") { panBy(0, -80); changed = true; }
  else if (event.key === "Home") { fitAll(); event.preventDefault(); return; }
  if (changed) {
    event.preventDefault();
    renderMap();
  }
});
cardPin.appendChild(makeIcon("pin"));
document.getElementById("card-close").appendChild(makeIcon("close"));
cardPin.addEventListener("click", () => {
  cardPinned = !cardPinned;
  updatePinAction();
  if (!cardPinned) scheduleCardHide();
});
document.getElementById("card-close").addEventListener("click", closeLocation);
availabilityCard.addEventListener("pointerenter", cancelCardHide);
availabilityCard.addEventListener("pointerleave", scheduleCardHide);
availabilityCard.addEventListener("focusin", cancelCardHide);
availabilityCard.addEventListener("focusout", scheduleCardHide);
cardHeader.addEventListener("pointerdown", event => {
  if (event.button !== 0 || event.target.closest("button")) return;
  event.preventDefault();
  const rect = availabilityCard.getBoundingClientRect();
  const mapRect = map.getBoundingClientRect();
  cardDrag = {
    pointerId: event.pointerId,
    offsetX: event.clientX - rect.left,
    offsetY: event.clientY - rect.top,
    mapLeft: mapRect.left,
    mapTop: mapRect.top,
  };
  cardHeader.classList.add("dragging");
  cardHeader.setPointerCapture(event.pointerId);
});
cardHeader.addEventListener("pointermove", event => {
  if (!cardDrag || event.pointerId !== cardDrag.pointerId) return;
  moveCard(event.clientX - cardDrag.mapLeft - cardDrag.offsetX, event.clientY - cardDrag.mapTop - cardDrag.offsetY);
});
cardHeader.addEventListener("pointerup", event => {
  if (!cardDrag || event.pointerId !== cardDrag.pointerId) return;
  cardHeader.releasePointerCapture(event.pointerId);
  cardDrag = null;
  cardHeader.classList.remove("dragging");
  cardPinned = true;
  updatePinAction();
});
dateFrom.addEventListener("input", () => applyDateFilter(dateFrom));
dateThrough.addEventListener("input", () => applyDateFilter(dateThrough));
document.querySelectorAll("#presets button").forEach(button => {
  button.addEventListener("click", () => applyPreset(button.dataset.days));
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !availabilityCard.hidden) closeLocation();
});
window.addEventListener("resize", () => {
  if (initialized) renderMap();
  if (!availabilityCard.hidden) {
    moveCard(Number.parseFloat(availabilityCard.style.left), Number.parseFloat(availabilityCard.style.top));
  }
});
void refreshData().then(startLiveUpdates);
// This keeps the stale-data indicator honest without requesting map data.
setInterval(renderStatus, 60_000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) void refreshData();
});
