"""Local (SQLite) mode: atomic commits, several processes, backups/restore with photos, Local<->Google transfer."""

import base64
import io
import json
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest
from conftest import PRINTER_CFG
from fakes import FakeSheets
from PIL import Image

from torino_parts.datasafety import (
    DataSafetyError, create_backup, list_backups, read_backup, restore_backup, run_transfer, transfer_preview,
)
from torino_parts.labels import LabelQueue, LabelService
from torino_parts.labels.printers import PreviewBackend
from torino_parts.photos import LocalPhotoStore
from torino_parts.rebuild import Garage
from torino_parts.store import SQLiteStore
from torino_parts.store.base import Append, Update
from torino_parts.store.sheets import SheetsStore


def _jpeg(color=(200, 30, 30)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _garage(store, tmp_path, photos):
    labels = LabelService(LabelQueue(tmp_path / f"labels-{id(store)}.db"), dict(PRINTER_CFG), tmp_path / "previews",
                          backend=PreviewBackend(PRINTER_CFG))
    return Garage(store, labels=labels, photos=LocalPhotoStore(photos))


def _seed(g: Garage) -> dict:
    g.create_bin("b1", "A-01")
    v = g.add_vehicle("v1", 1970, "Ford", "Torino")["vehicle_id"]
    p = g.receive_stock("r1", "A-01", 2, "used", new_part={"name": "Door hinge", "side_position": "left"},
                        print_label=False, photo_base64=_jpeg())["part"]["part_id"]
    j = g.create_job("j1", v, "Hang left door", "body", parts=[{"part_id": p, "qty": 2}])["job_id"]
    ph = g.attach_photo("ph1", job_id=j, part_id=p, caption="before", photo_base64=_jpeg((10, 200, 10)))["photo_id"]
    return {"v": v, "p": p, "j": j, "ph": ph}


def test_sqlite_commit_is_all_or_nothing(tmp_path):
    s = SQLiteStore(tmp_path / "t.db")
    s.commit([Append("Bins", {"bin_code": "A-01"})])
    before = s.data_version()
    with pytest.raises(IndexError):
        s.commit([Append("Bins", {"bin_code": "A-02"}), Update("Bins", 99, {"description": "x"})])
    assert [b["bin_code"] for b in s.read()["Bins"]] == ["A-01"] and s.data_version() == before


def test_two_processes_see_each_others_writes(tmp_path):
    a, b = SQLiteStore(tmp_path / "t.db"), SQLiteStore(tmp_path / "t.db")  # e.g. desktop app and an MCP server
    a.read()
    b.commit([Append("Bins", {"bin_code": "B-04", "my_column": "kept"})])  # user-added column survives
    rows = a.read()["Bins"]  # cached snapshot is dropped because the version changed
    assert rows[0]["bin_code"] == "B-04" and rows[0]["my_column"] == "kept"


def test_backup_and_restore_with_photos(tmp_path):
    store = SQLiteStore(tmp_path / "local" / "torino.db")
    g = _garage(store, tmp_path, tmp_path / "local" / "photos")
    ids = _seed(g)
    bk = create_backup(store, tmp_path / "local" / "photos", "local", "manual", dest_dir=tmp_path / "bk")
    assert bk["photo_files"] == 2 and bk["counts"]["Photos"] == 1 and not bk["missing_photo_files"]
    man = read_backup(bk["path"])
    assert man["photos_in_file"] == 2

    g.add_vehicle("v2", 1987, "Chevrolet", "Monte Carlo")  # a change made after the backup
    preview = restore_backup(bk["path"], store, tmp_path / "local" / "photos")
    assert preview["preview"] and preview["current"]["counts"]["Vehicles"] == 2  # nothing changed yet
    assert len(g.list_vehicles()["vehicles"]) == 2

    out = restore_backup(bk["path"], store, tmp_path / "local" / "photos", confirm=True,
                         safety_backup=lambda: create_backup(store, tmp_path / "local" / "photos", "local",
                                                             "before-restore", dest_dir=tmp_path / "bk"))
    assert out["restored"] and out["verified_counts"] == bk["counts"]
    assert [v["vehicle_id"] for v in g.list_vehicles()["vehicles"]] == [ids["v"]]
    assert read_backup(out["safety_backup"])["counts"]["Vehicles"] == 2  # the restore itself can be undone
    job = g.get_job(ids["j"])
    assert job["photos"][0]["photo_id"] == ids["ph"] and job["parts_needed"][0]["part_id"] == ids["p"]

    # restore on "another PC": empty database, different photos folder -> links point at the new files
    other = SQLiteStore(tmp_path / "pc2" / "torino.db")
    restore_backup(bk["path"], other, tmp_path / "pc2" / "photos", confirm=True)
    links = [p["link"] for p in other.read()["Photos"]] + other.read()["Parts"][0]["photo_links"].split("\n")
    for ln in links:
        assert "pc2" in ln and Path(url2pathname(urlparse(ln).path)).is_file()
    assert {b["name"] for b in list_backups(tmp_path / "bk")} >= {Path(bk["path"]).name}


def test_damaged_backup_is_refused(tmp_path):
    bad = tmp_path / "torino-local-x.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": "something-else"}))
        z.writestr("torino.db", b"not a db")
    with pytest.raises(DataSafetyError):
        read_backup(bad)


