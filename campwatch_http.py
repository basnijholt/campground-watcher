#!/usr/bin/env python3
"""Small, dependency-free HTTPS clients used by the campground watcher.

Only allow-listed HTTPS endpoints are exposed.  Responses are size-limited and
decoded as JSON so an upstream HTML/WAF response cannot be mistaken for data.
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable, Collection


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
MAX_JSON_BYTES = 20 * 1024 * 1024
RECREATION_GOV_HOST = "www.recreation.gov"
OSM_ROUTING_HOST = "routing.openstreetmap.de"


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    """Replace a JSON file atomically instead of following an existing symlink."""
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


class HttpRequestError(RuntimeError):
    """A bounded, sanitized description of an HTTP or JSON failure."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class RequestPacer:
    """Coordinate a small, shared request rate across worker threads.

    One instance is shared for a single upstream provider.  It spaces request
    *starts* without serializing the whole response body, so slow network I/O
    can overlap while a provider still sees a deliberately modest request rate.
    When any worker is throttled, :meth:`defer` pauses all of that provider's
    workers before their next request.
    """

    def __init__(
        self,
        min_interval: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if min_interval < 0:
            raise ValueError("request pacing interval must be non-negative")
        self.min_interval = float(min_interval)
        self.monotonic = monotonic
        self.sleeper = sleeper
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def wait(self) -> None:
        """Wait until this provider may receive another request start."""
        while True:
            with self._lock:
                now = self.monotonic()
                remaining = self._next_request_at - now
                if remaining <= 0:
                    self._next_request_at = now + self.min_interval
                    return
            self.sleeper(remaining)

    def defer(self, delay: float) -> None:
        """Delay all future request starts for the provider by ``delay``."""
        try:
            delay = max(0.0, float(delay))
        except (TypeError, ValueError) as exc:
            raise ValueError("request pacing delay must be numeric") from exc
        with self._lock:
            self._next_request_at = max(
                self._next_request_at, self.monotonic() + delay
            )


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects disabled", headers, fp)


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _retry_after_seconds(headers, now: Callable[[], float]) -> float | None:
    """Parse Retry-After without ever allowing it to extend our local cap."""
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
            return max(0.0, parsed.timestamp() - now())
        except (TypeError, ValueError, OverflowError):
            return None


class JsonHttpClient:
    """Internal HTTPS transport with an explicit hostname allowlist.

    Callers should normally use one of the provider-specific clients below,
    which fix the endpoint paths as well as the hostname.
    """

    def __init__(
        self,
        *,
        allowed_hosts: Collection[str],
        timeout: float = 30,
        attempts: int = 3,
        max_retry_delay: float = 10,
        retry_wait_budget: float = 30,
        jitter_ratio: float = 0.25,
        opener=None,
        sleeper: Callable[[float], None] = time.sleep,
        randomizer: Callable[[], float] = random.random,
        wall_clock: Callable[[], float] = time.time,
        pacer: RequestPacer | None = None,
    ):
        hosts = frozenset(str(host).lower() for host in allowed_hosts)
        if not hosts or any(not host or "/" in host for host in hosts):
            raise ValueError("at least one valid allowed host is required")
        if timeout <= 0 or max_retry_delay < 0 or retry_wait_budget < 0:
            raise ValueError("HTTP timeout and retry limits must be non-negative")
        self.allowed_hosts = hosts
        self.timeout = timeout
        self.attempts = min(5, max(1, int(attempts)))
        self.max_retry_delay = max_retry_delay
        self.retry_wait_budget = retry_wait_budget
        self.jitter_ratio = min(1.0, max(0.0, jitter_ratio))
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()), _NoRedirects()
        )
        self.sleeper = sleeper
        self.randomizer = randomizer
        self.wall_clock = wall_clock
        self.pacer = pacer

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("JSON requests require an absolute HTTPS URL")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("JSON request URL has an invalid port") from exc
        hostname = parsed.hostname.lower()
        if hostname not in self.allowed_hosts:
            raise ValueError("JSON request host is not allow-listed")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("credentials are not allowed in JSON request URLs")
        if port not in (None, 443):
            raise ValueError("JSON requests may only use the standard HTTPS port")
        if parsed.query:
            raise ValueError("pass query parameters separately")
        if parsed.fragment:
            raise ValueError("fragments are not allowed in JSON request URLs")
        query = urllib.parse.urlencode(
            {k: _query_value(v) for k, v in (params or {}).items() if v is not None}
        )
        full_url = urllib.parse.urlunsplit(
            ("https", hostname, parsed.path or "/", query, "")
        )
        request_headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": USER_AGENT,
        }
        request_headers.update(headers or {})

        last_error: Exception | None = None
        retry_wait_used = 0.0
        for attempt in range(self.attempts):
            req = urllib.request.Request(full_url, headers=request_headers, method="GET")
            try:
                if self.pacer is not None:
                    self.pacer.wait()
                with self.opener.open(req, timeout=self.timeout) as response:
                    status = getattr(response, "status", 200)
                    if status >= 300:
                        raise HttpRequestError(
                            f"HTTP {status}",
                            status=status,
                            retry_after=_retry_after_seconds(
                                getattr(response, "headers", None), self.wall_clock
                            ),
                        )
                    raw = response.read(MAX_JSON_BYTES + 1)
                    if len(raw) > MAX_JSON_BYTES:
                        raise HttpRequestError("JSON response exceeded 20 MiB", status=status)
                    try:
                        return json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise HttpRequestError(
                            "server returned a non-JSON response", status=status
                        ) from exc
            except urllib.error.HTTPError as exc:
                last_error = HttpRequestError(
                    f"HTTP {exc.code}",
                    status=exc.code,
                    retry_after=_retry_after_seconds(exc.headers, self.wall_clock),
                )
                exc.close()
                retryable = exc.code == 429 or 500 <= exc.code < 600
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = HttpRequestError(f"transport error: {type(exc).__name__}")
                retryable = True
            except HttpRequestError as exc:
                last_error = exc
                retryable = exc.status == 429 or (
                    exc.status is not None and 500 <= exc.status < 600
                )
            if self.pacer is not None and last_error.status == 429:
                # Do this even for a final failed attempt.  Other queued
                # targets must not immediately keep hitting a provider that
                # just told this worker to slow down.
                shared_delay = (
                    last_error.retry_after
                    if last_error.retry_after is not None
                    else 2**attempt
                )
                self.pacer.defer(min(self.max_retry_delay, shared_delay))
            if not retryable or attempt + 1 >= self.attempts:
                break
            if last_error.retry_after is not None:
                delay = min(self.max_retry_delay, last_error.retry_after)
            else:
                backoff = min(self.max_retry_delay, 2**attempt)
                delay = backoff * (1 + self.jitter_ratio * self.randomizer())
                delay = min(self.max_retry_delay, delay)
            remaining_wait = self.retry_wait_budget - retry_wait_used
            if remaining_wait <= 0:
                break
            delay = min(delay, remaining_wait)
            self.sleeper(delay)
            retry_wait_used += delay
        assert last_error is not None
        raise last_error


