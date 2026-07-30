#!/usr/bin/env python3
"""Unified campground availability watcher (recreation.gov + Washington State Parks).

Robust design:
- Uses only the Python standard library; nothing is downloaded or installed.
- Calls the public Recreation.gov and GoingToCamp HTTPS endpoints directly.
- Applies filters: exclude group/overflow sites,
  require >= MIN_NIGHTS consecutive nights.
- Diffs vs last_state.json; appends NEW availability to alerts.jsonl.
- LLM-free; its scheduler uses adaptive polling. A manual run prints a summary.

Config lives in watch_config.json (campground IDs to watch). If absent, it is
auto-built on first run from candidates.json (recreation.gov) + the curated WA
State Parks list.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
import socket
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from campwatch_http import (
    GTC_HOSTS,
    GoingToCampClient,
    HttpRequestError,
    RecreationGovClient,
    atomic_write_json,
)

# ---- direct GoingToCamp availability -----------------------------------------
# We do not need per-site details for an availability alert. Instead:
#   1. resolve the park's rootMapId (LIST_CAMPGROUNDS)
#   2. MAPDATA on rootMapId returns child mapIds in mapLinkAvailabilities
#   3. MAPDATA on each child map (getDailyAvailability=True) returns
#      resourceAvailabilities: {resourceId: [ {availability: 1|2|3|...}, ... ]}
#      where the slot list is one entry per night in [start, end), in order.
#   availability == 0 means that night is a candidate; occupancy confirms it.
NON_GROUP_EQUIPMENT = -32768

# GoingToCamp daily-availability "available" code.
# Empirically decoded 2026-06-15 by comparing in-season vs winter code
# distributions across many parks AND cross-checking a known-closed park:
#   0 = AVAILABLE (bookable)  -- present in-season, varies by date
#   1 = booked/reserved       -- present in-season, absent in far winter
#   2 = NOT OPERATING/closed  -- CONSTANT all dates (e.g. Saltwater = all 2)
#   3 = not in season / not yet released
#   4/5 = non-bookable (utility/group/special), effectively constant
# A prior interpretation of code 2 was wrong: it reported closed parks as
# available. The booking UI and occupancy endpoint confirm that code 0 is open.
_GTC_AVAILABLE = 0

# GoingToCamp occupancy "available" code (separate enum from MAPDATA).
#   0 = Available, 1 = Filtered, 2 = Unavailable
# The booking site's frontend uses /api/occupancy as the source of truth for
# what is actually web-bookable. A site can be MAPDATA-available (0) yet NOT
# web-bookable (walk-in / host / hold) -- occupancy is what filters those out.
_GTC_OCCUPANCY_AVAILABLE = 0

# Per-rec-area caches so we hit the metadata endpoints once.
_booking_categories_cache: dict = {}
_sub_equipment_cache: dict = {}


class AvailabilityVerificationError(RuntimeError):
    """The authoritative occupancy endpoint could not verify a park."""


def _rootmap_index(client, rec_area_id):
    return {
        fac.get("resourceLocationId"): fac.get("rootMapId")
        for fac in client.request_json(rec_area_id, "LIST_CAMPGROUNDS")
        if fac.get("resourceLocationId") is not None
    }


def _booking_category(client, rec_area_id, booking_category_id=0):
    if rec_area_id not in _booking_categories_cache:
        _booking_categories_cache[rec_area_id] = client.request_json(
            rec_area_id, "BOOKING_CATEGORIES"
        )
    for cat in _booking_categories_cache[rec_area_id]:
        if cat.get("bookingCategoryId") == booking_category_id:
            return cat
    return None


def _people_capacity_counts(client, rec_area_id, booking_category_id=0, party_size=1):
    cat = _booking_category(client, rec_area_id, booking_category_id)
    cap_id = (cat or {}).get("capacityCategoryId")
    if cap_id is None:
        return []
    return [{"capacityCategoryId": cap_id, "count": party_size}]


def _default_sub_equipment(client, rec_area_id):
    if rec_area_id not in _sub_equipment_cache:
        cats = client.request_json(rec_area_id, "LIST_EQUIPMENT")
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


def _bookable_resource_ids(client, rec_area_id, fid, start, end):
    """Return resourceIds that the authoritative occupancy API says are bookable."""
    occ_filter = {
        "bookingCategoryId": 0,
        "equipmentCategoryId": NON_GROUP_EQUIPMENT,
        "subEquipmentCategoryId": _default_sub_equipment(client, rec_area_id) or "",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "filterData": "[]",
        "boatLength": 0,
        "boatDraft": 0,
        "boatWidth": 0,
        "peopleCapacityCategoryCounts": json.dumps(
            _people_capacity_counts(client, rec_area_id)
        ),
        "numEquipment": 1,
        "resourceLocationId": fid,
        "cartUid": "",
        "cartTransactionUid": "",
        "bookingUid": "",
        "groupHoldUid": "",
    }
    occ = client.request_json(rec_area_id, "OCCUPANCY", occ_filter)
    if not isinstance(occ, dict) or not isinstance(occ.get("resourceOccupancy"), list):
        raise AvailabilityVerificationError("occupancy returned invalid data")
    return {
        str(ro["resourceId"])
        for ro in occ["resourceOccupancy"]
        if ro.get("availability") == _GTC_OCCUPANCY_AVAILABLE
    }


def _gtc_mapdata(client, rec_area_id, map_id, fid, start, end):
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
    result = client.request_json(rec_area_id, "MAPDATA", search_filter)
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("resourceAvailabilities"), dict)
        or not isinstance(result.get("mapLinkAvailabilities"), dict)
    ):
        raise HttpRequestError("GoingToCamp MAPDATA returned invalid data")
    return result


def gtc_available_nights(rec_area_id, fid, start, end, root_map_id=None, client=None):
    """Return {resource_label: set(date)} of open nights for a WA park facility.

    Uses MAPDATA directly. root_map_id may be supplied from config to skip the
    per-area index lookup.
    """
    client = client or GoingToCampClient()
    if root_map_id is None:
        idx = _rootmap_index(client, rec_area_id)
        root_map_id = idx.get(fid)
    if root_map_id is None:
        raise ValueError(f"no rootMapId for facility {fid}")

    n_days = (end - start).days

    # ---- Step 1: MAPDATA sweep over the whole window (cheap, gives candidates).
    # MAPDATA availability == 0 is NECESSARY but NOT SUFFICIENT -- it also flags
    # walk-in / host / non-web-bookable sites as 0. We treat these as candidates
    # only and confirm each one via /api/occupancy below.
    cand: dict[str, set] = {}  # resourceId(str) -> set(open dates)
    root = _gtc_mapdata(client, rec_area_id, root_map_id, fid, start, end)
    child_map_ids = list((root.get("mapLinkAvailabilities") or {}).keys())
    if len(child_map_ids) > 1000:
        raise HttpRequestError("GoingToCamp returned too many child maps")
    maps_to_scan = [root_map_id] + [int(c) for c in child_map_ids]
    for mid in maps_to_scan:
        res = root if mid == root_map_id else _gtc_mapdata(
            client, rec_area_id, mid, fid, start, end
        )
        for rid, slots in (res.get("resourceAvailabilities") or {}).items():
            open_dates = {
                start + dt.timedelta(days=i)
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
    occ_cache: dict[tuple, set[str]] = {}

    def occ_set(w_start, w_end):
        ck = (w_start, w_end)
        if ck not in occ_cache:
            try:
                occ_cache[ck] = _bookable_resource_ids(
                    client, rec_area_id, fid, w_start, w_end
                )
            except Exception as exc:  # noqa: BLE001
                raise AvailabilityVerificationError(
                    f"occupancy verification failed ({type(exc).__name__})"
                ) from exc
        return occ_cache[ck]

    by_site: dict[str, set] = {}
    for rid, dates in cand.items():
        verified: set = set()
        for run_start, length in consecutive_runs(dates):
            if length < MIN_NIGHTS:
                continue
            w_end = run_start + dt.timedelta(days=length)
            ok = occ_set(run_start, w_end)
            if rid in ok:
                for i in range(length):
                    verified.add(run_start + dt.timedelta(days=i))
        if verified:
            by_site.setdefault(str(rid), set()).update(verified)
    return by_site
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent
CANDIDATES = HERE / "candidates.json"
CONFIG = HERE / "watch_config.json"
STATE_FILE = HERE / "last_state.json"
COMPLETE_STATE_FILE = HERE / "last_complete_state.json"
SCAN_PROGRESS = HERE / "scan_progress.json"
ALERTS = HERE / "alerts.jsonl"
SENT_PINGS = HERE / "sent_pings.json"  # ledger of trigger event-ids already sent
TARGETS_FILE = HERE / "watch_targets.json"  # private, gitignored trip dates
SCHEDULE_STATE = HERE / "schedule_state.json"
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

# ---- private target-weekend filter ------------------------------------------
# watch_targets.json is intentionally ignored by Git so future travel dates do
# not end up in source control.  An empty/missing file pauses all network polls.
def load_target_weekends(path: Path = TARGETS_FILE):
    if not path.exists():
        return []
    if path.is_symlink():
        raise ValueError("watch_targets.json must not be a symlink")
    os.chmod(path, 0o600)
    raw = json.loads(path.read_text())
    if raw.get("watch_all") is True:
        return None
    weekends = []
    for item in raw.get("weekends", []):
        label = str(item["label"]).strip()
        nights = [dt.date.fromisoformat(value) for value in item["nights"]]
        if not label or not nights:
            raise ValueError("each target weekend needs a label and at least one night")
        weekends.append((label, nights))
    return weekends


TARGET_WEEKENDS = load_target_weekends()


def recommended_poll_minutes(today: dt.date | None = None):
    """Adaptive interval: near trips get fast polls; no targets means paused."""
    today = today or dt.date.today()
    if TARGET_WEEKENDS is None:
        return 15
    future_nights = [
        night
        for _, required in TARGET_WEEKENDS
        for night in required
        if night >= today
    ]
    if not future_nights:
        return None
    days = (min(future_nights) - today).days
    if days <= 7:
        return 10
    if days <= 30:
        return 30
    return 60


def targets_in_watch_window(today: dt.date | None = None) -> bool:
    today = today or dt.date.today()
    if TARGET_WEEKENDS is None:
        return True
    end = today + dt.timedelta(days=WINDOW_DAYS)
    return any(
        required and all(today <= night < end for night in required)
        for _, required in TARGET_WEEKENDS
    )


def _target_fingerprint() -> str:
    content = TARGETS_FILE.read_bytes() if TARGETS_FILE.exists() else b"missing"
    return hashlib.sha256(content).hexdigest()[:16]


def scheduled_poll_due(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now().astimezone()
    if not SCHEDULE_STATE.exists():
        return True
    try:
        state = json.loads(SCHEDULE_STATE.read_text())
        if state.get("target_fingerprint") != _target_fingerprint():
            return True
        return now >= dt.datetime.fromisoformat(state["next_poll_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return True


def record_next_poll(minutes: int, now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now().astimezone()
    atomic_write_json(
        SCHEDULE_STATE,
        {
            "last_success_at": now.isoformat(timespec="seconds"),
            "next_poll_at": (now + dt.timedelta(minutes=minutes)).isoformat(
                timespec="seconds"
            ),
            "interval_minutes": minutes,
            "target_fingerprint": _target_fingerprint(),
        },
    )


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
    print(f"[{ts}] {msg}", flush=True)


def secure_append_jsonl(path: Path, values: list[dict]) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a") as handle:
        for value in values:
            handle.write(json.dumps(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_state_checkpoint(
    previous: dict, current: dict, *, complete: bool = False
) -> None:
    """Persist completed targets without discarding unprocessed prior results."""
    snapshot = dict(current) if complete else {**previous, **current}
    atomic_write_json(STATE_FILE, snapshot)


def _load_complete_state() -> dict:
    """Load the stable alert baseline, migrating an older final state once."""
    if COMPLETE_STATE_FILE.exists():
        return json.loads(COMPLETE_STATE_FILE.read_text())
    previous = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    atomic_write_json(COMPLETE_STATE_FILE, previous)
    return previous


def _write_scan_progress(
    *,
    status: str,
    started_at: str,
    completed: int,
    total: int,
    last_target: str | None = None,
) -> None:
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    progress = {
        "status": status,
        "started_at": started_at,
        "updated_at": now,
        "completed": completed,
        "total": total,
    }
    if last_target is not None:
        progress["last_target"] = last_target
    if status in {"complete", "failed"}:
        progress["finished_at"] = now
    atomic_write_json(SCAN_PROGRESS, progress)


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
        atomic_write_json(SENT_PINGS, pruned)
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


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects disabled", headers, fp)


def validate_webhook_url(url: str) -> str:
    """Require HTTPS and reject local/private destinations to prevent SSRF."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("webhook must use an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("webhook URL must not contain user-info credentials")
    allow_private = os.environ.get("CAMPWATCH_ALLOW_PRIVATE_WEBHOOK", "0") == "1"
    if not allow_private:
        try:
            addresses = socket.getaddrinfo(
                parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
            )
        except socket.gaierror as exc:
            raise ValueError("webhook hostname could not be resolved") from exc
        for address in {item[4][0] for item in addresses}:
            if not ipaddress.ip_address(address).is_global:
                raise ValueError("webhook resolves to a non-public address")
    return url


