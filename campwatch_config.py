#!/usr/bin/env python3
"""Load local campground-watcher settings without executing config as code."""
from __future__ import annotations

import json
import os
from pathlib import Path


HERE = Path(__file__).resolve().parent
PRIVATE_SETTINGS_FILE = HERE / "secrets" / "config.json"

SETTING_ENV_NAMES = {
    "home_lat": "CAMPWATCH_HOME_LAT",
    "home_lon": "CAMPWATCH_HOME_LON",
    "webhook_url": "CAMPWATCH_WEBHOOK_URL",
    "webhook_text_key": "CAMPWATCH_WEBHOOK_TEXT_KEY",
    "notify": "CAMPWATCH_NOTIFY",
}


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
