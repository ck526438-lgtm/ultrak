"""User config and machine-specific calibration."""

import os

from .utils import APP_DIR, json_dump, json_load, ensure_app_dir

CONFIG_FILE = os.path.join(str(APP_DIR), "config.json")
DEFAULTS = {
    "mode": "auto",
    "preset": "medium",
    "crf": 18,
    "audio": "copy",
    "grade": "natural",
    "preview": True,
    "budget_minutes": 0,
    "max_bpp": 0.06,
    "calibration": {},
    "last_out_dir": "",
}


def load() -> dict:
    data = json_load(CONFIG_FILE) or {}
    merged = {**DEFAULTS, **data}
    merged["calibration"] = {**{}, **(data or {}).get("calibration", {})}
    return merged


def save(updates: dict) -> dict:
    ensure_app_dir()
    merged = load()
    for key, value in updates.items():
        if key == "calibration":
            merged["calibration"] = {**merged.get("calibration", {}), **value}
        else:
            merged[key] = value
    json_dump(CONFIG_FILE, merged)
    return merged


def calibration(base: dict) -> dict:
    """Overlay this machine's measured calibration on the shipped reference numbers."""
    return {**base, **(load().get("calibration") or {})}
