#!/usr/bin/env python3
"""Load local campground-watcher settings without executing config as code."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
PRIVATE_SETTINGS_FILE = HERE / "secrets" / "config.json"
PROVIDER_RULES_FILE = HERE / "provider_rules.json"

SETTING_ENV_NAMES = {
    "home_lat": "CAMPWATCH_HOME_LAT",
    "home_lon": "CAMPWATCH_HOME_LON",
    "webhook_url": "CAMPWATCH_WEBHOOK_URL",
    "webhook_text_key": "CAMPWATCH_WEBHOOK_TEXT_KEY",
    "notify": "CAMPWATCH_NOTIFY",
}


def _required_string(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"provider_rules.json requires a non-empty {description}")
    return value.strip()


def _positive_number(value: Any, description: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"provider_rules.json requires a positive {description}") from exc
    if number <= 0:
        raise RuntimeError(f"provider_rules.json requires a positive {description}")
    return number


def _string_list(value: Any, description: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RuntimeError(f"provider_rules.json requires {description} as a list")
    values = tuple(_required_string(item, description).upper() for item in value)
    if not values:
        raise RuntimeError(f"provider_rules.json requires at least one {description}")
    return values


def load_provider_rules(path: Path = PROVIDER_RULES_FILE) -> dict[str, Any]:
    """Load and validate editable provider labels and filtering heuristics."""
    try:
        raw = json.loads(path.read_text())
        recreation_gov = raw["recreation_gov"]
        going_to_camp = raw["going_to_camp"]
        watch = raw["watch"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("provider_rules.json is missing or invalid") from exc
    if not all(isinstance(value, dict) for value in (recreation_gov, going_to_camp, watch)):
        raise RuntimeError("provider_rules.json sections must be JSON objects")

    raw_rec_areas = going_to_camp.get("rec_areas")
    if not isinstance(raw_rec_areas, dict) or not raw_rec_areas:
        raise RuntimeError("provider_rules.json requires going_to_camp.rec_areas")
    rec_areas = {}
    for area_id, label in raw_rec_areas.items():
        try:
            parsed_area_id = int(area_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("provider_rules.json rec-area IDs must be integers") from exc
        if parsed_area_id <= 0:
            raise RuntimeError("provider_rules.json rec-area IDs must be positive")
        rec_areas[parsed_area_id] = _required_string(label, "rec-area label")

    estimated_drive = going_to_camp.get("estimated_drive")
    if not isinstance(estimated_drive, dict):
        raise RuntimeError("provider_rules.json requires going_to_camp.estimated_drive")
    return {
        "recreation_gov": {
            "state_code": _required_string(
                recreation_gov.get("state_code"), "recreation_gov.state_code"
            ).upper(),
            "state_name": _required_string(
                recreation_gov.get("state_name"), "recreation_gov.state_name"
            ),
            "minimum_rating": _positive_number(
                recreation_gov.get("minimum_rating"), "recreation_gov.minimum_rating"
            ),
            "default_max_distance_km": _positive_number(
                recreation_gov.get("default_max_distance_km"),
                "recreation_gov.default_max_distance_km",
            ),
        },
        "going_to_camp": {
            "rec_areas": rec_areas,
            "non_camp_markers": _string_list(
                going_to_camp.get("non_camp_markers"), "going_to_camp.non_camp_markers"
            ),
            "estimated_drive": {
                "max_hours": _positive_number(
                    estimated_drive.get("max_hours"), "estimated_drive.max_hours"
                ),
                "margin_hours": _positive_number(
                    estimated_drive.get("margin_hours"), "estimated_drive.margin_hours"
                ),
                "road_factor": _positive_number(
                    estimated_drive.get("road_factor"), "estimated_drive.road_factor"
                ),
                "average_kmh": _positive_number(
                    estimated_drive.get("average_kmh"), "estimated_drive.average_kmh"
                ),
            },
        },
        "watch": {
            "group_markers": _string_list(watch.get("group_markers"), "watch.group_markers"),
        },
    }


def home_coordinates(env: dict[str, str]) -> tuple[float, float]:
    """Read required, valid home coordinates from private settings or the environment."""
    names = ("CAMPWATCH_HOME_LAT", "CAMPWATCH_HOME_LON")
    missing = [name for name in names if not env.get(name, "").strip()]
    if missing:
        raise RuntimeError(
            "set CAMPWATCH_HOME_LAT and CAMPWATCH_HOME_LON in the environment "
            "or secrets/config.json"
        )
    try:
        latitude = float(env[names[0]])
        longitude = float(env[names[1]])
    except ValueError as exc:
        raise RuntimeError("home coordinates must be numeric") from exc
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise RuntimeError("home coordinates are out of range")
    return latitude, longitude


def load_private_settings(
    env: dict[str, str], path: Path = PRIVATE_SETTINGS_FILE
) -> None:
    """Copy known settings from a non-symlinked, owner-only JSON file."""
    if not path.exists():
        return
    stat = path.lstat()
    if path.is_symlink() or not path.is_file() or stat.st_mode & 0o077:
        raise RuntimeError("secrets/config.json must be a regular mode-600 file")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError("secrets/config.json must contain a JSON object")
    unknown = set(data) - set(SETTING_ENV_NAMES)
    if unknown:
        raise RuntimeError("secrets/config.json contains unsupported settings")
    for key, env_name in SETTING_ENV_NAMES.items():
        if env_name not in env and key in data:
            env[env_name] = str(data[key])


def local_environment(path: Path = PRIVATE_SETTINGS_FILE) -> dict[str, str]:
    """Return process settings overlaid with the optional private JSON file."""
    env = dict(os.environ)
    load_private_settings(env, path)
    return env