def _post_webhook(url: str, payload: dict) -> int:
    validated = validate_webhook_url(url)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        validated,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirects())
    with opener.open(req, timeout=30) as response:
        response.read(1)
        return getattr(response, "status", 200)


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
        status = _post_webhook(WEBHOOK_URL, payload)
        if status >= 300:
            log(f"  WARN webhook rc={status} for {name}")
            return False
    except Exception as exc:  # noqa: BLE001
        # Never log the URL: webhook paths often contain bearer-like secrets.
        log(f"  WARN webhook send failed for {name}: {type(exc).__name__}")
        return False
    _record_sent_ping(ledger, event_id)
    log(f"  -> notification sent for {name} (event-id {event_id})")
    return True


# Equipment ids for a standard (non-group) campsite booking deep link.
GTC_EQUIPMENT_ID = -32768  # NON_GROUP_EQUIPMENT
GTC_DOMAIN = "washington.goingtocamp.com"


def gtc_booking_url(
    map_id, facility_id, start: str, nights: int, domain: str = GTC_DOMAIN
) -> str:
    """Build a WA GoingToCamp deep link that opens the park's results page
    pre-filled with the stay dates. `start` is an ISO date string; end = start
    plus nights."""
    s = dt.date.fromisoformat(start)
    e = s + dt.timedelta(days=nights)
    return (
        f"https://{domain}/create-booking/results?mapId={map_id}"
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
            if c.get("reservable") is False or is_group(c.get("name", "")):
                continue
            cfg["recdotgov"].append(
                {
                    "id": int(c["id"]),
                    "name": c["name"],
                    "rating": c.get("rating"),
                    "distance_km": c.get("distance_km"),
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
    atomic_write_json(CONFIG, cfg)
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


def _month_starts(start: dt.date, end: dt.date):
    month = start.replace(day=1)
    while month < end:
        yield month
        month = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def _safe_site_label(value, campsite_id) -> str:
    label = str(value or campsite_id or "site")
    label = "".join(ch if ch.isprintable() else "?" for ch in label)
    return f"{label.replace('|', '/')} (#{campsite_id})"


def recgov_available_nights(client, campground_id, start, end):
    """Return only explicitly Available recreation.gov nights, by unique site."""
    by_site: dict[str, set] = {}
    for month in _month_starts(start, end):
        data = client.month(campground_id, month)
        for campsite_id, site in data["campsites"].items():
            site_name = site.get("site") or campsite_id
            site_type = site.get("campsite_type") or ""
            if is_group(str(site_name), str(site_type)):
                continue
            label = _safe_site_label(site_name, campsite_id)
            for date_text, status in (site.get("availabilities") or {}).items():
                if status != "Available":
                    continue
                day = dt.date.fromisoformat(date_text[:10])
                if start <= day < end:
                    by_site.setdefault(label, set()).add(day)
    return by_site


def main(*, scheduled: bool = False):
    if scheduled and not scheduled_poll_due():
        return 0
    interval = recommended_poll_minutes()
    if interval is None:
        log("Polling paused: add a future trip to private watch_targets.json.")
        if scheduled:
            record_next_poll(24 * 60)
        return 0
    if not targets_in_watch_window():
        log(f"Polling deferred: no complete target trip is within {WINDOW_DAYS} days.")
        if scheduled:
            record_next_poll(24 * 60)
        return 0

    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else build_config()
    start = dt.date.today()
    end = start + dt.timedelta(days=WINDOW_DAYS)
    log(
        f"Watching {len(cfg['recdotgov'])} rec.gov + "
        f"{len(cfg['going_to_camp'])} WA-State-Park campgrounds; "
        f"window {start}..{end}, min {MIN_NIGHTS} nights; cadence {interval}m"
    )

    # Preserve the last fully completed result as the alert baseline. Incremental
    # checkpoints update STATE_FILE only, so an interrupted scan cannot consume
    # openings that still need to be alerted on the next complete run.
    prev_state = _load_complete_state()
    new_state, summary, new_alerts = {}, [], []
    rec_client = RecreationGovClient()
    gtc_client = GoingToCampClient()
    total_targets = len(cfg["recdotgov"]) + len(cfg["going_to_camp"])
    completed_targets = 0
    started_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    last_target = None
    _write_scan_progress(
        status="running",
        started_at=started_at,
        completed=0,
        total=total_targets,
    )

    def checkpoint(name: str):
        nonlocal completed_targets, last_target
        completed_targets += 1
        last_target = name
        _write_state_checkpoint(prev_state, new_state)
        _write_scan_progress(
            status="running",
            started_at=started_at,
            completed=completed_targets,
            total=total_targets,
            last_target=name,
        )
        log(f"  [{completed_targets}/{total_targets}] checked {name}")

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

    try:
        # --- recreation.gov ---
        for cg in cfg["recdotgov"]:
            cid = cg["id"]
            try:
                by_site = recgov_available_nights(rec_client, cid, start, end)
            except Exception as exc:  # noqa: BLE001
                log(f"  WARN rec.gov #{cid} {cg['name']}: {type(exc).__name__}")
                new_state[f"rg:{cid}"] = prev_state.get(f"rg:{cid}", [])
            else:
                process(
                    f"rg:{cid}", cg["name"], cg.get("rating"), cg.get("dist_mi"), by_site,
                    f"https://www.recreation.gov/camping/campgrounds/{cid}",
                    link_fn=lambda site, start, nights, _cid=cid: recgov_booking_url(
                        _cid, start, nights
                    ),
                )
            checkpoint(cg["name"])

        # --- Washington State Parks (GoingToCamp, direct MAPDATA) ---
        for cg in cfg["going_to_camp"]:
            fid = cg["id"]
            if cg.get("root_map_id") is None:
                # Non-campground facility (no reservable map); nothing to watch.
                new_state[f"wa:{fid}"] = prev_state.get(f"wa:{fid}", [])
            else:
                try:
                    by_site = gtc_available_nights(
                        cg["rec_area"], fid, start, end,
                        root_map_id=cg.get("root_map_id"), client=gtc_client,
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"  WARN WA park {cg['name']} ({fid}): {type(exc).__name__}")
                    new_state[f"wa:{fid}"] = prev_state.get(f"wa:{fid}", [])
                else:
                    _map_id = cg.get("root_map_id")
                    _domain = GTC_HOSTS[int(cg["rec_area"])]
                    process(
                        f"wa:{fid}", cg["name"] + " (WA State Park)", None, None, by_site,
                        f"https://{_domain}",
                        link_fn=lambda site, start, nights, _m=_map_id, _f=fid, _d=_domain: gtc_booking_url(
                            _m, _f, start, nights, domain=_d
                        ),
                    )
            checkpoint(cg["name"])
    except BaseException:
        _write_scan_progress(
            status="failed",
            started_at=started_at,
            completed=completed_targets,
            total=total_targets,
            last_target=last_target,
        )
        raise

    _write_state_checkpoint(prev_state, new_state, complete=True)
    if new_alerts:
        secure_append_jsonl(ALERTS, new_alerts)
        log(f"!! {len(new_alerts)} campground(s) with NEW availability -> alerts.jsonl")
        # Instant webhook per campground with new openings. Change-only:
        # this branch runs ONLY when there is genuinely new availability.
        for a in new_alerts:
            send_trigger(a["name"], a["url"], a["new_runs"], a["runs"])
    else:
        log("No new availability since last check.")
    atomic_write_json(COMPLETE_STATE_FILE, new_state)
    _write_scan_progress(
        status="complete",
        started_at=started_at,
        completed=completed_targets,
        total=total_targets,
        last_target=last_target,
    )

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
    if scheduled:
        record_next_poll(interval)
    return 0


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--scheduled",
            action="store_true",
            help="honor adaptive cadence and update schedule_state.json",
        )
        parser.add_argument(
            "--all-once",
            action="store_true",
            help="scan all qualifying openings once without changing private targets",
        )
        args = parser.parse_args()
        if args.all_once:
            TARGET_WEEKENDS = None
        sys.exit(main(scheduled=args.scheduled))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