class OsmDrivingRouter:
    """Small, bounded client for OpenStreetMap's public OSRM car router.

    This is deliberately used only during an explicit candidate rebuild.  It
    batches destinations, sends a descriptive User-Agent, and never treats a
    route failure as a usable distance.
    """

    PATH = "/routed-car/table/v1/driving"
    MAX_DESTINATIONS_PER_REQUEST = 25

    def __init__(
        self,
        http: JsonHttpClient | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.http = http or JsonHttpClient(
            allowed_hosts=(OSM_ROUTING_HOST,), timeout=45, attempts=2
        )
        self.sleeper = sleeper

    @staticmethod
    def _coordinate(latitude: float, longitude: float) -> str:
        try:
            lat = float(latitude)
            lon = float(longitude)
        except (TypeError, ValueError) as exc:
            raise ValueError("routing coordinates must be numeric") from exc
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("routing coordinates are out of range")
        return f"{lon:.6f},{lat:.6f}"

    def driving_routes(
        self, origin_lat: float, origin_lon: float, destinations: list[tuple[float, float]]
    ) -> list[tuple[float | None, float | None]]:
        """Return ``(road km, route seconds)`` per destination, or ``(None, None)``."""
        origin = self._coordinate(origin_lat, origin_lon)
        results: list[tuple[float | None, float | None]] = []
        for offset in range(0, len(destinations), self.MAX_DESTINATIONS_PER_REQUEST):
            batch = destinations[offset : offset + self.MAX_DESTINATIONS_PER_REQUEST]
            coordinates = ";".join([origin, *(self._coordinate(*item) for item in batch)])
            data = self.http.get_json(
                f"https://{OSM_ROUTING_HOST}{self.PATH}/{coordinates}",
                params={"sources": "0", "annotations": "distance,duration"},
                headers={"User-Agent": "campground-watcher/1.0 (local candidate rebuild)"},
            )
            distances = data.get("distances") if isinstance(data, dict) else None
            durations = data.get("durations") if isinstance(data, dict) else None
            distance_row = (
                distances[0] if isinstance(distances, list) and len(distances) == 1 else None
            )
            duration_row = (
                durations[0] if isinstance(durations, list) and len(durations) == 1 else None
            )
            if (
                not isinstance(distance_row, list)
                or not isinstance(duration_row, list)
                or len(distance_row) != len(batch) + 1
                or len(duration_row) != len(batch) + 1
            ):
                raise HttpRequestError("OpenStreetMap routing returned invalid route data")
            for meters, seconds in zip(distance_row[1:], duration_row[1:]):
                if meters is None and seconds is None:
                    results.append((None, None))
                elif (
                    isinstance(meters, (int, float))
                    and 0 <= meters <= 2_000_000
                    and isinstance(seconds, (int, float))
                    and 0 <= seconds <= 172_800
                ):
                    results.append((float(meters) / 1000, float(seconds)))
                else:
                    raise HttpRequestError("OpenStreetMap routing returned invalid route data")
            if offset + len(batch) < len(destinations):
                self.sleeper(1.0)
        return results

    def driving_distances_km(
        self, origin_lat: float, origin_lon: float, destinations: list[tuple[float, float]]
    ) -> list[float | None]:
        """Compatibility helper returning only the road-distance component."""
        return [
            distance
            for distance, _duration in self.driving_routes(
                origin_lat, origin_lon, destinations
            )
        ]


GTC_HOSTS = {
    3: "washington.goingtocamp.com",
    6: "tacomapower.goingtocamp.com",
}
GTC_ENDPOINTS = {
    "LIST_CAMPGROUNDS": "/api/resourceLocation",
    "LIST_EQUIPMENT": "/api/equipment",
    "MAPDATA": "/api/availability/map",
    "OCCUPANCY": "/api/occupancy",
    "BOOKING_CATEGORIES": "/api/bookingCategories",
    "CAMP_DETAILS": "/api/maps",
}


class GoingToCampClient:
    """Minimal allow-listed client for the two GoingToCamp areas we support."""

    def __init__(
        self,
        http: JsonHttpClient | None = None,
        *,
        pacer: RequestPacer | None = None,
    ):
        self.http = http or JsonHttpClient(
            allowed_hosts=GTC_HOSTS.values(), pacer=pacer
        )

    def request_json(
        self,
        rec_area_id: int,
        endpoint_name: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            host = GTC_HOSTS[int(rec_area_id)]
            path = GTC_ENDPOINTS[endpoint_name]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("unsupported GoingToCamp area or endpoint") from exc
        data = self.http.get_json(f"https://{host}{path}", params=params)
        list_endpoints = {
            "LIST_CAMPGROUNDS",
            "LIST_EQUIPMENT",
            "BOOKING_CATEGORIES",
            "CAMP_DETAILS",
        }
        if endpoint_name in list_endpoints:
            if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
                raise HttpRequestError(
                    f"GoingToCamp {endpoint_name} returned invalid data"
                )
        elif endpoint_name == "MAPDATA":
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("resourceAvailabilities"), dict)
                or not isinstance(data.get("mapLinkAvailabilities"), dict)
            ):
                raise HttpRequestError("GoingToCamp MAPDATA returned invalid data")
        elif endpoint_name == "OCCUPANCY":
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("resourceOccupancy"), list)
                or any(
                    not isinstance(item, dict)
                    for item in data.get("resourceOccupancy", [])
                )
            ):
                raise HttpRequestError("GoingToCamp OCCUPANCY returned invalid data")
        return data


