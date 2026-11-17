"""Build the application service (inventory + rebuild workflow) from the saved configuration.

The desktop app, the phone pages, the CLI and the MCP server all use ``build_inventory`` so they
share one implementation and the same records.
"""

from __future__ import annotations

from typing import Any

from .config import BACKEND_LABEL, data_dir, home_dir, load_config, save_config, source_key
from .labels import LabelQueue, LabelService
from .photos import DrivePhotoStore, LocalPhotoStore
from .rebuild import Garage
from .store import LocalStore, SQLiteStore

_UPGRADED: set[str] = set()


def build_store(cfg: dict[str, Any], backend: str | None = None):
    """(store, photo store) for ``backend`` (default: the selected one)."""
    backend = backend or cfg["backend"]
    ddir = data_dir(cfg, backend)
    if backend == "google":
        from .google_setup import services
        from .store.sheets import SheetsStore

        g = cfg["google"]
        if not g.get("spreadsheet_id"):
            raise RuntimeError("Google backend selected but no workbook yet. Run `torino setup-google`.")
        sheets, drive = services()
        store = SheetsStore(sheets, g["spreadsheet_id"], ddir / "sheet.lock")
        if g["spreadsheet_id"] not in _UPGRADED:
            store.ensure_schema()  # adds new tabs/columns to older workbooks; never removes anything
            _UPGRADED.add(g["spreadsheet_id"])
        return store, DrivePhotoStore(drive, g["photos_folder_id"])
    if backend == "local":
        return SQLiteStore(ddir / "torino.db"), LocalPhotoStore(ddir / "photos")
    if backend == "demo":
        return LocalStore(ddir / "workbook.json"), LocalPhotoStore(ddir / "photos")
    raise ValueError(f"unknown backend {backend!r}")


def build_inventory(cfg: dict[str, Any] | None = None, backend=None) -> Garage:
    cfg = cfg or load_config()
    ddir = data_dir(cfg)
    store, photos = build_store(cfg)
    labels = LabelService(LabelQueue(ddir / "labels.db"), cfg["printer"], ddir / "label-previews", backend=backend)
    g = Garage(store, labels=labels, photos=photos)
    g.source_key = source_key(cfg)
    return g


def get_active_vehicle(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    return (cfg.get("ui", {}).get("active_vehicle", {}) or {}).get(cfg["backend"], "") or ""


def set_active_vehicle(vehicle_id: str) -> None:
    cfg = load_config()
    cfg.setdefault("ui", {}).setdefault("active_vehicle", {})[cfg["backend"]] = vehicle_id
    save_config(cfg)


def resolve_vehicle(inv: Garage, vehicle_id: str | None) -> str:
    """Explicit vehicle, else the active one, else the only one."""
    if vehicle_id:
        return vehicle_id.upper()
    active = get_active_vehicle()
    if active:
        return active
    vs = inv.list_vehicles()["vehicles"]
    if len(vs) == 1:
        return vs[0]["vehicle_id"]
    from .inventory import InventoryError

    raise InventoryError("which vehicle? pass vehicle_id (or set the active vehicle)",
                         vehicles=[v["label"] for v in vs])


def describe(inv: Garage, cfg: dict[str, Any]) -> dict[str, Any]:
    b = cfg["backend"]
    return {
        "backend": b,
        "mode": BACKEND_LABEL.get(b, b),
        "demo_mode": b == "demo",
        "real_data": b != "demo",
        "where": {"demo": "sample records on this PC (not real)",
                  "local": "your records in a database on this PC (works offline)",
                  "google": "your records in your Google Sheet and Drive"}.get(b, b),
        "store": inv.store.describe(),
        "home": str(home_dir()),
        "active_vehicle": get_active_vehicle(cfg) or None,
        "printer_mode": cfg["printer"]["mode"],
        "auto_print": cfg["printer"]["auto_print"],
        "tape_mm": cfg["printer"]["tape_mm"],
    }
