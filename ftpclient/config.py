"""Persisted application settings: connection profiles and UI state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import APP_NAME
from .models import Profile

DEFAULT_CONFIG_DIR = Path.home() / "Library" / "Application Support" / APP_NAME

DEFAULT_SETTINGS = {
    "window_geometry": "1220x780",
    "last_profile": "",
    "show_hidden_local": False,
    "show_hidden_remote": True,
    "confirm_delete": True,
    "local_path": str(Path.home()),
    "sash_local": 0,
    "sash_vertical": 0,
}


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path, fallback):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


class Config:
    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.profiles_file = self.config_dir / "profiles.json"
        self.settings_file = self.config_dir / "settings.json"

    # -- profiles ---------------------------------------------------------
    def load_profiles(self) -> list[Profile]:
        raw = _read_json(self.profiles_file, [])
        profiles: list[Profile] = []
        if isinstance(raw, list):
            for item in raw:
                try:
                    profiles.append(Profile.from_dict(item))
                except (TypeError, ValueError):
                    continue
        return profiles

    def save_profiles(self, profiles: list[Profile]) -> None:
        _atomic_write(self.profiles_file, [p.to_dict() for p in profiles])

    # -- settings ---------------------------------------------------------
    def load_settings(self) -> dict:
        settings = dict(DEFAULT_SETTINGS)
        raw = _read_json(self.settings_file, {})
        if isinstance(raw, dict):
            settings.update(raw)
        return settings

    def save_settings(self, settings: dict) -> None:
        _atomic_write(self.settings_file, settings)
