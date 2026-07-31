#!/usr/bin/env python3
"""Offline tests for the dependency-free campground watcher."""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

import campwatch_http
import campwatch_config
import build_candidates
import availability_map
import run_watch
import watch


class FakeHttpResponse:
    def __init__(self, body=b"{}", *, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit):
        return self.body[:limit]


class FakeOpener:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class HttpTransportTests(unittest.TestCase):
    HOST = "api.example.com"

    def client(self, opener, **kwargs):
        return campwatch_http.JsonHttpClient(
            allowed_hosts={self.HOST},
            opener=opener,
            sleeper=kwargs.pop("sleeper", lambda delay: None),
            randomizer=kwargs.pop("randomizer", lambda: 0.0),
            **kwargs,
        )

    def test_destination_boundary_rejects_unsafe_url_forms(self):
        client = self.client(FakeOpener())
        unsafe = [
            "http://api.example.com/data",
            "https://other.example.com/data",
            "https://user@api.example.com/data",
            "https://api.example.com:444/data",
            "https://api.example.com/data?embedded=true",
            "https://api.example.com/data#fragment",
        ]
        for url in unsafe:
            with self.subTest(url=url), self.assertRaises(ValueError):
                client.get_json(url)

    def test_request_uses_allowlisted_url_and_encodes_parameters(self):
        opener = FakeOpener(FakeHttpResponse(b'{"ok": true}'))
        client = self.client(opener)
        self.assertEqual(
            client.get_json(
                "https://api.example.com/data",
                params={"flag": True, "filter": {"a": 1}},
            ),
            {"ok": True},
        )
        request, timeout = opener.calls[0]
        self.assertEqual(timeout, 30)
        self.assertEqual(request.host, self.HOST)
        self.assertIn("flag=true", request.full_url)
        self.assertIn("filter=%7B%22a%22%3A1%7D", request.full_url)

    def test_redirect_status_is_not_followed_or_retried(self):
        headers = Message()
        headers["Location"] = "https://other.example.com/"
        redirect = urllib.error.HTTPError(
            "https://api.example.com/data", 302, "redirect", headers, None
        )
        opener = FakeOpener(redirect)
        client = self.client(opener)
        with self.assertRaises(campwatch_http.HttpRequestError) as raised:
            client.get_json("https://api.example.com/data")
        self.assertEqual(raised.exception.status, 302)
        self.assertEqual(len(opener.calls), 1)

    def test_retryable_status_honors_capped_retry_after(self):
        sleeps = []
        opener = FakeOpener(
            FakeHttpResponse(b"busy", status=503, headers={"Retry-After": "20"}),
            FakeHttpResponse(b'{"ok": true}'),
        )
        client = self.client(opener, max_retry_delay=3, sleeper=sleeps.append)
        self.assertEqual(
            client.get_json("https://api.example.com/data"), {"ok": True}
        )
        self.assertEqual(sleeps, [3])
        self.assertEqual(len(opener.calls), 2)

    def test_transport_retry_uses_jittered_backoff(self):
        sleeps = []
        opener = FakeOpener(
            urllib.error.URLError("offline"),
            FakeHttpResponse(b'{"ok": true}'),
        )
        client = self.client(
            opener, sleeper=sleeps.append, randomizer=lambda: 0.5
        )
        self.assertEqual(
            client.get_json("https://api.example.com/data"), {"ok": True}
        )
        self.assertEqual(sleeps, [1.125])

    def test_retry_wait_budget_prevents_another_attempt(self):
        opener = FakeOpener(
            FakeHttpResponse(b"busy", status=503),
            FakeHttpResponse(b'{"should_not": "run"}'),
        )
        client = self.client(opener, retry_wait_budget=0)
        with self.assertRaises(campwatch_http.HttpRequestError):
            client.get_json("https://api.example.com/data")
        self.assertEqual(len(opener.calls), 1)

    def test_malformed_json_is_not_retried(self):
        opener = FakeOpener(FakeHttpResponse(b"<html>blocked</html>"))
        client = self.client(opener)
        with self.assertRaisesRegex(
            campwatch_http.HttpRequestError, "non-JSON response"
        ):
            client.get_json("https://api.example.com/data")
        self.assertEqual(len(opener.calls), 1)

    def test_oversized_body_is_rejected_without_retry(self):
        opener = FakeOpener(FakeHttpResponse(b"123456789"))
        client = self.client(opener)
        with (
            mock.patch.object(campwatch_http, "MAX_JSON_BYTES", 8),
            self.assertRaisesRegex(campwatch_http.HttpRequestError, "exceeded"),
        ):
            client.get_json("https://api.example.com/data")
        self.assertEqual(len(opener.calls), 1)


