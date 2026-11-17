import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeSheets  # noqa: E402
from torino_parts.inventory import Inventory  # noqa: E402
from torino_parts.labels import LabelQueue, LabelService  # noqa: E402
from torino_parts.labels.printers import PreviewBackend  # noqa: E402
from torino_parts.photos import LocalPhotoStore  # noqa: E402
from torino_parts.store import LocalStore  # noqa: E402
from torino_parts.store.sheets import SheetsStore  # noqa: E402

PRINTER_CFG = {
    "mode": "preview", "auto_print": False, "queue": "Brother PT-P700", "template": "",
    "tape_mm": 24, "fields": {"name": "PartName", "part_id": "PartID", "bin": "BinCode", "barcode": "Barcode"},
    "timeout_s": 5, "check_tape": True,
}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TORINO_HOME", str(tmp_path / "home"))


def make_store(kind: str, tmp_path: Path):
    if kind == "local":
        return LocalStore(tmp_path / "wb.json")
    if kind == "sqlite":
        from torino_parts.store import SQLiteStore
        return SQLiteStore(tmp_path / "torino.db")
    fake = FakeSheets()
    s = SheetsStore(fake, "sheet123", tmp_path / "sheet.lock")
    s.ensure_schema()
    s.fake = fake
    return s


@pytest.fixture(params=["local", "sqlite", "sheets"])
def inv(request, tmp_path):
    store = make_store(request.param, tmp_path)
    labels = LabelService(LabelQueue(tmp_path / "labels.db"), dict(PRINTER_CFG), tmp_path / "previews",
                          backend=PreviewBackend(PRINTER_CFG))
    i = Inventory(store, labels=labels, photos=LocalPhotoStore(tmp_path / "photos"))
    yield i
    labels.queue.close()


def seed(inv: Inventory) -> dict:
    for b in ("A-01", "A-02", "B-04"):
        inv.create_bin(f"bin-{b}", b)
    t = inv.add_vehicle("v1", 1970, "Ford", "Torino", nickname="Torino")["vehicle_id"]
    m = inv.add_vehicle("v2", 1987, "Chevrolet", "Monte Carlo")["vehicle_id"]
    h = inv.receive_stock("h1", "A-01", 2, "used", print_label=False, donor_vehicle_id=m,
                          new_part={"name": "Door hinge, upper", "side_position": "left",
                                    "fitment": [{"make": "Ford", "model": "Torino", "year_from": 1970, "year_to": 1971, "verified": True},
                                                {"make": "Mercury", "model": "Montego", "year_from": 1970, "year_to": 1971}]})
    hid = h["part"]["part_id"]
    inv.receive_stock("h2", "A-02", 1, "new", part_id=hid, print_label=False)
    r = inv.receive_stock("h3", "A-01", 1, "used", print_label=False,
                          new_part={"name": "Door hinge, upper", "side_position": "right"})
    return {"torino": t, "monte": m, "hinge_lh": hid, "hinge_rh": r["part"]["part_id"]}


@pytest.fixture(params=["local", "sqlite", "sheets"])
def garage(request, tmp_path):
    from torino_parts.rebuild import Garage

    store = make_store(request.param, tmp_path)
    labels = LabelService(LabelQueue(tmp_path / "labels.db"), dict(PRINTER_CFG), tmp_path / "previews",
                          backend=PreviewBackend(PRINTER_CFG))
    g = Garage(store, labels=labels, photos=LocalPhotoStore(tmp_path / "photos"))
    yield g
    labels.queue.close()
