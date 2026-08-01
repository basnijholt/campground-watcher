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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import datetime as dt
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
import threading
import traceback
import urllib.parse
from pathlib import Path

from campwatch_config import load_provider_rules
from campwatch_http import (
    GTC_HOSTS,
    GoingToCampClient,
    HttpRequestError,
    RequestPacer,
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
#   availability == 0 means that night is an availability candidate; occupancy
#   removes several non-reservable resource types but is not a full transaction
#   or policy validation.
NON_GROUP_EQUIPMENT = -32768

# GoingToCamp daily-availability "available" code.
# Empirically decoded 2026-06-15 by comparing in-season vs winter code
# distributions across many parks AND cross-checking a known-closed park:
#   0 = AVAILABLE candidate   -- present in-season, varies by date
#   1 = booked/reserved       -- present in-season, absent in far winter
#   2 = NOT OPERATING/closed  -- CONSTANT all dates (e.g. Saltwater = all 2)
#   3 = not in season / not yet released
#   4/5 = non-bookable (utility/group/special), effectively constant
# A prior interpretation of code 2 was wrong: it reported closed parks as
# available. The booking UI and occupancy endpoint confirm that code 0 is open.
_GTC_AVAILABLE = 0

# GoingToCamp occupancy "available" code (separate enum from MAPDATA).
#   0 = Available, 1 = Filtered, 2 = Unavailable
# A site can be MAPDATA-available (0) yet excluded by the occupancy endpoint
# (walk-in / host / hold). This endpoint does not enforce every booking policy,
# such as the Washington State Parks maximum stay length.
_GTC_OCCUPANCY_AVAILABLE = 0

# Per-rec-area caches so we hit the metadata endpoints once.
_booking_categories_cache: dict = {}
_sub_equipment_cache: dict = {}
_gtc_metadata_cache_lock = threading.Lock()


class AvailabilityVerificationError(RuntimeError):
    """The occupancy endpoint could not check a park's candidate sites."""


def _rootmap_index(client, rec_area_id):
    return {
        fac.get("resourceLocationId"): fac.get("rootMapId")
        for fac in client.request_json(rec_area_id, "LIST_CAMPGROUNDS")
        if fac.get("resourceLocationId") is not None
    }


def _booking_category(client, rec_area_id, booking_category_id=0):
    # Worker threads share this small per-provider cache.  Keep the first
    # metadata fetch single-flight so parallel park scans do not create an
    # avoidable burst of identical requests.
    with _gtc_metadata_cache_lock:
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
    with _gtc_metadata_cache_lock:
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


def _occupancy_available_resource_ids(client, rec_area_id, fid, start, end):
    """Return resource IDs marked available by occupancy, not a final booking guarantee."""
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
    """Return {resource_label: set(date)} of observed open nights for a WA facility.

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
    # MAPDATA availability == 0 is necessary but not sufficient -- it also flags
    # walk-in / host / non-reservable sites as 0. We treat these as candidates
    # and remove occupancy-excluded resources below.
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

    # ---- Step 2: occupancy-filter per consecutive run.
    # It must be queried per *stay window* (a 90-day occupancy query returns
    # nothing). For each candidate consecutive run (>= MIN_NIGHTS), keep the run
    # only if its resource is returned as available for that exact window. This
    # still does not validate provider policy limits or a signed-in transaction.
    occ_cache: dict[tuple, set[str]] = {}

    def occ_set(w_start, w_end):
        ck = (w_start, w_end)
        if ck not in occ_cache:
            try:
                occ_cache[ck] = _occupancy_available_resource_ids(
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
WEBHOOK_OUTBOX = HERE / "webhook_outbox.json"  # durable failed webhook payloads
TARGETS_FILE = HERE / "watch_targets.json"  # private, gitignored trip dates
SCHEDULE_STATE = HERE / "schedule_state.json"
# Suppress re-sending the same opening (same event-id) within this many hours.
# WA park availability flaps (a site appears/disappears across runs), which would
# otherwise re-notify the same opening every cycle. This avoids wasted sends.
PING_SUPPRESS_HOURS = 24

# ---- watch window + rules ----
WINDOW_DAYS = 90
MIN_NIGHTS = 2

# Conservative caps give I/O overlap without imitating a high-volume scraper.
# Separate pools prevent a slow or throttled provider from blocking the other.
REC_GOV_DEFAULT_WORKERS = 3
GTC_DEFAULT_WORKERS = 2
REC_GOV_MAX_WORKERS = 4
GTC_MAX_WORKERS = 3
# These retain the rough request-start rate the prior sequential scanner reached
# on a healthy connection, while capping in-flight work and leaving headroom for
# the shared 429 cooldown.
REC_GOV_REQUEST_INTERVAL_SECONDS = 0.125
GTC_REQUEST_INTERVAL_SECONDS = 0.25

# ---- instant notifications via a generic webhook ----
# On each new observed opening, POST a JSON payload to an optional
# webhook URL. Set CAMPWATCH_WEBHOOK_URL to a Discord/Slack/ntfy/Telegram/
# custom endpoint. If unset, notifications are skipped (the watcher still
# writes openings to alerts.jsonl and you can read them with report.py /
# weekend.py). Change-only + idempotent + fails soft; never raises.
NOTIFY_ENABLED = os.environ.get("CAMPWATCH_NOTIFY", "1") != "0"
WEBHOOK_URL = os.environ.get("CAMPWATCH_WEBHOOK_URL", "")


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a small, safe worker-count override from the environment."""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def availability_worker_counts() -> tuple[int, int]:
    """Return independently capped recreation.gov and GoingToCamp workers."""
    return (
        _bounded_env_int(
            "CAMPWATCH_RECGOV_WORKERS",
            REC_GOV_DEFAULT_WORKERS,
            1,
            REC_GOV_MAX_WORKERS,
        ),
        _bounded_env_int(
            "CAMPWATCH_GTC_WORKERS",
            GTC_DEFAULT_WORKERS,
            1,
            GTC_MAX_WORKERS,
        ),
    )


# Optional: send the human-readable text under this JSON key (Discord uses
# "content", Slack uses "text", ntfy uses "message"). Default "content".
_configured_webhook_text_key = os.environ.get("CAMPWATCH_WEBHOOK_TEXT_KEY", "content")
_reserved_webhook_keys = {
    "campground",
    "event_id",
    "new_run_count",
    "new_runs",
    "new_runs_truncated",
    "sites",
    "sites_truncated",
    "stay_groups",
    "url",
}
WEBHOOK_TEXT_KEY = (
    _configured_webhook_text_key
    if 1 <= len(_configured_webhook_text_key) <= 64
    and _configured_webhook_text_key not in _reserved_webhook_keys
    else "content"
)
MAX_WEBHOOK_TEXT_CHARS = 1_800
MAX_WEBHOOK_DETAIL_GROUPS = 10
MAX_WEBHOOK_SAMPLED_RUNS = 10
MAX_WEBHOOK_BODY_BYTES = 64 * 1024
MAX_WEBHOOK_OUTBOX_ITEMS = 100

GROUP_MARKERS = load_provider_rules()["watch"]["group_markers"]

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
    signature: str,
    completed_keys: list[str],
    failed_keys: list[str],
    coverage_first_night: str,
    coverage_last_night: str,
    last_target: str | None = None,
) -> None:
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    progress = {
        "status": status,
        "started_at": started_at,
        "updated_at": now,
        "completed": completed,
        "total": total,
        "signature": signature,
        "completed_keys": completed_keys,
        "failed_keys": sorted(set(failed_keys)),
        "coverage": {
            "first_night": coverage_first_night,
            "last_night": coverage_last_night,
        },
        "pid": os.getpid(),
    }
    if last_target is not None:
        progress["last_target"] = last_target
    if status in {"complete", "failed"}:
        progress["finished_at"] = now
    atomic_write_json(SCAN_PROGRESS, progress)