class ProviderClientTests(unittest.TestCase):
    def test_osm_router_uses_bounded_distance_table(self):
        http = mock.Mock()
        http.get_json.return_value = {"distances": [[0, 12_345, None]]}
        router = campwatch_http.OsmDrivingRouter(http=http)
        self.assertEqual(
            router.driving_distances_km(47.0, -122.0, [(47.1, -122.1), (47.2, -122.2)]),
            [12.345, None],
        )
        url = http.get_json.call_args.args[0]
        self.assertTrue(url.startswith("https://routing.openstreetmap.de/routed-car/table/v1/driving/"))
        self.assertEqual(http.get_json.call_args.kwargs["params"], {
            "sources": "0", "annotations": "distance"
        })

    def test_going_to_camp_rejects_malformed_endpoint_schema(self):
        http = mock.Mock()
        http.get_json.return_value = {"resourceLocations": []}
        client = campwatch_http.GoingToCampClient(http=http)
        with self.assertRaises(campwatch_http.HttpRequestError):
            client.request_json(3, "LIST_CAMPGROUNDS")

    def test_recreation_gov_rejects_malformed_campsite_schema(self):
        http = mock.Mock()
        http.get_json.return_value = {
            "campsites": {"123": {"availabilities": "not-a-map"}}
        }
        client = campwatch_http.RecreationGovClient(http=http)
        with self.assertRaises(campwatch_http.HttpRequestError):
            client.month(123, dt.date(2026, 8, 1))


class AvailabilityMapTests(unittest.TestCase):
    def test_map_data_joins_available_state_with_local_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {
                "config_path": root / "watch_config.json",
                "state_path": root / "last_state.json",
                "progress_path": root / "scan_progress.json",
                "candidates_path": root / "candidates.json",
                "wa_parks_path": root / "wa_parks.json",
            }
            paths["config_path"].write_text(
                json.dumps(
                    {
                        "recdotgov": [
                            {"id": 1, "name": "Federal Camp", "distance_km": 16.1, "dist_mi": 10}
                        ],
                        "going_to_camp": [
                            {
                                "id": -2,
                                "name": "State Park",
                                "rec_area": 3,
                                "root_map_id": -3,
                                "est_drive_hrs": 1.2,
                            }
                        ],
                    }
                )
            )
            paths["state_path"].write_text(
                json.dumps(
                    {
                        "rg:1": [
                            *[f"A|2026-08-{day:02d}|2" for day in range(1, 14)],
                            "B|2026-08-08|3",
                        ],
                        "wa:-2": ["-20|2026-08-09|2"],
                    }
                )
            )
            paths["progress_path"].write_text(
                json.dumps(
                    {
                        "status": "running",
                        "completed": 4,
                        "total": 10,
                        "signature": "private-internal-value",
                        "pid": 123,
                    }
                )
            )
            paths["candidates_path"].write_text(
                json.dumps([{"id": 1, "lat": 47.1, "lon": -122.1}])
            )
            paths["wa_parks_path"].write_text(
                json.dumps([{"facility_id": -2, "lat": 47.2, "lon": -122.2}])
            )

            data = availability_map.build_map_data(**paths)
            self.assertEqual([item["name"] for item in data["locations"]], [
                "Federal Camp", "State Park"
            ])
            self.assertEqual(data["locations"][0]["available_sites"], 2)
            self.assertEqual(len(data["locations"][0]["runs"]), 14)
            self.assertIn("recreation.gov", data["locations"][0]["booking_url"])
            self.assertIn("goingtocamp.com", data["locations"][1]["booking_url"])
            self.assertEqual(data["progress"], {
                "status": "running", "completed": 4, "total": 10
            })
            self.assertNotIn("signature", data["progress"])
            self.assertIsNotNone(data["data_updated_at"])
            self.assertEqual(data["bounds"]["north"], 47.2)

    def test_map_html_uses_no_external_javascript_and_attributes_tiles(self):
        self.assertNotIn("<script src=", availability_map.MAP_HTML)
        self.assertIn("https://tile.openstreetmap.org/{z}/{x}/{y}.png", availability_map.MAP_HTML)
        self.assertIn("© OpenStreetMap contributors", availability_map.MAP_HTML)
        self.assertIn('id="date-from" type="date"', availability_map.MAP_HTML)
        self.assertIn('id="date-through" type="date"', availability_map.MAP_HTML)
        self.assertIn('id="popup" role="dialog"', availability_map.MAP_HTML)
        self.assertIn('className = "run-table"', availability_map.MAP_HTML)
        self.assertIn('["Check in", "Available stays"]', availability_map.MAP_HTML)
        self.assertIn('function availabilityDateGroups(location)', availability_map.MAP_HTML)
        self.assertIn('function makeStayChip(location, run)', availability_map.MAP_HTML)
        self.assertIn('className = "run-table-shell"', availability_map.MAP_HTML)
        self.assertIn('Stale availability data:', availability_map.MAP_HTML)
        self.assertNotIn('id="hover-card"', availability_map.MAP_HTML)
        self.assertNotIn('hoverPinned', availability_map.MAP_HTML)
        self.assertIn('function closeLocation()', availability_map.MAP_HTML)


