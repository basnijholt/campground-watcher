#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["camply"]
# ///
"""Unified campground availability watcher (recreation.gov + Washington State Parks).

Robust design:
- Drives ALL availability through camply's Python API (handles auth / encoding /
  Azure WAF for both RecreationDotGov and GoingToCamp providers).
- Monkeypatches a known camply GoingToCamp bug (_process_facilities_responses
  raises KeyError on facilities missing from campground_details).
- Applies filters: exclude group/overflow sites,
  require >= MIN_NIGHTS consecutive nights.
- Diffs vs last_state.json; appends NEW availability to alerts.jsonl.
- LLM-free; meant to run from cron every 10 min. A manual run prints a summary.

Config lives in watch_config.json (campground IDs to watch). If absent, it is
auto-built on first run from candidates.json (recreation.gov) + the curated WA
State Parks list.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import urllib.request
import sys
import traceback
from pathlib import Path

# ---- monkeypatch camply GoingToCamp bugs BEFORE importing search classes ----
# ROOT CAUSE (HTTP 400 on every WA State Park):
#   camply's _process_facilities_responses resolves a facility's mapId from the
#   CAMP_DETAILS endpoint (/api/maps). For Washington State Parks that endpoint
#   returns only 6 region-level maps, ALL with resourceLocationId == null, so the
#   per-facility lookup always yields None. MAPDATA then gets mapId=None and the
#   GoingToCamp API replies 400 Bad Request.
# FIX:
#   The correct per-facility map is facility["rootMapId"] from the
#   LIST_CAMPGROUNDS endpoint (/api/resourceLocation). We build a
#   resource_location_id -> rootMapId map once per provider instance and use it.
#   camply's own list_site_availability then traverses the child maps
#   (mapLinkAvailabilities) where the real campsite resources live.
from camply.providers.going_to_camp import going_to_camp_provider as _gtc
from camply.containers import CampgroundFacility


def _rootmap_index(self, rec_area_id):
    """resource_location_id -> rootMapId, cached on the provider instance."""
    cache = getattr(self, "_rootmap_cache", None)
    if cache is None:
        cache = self._rootmap_cache = {}
    if rec_area_id not in cache:
        idx = {}
        for fac in self._api_request(rec_area_id, "LIST_CAMPGROUNDS"):
            rli = fac.get("resourceLocationId")
            if rli is not None:
                idx[rli] = fac.get("rootMapId")
        cache[rec_area_id] = idx
    return cache[rec_area_id]


def _patched_process(self, rec_area, facility):
    # Resolve mapId from rootMapId (LIST_CAMPGROUNDS), NOT the broken
    # CAMP_DETAILS lookup that always returns None for WA State Parks.
    idx = _rootmap_index(self, facility.rec_area_id)
    facility.id = idx.get(facility.resource_location_id)
    if facility.region_name:
        formatted = f"{rec_area.recreation_area}, {facility.region_name}"
    else:
        formatted = f"{rec_area.recreation_area}"
    cg = CampgroundFacility(
        facility_name=facility.resource_location_name,
        recreation_area=formatted,
        facility_id=facility.resource_location_id,
        recreation_area_id=facility.rec_area_id,
        map_id=facility.id,
    )
    return facility, cg


_gtc.GoingToCamp._process_facilities_responses = _patched_process
# ---------------------------------------------------------------------------

# ---- direct GoingToCamp availability (bypasses camply's broken search) -------
# camply's SearchGoingToCamp.get_all_campsites() also calls get_site_details()
# against /api/resource/details, which now returns HTTP 404 (the endpoint was
# removed/moved by GoingToCamp). We do NOT need per-site details for an
# availability alert. Instead we hit MAPDATA directly:
#   1. resolve the park's rootMapId (LIST_CAMPGROUNDS)
#   2. MAPDATA on rootMapId returns child mapIds in mapLinkAvailabilities
#   3. MAPDATA on each child map (getDailyAvailability=True) returns
#      resourceAvailabilities: {resourceId: [ {availability: 1|2|3|...}, ... ]}
#      where the slot list is one entry per night in [start, end), in order.
#   availability == 2 means that night is OPEN/bookable (see _GTC_AVAILABLE).
NON_GROUP_EQUIPMENT = _gtc.NON_GROUP_EQUIPMENT

# GoingToCamp daily-availability "available" code.
# Empirically decoded 2026-06-15 by comparing in-season vs winter code
# distributions across many parks AND cross-checking a known-closed park:
#   0 = AVAILABLE (bookable)  -- present in-season, varies by date
#   1 = booked/reserved       -- present in-season, absent in far winter
#   2 = NOT OPERATING/closed  -- CONSTANT all dates (e.g. Saltwater = all 2)
#   3 = not in season / not yet released
#   4/5 = non-bookable (utility/group/special), effectively constant
# This matches camply's own check (availability == 0). A prior "fix" to ==2
# was WRONG: it reported CLOSED parks (all 2) as available. Verified against
# the live site (Saltwater showed "Not Operating", code 2).
_GTC_AVAILABLE = 0

# GoingToCamp occupancy "available" code (separate enum from MAPDATA).
#   0 = Available, 1 = Filtered, 2 = Unavailable
# The booking site's frontend uses /api/occupancy as the source of truth for
# what is actually web-bookable. A site can be MAPDATA-available (0) yet NOT
# web-bookable (walk-in / host / hold) -- occupancy is what filters those out.
# Fix discovered by codex against upstream camply (PR-style); ported here so the
# watcher stays self-contained. Verified: Fort Townsend 2026-07-03..05 phantom
# site -2147480786 is correctly excluded (matches the live booking site).
_GTC_OCCUPANCY_AVAILABLE = 0

# Register the endpoints the stock installed camply lacks (idempotent).
if "OCCUPANCY" not in _gtc.ENDPOINTS:
    _gtc.ENDPOINTS["OCCUPANCY"] = "https://{}/api/occupancy"
if "BOOKING_CATEGORIES" not in _gtc.ENDPOINTS:
    _gtc.ENDPOINTS["BOOKING_CATEGORIES"] = "https://{}/api/bookingCategories"

# Per-rec-area caches so we hit the metadata endpoints once.
_booking_categories_cache: dict = {}
_sub_equipment_cache: dict = {}


def _booking_category(provider, rec_area_id, booking_category_id=0):
    if rec_area_id not in _booking_categories_cache:
        _booking_categories_cache[rec_area_id] = provider._api_request(
            rec_area_id, "BOOKING_CATEGORIES"
        )
    for cat in _booking_categories_cache[rec_area_id]:
        if cat.get("bookingCategoryId") == booking_category_id:
            return cat
    return None


def _people_capacity_counts(provider, rec_area_id, booking_category_id=0, party_size=1):
    cat = _booking_category(provider, rec_area_id, booking_category_id)
    cap_id = (cat or {}).get("capacityCategoryId")
    if cap_id is None:
        return []
    return [{"capacityCategoryId": cap_id, "count": party_size}]


def _default_sub_equipment(provider, rec_area_id):
    if rec_area_id not in _sub_equipment_cache:
        cats = provider._api_request(rec_area_id, "LIST_EQUIPMENT")
        non_group = next(
            (c for c in cats if c.get("equipmentCategoryId") == NON_GROUP_EQUIPMENT),
            None,
        )
        sub = None
        if non_group:
            subs = non_group.get("subEquipmentCategories") or []
            if subs:
                sub = subs[0].get("subEquipmentCategoryId")
        _sub_equipment_cache[rec_area_id] = sub
    return _sub_equipment_cache[rec_area_id]


def _bookable_resource_ids(provider, rec_area_id, fid, start, end):
    """Return the set of resourceIds that /api/occupancy says are web-bookable.

    Returns None if occupancy is unavailable (then we fall back to MAPDATA-only,
    logging so we know the result is unverified).
    """
    occ_filter = {
        "bookingCategoryId": 0,
        "equipmentCategoryId": NON_GROUP_EQUIPMENT,
        "subEquipmentCategoryId": _default_sub_equipment(provider, rec_area_id) or "",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "filterData": "[]",
        "boatLength": 0,
        "boatDraft": 0,
        "boatWidth": 0,
        "peopleCapacityCategoryCounts": json.dumps(
            _people_capacity_counts(provider, rec_area_id)
        ),
        "numEquipment": 1,
        "resourceLocationId": fid,
        "cartUid": "",
        "cartTransactionUid": "",
        "bookingUid": "",
        "groupHoldUid": "",
    }
    occ = provider._api_request(rec_area_id, "OCCUPANCY", occ_filter)
    return {
        str(ro["resourceId"])
        for ro in (occ.get("resourceOccupancy") or [])
        if ro.get("availability") == _GTC_OCCUPANCY_AVAILABLE
    }


def _gtc_mapdata(provider, rec_area_id, map_id, fid, start, end):
    search_filter = {
        "mapId": map_id,
        "resourceLocationId": fid,
        "bookingCategoryId": 0,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "isReserving": True,
        "getDailyAvailability": True,
        "partySize": 1,
        "numEquipment": 1,
        "equipmentCategoryId": NON_GROUP_EQUIPMENT,
        "filterData": [],
    }
    return provider._api_request(rec_area_id, "MAPDATA", search_filter)


def gtc_available_nights(rec_area_id, fid, start, end, root_map_id=None):
    """Return {resource_label: set(date)} of open nights for a WA park facility.

    Bypasses camply's broken get_site_details(); uses MAPDATA directly.
    root_map_id may be supplied (from config) to skip the per-area index lookup.
    """
    import datetime as _dt

    provider = _gtc.GoingToCamp()
    if root_map_id is None:
        idx = _rootmap_index(provider, rec_area_id)
        root_map_id = idx.get(fid)
    if root_map_id is None:
        raise ValueError(f"no rootMapId for facility {fid}")

    n_days = (end - start).days

    # ---- Step 1: MAPDATA sweep over the whole window (cheap, gives candidates).
    # MAPDATA availability == 0 is NECESSARY but NOT SUFFICIENT -- it also flags
    # walk-in / host / non-web-bookable sites as 0. We treat these as candidates
    # only and confirm each one via /api/occupancy below.
    cand: dict[str, set] = {}  # resourceId(str) -> set(open dates)
    root = _gtc_mapdata(provider, rec_area_id, root_map_id, fid, start, end)
    child_map_ids = list((root.get("mapLinkAvailabilities") or {}).keys())
    maps_to_scan = [root_map_id] + [int(c) for c in child_map_ids]
    for mid in maps_to_scan:
        res = root if mid == root_map_id else _gtc_mapdata(
            provider, rec_area_id, mid, fid, start, end
        )
        for rid, slots in (res.get("resourceAvailabilities") or {}).items():
            open_dates = {
                start + _dt.timedelta(days=i)
                for i, slot in enumerate(slots[:n_days])
                if slot.get("availability") == _GTC_AVAILABLE
            }
            if open_dates:
                cand.setdefault(str(rid), set()).update(open_dates)
    if not cand:
        return {}

    # ---- Step 2: occupancy-verify per consecutive run.
    # /api/occupancy is the booking site's source of truth for web-bookability,
    # but it must be queried per *stay window* (a 90-day occupancy query returns
    # nothing). So for each candidate site's consecutive run (>= MIN_NIGHTS), do
    # one short-window occupancy call and keep the run only if the site is in the
    # bookable set for that exact window. Results cached per window to dedupe.
    occ_cache: dict[tuple, set] = {}

    def occ_set(w_start, w_end):
        ck = (w_start, w_end)
        if ck not in occ_cache:
            try:
                occ_cache[ck] = _bookable_resource_ids(
                    provider, rec_area_id, fid, w_start, w_end
                )
            except Exception as e:  # noqa: BLE001
                log(f"  WARN occupancy check failed for facility {fid}: {e}")
                occ_cache[ck] = None  # None = could not verify
        return occ_cache[ck]

    by_site: dict[str, set] = {}
    for rid, dates in cand.items():
        verified: set = set()
        for run_start, length in consecutive_runs(dates):
            if length < MIN_NIGHTS:
                continue
            w_end = run_start + _dt.timedelta(days=length)
            ok = occ_set(run_start, w_end)
            # ok is None => occupancy unavailable; fall back to MAPDATA (keep).
            if ok is None or rid in ok:
                for i in range(length):
                    verified.add(run_start + _dt.timedelta(days=i))
        if verified:
            label = f"{abs(int(rid)) % 100000}"
            by_site.setdefault(label, set()).update(verified)
    return by_site
# ---------------------------------------------------------------------------

from camply.containers import SearchWindow  # noqa: E402
from camply.search import SearchRecreationDotGov  # noqa: E402

HERE = Path(__file__).parent
CANDIDATES = HERE / "candidates.json"
CONFIG = HERE / "watch_config.json"
STATE_FILE = HERE / "last_state.json"
ALERTS = HERE / "alerts.jsonl"
LOG = HERE / "watch.log"
SENT_PINGS = HERE / "sent_pings.json"  # ledger of trigger event-ids already sent
# Suppress re-sending the same opening (same event-id) within this many hours.
# WA park availability flaps (a site appears/disappears across runs), which would
# otherwise re-notify the same opening every cycle. This avoids wasted sends.
PING_SUPPRESS_HOURS = 24

# ---- watch window + rules ----
WINDOW_DAYS = 90
MIN_NIGHTS = 2

# ---- instant notifications via a generic webhook ----
# On each new occupancy-verified opening, POST a JSON payload to an optional
# webhook URL. Set CAMPWATCH_WEBHOOK_URL to a Discord/Slack/ntfy/Telegram/
# custom endpoint. If unset, notifications are skipped (the watcher still
# writes openings to alerts.jsonl and you can read them with report.py /
# weekend.py). Change-only + idempotent + fails soft; never raises.
NOTIFY_ENABLED = os.environ.get("CAMPWATCH_NOTIFY", "1") != "0"
WEBHOOK_URL = os.environ.get("CAMPWATCH_WEBHOOK_URL", "")
# Optional: send the human-readable text under this JSON key (Discord uses
# "content", Slack uses "text", ntfy uses "message"). Default "content".
WEBHOOK_TEXT_KEY = os.environ.get("CAMPWATCH_WEBHOOK_TEXT_KEY", "content")

GROUP_MARKERS = ("GROUP", "GRP", "HORSE CAMP", "GROUP SITE", "GROUP CAMP", "OVERFLOW")

# ---- target weekends filter ----
# Only alert for runs that cover one of these specific weekends. Each weekend is
# defined by the set of *nights* (a night is identified by its check-in date)
# that a stay must include to count. A standard Fri->Sun weekend booking is the
# Friday night + the Saturday night (check in Fri, check out Sun = 2 nights).
# A run [start, start+nights) qualifies if it contains ALL required nights of
# any single target weekend.
# Set TARGET_WEEKENDS = None to disable the filter (alert on everything again).
# All target weekends booked as of 2026-06-24 -> nothing left to watch.
# The watcher still runs (timer stays active) but with an empty target list it
# matches nothing and stays silent. Add weekends back here to resume alerts.
#   Jul 10-12    -> Manchester Site 43    (res IWWA26-5638176B1)
#   Jul 17-19    -> Twin Harbors Site 31  (res IWWA26-5638263B1, Thu Jul 16-Sun Jul 19)
#   Jul 31-Aug 2 -> Jarrell Cove Site 21  (res IWWA26-5637427B1)
TARGET_WEEKENDS: list = [
    # (label, [required night check-in dates])  Fri + Sat nights of a Fri->Sun stay.
    # PAUSED 2026-06-25: Aug 7-9 has abundant single-site inventory (Dash Point,
    # Camano, Penrose, Joemma, Fort Ebey, Illahee all open). Muted per-site pings
    # while Bas decides between Dash Point vs Camano. Re-add the line below to resume:
    #   ("Aug 7-9", [dt.date(2026, 8, 7), dt.date(2026, 8, 8)]),
]


def covers_target_weekend(run_start: "dt.date", nights: int):
    """Return the matching weekend label if the run covers a target weekend.

    A run occupies nights [run_start, run_start + nights). It matches a target
    weekend when every required night of that weekend falls inside the run.
    Returns the weekend label string, or None if no target weekend is covered.
    """
    if TARGET_WEEKENDS is None:
        return "all"
    run_nights = {run_start + dt.timedelta(days=i) for i in range(nights)}
    for label, required in TARGET_WEEKENDS:
        if all(n in run_nights for n in required):
            return label
    return None

# Washington State Parks (+ Tacoma Power) campgrounds within ~2hr drive.
# Built by build_wa_parks.py -> wa_parks.json (ALL qualifying parks, not a
# hand-picked subset). Each entry has facility_id, rec_area_id, name,
# root_map_id, est_drive_hrs.
WA_PARKS_FILE = Path(__file__).parent / "wa_parks.json"


def log(msg: str):
    ts = dt.datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    print(line)
    with LOG.open("a") as f:
        f.write(line + "\n")


def _load_sent_pings() -> dict:
    """Load the ledger of {event_id: iso_timestamp} of pings already sent."""
    if SENT_PINGS.exists():
        try:
            return json.loads(SENT_PINGS.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _record_sent_ping(ledger: dict, event_id: str):
    """Record event_id as sent now and prune entries older than the suppress
    window so the ledger does not grow unbounded."""
    now = dt.datetime.now()
    ledger[event_id] = now.isoformat(timespec="seconds")
    cutoff = now - dt.timedelta(hours=PING_SUPPRESS_HOURS)
    pruned = {
        k: v
        for k, v in ledger.items()
        if dt.datetime.fromisoformat(v) >= cutoff
    }
    try:
        SENT_PINGS.write_text(json.dumps(pruned, indent=2))
    except Exception:  # noqa: BLE001
        pass


def _recently_sent(ledger: dict, event_id: str) -> bool:
    """True if event_id was sent within PING_SUPPRESS_HOURS."""
    ts = ledger.get(event_id)
    if not ts:
        return False
    try:
        when = dt.datetime.fromisoformat(ts)
    except Exception:  # noqa: BLE001
        return False
    return (dt.datetime.now() - when) < dt.timedelta(hours=PING_SUPPRESS_HOURS)


def send_trigger(name: str, url: str, new_runs: list[str], runs: list[dict]) -> bool:
    """Send one instant notification for a campground's NEW openings.

    Fires ONLY from the new-alert path (real change). Idempotent: the event-id
    is a stable hash of the campground key + the sorted new-run keys, so the
    same batch of openings will not double-notify even across runs. Suppresses
    re-notifying flapping openings within a 24h window. Fails soft: on any error
    it logs a WARN and returns False, leaving the opening recorded in
    alerts.jsonl. Never raises.
    """
    if not NOTIFY_ENABLED or not WEBHOOK_URL:
        return False

    # Stable idempotency id from the exact set of new runs.
    digest = hashlib.sha256(
        (name + "|" + "|".join(sorted(new_runs))).encode()
    ).hexdigest()[:16]
    event_id = f"camp-{digest}"

    # Suppress re-notifying the same opening within the suppress window (WA park
    # availability flaps, which would otherwise re-fire every cycle).
    ledger = _load_sent_pings()
    if _recently_sent(ledger, event_id):
        log(f"  (suppressed re-notify for {name}, event-id {event_id} sent recently)")
        return False

    # Build a human message: one line per open SITE, each with its own deep
    # link (pre-filled with the stay dates) so you can jump straight to booking.
    # new_runs are "site|start|nights" keys.
    by_key = {f"{r['site']}|{r['start']}|{r['nights']}": r for r in runs}
    lines = []
    sites_data = []
    for k in new_runs:
        r = by_key.get(k)
        if not r:
            continue
        wk = f" [{r['weekend']}]" if r.get("weekend") and r["weekend"] != "all" else ""
        link = r.get("booking_url") or url
        lines.append(
            f"\u2022 Site {r['site']}: {r['nights']} nights from {r['start']}{wk}\n  {link}"
        )
        sites_data.append(
            {
                "site": r["site"],
                "start": r["start"],
                "nights": r["nights"],
                "weekend": r.get("weekend"),
                "booking_url": link,
            }
        )
    detail = "\n".join(lines) if lines else "; ".join(new_runs)
    message = f"\U0001f3d5\ufe0f NEW availability at {name}\n{detail}"

    payload = {
        WEBHOOK_TEXT_KEY: message,
        "campground": name,
        "url": url,
        "sites": sites_data,
        "new_runs": new_runs,
        "event_id": event_id,
    }
    try:
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            if resp.status >= 300:
                log(f"  WARN webhook rc={resp.status} for {name}")
                return False
    except Exception as e:  # noqa: BLE001
        emsg = str(e).replace("\n", " ")[:200]
        log(f"  WARN webhook send failed for {name}: {emsg}")
        return False
    _record_sent_ping(ledger, event_id)
    log(f"  -> notification sent for {name} (event-id {event_id})")
    return True


# Equipment ids for a standard (non-group) campsite booking deep link.
GTC_EQUIPMENT_ID = -32768  # NON_GROUP_EQUIPMENT
GTC_DOMAIN = "washington.goingtocamp.com"


def gtc_booking_url(map_id, facility_id, start: str, nights: int) -> str:
    """Build a WA GoingToCamp deep link that opens the park's results page
    pre-filled with the stay dates (same format camply's get_reservation_link
    produces). `start` is an ISO date string; end = start + nights."""
    s = dt.date.fromisoformat(start)
    e = s + dt.timedelta(days=nights)
    return (
        f"https://{GTC_DOMAIN}/create-booking/results?mapId={map_id}"
        f"&bookingCategoryId=0"
        f"&startDate={s.isoformat()}"
        f"&endDate={e.isoformat()}"
        f"&isReserving=true"
        f"&equipmentId={GTC_EQUIPMENT_ID}"
        f"&subEquipmentId={GTC_EQUIPMENT_ID}"
        f"&partySize=1"
        f"&resourceLocationId={facility_id}"
    )


def recgov_booking_url(cid, start: str, nights: int) -> str:
    """recreation.gov campground availability page deep link. The site does not
    accept site-level deep links via querystring reliably, but the availability
    tab is the actionable landing page for booking."""
    return f"https://www.recreation.gov/camping/campgrounds/{cid}/availability"


def is_group(name: str, site_type: str = "") -> bool:
    blob = f"{name} {site_type}".upper()
    return any(m in blob for m in GROUP_MARKERS)


def build_config():
    cfg = {"recdotgov": [], "going_to_camp": []}
    if CANDIDATES.exists():
        for c in json.loads(CANDIDATES.read_text()):
            if is_group(c.get("name", "")):
                continue
            cfg["recdotgov"].append(
                {
                    "id": int(c["id"]),
                    "name": c["name"],
                    "rating": c.get("rating"),
                    "dist_mi": c.get("dist_mi"),
                }
            )
    if WA_PARKS_FILE.exists():
        for p in json.loads(WA_PARKS_FILE.read_text()):
            cfg["going_to_camp"].append(
                {
                    "id": p["facility_id"],
                    "name": p["name"],
                    "rec_area": p["rec_area_id"],
                    "est_drive_hrs": p.get("est_drive_hrs"),
                    "root_map_id": p.get("root_map_id"),
                }
            )
    CONFIG.write_text(json.dumps(cfg, indent=2))
    return cfg


def consecutive_runs(dates):
    if not dates:
        return []
    s = sorted(dates)
    runs, run_start, prev, length = [], s[0], s[0], 1
    for d in s[1:]:
        if (d - prev).days == 1:
            length += 1
        else:
            runs.append((run_start, length))
            run_start, length = d, 1
        prev = d
    runs.append((run_start, length))
    return runs


def collect(search):
    """Run a camply search, return {site_name: set(dates)} of Available nights,
    excluding group/overflow site types."""
    by_site: dict[str, set] = {}
    matches = search.get_matching_campsites(log=False, verbose=False)
    for m in matches:
        stype = (m.campsite_type or "").upper()
        sname = m.campsite_site_name or "site"
        if is_group(sname, stype):
            continue
        d = m.booking_date
        if isinstance(d, dt.datetime):
            d = d.date()
        by_site.setdefault(sname, set()).add(d)
    return by_site


def main():
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else build_config()
    start = dt.date.today()
    end = start + dt.timedelta(days=WINDOW_DAYS)
    sw = SearchWindow(start_date=start, end_date=end)
    log(
        f"Watching {len(cfg['recdotgov'])} rec.gov + "
        f"{len(cfg['going_to_camp'])} WA-State-Park campgrounds; "
        f"window {start}..{end}, min {MIN_NIGHTS} nights"
    )

    prev_state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    new_state, summary, new_alerts = {}, [], []

    def process(key, name, rating, dist_mi, by_site, url, link_fn=None):
        runs = []
        for sname, ds in by_site.items():
            for rs, length in consecutive_runs(ds):
                if length < MIN_NIGHTS:
                    continue
                wk = covers_target_weekend(rs, length)
                if wk is None:
                    continue
                run = {
                    "site": sname,
                    "start": rs.isoformat(),
                    "nights": length,
                    "weekend": wk,
                }
                if link_fn is not None:
                    try:
                        run["booking_url"] = link_fn(sname, rs.isoformat(), length)
                    except Exception:  # noqa: BLE001
                        run["booking_url"] = url
                runs.append(run)
        if not runs:
            new_state[key] = []
            return
        runs.sort(key=lambda r: (r["start"], r["site"]))
        run_keys = sorted(f"{r['site']}|{r['start']}|{r['nights']}" for r in runs)
        new_state[key] = run_keys
        fresh = [k for k in run_keys if k not in set(prev_state.get(key, []))]
        entry = {
            "key": key, "name": name, "rating": rating, "dist_mi": dist_mi,
            "runs": runs, "url": url,
        }
        summary.append(entry)
        if fresh:
            a = dict(entry)
            a["new_runs"] = fresh
            a["detected_at"] = dt.datetime.now().isoformat(timespec="seconds")
            new_alerts.append(a)

    # --- recreation.gov ---
    for cg in cfg["recdotgov"]:
        cid = cg["id"]
        try:
            s = SearchRecreationDotGov(
                search_window=sw, campgrounds=[cid], verbose=False
            )
            by_site = collect(s)
        except IndexError:
            # Non-reservable / facility-only duplicate IDs (no availability map).
            # Harmless; skip quietly so logs stay clean.
            new_state[f"rg:{cid}"] = prev_state.get(f"rg:{cid}", [])
            continue
        except Exception as e:  # noqa: BLE001
            log(f"  WARN rec.gov #{cid} {cg['name']}: {e}")
            new_state[f"rg:{cid}"] = prev_state.get(f"rg:{cid}", [])
            continue
        process(
            f"rg:{cid}", cg["name"], cg.get("rating"), cg.get("dist_mi"), by_site,
            f"https://www.recreation.gov/camping/campgrounds/{cid}",
            link_fn=lambda site, start, nights, _cid=cid: recgov_booking_url(
                _cid, start, nights
            ),
        )

    # --- Washington State Parks (GoingToCamp, direct MAPDATA) ---
    for cg in cfg["going_to_camp"]:
        fid = cg["id"]
        if cg.get("root_map_id") is None:
            # Non-campground facility (no reservable map); nothing to watch.
            new_state[f"wa:{fid}"] = prev_state.get(f"wa:{fid}", [])
            continue
        try:
            by_site = gtc_available_nights(
                cg["rec_area"], fid, start, end, root_map_id=cg.get("root_map_id")
            )
        except Exception as e:  # noqa: BLE001
            # Cap the error text: GoingToCamp's Azure WAF returns multi-KB HTML
            # error pages on intermittent 403s, which would flood the log.
            emsg = str(e).replace("\n", " ")[:160]
            log(f"  WARN WA park {cg['name']} ({fid}): {emsg}")
            new_state[f"wa:{fid}"] = prev_state.get(f"wa:{fid}", [])
            continue
        _map_id = cg.get("root_map_id")
        process(
            f"wa:{fid}", cg["name"] + " (WA State Park)", None, None, by_site,
            "https://washington.goingtocamp.com",
            link_fn=lambda site, start, nights, _m=_map_id, _f=fid: gtc_booking_url(
                _m, _f, start, nights
            ),
        )

    STATE_FILE.write_text(json.dumps(new_state, indent=2))
    if new_alerts:
        with ALERTS.open("a") as f:
            for a in new_alerts:
                f.write(json.dumps(a) + "\n")
        log(f"!! {len(new_alerts)} campground(s) with NEW availability -> alerts.jsonl")
        # Instant Matrix ping per campground with new openings. Change-only:
        # this branch runs ONLY when there is genuinely new availability.
        for a in new_alerts:
            send_trigger(a["name"], a["url"], a["new_runs"], a["runs"])
    else:
        log("No new availability since last check.")

    # Human summary (manual run)
    print("\n=== CURRENT QUALIFYING AVAILABILITY ===")
    if not summary:
        print("(none right now within the window/filters)")
    for e in sorted(summary, key=lambda x: (x["dist_mi"] or 9999, x["name"])):
        rt = f"{e['rating']:.1f}*" if e["rating"] else ""
        dm = f"{e['dist_mi']}mi " if e["dist_mi"] else ""
        print(f"\n⛺ {e['name']}  {dm}{rt}\n   {e['url']}")
        for r in e["runs"][:6]:
            wk = f" [{r['weekend']}]" if r.get("weekend") and r["weekend"] != "all" else ""
            print(f"     site {r['site']}: {r['nights']} nights from {r['start']}{wk}")
            if r.get("booking_url"):
                print(f"        {r['booking_url']}")
        if len(e["runs"]) > 6:
            print(f"     ... +{len(e['runs']) - 6} more runs")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)