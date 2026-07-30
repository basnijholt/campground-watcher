#!/usr/bin/env python3
"""Portable scheduled-run wrapper with locking and bounded logs.

Uses only the Python standard library and works on macOS and Linux.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

from campwatch_config import PRIVATE_SETTINGS_FILE, load_private_settings


HERE = Path(__file__).resolve().parent
LOCK_FILE = HERE / ".run.lock"
LOG_FILE = HERE / "cron.log"
SECRETS_FILE = PRIVATE_SETTINGS_FILE


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def rotate_log(path: Path, max_bytes: int, backups: int) -> None:
    """Keep at most `backups` old files; refuse to operate on symlinks."""
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    paths = [path.with_name(f"{path.name}.{index}") for index in range(1, backups + 1)]
    if path.is_symlink() or any(item.is_symlink() for item in paths if item.exists()):
        raise RuntimeError("refusing to rotate a symlinked log")
    oldest = paths[-1]
    if oldest.exists():
        oldest.unlink()
    for source, target in zip(reversed(paths[:-1]), reversed(paths[1:])):
        if source.exists():
            os.replace(source, target)
    os.replace(path, paths[0])


def _open_private_append(path: Path):
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "a", buffering=1)


def main() -> int:
    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    lock_fd = os.open(LOCK_FILE, lock_flags, 0o600)
    os.fchmod(lock_fd, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return 0
    try:
        max_bytes = _bounded_env_int(
            "CAMPWATCH_LOG_MAX_BYTES", 2 * 1024 * 1024, 64 * 1024, 100 * 1024 * 1024
        )
        backups = _bounded_env_int("CAMPWATCH_LOG_BACKUPS", 4, 1, 20)
        rotate_log(LOG_FILE, max_bytes, backups)

        with _open_private_append(LOG_FILE) as log_handle:
            try:
                env = dict(os.environ)
                env["PYTHONUNBUFFERED"] = "1"
                load_private_settings(env)
                result = subprocess.run(
                    [sys.executable, str(HERE / "watch.py"), "--scheduled"],
                    cwd=HERE,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                return result.returncode
            except Exception as exc:  # noqa: BLE001
                log_handle.write(f"runner error: {type(exc).__name__}: {exc}\n")
                return 1
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