class CandidateDiscoveryTests(unittest.TestCase):
    class Client:
        def __init__(self, response):
            self.response = response
            self.calls = []

        def search(self, params):
            self.calls.append(params)
            return self.response

    def test_fetch_modes_only_change_the_upstream_query(self):
        response = {"results": [], "total": 0}
        server = self.Client(response)
        local = self.Client(response)
        common = {
            "home_lat": 47.0,
            "home_lon": -122.0,
            "max_distance_km": 90.0,
            "sleeper": lambda delay: None,
        }
        build_candidates.fetch_catalog(
            server, distance_filter="server", **common
        )
        build_candidates.fetch_catalog(
            local, distance_filter="client", **common
        )
        drive = self.Client(response)
        build_candidates.fetch_catalog(drive, distance_filter="drive", **common)
        self.assertEqual(server.calls[0]["radius"], 90.0)
        self.assertEqual(server.calls[0]["lat"], 47.0)
        self.assertEqual(server.calls[0]["lng"], -122.0)
        self.assertNotIn("lat", local.calls[0])
        self.assertNotIn("lng", local.calls[0])
        self.assertEqual(local.calls[0]["q"], "Washington")
        self.assertNotIn("lat", drive.calls[0])
        self.assertEqual(drive.calls[0]["q"], "Washington")

    def test_common_selection_uses_kilometers_and_reports_both_units(self):
        valid = {
            "entity_id": "123",
            "entity_type": "campground",
            "state_code": "Washington",
            "name": "Example Campground",
            "latitude": 47.45,
            "longitude": -122.0,
            "reservable": True,
            "campsites_count": "10",
            "campsite_type_of_use": ["Overnight"],
            "average_rating": 4.5,
        }
        too_far = dict(valid, entity_id="456", latitude=48.0)
        candidates = build_candidates.select_candidates(
            [valid, too_far],
            home_lat=47.0,
            home_lon=-122.0,
            max_distance_km=90.0,
        )
        self.assertEqual([item["id"] for item in candidates], ["123"])
        self.assertAlmostEqual(candidates[0]["distance_km"], 50.0, delta=0.2)
        self.assertAlmostEqual(candidates[0]["dist_mi"], 31.1, delta=0.2)

    def test_drive_selection_uses_road_distance_and_never_server_filtering(self):
        class Router:
            def __init__(self):
                self.calls = []

            def driving_distances_km(self, lat, lon, destinations):
                self.calls.append((lat, lon, destinations))
                return [72.5, 95.0]

        base = {
            "entity_type": "campground",
            "state_code": "Washington",
            "reservable": True,
            "campsites_count": "10",
            "campsite_type_of_use": ["Overnight"],
            "average_rating": 4.5,
            "longitude": -122.0,
        }
        router = Router()
        candidates = build_candidates.select_candidates(
            [
                dict(base, entity_id="123", name="Near", latitude=47.45),
                dict(base, entity_id="456", name="Far", latitude=47.50),
            ],
            home_lat=47.0,
            home_lon=-122.0,
            max_distance_km=90.0,
            distance_filter="drive",
            router=router,
        )
        self.assertEqual([item["id"] for item in candidates], ["123"])
        self.assertEqual(candidates[0]["distance_method"], "driving")
        self.assertEqual(candidates[0]["distance_km"], 72.5)
        self.assertEqual(len(router.calls), 1)