class FakeDrivePhotos:
    """Drive photo store stand-in: idempotent per request_id, like DrivePhotoStore."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.by_request: dict[str, str] = {}

    def save(self, request_id, part_id, *, path=None, base64_data=None, drive_ref=None):
        if request_id not in self.by_request:
            fid = f"F{len(self.files):010d}abc"
            self.files[fid] = Path(path).read_bytes()
            self.by_request[request_id] = fid
        fid = self.by_request[request_id]
        return {"link": f"https://drive.google.com/file/d/{fid}/view", "stored": "drive"}

    def fetch(self, fid):
        return self.files[fid], "image/jpeg"


def test_transfer_local_to_google_and_back_keeps_ids_and_photos(tmp_path):
    local = SQLiteStore(tmp_path / "local" / "torino.db")
    g = _garage(local, tmp_path, tmp_path / "local" / "photos")
    ids = _seed(g)
    sheets = SheetsStore(FakeSheets(), "sheet1", tmp_path / "s.lock")
    sheets.ensure_schema()
    drive = FakeDrivePhotos()
    pv = transfer_preview(local, sheets, "Local", "Google")
    assert pv["ok"] and pv["records"] > 10 and pv["photo_files_to_copy"] == 2

    out = run_transfer(local, LocalPhotoStore(tmp_path / "local" / "photos"), sheets, drive, "Local", "Google")
    assert out["verified_same_ids"] and out["photos_copied"] == 2 and not out["warnings"]
    gg = _garage(sheets, tmp_path, tmp_path / "unused")
    job = gg.get_job(ids["j"])
    assert job["parts_needed"][0]["part_id"] == ids["p"] and job["photos"][0]["photo_id"] == ids["ph"]
    assert all("drive.google.com" in p["link"] for p in sheets.read()["Photos"])
    assert gg.get_part(ids["p"])["photo_links"][0].startswith("https://drive.google.com")
    # a request retried after the transfer is still recognised (request IDs travelled too)
    assert gg.create_job("j1", ids["v"], "Hang left door", "body", parts=[{"part_id": ids["p"], "qty": 2}])["duplicate_request"]

    # refusing to mix into a non-empty target
    assert not transfer_preview(local, sheets, "Local", "Google")["ok"]
    with pytest.raises(DataSafetyError):
        run_transfer(local, LocalPhotoStore(tmp_path / "local" / "photos"), sheets, drive, "Local", "Google")

    # Google -> a fresh Local database on another PC: photos downloaded, links local again
    fresh = SQLiteStore(tmp_path / "pc2" / "torino.db")
    back = run_transfer(sheets, drive, fresh, LocalPhotoStore(tmp_path / "pc2" / "photos"), "Google", "Local")
    assert back["photos_copied"] == 2
    links = [p["link"] for p in fresh.read()["Photos"]]
    assert links[0].startswith("file:") and Path(url2pathname(urlparse(links[0]).path)).is_file()
    assert [j["job_id"] for j in fresh.read()["Jobs"]] == [ids["j"]]


def test_running_mcp_session_follows_source_switch(tmp_path):
    """Desktop, phone and MCP use the same selected source: a Codex session that was started in Demo
    mode writes to Local after the user switches to Local in the app."""
    import asyncio
    import os
    import sys

    from mcp.client.client import Client
    from mcp.client.stdio import StdioServerParameters

    from torino_parts.config import load_config, save_config

    home = tmp_path / "home"
    env = {**os.environ, "TORINO_HOME": str(home)}
    os.environ["TORINO_HOME"] = str(home)

    def _pl(r):
        assert not r.is_error, r.content[0].text
        return r.structured_content or json.loads(r.content[0].text)

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "torino_parts.cli", "serve"], env=env)
        async with Client(params, read_timeout_seconds=60) as c:
            first = _pl(await c.call_tool("inventory_info", {}))
            cfg = load_config()
            cfg["backend"] = "local"
            save_config(cfg)
            second = _pl(await c.call_tool("inventory_info", {}))
            _pl(await c.call_tool("create_bin", {"request_id": "after-switch", "bin_code": "L-01"}))
            return first, second

    first, second = asyncio.run(run())
    assert first["mode"] == "Demo" and second["mode"] == "Local" and second["real_data"]
    db = SQLiteStore(home / "local" / "torino.db")
    assert [b["bin_code"] for b in db.read()["Bins"]] == ["L-01"]