def _scan_signature(cfg: dict, start: dt.date, end: dt.date) -> str:
    if TARGET_WEEKENDS is None:
        target_filter = "all"
    else:
        target_filter = [
            [label, [night.isoformat() for night in nights]]
            for label, nights in TARGET_WEEKENDS
        ]
    payload = {
        "config": cfg,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "target_filter": target_filter,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _pid_is_alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _load_resume_checkpoint(signature: str) -> tuple[list[str], dict]:
    """Return completed keys and their saved state for a compatible partial scan."""
    if not SCAN_PROGRESS.exists() or not STATE_FILE.exists():
        return [], {}
    try:
        progress = json.loads(SCAN_PROGRESS.read_text())
        if (
            progress.get("status") not in {"running", "failed"}
            or progress.get("signature") != signature
        ):
            return [], {}
        pid = progress.get("pid")
        if (
            progress.get("status") == "running"
            and pid != os.getpid()
            and _pid_is_alive(pid)
        ):
            raise RuntimeError("an identical availability scan is already running")
        snapshot = json.loads(STATE_FILE.read_text())
        keys = progress.get("completed_keys")
        if not isinstance(snapshot, dict) or not isinstance(keys, list):
            return [], {}
        failed = {str(key) for key in progress.get("failed_keys", [])}
        completed = [
            str(key) for key in keys
            if str(key) in snapshot and str(key) not in failed
        ]
        return completed, {key: snapshot[key] for key in completed}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return [], {}


def _load_sent_pings() -> dict:
    """Load the ledger of {event_id: iso_timestamp} of pings already sent."""
    if SENT_PINGS.exists():
        try:
            values = json.loads(SENT_PINGS.read_text())
            return values if isinstance(values, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _record_sent_ping(ledger: dict, event_id: str):
    """Record event_id as sent now and prune entries older than the suppress
    window so the ledger does not grow unbounded."""
    now = dt.datetime.now()
    ledger[event_id] = now.isoformat(timespec="seconds")
    cutoff = now - dt.timedelta(hours=PING_SUPPRESS_HOURS)
    pruned = {}
    for key, value in ledger.items():
        try:
            when = dt.datetime.fromisoformat(value)
            if when.tzinfo is not None:
                when = when.astimezone().replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
        if when >= cutoff:
            pruned[str(key)] = value
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
    now = dt.datetime.now(when.tzinfo) if when.tzinfo is not None else dt.datetime.now()
    age = now - when
    return dt.timedelta() <= age < dt.timedelta(hours=PING_SUPPRESS_HOURS)


def _validated_webhook_destination(
    url: str,
) -> tuple[urllib.parse.SplitResult, tuple[str, ...]]:
    """Return a validated URL and the public addresses it resolved to.

    The caller must connect to one of the returned addresses instead of resolving
    the hostname again.  Keeping validation and connection bound to the same DNS
    answer prevents a public-to-private DNS rebinding between those two steps.
    """
    if len(url) > 8_192:
        raise ValueError("webhook URL is too long")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("webhook must use an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("webhook URL must not contain user-info credentials")
    if parsed.fragment:
        raise ValueError("webhook URL must not contain a fragment")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("webhook URL has an invalid port") from exc
    allow_private = os.environ.get("CAMPWATCH_ALLOW_PRIVATE_WEBHOOK", "0") == "1"
    try:
        answers = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("webhook hostname could not be resolved") from exc
    addresses = tuple(dict.fromkeys(item[4][0] for item in answers))
    if not addresses:
        raise ValueError("webhook hostname did not resolve to an address")
    if not allow_private and any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise ValueError("webhook resolves to a non-public address")
    return parsed, addresses


def validate_webhook_url(url: str) -> str:
    """Require HTTPS and reject local/private destinations to prevent SSRF."""
    _validated_webhook_destination(url)
    return url


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a validated address with the original TLS name."""

    def __init__(self, hostname: str, port: int, address: str, *, timeout: float):
        context = ssl.create_default_context()
        context.set_alpn_protocols(["http/1.1"])
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=context,
        )
        self._validated_address = address

    def connect(self) -> None:
        self.sock = self._create_connection(
            (self._validated_address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _is_discord_hostname(hostname: str | None) -> bool:
    hostname = (hostname or "").lower()
    return hostname in {"discord.com", "discordapp.com"} or hostname.endswith(
        (".discord.com", ".discordapp.com")
    )


def _webhook_request_target(parsed: urllib.parse.SplitResult) -> str:
    query = parsed.query
    if _is_discord_hostname(parsed.hostname):
        parameters = urllib.parse.parse_qsl(query, keep_blank_values=True)
        if not any(key == "wait" for key, _value in parameters):
            parameters.append(("wait", "true"))
        query = urllib.parse.urlencode(parameters)
    return urllib.parse.urlunsplit(("", "", parsed.path or "/", query, ""))


def _post_webhook(url: str, payload: dict) -> int:
    parsed, addresses = _validated_webhook_destination(url)
    body = json.dumps(payload, separators=(",", ":")).encode()
    if len(body) > MAX_WEBHOOK_BODY_BYTES:
        raise ValueError("webhook payload exceeded the local size limit")
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "User-Agent": "campground-watcher/1.0",
    }
    target = _webhook_request_target(parsed)
    port = parsed.port or 443
    last_error: Exception | None = None
    for address in addresses:
        connection = _PinnedHTTPSConnection(
            parsed.hostname, port, address, timeout=30
        )
        try:
            connection.request("POST", target, body=body, headers=headers)
            response = connection.getresponse()
            response.read(1)
            return response.status
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()
    assert last_error is not None
    raise last_error


def _load_webhook_outbox() -> list[dict]:
    if not WEBHOOK_OUTBOX.exists():
        return []
    try:
        values = json.loads(WEBHOOK_OUTBOX.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("webhook outbox could not be read") from exc
    if not isinstance(values, list) or any(
        not isinstance(item, dict) for item in values
    ):
        raise RuntimeError("webhook outbox has an invalid format")
    return values


def _save_webhook_outbox(items: list[dict]) -> None:
    atomic_write_json(WEBHOOK_OUTBOX, items)


def _queue_webhook_payload(event_id: str, payload: dict) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    if len(encoded) > MAX_WEBHOOK_BODY_BYTES:
        raise RuntimeError("webhook payload is too large to queue")
    items = _load_webhook_outbox()
    if any(item.get("event_id") == event_id for item in items):
        return
    if len(items) >= MAX_WEBHOOK_OUTBOX_ITEMS:
        raise RuntimeError("webhook outbox is full")
    items.append(
        {
            "event_id": event_id,
            "payload": payload,
            "attempts": 0,
            "next_attempt_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
    )
    _save_webhook_outbox(items)


def _event_is_queued(event_id: str) -> bool:
    return any(
        item.get("event_id") == event_id for item in _load_webhook_outbox()
    )


def flush_webhook_outbox(now: dt.datetime | None = None) -> int:
    """Retry due webhook payloads and retain failures with bounded backoff."""
    if not NOTIFY_ENABLED or not WEBHOOK_URL:
        return 0
    now = now or dt.datetime.now()
    ledger = _load_sent_pings()
    remaining = []
    sent = 0
    for item in _load_webhook_outbox():
        event_id = item.get("event_id")
        payload = item.get("payload")
        if not isinstance(event_id, str) or not isinstance(payload, dict):
            continue
        if _recently_sent(ledger, event_id):
            continue
        try:
            due = dt.datetime.fromisoformat(str(item.get("next_attempt_at")))
            if due.tzinfo is not None:
                due = due.astimezone().replace(tzinfo=None)
        except (TypeError, ValueError):
            due = now
        try:
            is_due = due <= now
        except TypeError:
            is_due = True
        if not is_due:
            remaining.append(item)
            continue
        try:
            status = _post_webhook(WEBHOOK_URL, payload)
            if status >= 300:
                raise HttpRequestError(f"HTTP {status}", status=status)
        except Exception:  # noqa: BLE001
            try:
                previous_attempts = max(0, int(item.get("attempts", 0)))
            except (TypeError, ValueError):
                previous_attempts = 0
            attempts = min(10, previous_attempts + 1)
            delay_minutes = min(60, 2**attempts)
            remaining.append(
                {
                    **item,
                    "attempts": attempts,
                    "next_attempt_at": (
                        now + dt.timedelta(minutes=delay_minutes)
                    ).isoformat(timespec="seconds"),
                }
            )
            continue
        _record_sent_ping(ledger, event_id)
        sent += 1
    _save_webhook_outbox(remaining)
    if sent:
        log(f"  -> delivered {sent} queued notification(s)")
    return sent


def _bounded_webhook_payload(
    name: str,
    url: str,
    event_id: str,
    new_runs: list[str],
    runs: list[dict],
) -> dict:
    """Summarize a potentially huge opening set into one bounded notification."""
    def bounded(value, limit):
        return str(value)[:limit]

    by_key = {run["state_key"]: run for run in runs}
    groups: dict[tuple, dict] = {}
    sampled_run_keys = []
    for run_key in new_runs:
        run = by_key.get(run_key)
        if not run:
            continue
        link = bounded(run.get("booking_url") or url, 2_048)
        weekend = bounded(run["weekend"], 120) if run.get("weekend") else None
        key = (
            bounded(run["start"], 10),
            run["nights"],
            weekend,
            link,
            run.get("observed_nights"),
            run.get("max_stay_nights"),
        )
        group = groups.setdefault(key, {"sites": set()})
        group["sites"].add(bounded(run["site"], 120))
        if len(sampled_run_keys) < MAX_WEBHOOK_SAMPLED_RUNS:
            sampled_run_keys.append(run_key)

    details = []
    structured = []
    for key, group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])):
        start, nights, weekend, link, observed_nights, max_stay_nights = key
        site_count = len(group["sites"])
        weekend_text = f" [{weekend}]" if weekend and weekend != "all" else ""
        limit_text = (
            f"; observed {observed_nights}, capped at {max_stay_nights}"
            if observed_nights
            else ""
        )
        details.append(
            f"• {site_count} site{'s' if site_count != 1 else ''}: "
            f"{nights} night{'s' if nights != 1 else ''} from {start}"
            f"{weekend_text}{limit_text}\n  {link}"
        )
        structured.append(
            {
                "start": start,
                "nights": nights,
                "weekend": weekend,
                "booking_url": link,
                "site_count": site_count,
                "sample_sites": sorted(group["sites"])[:3],
                "observed_nights": observed_nights,
                "max_stay_nights": max_stay_nights,
            }
        )

    header = f"\U0001f3d5\ufe0f NEW availability at {bounded(name, 120)}"
    included_lines = []
    included_groups = []
    for line, group in zip(details[:MAX_WEBHOOK_DETAIL_GROUPS], structured):
        candidate_lines = [*included_lines, line]
        omitted = max(0, len(details) - len(candidate_lines))
        footer = (
            f"\n… {omitted} more stay option{'s' if omitted != 1 else ''}; "
            f"{len(new_runs)} new run{'s' if len(new_runs) != 1 else ''} saved locally."
            if omitted
            else ""
        )
        candidate = header + "\n" + "\n".join(candidate_lines) + footer
        if len(candidate) > MAX_WEBHOOK_TEXT_CHARS:
            break
        included_lines = candidate_lines
        included_groups.append(group)
    omitted = max(0, len(details) - len(included_lines))
    footer = (
        f"\n… {omitted} more stay option{'s' if omitted != 1 else ''}; "
        f"{len(new_runs)} new run{'s' if len(new_runs) != 1 else ''} saved locally."
        if omitted
        else ""
    )
    message = header
    if included_lines:
        message += "\n" + "\n".join(included_lines)
    elif new_runs:
        message += f"\n{len(new_runs)} new runs saved locally."
    message = (message + footer)[:MAX_WEBHOOK_TEXT_CHARS]
    sampled_sites = []
    for run_key in sampled_run_keys:
        run = by_key[run_key]
        sampled_site = {
            "site": bounded(run["site"], 120),
            "start": bounded(run["start"], 10),
            "nights": run["nights"],
            "weekend": (
                bounded(run["weekend"], 120) if run.get("weekend") else None
            ),
            "booking_url": bounded(run.get("booking_url") or url, 2_048),
        }
        if run.get("observed_nights"):
            sampled_site["observed_nights"] = run["observed_nights"]
            sampled_site["max_stay_nights"] = run.get("max_stay_nights")
        sampled_sites.append(sampled_site)
    payload = {
        WEBHOOK_TEXT_KEY: message,
        "campground": bounded(name, 120),
        "url": bounded(url, 2_048),
        # Keep the original per-site field for webhook consumers. It is now a
        # bounded sample; aggregate groups and counts describe the full event.
        "sites": sampled_sites,
        "sites_truncated": len(new_runs) > len(sampled_sites),
        "stay_groups": included_groups,
        "new_run_count": len(new_runs),
        "new_runs": [bounded(run_key, 256) for run_key in sampled_run_keys],
        "new_runs_truncated": len(new_runs) > len(sampled_run_keys),
        "event_id": bounded(event_id, 128),
    }
    parsed = urllib.parse.urlsplit(WEBHOOK_URL)
    if (
        WEBHOOK_TEXT_KEY == "content"
        and _is_discord_hostname(parsed.hostname)
    ):
        payload["allowed_mentions"] = {"parse": []}
    return payload


def send_trigger(name: str, url: str, new_runs: list[str], runs: list[dict]) -> bool:
    """Send one instant notification for a campground's NEW openings.

    Fires ONLY from the new-alert path (real change). Idempotent: the event-id
    is a stable hash of the campground key + the sorted new-run keys, so the
    same batch of openings will not double-notify even across runs. Suppresses
    re-notifying flapping openings within a 24h window. Fails soft: on any error
    it logs a WARN and returns False, leaving the opening recorded in
    alerts.jsonl. A failed request is durably queued; False means neither the
    request nor its durable queue entry could be persisted. Never raises.
    """
    if not NOTIFY_ENABLED or not WEBHOOK_URL:
        return True

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
        return True
    try:
        if _event_is_queued(event_id):
            log(f"  (notification already queued for {name}, event-id {event_id})")
            return True
    except Exception as exc:  # noqa: BLE001
        # Continue with an immediate send, but do not overwrite an unreadable
        # outbox if that send fails.
        log(f"  WARN webhook outbox unavailable: {type(exc).__name__}")

    try:
        payload = _bounded_webhook_payload(name, url, event_id, new_runs, runs)
    except Exception as exc:  # noqa: BLE001
        log(f"  WARN webhook payload could not be built for {name}: {type(exc).__name__}")
        return False
    try:
        status = _post_webhook(WEBHOOK_URL, payload)
        if status >= 300:
            raise HttpRequestError(f"HTTP {status}", status=status)
    except Exception as exc:  # noqa: BLE001
        # Never log the URL: webhook paths often contain bearer-like secrets.
        try:
            _queue_webhook_payload(event_id, payload)
        except Exception as queue_exc:  # noqa: BLE001
            log(
                f"  WARN webhook send and durable queue failed for {name}: "
                f"{type(exc).__name__}/{type(queue_exc).__name__}"
            )
            return False
        log(
            f"  WARN webhook send failed for {name}: {type(exc).__name__}; "
            "queued for retry"
        )
        return True
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


def _recgov_target_result(campground: dict, start: dt.date, end: dt.date, pacer):
    """Poll one recreation.gov target with a thread-local HTTP client."""
    try:
        return recgov_available_nights(
            RecreationGovClient(pacer=pacer), campground["id"], start, end
        ), None
    except Exception as exc:  # noqa: BLE001 - individual targets fail closed
        return None, exc


def _gtc_target_result(campground: dict, start: dt.date, end: dt.date, pacer):
    """Poll one GoingToCamp target with a thread-local HTTP client."""
    if campground.get("root_map_id") is None:
        # Non-campground facility (no reservable map); nothing to watch.
        return None, None
    try:
        return gtc_available_nights(
            campground["rec_area"],
            campground["id"],
            start,
            end,
            root_map_id=campground.get("root_map_id"),
            client=GoingToCampClient(pacer=pacer),
        ), None
    except Exception as exc:  # noqa: BLE001 - individual targets fail closed
        return None, exc


def _http_error_label(exc: Exception) -> str:
    """Keep progress warnings brief while retaining a provider status code."""
    if isinstance(exc, HttpRequestError) and exc.status is not None:
        return f"{type(exc).__name__} HTTP {exc.status}"
    return type(exc).__name__


def _summary_from_state(cfg: dict, state: dict, previous: dict):
    """Rebuild human/alert details so resumed targets need not be polled again."""
    summary = []
    new_alerts = []
    stay_limits = load_provider_rules()["going_to_camp"]["stay_limits"]

    def add_entry(key, name, rating, dist_mi, url, link_fn=None, max_stay_nights=None):
        run_keys = state.get(key, [])
        if not run_keys:
            return
        runs = []
        for run_key in run_keys:
            site, start_text, nights_text = run_key.rsplit("|", 2)
            observed_nights = int(nights_text)
            nights = min(observed_nights, max_stay_nights or observed_nights)
            weekend = covers_target_weekend(dt.date.fromisoformat(start_text), nights)
            run = {
                "site": site,
                "start": start_text,
                "nights": nights,
                "weekend": weekend,
                "state_key": run_key,
            }
            if nights != observed_nights:
                run["observed_nights"] = observed_nights
                run["max_stay_nights"] = max_stay_nights
            if link_fn is not None:
                try:
                    run["booking_url"] = link_fn(site, start_text, nights)
                except Exception:  # noqa: BLE001
                    run["booking_url"] = url
            runs.append(run)
        runs.sort(key=lambda run: (run["start"], run["site"]))
        entry = {
            "key": key,
            "name": name,
            "rating": rating,
            "dist_mi": dist_mi,
            "runs": runs,
            "url": url,
        }
        summary.append(entry)
        old_keys = set(previous.get(key, []))
        fresh = sorted(run_key for run_key in run_keys if run_key not in old_keys)
        if fresh:
            alert = dict(entry)
            alert["new_runs"] = fresh
            alert["detected_at"] = dt.datetime.now().isoformat(timespec="seconds")
            new_alerts.append(alert)

    for campground in cfg["recdotgov"]:
        campground_id = campground["id"]
        add_entry(
            f"rg:{campground_id}",
            campground["name"],
            campground.get("rating"),
            campground.get("dist_mi"),
            f"https://www.recreation.gov/camping/campgrounds/{campground_id}",
            link_fn=lambda site, start_text, nights, _id=campground_id: recgov_booking_url(
                _id, start_text, nights
            ),
        )
    for campground in cfg["going_to_camp"]:
        facility_id = campground["id"]
        rec_area = int(campground["rec_area"])
        domain = GTC_HOSTS[rec_area]
        map_id = campground.get("root_map_id")
        stay_limit = stay_limits.get(rec_area)
        add_entry(
            f"wa:{facility_id}",
            campground["name"] + " (WA State Park)",
            None,
            None,
            f"https://{domain}",
            link_fn=lambda site, start_text, nights, _map=map_id, _facility=facility_id, _domain=domain: gtc_booking_url(
                _map, _facility, start_text, nights, domain=_domain
            ),
            max_stay_nights=(stay_limit or {}).get("max_nights"),
        )
    return summary, new_alerts


def main(*, scheduled: bool = False):
    try:
        flush_webhook_outbox()
    except Exception as exc:  # noqa: BLE001
        log(f"WARN queued notifications could not be retried: {type(exc).__name__}")
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
    coverage_last_night = end - dt.timedelta(days=1)
    log(
        f"Watching {len(cfg['recdotgov'])} rec.gov + "
        f"{len(cfg['going_to_camp'])} WA-State-Park campgrounds; "
        f"checked nights {start}..{coverage_last_night}, "
        f"min {MIN_NIGHTS} nights; cadence {interval}m"
    )

    # Preserve the last fully completed result as the alert baseline. Incremental
    # checkpoints update STATE_FILE only, so an interrupted scan cannot consume
    # openings that still need to be alerted on the next complete run.
    prev_state = _load_complete_state()
    rec_workers, gtc_workers = availability_worker_counts()
    # Each target task gets its own client because urllib's cookie jar/opener
    # is not a shared-thread transport.  The pacer is shared per upstream
    # provider so request starts stay modest and a 429 slows every worker for
    # that provider.
    rec_pacer = RequestPacer(REC_GOV_REQUEST_INTERVAL_SECONDS)
    gtc_pacer = RequestPacer(GTC_REQUEST_INTERVAL_SECONDS)
    total_targets = len(cfg["recdotgov"]) + len(cfg["going_to_camp"])
    signature = _scan_signature(cfg, start, end)
    completed_keys, new_state = _load_resume_checkpoint(signature)
    completed_key_set = set(completed_keys)
    failed_keys: list[str] = []
    completed_targets = len(completed_keys)
    started_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    last_target = None
    if completed_targets:
        log(
            f"Resuming compatible checkpoint: skipping "
            f"{completed_targets}/{total_targets} completed campgrounds."
        )
    _write_scan_progress(
        status="running",
        started_at=started_at,
        completed=completed_targets,
        total=total_targets,
        signature=signature,
        completed_keys=completed_keys,
        failed_keys=failed_keys,
        coverage_first_night=start.isoformat(),
        coverage_last_night=coverage_last_night.isoformat(),
    )

    def checkpoint(key: str, name: str):
        nonlocal completed_targets, last_target
        if key not in completed_key_set:
            completed_keys.append(key)
            completed_key_set.add(key)
            completed_targets += 1
        last_target = name
        _write_state_checkpoint(prev_state, new_state)
        _write_scan_progress(
            status="running",
            started_at=started_at,
            completed=completed_targets,
            total=total_targets,
            signature=signature,
            completed_keys=completed_keys,
            failed_keys=failed_keys,
            coverage_first_night=start.isoformat(),
            coverage_last_night=coverage_last_night.isoformat(),
            last_target=name,
        )
        log(f"  [{completed_targets}/{total_targets}] checked {name}")

    def process(key, by_site):
        run_keys = []
        for sname, ds in by_site.items():
            for rs, length in consecutive_runs(ds):
                if length < MIN_NIGHTS:
                    continue
                wk = covers_target_weekend(rs, length)
                if wk is None:
                    continue
                run_keys.append(f"{sname}|{rs.isoformat()}|{length}")
        new_state[key] = sorted(run_keys)

    try:
        rec_pending = [
            cg for cg in cfg["recdotgov"]
            if f"rg:{cg['id']}" not in completed_key_set
        ]
        if rec_pending:
            log(
                f"  Fetching recreation.gov with {rec_workers} worker(s) "
                f"({REC_GOV_REQUEST_INTERVAL_SECONDS:.2f}s minimum request spacing)."
            )

        gtc_pending = [
            cg for cg in cfg["going_to_camp"]
            if f"wa:{cg['id']}" not in completed_key_set
        ]
        # Retain the prior state for non-campground facilities without sending
        # any request.  This preserves the sequential scan's behavior.
        for cg in [item for item in gtc_pending if item.get("root_map_id") is None]:
            fid = cg["id"]
            key = f"wa:{fid}"
            new_state[key] = prev_state.get(key, [])
            checkpoint(key, cg["name"])

        gtc_network_pending = [
            item for item in gtc_pending if item.get("root_map_id") is not None
        ]
        if gtc_network_pending:
            log(
                f"  Fetching GoingToCamp with {gtc_workers} worker(s) "
                f"({GTC_REQUEST_INTERVAL_SECONDS:.2f}s minimum request spacing)."
            )

        # The pools run together, but each has an independent worker cap and
        # request pacer.  A worker returns its data to this main thread, which
        # keeps checkpoints and state writes atomic and ordered.
        if rec_pending or gtc_network_pending:
            with (
                ThreadPoolExecutor(
                    max_workers=rec_workers, thread_name_prefix="recgov"
                ) as rec_pool,
                ThreadPoolExecutor(
                    max_workers=gtc_workers, thread_name_prefix="goingtocamp"
                ) as gtc_pool,
            ):
                futures = {
                    rec_pool.submit(
                        _recgov_target_result, cg, start, end, rec_pacer
                    ): ("recgov", cg)
                    for cg in rec_pending
                }
                futures.update(
                    {
                        gtc_pool.submit(
                            _gtc_target_result, cg, start, end, gtc_pacer
                        ): ("goingtocamp", cg)
                        for cg in gtc_network_pending
                    }
                )
                while futures:
                    completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in completed:
                        provider, cg = futures.pop(future)
                        by_site, error = future.result()
                        if provider == "recgov":
                            cid = cg["id"]
                            key = f"rg:{cid}"
                            if error is not None:
                                log(
                                    f"  WARN rec.gov #{cid} {cg['name']}: "
                                    f"{_http_error_label(error)}"
                                )
                                failed_keys.append(key)
                                new_state[key] = prev_state.get(key, [])
                            else:
                                process(key, by_site)
                        else:
                            fid = cg["id"]
                            key = f"wa:{fid}"
                            if error is not None:
                                log(
                                    f"  WARN WA park {cg['name']} ({fid}): "
                                    f"{_http_error_label(error)}"
                                )
                                failed_keys.append(key)
                                new_state[key] = prev_state.get(key, [])
                            else:
                                process(key, by_site)
                        checkpoint(key, cg["name"])
    except BaseException:
        _write_scan_progress(
            status="failed",
            started_at=started_at,
            completed=completed_targets,
            total=total_targets,
            signature=signature,
            completed_keys=completed_keys,
            failed_keys=failed_keys,
            coverage_first_night=start.isoformat(),
            coverage_last_night=coverage_last_night.isoformat(),
            last_target=last_target,
        )
        raise

    _write_state_checkpoint(prev_state, new_state, complete=True)
    summary, new_alerts = _summary_from_state(cfg, new_state, prev_state)
    notifications_durable = True
    if new_alerts:
        secure_append_jsonl(ALERTS, new_alerts)
        log(f"!! {len(new_alerts)} campground(s) with NEW availability -> alerts.jsonl")
        # Instant webhook per campground with new openings. Change-only:
        # this branch runs ONLY when there is genuinely new availability.
        for a in new_alerts:
            notifications_durable = (
                send_trigger(a["name"], a["url"], a["new_runs"], a["runs"])
                and notifications_durable
            )
    else:
        log("No new availability since last check.")
    if notifications_durable:
        atomic_write_json(COMPLETE_STATE_FILE, new_state)
    else:
        log(
            "WARN notification state could not be persisted; retaining the prior "
            "complete baseline so the alert is retried."
        )
    _write_scan_progress(
        status="complete",
        started_at=started_at,
        completed=completed_targets,
        total=total_targets,
        signature=signature,
        completed_keys=completed_keys,
        failed_keys=failed_keys,
        coverage_first_night=start.isoformat(),
        coverage_last_night=coverage_last_night.isoformat(),
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
        parser.add_argument(
            "--rebuild-config",
            action="store_true",
            help="rebuild watch_config.json from the generated candidate lists",
        )
        args = parser.parse_args()
        if args.rebuild_config:
            cfg = build_config()
            print(
                f"Rebuilt watch_config.json with {len(cfg['recdotgov'])} rec.gov and "
                f"{len(cfg['going_to_camp'])} WA campground(s)."
            )
            sys.exit(0)
        if args.all_once:
            TARGET_WEEKENDS = None
        sys.exit(main(scheduled=args.scheduled))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