class WatchLogicTests(unittest.TestCase):
    def setUp(self):
        watch._booking_categories_cache.clear()
        watch._sub_equipment_cache.clear()

    def test_is_group(self):
        self.assertTrue(watch.is_group("Group Site A"))
        self.assertTrue(watch.is_group("Equestrian", "HORSE CAMP"))
        self.assertTrue(watch.is_group("Overflow Lot"))
        self.assertFalse(watch.is_group("Site 042"))

    def test_consecutive_runs(self):
        day = dt.date(2026, 7, 1)
        dates = {day, day + dt.timedelta(days=1), day + dt.timedelta(days=5)}
        self.assertEqual(
            watch.consecutive_runs(dates),
            [(day, 2), (day + dt.timedelta(days=5), 1)],
        )

    def test_booking_urls(self):
        url = watch.gtc_booking_url(-10, -20, "2026-08-07", 2)
        self.assertTrue(url.startswith("https://washington.goingtocamp.com/"))
        self.assertIn("startDate=2026-08-07", url)
        self.assertIn("endDate=2026-08-09", url)
        self.assertEqual(
            watch.recgov_booking_url(232064, "2026-08-07", 2),
            "https://www.recreation.gov/camping/campgrounds/232064/availability",
        )

    def test_target_matching_and_adaptive_cadence(self):
        target = dt.date(2026, 8, 7)
        targets = [("weekend", [target, target + dt.timedelta(days=1)])]
        with mock.patch.object(watch, "TARGET_WEEKENDS", targets):
            self.assertEqual(watch.covers_target_weekend(target, 2), "weekend")
            self.assertIsNone(watch.covers_target_weekend(target, 1))
            self.assertEqual(
                watch.recommended_poll_minutes(dt.date(2026, 8, 1)), 10
            )
            self.assertEqual(
                watch.recommended_poll_minutes(dt.date(2026, 7, 15)), 30
            )
            self.assertEqual(
                watch.recommended_poll_minutes(dt.date(2026, 6, 1)), 60
            )
            self.assertTrue(watch.targets_in_watch_window(dt.date(2026, 8, 1)))
            self.assertFalse(watch.targets_in_watch_window(dt.date(2026, 1, 1)))
        with mock.patch.object(watch, "TARGET_WEEKENDS", []):
            self.assertIsNone(watch.recommended_poll_minutes(dt.date(2026, 8, 1)))

    def test_load_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.json"
            path.write_text(
                json.dumps(
                    {
                        "watch_all": False,
                        "weekends": [
                            {"label": "trip", "nights": ["2026-08-07", "2026-08-08"]}
                        ],
                    }
                )
            )
            self.assertEqual(
                watch.load_target_weekends(path),
                [
                    (
                        "trip",
                        [dt.date(2026, 8, 7), dt.date(2026, 8, 8)],
                    )
                ],
            )

    def test_schedule_reacts_immediately_when_private_targets_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            targets = Path(tmp) / "targets.json"
            state = Path(tmp) / "schedule.json"
            targets.write_text('{"watch_all": false, "weekends": []}')
            with (
                mock.patch.object(watch, "TARGETS_FILE", targets),
                mock.patch.object(watch, "SCHEDULE_STATE", state),
            ):
                now = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
                watch.record_next_poll(60, now=now)
                self.assertFalse(
                    watch.scheduled_poll_due(now + dt.timedelta(minutes=30))
                )
                targets.write_text('{"watch_all": true, "weekends": []}')
                self.assertTrue(
                    watch.scheduled_poll_due(now + dt.timedelta(minutes=30))
                )

    def test_incremental_checkpoint_preserves_unprocessed_previous_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "last_state.json"
            previous = {"rg:1": ["old"], "wa:2": ["not-processed-yet"]}
            current = {"rg:1": ["new"]}
            with mock.patch.object(watch, "STATE_FILE", state_file):
                watch._write_state_checkpoint(previous, current)
                self.assertEqual(
                    json.loads(state_file.read_text()),
                    {"rg:1": ["new"], "wa:2": ["not-processed-yet"]},
                )
                watch._write_state_checkpoint(previous, current, complete=True)
                self.assertEqual(json.loads(state_file.read_text()), current)

    def test_incremental_state_is_not_the_completed_alert_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "last_state.json"
            complete_file = Path(tmp) / "last_complete_state.json"
            state_file.write_text('{"rg:1": ["partial-new-result"]}')
            complete_file.write_text('{"rg:1": ["last-complete-result"]}')
            with (
                mock.patch.object(watch, "STATE_FILE", state_file),
                mock.patch.object(watch, "COMPLETE_STATE_FILE", complete_file),
            ):
                self.assertEqual(
                    watch._load_complete_state(),
                    {"rg:1": ["last-complete-result"]},
                )
                complete_file.unlink()
                self.assertEqual(
                    watch._load_complete_state(),
                    {"rg:1": ["partial-new-result"]},
                )

    def test_compatible_checkpoint_resumes_completed_target_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress_file = Path(tmp) / "scan_progress.json"
            state_file = Path(tmp) / "last_state.json"
            state_file.write_text(
                json.dumps({"rg:1": ["saved"], "rg:2": ["not-checkpointed"]})
            )
            progress_file.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "signature": "same-scan",
                        "completed_keys": ["rg:1"],
                    }
                )
            )
            with (
                mock.patch.object(watch, "SCAN_PROGRESS", progress_file),
                mock.patch.object(watch, "STATE_FILE", state_file),
            ):
                self.assertEqual(
                    watch._load_resume_checkpoint("same-scan"),
                    (["rg:1"], {"rg:1": ["saved"]}),
                )
                self.assertEqual(watch._load_resume_checkpoint("changed-scan"), ([], {}))

    def test_resumed_state_rebuilds_summary_and_new_alerts(self):
        cfg = {
            "recdotgov": [
                {"id": 1, "name": "Example", "rating": 4.5, "dist_mi": 10.0}
            ],
            "going_to_camp": [],
        }
        state = {"rg:1": ["A|2026-08-07|2"]}
        with mock.patch.object(watch, "TARGET_WEEKENDS", None):
            summary, alerts = watch._summary_from_state(cfg, state, {})
        self.assertEqual(summary[0]["runs"][0]["site"], "A")
        self.assertEqual(alerts[0]["new_runs"], ["A|2026-08-07|2"])

    def test_main_skips_targets_from_a_compatible_resume_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file = root / "watch_config.json"
            state_file = root / "last_state.json"
            complete_file = root / "last_complete_state.json"
            progress_file = root / "scan_progress.json"
            today = dt.date.today()
            saved_run = f"A|{today.isoformat()}|2"
            cfg = {
                "recdotgov": [
                    {"id": 1, "name": "Already done", "rating": 4.5, "dist_mi": 1},
                    {"id": 2, "name": "Still pending", "rating": 4.5, "dist_mi": 2},
                ],
                "going_to_camp": [],
            }
            config_file.write_text(json.dumps(cfg))
            state_file.write_text(json.dumps({"rg:1": [saved_run]}))
            complete_file.write_text(json.dumps({"rg:1": [saved_run]}))
            with mock.patch.object(watch, "TARGET_WEEKENDS", None):
                signature = watch._scan_signature(
                    cfg, today, today + dt.timedelta(days=watch.WINDOW_DAYS)
                )
            progress_file.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "signature": signature,
                        "completed_keys": ["rg:1"],
                    }
                )
            )
            with (
                mock.patch.object(watch, "TARGET_WEEKENDS", None),
                mock.patch.object(watch, "CONFIG", config_file),
                mock.patch.object(watch, "STATE_FILE", state_file),
                mock.patch.object(watch, "COMPLETE_STATE_FILE", complete_file),
                mock.patch.object(watch, "SCAN_PROGRESS", progress_file),
                mock.patch.object(watch, "RecreationGovClient", return_value=mock.Mock()),
                mock.patch.object(watch, "GoingToCampClient", return_value=mock.Mock()),
                mock.patch.object(watch, "recgov_available_nights", return_value={}) as poll,
                mock.patch("builtins.print"),
            ):
                self.assertEqual(watch.main(), 0)
            self.assertEqual(poll.call_count, 1)
            self.assertEqual(poll.call_args.args[1], 2)
            self.assertEqual(
                json.loads(state_file.read_text()),
                {"rg:1": [saved_run], "rg:2": []},
            )
            self.assertEqual(json.loads(progress_file.read_text())["status"], "complete")


