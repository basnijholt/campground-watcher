#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["camply", "pytest"]
# ///
"""Tests for the campground watcher.

Two groups:
  1. Pure-logic filter helpers (offline, deterministic):
       is_group, consecutive_runs
  2. Washington State Parks availability path (gtc_available_nights):
       - an offline test that mocks the GoingToCamp provider's _api_request to
         prove the MAPDATA traversal parses daily slots into night-runs WITHOUT
         ever calling the broken /api/resource/details endpoint and WITHOUT a 400.
       - an optional LIVE test (skipped unless CAMPLY_LIVE=1) that hits the real
         GoingToCamp API for one park and asserts no 400/404.

Run:  uv run --python 3.12 -m pytest test_watch.py -v
  or: uv run --python 3.12 test_watch.py
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent


def _load_watch():
    """Import watch.py as a module (applies its camply monkeypatch)."""
    spec = importlib.util.spec_from_file_location("watchmod", HERE / "watch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["watchmod"] = mod
    spec.loader.exec_module(mod)
    return mod


watch = _load_watch()


# --------------------------------------------------------------------------- #
# 1. Filter helpers
# --------------------------------------------------------------------------- #
def test_is_group():
    assert watch.is_group("Group Site A")
    assert watch.is_group("Equestrian", "HORSE CAMP")
    assert watch.is_group("Overflow Lot")
    assert not watch.is_group("Site 042")
    assert not watch.is_group("Lakeview", "STANDARD NONELECTRIC")


def test_consecutive_runs():
    d = dt.date(2026, 7, 1)
    dates = {d, d + dt.timedelta(days=1), d + dt.timedelta(days=2),
             d + dt.timedelta(days=5)}
    runs = watch.consecutive_runs(dates)
    # one 3-night run starting Jul 1, one 1-night run starting Jul 6
    assert (d, 3) in runs
    assert (d + dt.timedelta(days=5), 1) in runs
    assert watch.consecutive_runs(set()) == []


def test_gtc_booking_url():
    url = watch.gtc_booking_url(-2147483371, -2147483589, "2026-07-10", 2)
    assert url.startswith("https://washington.goingtocamp.com/create-booking/results?")
    assert "mapId=-2147483371" in url
    assert "resourceLocationId=-2147483589" in url
    assert "startDate=2026-07-10" in url
    assert "endDate=2026-07-12" in url   # start + 2 nights
    assert "equipmentId=-32768" in url


def test_recgov_booking_url():
    url = watch.recgov_booking_url(232064, "2026-07-27", 7)
    assert url == "https://www.recreation.gov/camping/campgrounds/232064/availability"


def test_covers_target_weekend():
    """Drive the assertions off the live TARGET_WEEKENDS config so the test
    stays correct as weekends are booked/removed. Verifies the date-math
    invariants: a stay matches iff it contains ALL required nights of a
    weekend; partial/off-target stays do not match."""
    f = watch.covers_target_weekend

    if not watch.TARGET_WEEKENDS:
        # No weekends configured (all booked) -> nothing ever matches.
        assert f(dt.date(2026, 7, 17), 2) is None
        assert f(dt.date(2026, 6, 24), 2) is None
        return

    for label, required in watch.TARGET_WEEKENDS:
        fri = required[0]  # first required night = the Friday
        # exact 2-night Fri->Sun stay matches
        assert f(fri, 2) == label
        # longer stay spanning the weekend also matches (Thu->Mon)
        assert f(fri - dt.timedelta(days=1), 4) == label
        # only the Friday night (missing Saturday) -> no match
        assert f(fri, 1) is None
        # Sat+Sun (missing Friday night) -> no match
        assert f(fri + dt.timedelta(days=1), 2) is None
        # a stay a week earlier (off-target) -> no match
        assert f(fri - dt.timedelta(days=7), 2) is None

    # A near-term opening unrelated to any target weekend -> no match.
    assert f(dt.date(2026, 6, 24), 2) is None





# --------------------------------------------------------------------------- #
# 2. WA State Parks availability path (offline mock)
# --------------------------------------------------------------------------- #
class _FakeProvider:
    """Mimics camply's GoingToCamp provider for the MAPDATA traversal.

    LIST_CAMPGROUNDS -> one facility with a rootMapId.
    MAPDATA on root  -> empty resources, one child map link.
    MAPDATA on child -> one resource with 3 open nights then closed.

    Crucially it raises if anyone calls SITE_DETAILS / resource/details,
    proving the watcher never depends on the broken endpoint.
    """

    REC = 3
    FID = -2147483647
    ROOT = -2147480000
    CHILD = -2147470000

    def __init__(self):
        self.calls = []

    # Two candidate sites in MAPDATA, BOTH MAPDATA-available (code 0):
    #   BOOKABLE -> also web-bookable per /api/occupancy (should survive)
    #   PHANTOM  -> NOT in occupancy bookable set (walk-in/host; must be dropped)
    BOOKABLE = "-2147460000"
    PHANTOM = "-2147460001"

    def _api_request(self, rec_area_id, endpoint_name, params=None):
        self.calls.append((endpoint_name, params))
        if endpoint_name == "SITE_DETAILS":
            raise AssertionError("SITE_DETAILS must not be called (404 endpoint)")
        if endpoint_name == "LIST_CAMPGROUNDS":
            return [{"resourceLocationId": self.FID, "rootMapId": self.ROOT}]
        if endpoint_name == "LIST_EQUIPMENT":
            return [{"equipmentCategoryId": watch.NON_GROUP_EQUIPMENT,
                     "subEquipmentCategories": [{"subEquipmentCategoryId": -32768}]}]
        if endpoint_name == "BOOKING_CATEGORIES":
            return [{"bookingCategoryId": 0, "capacityCategoryId": 99}]
        if endpoint_name == "OCCUPANCY":
            # Only the BOOKABLE site is truly web-bookable (availability 0).
            # The PHANTOM site is "Unavailable" (2) per occupancy, so dropped.
            return {"resourceOccupancy": [
                {"resourceId": self.BOOKABLE, "availability": 0},
                {"resourceId": self.PHANTOM, "availability": 2},
            ]}
        if endpoint_name == "MAPDATA":
            mid = params["mapId"]
            if mid == self.ROOT:
                return {
                    "mapId": self.ROOT,
                    "resourceAvailabilities": {},
                    "mapLinkAvailabilities": {str(self.CHILD): []},
                    "mapAvailabilities": [],
                }
            if mid == self.CHILD:
                # 31-night window: first 3 nights open (0), rest closed (3).
                # BOTH sites look MAPDATA-available; occupancy must separate them.
                slots = [{"availability": 0}] * 3 + [{"availability": 3}] * 28
                return {
                    "mapId": self.CHILD,
                    "resourceAvailabilities": {
                        self.BOOKABLE: slots,
                        self.PHANTOM: slots,
                    },
                    "mapLinkAvailabilities": {},
                    "mapAvailabilities": [],
                }
        raise AssertionError(f"unexpected endpoint {endpoint_name}")


def test_gtc_available_nights_offline(monkeypatch):
    fake = _FakeProvider()
    # Make watch.py's _gtc.GoingToCamp() return our fake; clear rootmap cache.
    monkeypatch.setattr(watch._gtc, "GoingToCamp", lambda *a, **k: fake)

    start = dt.date(2026, 9, 20)
    end = start + dt.timedelta(days=31)
    by_site = watch.gtc_available_nights(_FakeProvider.REC, _FakeProvider.FID,
                                         start, end)

    # Both sites are MAPDATA-available, but occupancy says only BOOKABLE is
    # truly web-bookable. The PHANTOM site (walk-in/host) must be dropped.
    assert len(by_site) == 1, f"phantom site not filtered: {by_site}"
    (label, dates), = by_site.items()
    # label is abs(resourceId) % 100000 of the BOOKABLE site
    assert label == str(abs(int(_FakeProvider.BOOKABLE)) % 100000)
    assert dates == {start, start + dt.timedelta(days=1),
                     start + dt.timedelta(days=2)}

    # the run-builder should see a single 3-night run >= MIN_NIGHTS
    runs = watch.consecutive_runs(dates)
    assert runs == [(start, 3)]

    # occupancy cross-check must have been queried
    endpoints = {c[0] for c in fake.calls}
    assert "OCCUPANCY" in endpoints
    # and prove SITE_DETAILS / resource/details was never requested
    assert "SITE_DETAILS" not in endpoints
    assert "MAPDATA" in endpoints
    assert "LIST_CAMPGROUNDS" in endpoints


@pytest.mark.skipif(
    os.environ.get("CAMPLY_LIVE") != "1",
    reason="set CAMPLY_LIVE=1 to run the live GoingToCamp API test",
)
def test_gtc_available_nights_live():
    """Live: hit the real WA State Parks API for one park; assert no 400/404.

    Fort Ebey (-2147483616) is a less-saturated park that reliably has
    availability; we just assert the call SUCCEEDS and returns parsed nights.
    """
    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    by_site = watch.gtc_available_nights(3, -2147483616, start, end)
    # The mechanism must succeed (no exception). Availability itself may vary,
    # but Fort Ebey is rarely fully booked 90 days out.
    assert isinstance(by_site, dict)


# --------------------------------------------------------------------------- #
# 3. Instant webhook notification (mocked HTTP)
# --------------------------------------------------------------------------- #
def test_send_trigger_disabled_is_noop(monkeypatch):
    """When notifications are disabled, no HTTP call is made and it returns False."""
    monkeypatch.setattr(watch, "NOTIFY_ENABLED", False)
    monkeypatch.setattr(watch, "WEBHOOK_URL", "https://example.com/hook")

    def boom(*a, **k):
        raise AssertionError("urlopen must not be called when disabled")

    monkeypatch.setattr(watch.urllib.request, "urlopen", boom)
    assert watch.send_trigger("X", "u", ["a|2026-07-10|2"], []) is False


def test_send_trigger_noop_without_url(monkeypatch):
    """With no webhook URL configured, it is a silent no-op (returns False)."""
    monkeypatch.setattr(watch, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(watch, "WEBHOOK_URL", "")
    assert watch.send_trigger("X", "u", ["a|2026-07-10|2"], []) is False


def test_send_trigger_fails_soft(monkeypatch):
    """A transport error returns False and never raises."""
    monkeypatch.setattr(watch, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(watch, "WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setattr(watch, "SENT_PINGS", HERE / "sent_pings.test.json")

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(watch.urllib.request, "urlopen", boom)
    assert watch.send_trigger("X", "u", ["a|2026-07-10|2"], []) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))