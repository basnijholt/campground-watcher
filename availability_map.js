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
const stayNights = document.getElementById("stay-nights");
const coverageNotice = document.getElementById("coverage-notice");
const freshness = document.getElementById("freshness");

function addIsoDays(value, days) {
  const date = new Date(`${value}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function dayCount(start, end) {
  return Math.round((Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86400000) + 1;
}

function minIsoDate(...values) {
  return values.filter(Boolean).sort((left, right) => left.localeCompare(right))[0] || null;
}

function maxIsoDate(...values) {
  const dates = values.filter(Boolean).sort((left, right) => left.localeCompare(right));
  return dates.length ? dates[dates.length - 1] : null;
}

function selectedStayNights() {
  const nights = Number(stayNights.value);
  return Number.isSafeInteger(nights) && nights >= 1 ? nights : null;
}

function stayLimitFor(location) {
  const limit = location.stay_limit;
  const maxNights = Number(limit?.max_nights);
  if (!Number.isInteger(maxNights) || maxNights < 1 || typeof limit?.label !== "string") return null;
  const parkWindowDays = Number(limit?.park_window_days);
  const annualMaxNights = Number(limit?.calendar_year_max_nights);
  return {
    ...limit,
    max_nights: maxNights,
    park_window_days: Number.isInteger(parkWindowDays) && parkWindowDays > 0 ? parkWindowDays : null,
    calendar_year_max_nights: Number.isInteger(annualMaxNights) && annualMaxNights > 0 ? annualMaxNights : null,
  };
}

function coverageBounds() {
  const coverage = mapData?.coverage;
  if (!coverage || typeof coverage.first_night !== "string" || typeof coverage.last_night !== "string") {
    return null;
  }
  return coverage.first_night <= coverage.last_night ? coverage : null;
}

function selectedCoverageRange() {
  const first = dateFrom.value;
  const lastCheckIn = dateThrough.value;
  if (!first || !lastCheckIn) return null;
  const nights = selectedStayNights();
  return {
    first,
    last: nights ? addIsoDays(lastCheckIn, nights - 1) : lastCheckIn,
  };
}

function coverageState() {
  const coverage = coverageBounds();
  const selected = selectedCoverageRange();
  if (!coverage || !selected) return "unknown";
  if (selected.last < coverage.first_night || selected.first > coverage.last_night) return "outside";
  if (selected.first < coverage.first_night || selected.last > coverage.last_night) return "partial";
  return "covered";
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

function availabilityDateGroups(location) {
  const grouped = new Map();
  function addObservedStay(checkIn, end, site) {
    if (!grouped.has(checkIn)) grouped.set(checkIn, new Map());
    const stays = grouped.get(checkIn);
    if (!stays.has(end)) {
      stays.set(end, {
        display_start: checkIn,
        display_end: end,
        sites: new Set(),
      });
    }
    stays.get(end).sites.add(site);
  }

  if (!location.selected_nights) {
    // A site's longer run also covers every shorter stay starting on each of
    // its open nights. Count that coverage cumulatively, rather than grouping
    // only sites whose *exact* raw run has a given end date.
    location.runs.forEach(run => {
      for (let checkIn = run.display_start; checkIn <= run.display_end; checkIn = addIsoDays(checkIn, 1)) {
        addObservedStay(checkIn, run.display_end, run.site);
      }
    });
    return [...grouped.entries()]
      .map(([checkIn, ends]) => {
        const cumulativeSites = new Set();
        const runs = [...ends.entries()]
          .sort(([left], [right]) => right.localeCompare(left))
          .map(([end, observedStay]) => {
            observedStay.sites.forEach(site => cumulativeSites.add(site));
            return {
              display_start: checkIn,
              display_end: end,
              site_count: cumulativeSites.size,
            };
          })
          .sort((left, right) => left.display_end.localeCompare(right.display_end));
        return {checkIn, runs};
      })
      .sort((left, right) => left.checkIn.localeCompare(right.checkIn));
  }

  location.runs.forEach(run => {
    for (let checkIn = run.first_check_in; checkIn <= run.last_check_in; checkIn = addIsoDays(checkIn, 1)) {
      addObservedStay(checkIn, addIsoDays(checkIn, location.selected_nights - 1), run.site);
    }
  });
  return [...grouped.entries()]
    .map(([checkIn, runs]) => ({
      checkIn,
      runs: [...runs.values()]
        .map(run => ({...run, site_count: run.sites.size}))
        .sort((left, right) => left.display_end.localeCompare(right.display_end)),
    }))
    .sort((left, right) => left.checkIn.localeCompare(right.checkIn));
}

function bookingUrlFor(location, run = null) {
  if (!location.key.startsWith("wa:")) return location.booking_url;
  try {
    const url = new URL(location.booking_url);
    if (run && url.searchParams.has("startDate")) {
      url.searchParams.set("startDate", run.display_start);
      url.searchParams.set("endDate", addIsoDays(run.display_end, 1));
      return url.href;
    }
    return location.booking_url;
  } catch (error) {
    return location.booking_url;
  }
}

function makeStayChip(location, run) {
  const nights = dayCount(run.display_start, run.display_end);
  const duration = location.selected_nights
    ? `${nights} observed night${nights === 1 ? "" : "s"}`
    : `${nights} consecutive night${nights === 1 ? "" : "s"} observed`;
  const text = `Book · ${duration} · ${run.site_count} site${run.site_count === 1 ? "" : "s"}`;
  const chip = document.createElement("a");
  chip.className = "stay-chip";
  chip.href = bookingUrlFor(location, run);
  chip.target = "_blank";
  chip.rel = "noopener";
  chip.textContent = text;
  chip.title = "Book this observed stay with the provider; availability is not guaranteed.";
  chip.setAttribute("aria-label", `${text} at ${location.name}, checking in ${formatDate(run.display_start)}. Availability is not guaranteed.`);
  return chip;
}

function makeAvailabilityTable(location, limit, moreText, groups = availabilityDateGroups(location)) {
  const container = document.createElement("div");
  const table = document.createElement("table");
  table.className = "run-table";
  table.setAttribute("aria-label", "Observed stays by check-in date");
  const caption = document.createElement("caption");
  caption.className = "sr-only";
  caption.textContent = `${location.name} availability`;
  table.appendChild(caption);
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  const headings = ["Check in", "Observed stays"];
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
  const coverage = coverageBounds();
  const bounds = coverage || availableBounds();
  if (!bounds) return;
  const first = coverage ? coverage.first_night : bounds.first;
  const last = coverage ? coverage.last_night : bounds.last;
  dateFrom.min = coverage ? first : "";
  dateFrom.max = coverage ? last : "";
  dateThrough.min = coverage ? first : "";
  dateThrough.max = coverage ? last : "";
  if (!filtersInitialized || usingAllDates) {
    dateFrom.value = first;
    dateThrough.value = last;
    filtersInitialized = true;
  }
}

function filterLocation(location) {
  const first = dateFrom.value;
  const last = dateThrough.value;
  const coverage = coverageBounds();
  const nights = selectedStayNights();
  const maxStayNights = stayLimitFor(location)?.max_nights;
  if (nights && maxStayNights && nights > maxStayNights) return null;
  const runs = [];
  location.runs.forEach(run => {
    const lastObservedNight = minIsoDate(run.last_night, coverage?.last_night);
    if (!lastObservedNight) return;
    if (!nights) {
      const displayStart = maxIsoDate(run.start, first, coverage?.first_night);
      const displayEnd = minIsoDate(lastObservedNight, last);
      if (!displayStart || !displayEnd || displayStart > displayEnd) return;
      runs.push({
        ...run,
        display_start: displayStart,
        display_end: displayEnd,
      });
      return;
    }
    const firstCheckIn = maxIsoDate(run.start, first, coverage?.first_night);
    const lastCheckIn = minIsoDate(
      last,
      addIsoDays(lastObservedNight, -(nights - 1)),
    );
    if (!firstCheckIn || !lastCheckIn || firstCheckIn > lastCheckIn) return;
    runs.push({
      ...run,
      first_check_in: firstCheckIn,
      last_check_in: lastCheckIn,
    });
  });
  if (!runs.length) return null;
  const earliest = nights
    ? runs.reduce((value, run) => run.first_check_in < value ? run.first_check_in : value, runs[0].first_check_in)
    : runs.reduce((value, run) => run.display_start < value ? run.display_start : value, runs[0].display_start);
  const latestNight = nights
    ? runs.reduce((value, run) => {
      const end = addIsoDays(run.last_check_in, nights - 1);
      return end > value ? end : value;
    }, addIsoDays(runs[0].last_check_in, nights - 1))
    : runs.reduce((value, run) => run.display_end > value ? run.display_end : value, runs[0].display_end);
  return {
    ...location,
    runs,
    selected_nights: nights,
    available_sites: new Set(runs.map(run => run.site)).size,
    available_runs: runs.length,
    earliest,
    latest_night: latestNight,
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
  // A pinned card stays with its campground until the user explicitly unpins
  // or closes it. Selection and hover must not silently replace it.
  if (cardPinned && cardLocationKey !== location.key) return;
  const shouldPlace = anchor && (availabilityCard.hidden || !cardPinned);
  cardLocationKey = location.key;
  cardAnchor = anchor;
  cardTitle.textContent = location.name;
  availabilityCard.setAttribute("aria-labelledby", "card-title");
  cardContent.replaceChildren();
  const availability = document.createElement("p");
  const stayDescription = location.selected_nights
    ? `${location.selected_nights}-night observed coverage`
    : "consecutive open nights observed";
  availability.textContent = `${location.available_sites} site${location.available_sites === 1 ? "" : "s"} with ${stayDescription} · ${formatDate(location.earliest)} – ${formatDate(location.latest_night)}`;
  const stayLimit = stayLimitFor(location);
  const policy = document.createElement("p");
  policy.className = "stay-limit";
  if (stayLimit) {
    const parkLimit = `no more than ${stayLimit.max_nights} nights in one park${stayLimit.park_window_days ? ` within ${stayLimit.park_window_days} days` : ""}`;
    const details = [parkLimit];
    if (stayLimit.calendar_year_max_nights) {
      details.push(`${stayLimit.calendar_year_max_nights} nights in all state parks per calendar year`);
    }
    policy.append(document.createTextNode(`Published ${stayLimit.label} policy: ${details.join("; ")}. `));
    if (typeof stayLimit.source_url === "string") {
      const source = document.createElement("a");
      source.href = stayLimit.source_url;
      source.target = "_blank";
      source.rel = "noopener";
      source.textContent = "Policy source";
      policy.appendChild(source);
    }
  }
  const runHeading = document.createElement("strong");
  runHeading.textContent = location.selected_nights ? "Observed coverage by check-in date" : "Observed consecutive stays";
  const groups = availabilityDateGroups(location);
  const runTable = makeAvailabilityTable(location, Number.POSITIVE_INFINITY, "", groups);
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
  const firstGroup = groups[0];
  link.href = bookingUrlFor(location, firstGroup?.runs[0]);
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "Book";
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
  const disclaimer = document.createElement("p");
  disclaimer.className = "booking-disclaimer";
  const updated = mapData?.data_updated_at ? new Date(mapData.data_updated_at) : null;
  const observedMinutes = updated && !Number.isNaN(updated.getTime())
    ? Math.max(0, Math.floor((Date.now() - updated.getTime()) / 60000))
    : null;
  const observedWhen = observedMinutes == null
    ? "at an unknown time"
    : `${observedMinutes} minute${observedMinutes === 1 ? "" : "s"} ago`;
  const hasLongDisplayedStay = groups.some(group => group.runs.some(run =>
    dayCount(run.display_start, run.display_end) > 10
  ));
  const disclaimerParts = [
    `Availability may have changed: all openings shown were observed ${observedWhen}.`,
  ];
  if (location.checked_this_scan === false) {
    disclaimerParts.push("This campground was not rechecked during the latest scan.");
  }
  if (hasLongDisplayedStay) {
    disclaimerParts.push("The provider may not support booking a reservation for a displayed stay longer than 10 nights.");
  }
  disclaimer.textContent = disclaimerParts.join(" ");
  const cardParts = [availability];
  if (stayLimit) cardParts.push(policy);
  cardParts.push(runHeading, runTable, distance, driveNote, links, provider, disclaimer);
  cardContent.append(...cardParts);
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
    const scanState = location.checked_this_scan === false ? "; not checked in the latest scan" : "";
    button.setAttribute("aria-label", `${location.name}: ${location.available_sites} sites with observed availability, ${location.earliest} through ${location.latest_night}${scanState}`);
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
  if (cardPinned && cardLocationKey !== location.key) return;
  center = {lat: location.lat, lon: location.lon};
  zoom = Math.max(zoom, 10);
  renderMap();
  showLocation(location, null, true);
}

function renderSidebar() {
  const list = document.getElementById("locations");
  const summary = document.getElementById("summary");
  list.replaceChildren();
  renderCoverageNotice();
  renderStayLimitNotice();
  const nights = selectedStayNights();
  const filterDescription = dateFrom.value && dateThrough.value
    ? ` from ${formatDate(dateFrom.value)} through ${formatDate(dateThrough.value)}`
    : "";
  const stayDescription = nights
    ? ` with a ${nights}-night stay`
    : "";
  summary.textContent = `${visibleLocations.length} of ${mapData.locations.length} campground(s) have saved observed availability${filterDescription}${stayDescription}.`;
  if (!visibleLocations.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    const state = coverageState();
    empty.textContent = state === "outside"
      ? "These dates were not checked, so availability is unknown."
      : state === "partial"
        ? "No qualifying availability was recorded in the checked part of this range; the remaining dates are unknown."
        : "No qualifying availability was recorded for this date range.";
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
    const scanState = location.checked_this_scan === false ? " · not checked in latest scan" : "";
    meta.textContent = `${location.available_sites} site(s) · ${location.earliest} → ${location.latest_night} · ${location.provider}${scanState}`;
    button.append(name, meta);
    button.addEventListener("click", () => focusLocation(location));
    list.appendChild(button);
  });
}

function renderCoverageNotice() {
  const coverage = coverageBounds();
  const state = coverageState();
  const failedCount = Array.isArray(mapData?.failed_keys) ? mapData.failed_keys.length : 0;
  coverageNotice.classList.toggle("warning", state !== "covered" || failedCount > 0);
  if (!coverage) {
    coverageNotice.textContent = "Checked-night coverage is unknown for this older scan. Dates outside recorded availability are unknown, not unavailable.";
    return;
  }
  const range = `${formatDate(coverage.first_night)} – ${formatDate(coverage.last_night)}`;
  const failedText = failedCount
    ? ` ${failedCount} campground${failedCount === 1 ? " was" : "s were"} not checked successfully; their current availability is unknown.`
    : "";
  if (state === "outside") {
    coverageNotice.textContent = `These dates are outside the checked nights (${range}); availability is unknown.${failedText}`;
  } else if (state === "partial") {
    coverageNotice.textContent = `Part of this selection is outside the checked nights (${range}). Results show only stays fully covered by the scan; un-checked dates are unknown.${failedText}`;
  } else {
    coverageNotice.textContent = `Checked nights: ${range}.${failedText}`;
  }
}

function renderStayLimitNotice() {
  const notice = document.getElementById("stay-limit-notice");
  const nights = selectedStayNights();
  const limits = new Map();
  (mapData?.locations || []).forEach(location => {
    const limit = stayLimitFor(location);
    if (limit) limits.set(`${limit.label}|${limit.max_nights}`, limit);
  });
  const exceeded = [...limits.values()].filter(limit => nights && nights > limit.max_nights);
  notice.hidden = !exceeded.length;
  if (!exceeded.length) return;
  notice.textContent = exceeded.map(limit =>
    `${limit.label} is excluded: its published limit is ${limit.max_nights} nights in one park${limit.park_window_days ? ` within ${limit.park_window_days} days` : ""}, below this ${nights}-night search.`
  ).join(" ");
}

function renderResults() {
  if (!mapData) return;
  visibleLocations = filteredLocationList();
  renderSidebar();
  renderMarkers();
  refreshOpenCard();
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
  const coverage = coverageBounds();
  const bounds = coverage
    ? {first: coverage.first_night, last: coverage.last_night}
    : availableBounds();
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
stayNights.addEventListener("input", renderResults);
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