class FakeRecClient:
    def month(self, campground_id, month):
        return {
            "count": 2,
            "campsites": {
                "100": {
                    "site": "A|1",
                    "campsite_type": "STANDARD",
                    "availabilities": {
                        "2026-08-07T00:00:00Z": "Available",
                        "2026-08-08T00:00:00Z": "Reserved",
                    },
                },
                "101": {
                    "site": "Group Site",
                    "campsite_type": "GROUP",
                    "availabilities": {"2026-08-07T00:00:00Z": "Available"},
                },
            },
        }


class RecreationGovTests(unittest.TestCase):
    def test_only_explicit_available_non_group_nights_are_kept(self):
        by_site = watch.recgov_available_nights(
            FakeRecClient(),
            123,
            dt.date(2026, 8, 1),
            dt.date(2026, 9, 1),
        )
        self.assertEqual(by_site, {"A/1 (#100)": {dt.date(2026, 8, 7)}})


class FakeGoingToCampClient:
    REC = 3
    FID = -100
    ROOT = -200
    CHILD = -300
    BOOKABLE = "-400"
    PHANTOM = "-401"

    def __init__(self, fail_occupancy=False):
        self.fail_occupancy = fail_occupancy
        self.calls = []

    def request_json(self, rec_area_id, endpoint_name, params=None):
        self.calls.append((endpoint_name, params))
        if endpoint_name == "LIST_CAMPGROUNDS":
            return [{"resourceLocationId": self.FID, "rootMapId": self.ROOT}]
        if endpoint_name == "LIST_EQUIPMENT":
            return [
                {
                    "equipmentCategoryId": watch.NON_GROUP_EQUIPMENT,
                    "subEquipmentCategories": [{"subEquipmentCategoryId": -32768}],
                }
            ]
        if endpoint_name == "BOOKING_CATEGORIES":
            return [{"bookingCategoryId": 0, "capacityCategoryId": 99}]
        if endpoint_name == "OCCUPANCY":
            if self.fail_occupancy:
                raise OSError("offline")
            return {
                "resourceOccupancy": [
                    {"resourceId": self.BOOKABLE, "availability": 0},
                    {"resourceId": self.PHANTOM, "availability": 2},
                ]
            }
        if endpoint_name == "MAPDATA":
            if params["mapId"] == self.ROOT:
                return {
                    "resourceAvailabilities": {},
                    "mapLinkAvailabilities": {str(self.CHILD): []},
                }
            slots = [{"availability": 0}] * 3 + [{"availability": 3}] * 5
            return {
                "resourceAvailabilities": {
                    self.BOOKABLE: slots,
                    self.PHANTOM: slots,
                },
                "mapLinkAvailabilities": {},
            }
        raise AssertionError(f"unexpected endpoint {endpoint_name}")


