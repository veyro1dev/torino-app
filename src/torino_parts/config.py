"""Configuration and on-disk locations.

Everything that is user- or machine-specific (Google OAuth token, client secret,
spreadsheet IDs, label queue, previews, demo workbook) lives under the *home*
directory, which defaults to ``%APPDATA%\\TorinoInventory`` and is never inside the
repository.  Override with the ``TORINO_HOME`` environment variable.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # "demo"   = sample records (local JSON workbook) for trying things out.
    # "local"  = your real records in a SQLite database on this PC: works offline, no Google account.
    # "google" = your real records in a Google Sheet + Drive photos (created by `torino setup-google`).
    "backend": "demo",
    "backups": {
        # where `Back up now` writes; empty = %APPDATA%\TorinoInventory\backups
        "folder": "",
        "keep": 30,
    },
    "google": {
        "spreadsheet_id": "",
        "folder_id": "",
        "photos_folder_id": "",
        "workbook_name": "Torino Parts Inventory",
    },
    "printer": {
        # "preview" renders PNG previews only; "live" sends jobs to b-PAC.
        "mode": "preview",
        # Only used in live mode: print automatically right after a successful intake.
        "auto_print": False,
        # Windows printer queue name as shown in Settings > Printers & scanners.
        "queue": "Brother PT-P700",
        # P-touch Editor template (.lbx) whose tape width matches the installed TZe tape.
        "template": "",
        # TZe tape width in mm: 3.5, 6, 9, 12, 18 or 24.
        "tape_mm": 24,
        # Object names inside the .lbx template.
        "fields": {
            "name": "PartName",
            "part_id": "PartID",
            "bin": "BinCode",
            "barcode": "Barcode",
        },
        # Seconds to wait for b-PAC before the outcome is treated as uncertain.
        "timeout_s": 45,
        # Refuse to print if b-PAC reports a different tape width than tape_mm.
        "check_tape": True,
    },
    "ui": {
        # active vehicle per backend, shared by the desktop app and MCP ("what can I do next?")
        "active_vehicle": {"demo": "", "google": ""},
    },
    "network": {
        "desktop_port": 8765,   # 127.0.0.1 only: the desktop window
        "phone_port": 8766,     # HTTPS on the home Wi-Fi: paired phones only
        "setup_port": 8767,     # plain HTTP on the home Wi-Fi: serves only the public CA certificate
        "phone_enabled": True,
    },
}


def home_dir() -> Path:
    env = os.environ.get("TORINO_HOME")
    if env:
        p = Path(env)
    else:
        base = os.environ.get("APPDATA") or str(Path.home() / ".config")
        p = Path(base) / "TorinoInventory"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return home_dir() / "config.json"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict[str, Any]:
    path = config_path()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return _merge(DEFAULTS, data)
    return copy.deepcopy(DEFAULTS)


def save_config(cfg: dict[str, Any]) -> None:
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _coerce(value: str, current: Any) -> Any:
    if isinstance(current, bool):
        if value.lower() in ("1", "true", "yes", "on"):
            return True
        if value.lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"expected true/false, got {value!r}")
    if isinstance(current, (int, float)) and not isinstance(current, bool):
        num = float(value)
        return int(num) if num.is_integer() and isinstance(current, int) else num
    return value


def set_value(cfg: dict[str, Any], dotted: str, value: str) -> None:
    keys = dotted.split(".")
    node = cfg
    for k in keys[:-1]:
        if k not in node or not isinstance(node[k], dict):
            raise KeyError(f"unknown setting {dotted!r}")
        node = node[k]
    if keys[-1] not in node:
        raise KeyError(f"unknown setting {dotted!r}")
    if dotted == "printer.tape_mm":
        v = float(value)
        if v not in VALID_TAPES:
            raise ValueError(f"tape_mm must be one of {sorted(VALID_TAPES)}")
        node[keys[-1]] = int(v) if v.is_integer() else v
        return
    if dotted == "printer.mode" and value not in ("preview", "live"):
        raise ValueError("printer.mode must be 'preview' or 'live'")
    if dotted == "backend" and value not in BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(BACKENDS)}")
    node[keys[-1]] = _coerce(value, node[keys[-1]])


VALID_TAPES = {3.5, 6, 9, 12, 18, 24}
BACKENDS = ("demo", "local", "google")
BACKEND_LABEL = {"demo": "Demo", "local": "Local", "google": "Google"}


def data_dir(cfg: dict[str, Any], backend: str | None = None) -> Path:
    """Per-backend folder for the label queue and previews (and the demo / local records)."""
    d = home_dir() / (backend or cfg["backend"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def backups_dir(cfg: dict[str, Any]) -> Path:
    d = Path(cfg.get("backups", {}).get("folder") or home_dir() / "backups")
    d.mkdir(parents=True, exist_ok=True)
    return d


def source_key(cfg: dict[str, Any]) -> tuple:
    """Identifies the selected data source; long-running servers rebuild when it changes."""
    return (cfg["backend"], cfg["google"].get("spreadsheet_id", "") if cfg["backend"] == "google" else "")