class RecreationGovClient:
    """Provider-specific recreation.gov client with fixed hosts and paths."""

    def __init__(
        self,
        http: JsonHttpClient | None = None,
        *,
        pacer: RequestPacer | None = None,
    ):
        self.http = http or JsonHttpClient(
            allowed_hosts={RECREATION_GOV_HOST}, pacer=pacer
        )

    def search(self, params: dict[str, Any]) -> dict:
        data = self.http.get_json(
            f"https://{RECREATION_GOV_HOST}/api/search", params=params
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("results"), list)
            or any(not isinstance(item, dict) for item in data.get("results", []))
        ):
            raise HttpRequestError("recreation.gov search returned invalid data")
        return data

    def month(self, campground_id: int, month) -> dict:
        campground_id = int(campground_id)
        if campground_id <= 0:
            raise ValueError("campground ID must be positive")
        data = self.http.get_json(
            f"https://{RECREATION_GOV_HOST}/api/camps/availability/campground/"
            f"{campground_id}/month",
            params={"start_date": month.strftime("%Y-%m-01T00:00:00.000Z")},
            headers={"Referer": f"https://{RECREATION_GOV_HOST}/"},
        )
        campsites = data.get("campsites") if isinstance(data, dict) else None
        if not isinstance(campsites, dict) or any(
            not isinstance(site, dict)
            or (
                site.get("availabilities") is not None
                and not isinstance(site.get("availabilities"), dict)
            )
            for site in campsites.values()
        ):
            raise HttpRequestError("recreation.gov response has invalid campsite data")
        return data