class GoingToCampTests(unittest.TestCase):
    def setUp(self):
        watch._booking_categories_cache.clear()
        watch._sub_equipment_cache.clear()

    def test_occupancy_removes_phantom_site(self):
        client = FakeGoingToCampClient()
        start = dt.date(2026, 8, 1)
        by_site = watch.gtc_available_nights(
            client.REC,
            client.FID,
            start,
            start + dt.timedelta(days=8),
            client=client,
        )
        self.assertEqual(
            by_site,
            {
                client.BOOKABLE: {
                    start,
                    start + dt.timedelta(days=1),
                    start + dt.timedelta(days=2),
                }
            },
        )

    def test_occupancy_failure_is_fail_closed(self):
        client = FakeGoingToCampClient(fail_occupancy=True)
        start = dt.date(2026, 8, 1)
        with self.assertRaises(watch.AvailabilityVerificationError):
            watch.gtc_available_nights(
                client.REC,
                client.FID,
                start,
                start + dt.timedelta(days=8),
                client=client,
            )


class WebhookTests(unittest.TestCase):
    def test_http_and_private_destinations_are_rejected(self):
        with self.assertRaises(ValueError):
            watch.validate_webhook_url("http://example.com/hook")
        private_answer = [(None, None, None, None, ("127.0.0.1", 443))]
        with mock.patch.object(watch.socket, "getaddrinfo", return_value=private_answer):
            with self.assertRaises(ValueError):
                watch.validate_webhook_url("https://example.com/hook")

    def test_public_https_destination_is_accepted(self):
        public_answer = [(None, None, None, None, ("93.184.216.34", 443))]
        with mock.patch.object(watch.socket, "getaddrinfo", return_value=public_answer):
            self.assertEqual(
                watch.validate_webhook_url("https://example.com/hook"),
                "https://example.com/hook",
            )

    def test_send_trigger_fails_soft_without_leaking_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_url = "https://example.com/a-secret-token"
            with (
                mock.patch.object(watch, "NOTIFY_ENABLED", True),
                mock.patch.object(watch, "WEBHOOK_URL", secret_url),
                mock.patch.object(watch, "SENT_PINGS", Path(tmp) / "pings.json"),
                mock.patch.object(watch, "_post_webhook", side_effect=OSError("boom")),
                mock.patch.object(watch, "log") as logger,
            ):
                self.assertFalse(watch.send_trigger("X", "u", ["a|2026-08-07|2"], []))
                logged = " ".join(str(call) for call in logger.call_args_list)
                self.assertNotIn(secret_url, logged)


class RunnerTests(unittest.TestCase):
    def test_log_rotation_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cron.log"
            path.write_text("x" * 100)
            run_watch.rotate_log(path, max_bytes=50, backups=2)
            self.assertFalse(path.exists())
            self.assertTrue((Path(tmp) / "cron.log.1").exists())

    def test_runner_uses_current_python_and_fixed_local_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = mock.Mock(returncode=0)
            with (
                mock.patch.object(run_watch, "HERE", root),
                mock.patch.object(run_watch, "LOCK_FILE", root / ".run.lock"),
                mock.patch.object(run_watch, "LOG_FILE", root / "cron.log"),
                mock.patch.object(run_watch.subprocess, "run", return_value=result) as call,
            ):
                self.assertEqual(run_watch.main(), 0)
                command = call.call_args.args[0]
                self.assertEqual(command[0], run_watch.sys.executable)
                self.assertEqual(command[1:], [str(root / "watch.py"), "--scheduled"])

    def test_private_settings_require_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"webhook_url": "https://example.com/hook"}')
            path.chmod(0o644)
            with self.assertRaises(RuntimeError):
                run_watch.load_private_settings({}, path)
            path.chmod(0o600)
            env = {}
            campwatch_config.load_private_settings(env, path)
            self.assertEqual(env["CAMPWATCH_WEBHOOK_URL"], "https://example.com/hook")


if __name__ == "__main__":
    unittest.main(verbosity=2)
