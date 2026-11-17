"""Local backups / restore, and deliberate transfer between Local and Google.

Backups (``.zip``) hold ``torino.db`` (a SQLite copy of every record, same IDs), every photo/receipt file,
and ``manifest.json`` (counts, schema version, checksum). They are written to a temporary name and
renamed when complete, so a crash never leaves a half-written backup that looks valid.

Restore replaces the Local records in one transaction (other open sessions just see the new data) after
first saving an automatic "before-restore" backup, so a restore can itself be undone.

Transfer copies every record with the same IDs and request IDs (so links between jobs, parts, photos,
shopping items and expenses stay intact) and copies photo files (Local file -> Drive upload, Drive ->
local file), rewriting only the photo links. The target must be empty, except that a Local target may be
replaced after an automatic backup. Nothing is deleted from the source.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import url2pathname

from . import __version__
from .config import backups_dir, load_config
from .schema import SCHEMA_VERSION, TABS
from .store.sqlite import SQLiteStore

FORMAT = "torino-backup"
LINK_FIELDS = {"Photos": ["link"], "Parts": ["photo_links"], "Expenses": ["receipt_link"]}


class DataSafetyError(ValueError):
    pass


# ----------------------------------------------------------------------- links
def is_file_link(link: str) -> bool:
    return link.startswith("file:")


def local_file(link: str, folder: Path) -> Path | None:
    """The file behind a local photo link; falls back to the same file name in ``folder``
    (backups restored on another PC or under another user name)."""
    if not is_file_link(link):
        return None
    p = Path(url2pathname(urlparse(link).path))
    if p.is_file():
        return p
    alt = Path(folder) / p.name
    return alt if alt.is_file() else None


def iter_links(rows: dict[str, list[dict]]):
    for tab, fields in LINK_FIELDS.items():
        for r in rows.get(tab, []):
            for f in fields:
                for ln in str(r.get(f) or "").split("\n"):
                    if ln.strip():
                        yield ln.strip()


def rewrite_links(rows: dict[str, list[dict]], mapping: dict[str, str]) -> None:
    for tab, fields in LINK_FIELDS.items():
        for r in rows.get(tab, []):
            for f in fields:
                v = str(r.get(f) or "")
                if v:
                    r[f] = "\n".join(mapping.get(x.strip(), x.strip()) for x in v.split("\n") if x.strip())


def _raw_rows(store) -> dict[str, list[dict]]:
    """Every record exactly as stored (user-added columns included), without internal keys."""
    raw = store._read()
    return {tab: [{k: v for k, v in r.items() if not str(k).startswith("_")} for r in raw.get(tab, [])] for tab in TABS}


def _counts(rows: dict[str, list[dict]]) -> dict[str, int]:
    return {tab: len(rows.get(tab, [])) for tab in TABS}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------- backups
def create_backup(store, photos_folder: Path, backend: str, reason: str = "manual",
                  dest_dir: Path | None = None, keep: int = 30) -> dict[str, Any]:
    dest_dir = Path(dest_dir or backups_dir(load_config()))
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"torino-{backend}-{stamp}-{reason}.zip"
    final = dest_dir / name
    n = 1
    while final.exists():
        n += 1
        final = dest_dir / f"torino-{backend}-{stamp}-{reason}-{n}.zip"
    with tempfile.TemporaryDirectory(prefix="torino-backup-") as tmp:
        db = Path(tmp) / "torino.db"
        if isinstance(store, SQLiteStore):
            store.backup_to(db)
            rows = _raw_rows(store)
        else:
            rows = _raw_rows(store)
            SQLiteStore(db).replace_all(rows)
            for extra in (db.with_suffix(".lock"),):
                extra.unlink(missing_ok=True)
        files: dict[str, Path] = {}
        folder = Path(photos_folder)
        if folder.is_dir():
            for p in folder.iterdir():
                if p.is_file():
                    files[p.name] = p
        missing = []
        for link in iter_links(rows):
            if is_file_link(link):
                p = local_file(link, folder)
                if p:
                    files.setdefault(p.name, p)
                else:
                    missing.append(link)
        manifest = {
            "format": FORMAT, "format_version": 1, "app_version": __version__, "schema_version": SCHEMA_VERSION,
            "backend": backend, "reason": reason, "created_at": datetime.now().isoformat(timespec="seconds"),
            "counts": _counts(rows), "records": sum(_counts(rows).values()), "photo_files": len(files),
            "missing_photo_files": missing, "db_sha256": _sha256(db),
        }
        partial = final.with_suffix(".zip.partial")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(db, "torino.db")
            for fname, p in sorted(files.items()):
                z.write(p, f"photos/{fname}", compress_type=zipfile.ZIP_STORED)  # photos are already compressed
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
        with zipfile.ZipFile(partial) as z:  # read it back before calling it a backup
            if z.testzip() is not None:
                raise DataSafetyError("backup file failed its own check")
        partial.replace(final)
    _prune(dest_dir, keep)
    return {"path": str(final), "name": final.name, "size": final.stat().st_size, **manifest}


def _prune(folder: Path, keep: int) -> None:
    """Only automatic backups are pruned; ones you made yourself are never deleted."""
    auto = sorted((p for p in folder.glob("torino-*-auto*.zip")), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in auto[keep:]:
        p.unlink(missing_ok=True)


def read_backup(path: Path | str) -> dict[str, Any]:
    """Validate a backup file and return its manifest (raises DataSafetyError if it is not usable)."""
    path = Path(path)
    if not path.is_file():
        raise DataSafetyError(f"backup not found: {path}")
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "manifest.json" not in names or "torino.db" not in names:
                raise DataSafetyError("not a Torino backup (manifest.json / torino.db missing)")
            man = json.loads(z.read("manifest.json"))
            if man.get("format") != FORMAT:
                raise DataSafetyError("not a Torino backup")
            bad = z.testzip()
            if bad:
                raise DataSafetyError(f"backup is damaged ({bad})")
            man["photos_in_file"] = sum(1 for n in names if n.startswith("photos/") and not n.endswith("/"))
    except zipfile.BadZipFile as e:
        raise DataSafetyError(f"not a valid zip file: {e}") from e
    if int(man.get("schema_version", 0)) > SCHEMA_VERSION:
        raise DataSafetyError(f"backup is from a newer Torino (schema {man['schema_version']}); update Torino first")
    man["path"] = str(path)
    man["name"] = path.name
    man["size"] = path.stat().st_size
    return man


def list_backups(folder: Path | None = None) -> list[dict[str, Any]]:
    folder = Path(folder or backups_dir(load_config()))
    out = []
    for p in sorted(folder.glob("torino-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            out.append(read_backup(p))
        except DataSafetyError as e:
            out.append({"path": str(p), "name": p.name, "error": str(e)})
    return out


def restore_backup(path: Path | str, store: SQLiteStore, photos_folder: Path, *, confirm: bool = False,
                   safety_backup: Callable[[], dict] | None = None) -> dict[str, Any]:
    """Preview (confirm=False) or restore a backup into the Local database."""
    man = read_backup(path)
    current = _counts(_raw_rows(store))
    preview = {"backup": {k: man[k] for k in ("name", "created_at", "backend", "counts", "records", "photo_files",
                                              "app_version") if k in man},
               "current": {"counts": current, "records": sum(current.values())},
               "will": "Replace ALL Local records with the backup. A backup of the current Local data is saved first."}
    if not confirm:
        return {"preview": True, **preview}
    with tempfile.TemporaryDirectory(prefix="torino-restore-") as tmp:
        with zipfile.ZipFile(path) as z:
            z.extract("torino.db", tmp)
            db = Path(tmp) / "torino.db"
            if man.get("db_sha256") and _sha256(db) != man["db_sha256"]:
                raise DataSafetyError("backup database checksum does not match; the file is damaged")
            with closing(sqlite3.connect(db)) as c:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise DataSafetyError("backup database failed its integrity check")
            src = SQLiteStore(db)  # also upgrades an older backup's schema in the temp copy
            rows = _raw_rows(src)
            pre = safety_backup() if safety_backup else None
            folder = Path(photos_folder)
            folder.mkdir(parents=True, exist_ok=True)
            copied = 0
            for n in z.namelist():
                if n.startswith("photos/") and not n.endswith("/"):
                    target = folder / Path(n).name
                    if not target.exists():
                        with z.open(n) as fsrc, open(target, "wb") as fdst:
                            shutil.copyfileobj(fsrc, fdst)
                        copied += 1
        mapping = {}
        for link in iter_links(rows):
            if is_file_link(link):
                p = folder / Path(url2pathname(urlparse(link).path)).name
                if p.is_file():
                    mapping[link] = p.resolve().as_uri()
        rewrite_links(rows, mapping)
        store.replace_all(rows)
    after = _counts(_raw_rows(store))
    if after != man["counts"]:
        raise DataSafetyError(f"restore check failed: expected {man['counts']}, got {after}")
    return {"restored": True, **preview, "photos_copied": copied, "verified_counts": after,
            "safety_backup": pre and pre.get("path")}


# -------------------------------------------------------------------- transfer
def transfer_preview(src_store, dst_store, src_label: str, dst_label: str, *, replace_ok: bool = False) -> dict[str, Any]:
    src_rows, dst_rows = _raw_rows(src_store), _raw_rows(dst_store)
    sc, dc = _counts(src_rows), _counts(dst_rows)
    links = list(dict.fromkeys(iter_links(src_rows)))
    problems = []
    if sum(sc.values()) == 0:
        problems.append(f"{src_label} has no records to transfer.")
    if sum(dc.values()) and not replace_ok:
        problems.append(f"{dst_label} already has {sum(dc.values())} records. Transfers only go into an empty "
                        f"{dst_label} so nothing is mixed or overwritten.")
    return {"from": src_label, "to": dst_label, "source_counts": sc, "target_counts": dc,
            "records": sum(sc.values()), "photo_links": len(links),
            "photo_files_to_copy": sum(1 for x in links if is_file_link(x) or "google.com" in x),
            "keeps": "Same IDs and request IDs, so jobs, parts, photos, shopping items and expenses stay linked.",
            "replaces_target": bool(sum(dc.values())) and replace_ok,
            "problems": problems, "ok": not problems}


def run_transfer(src_store, src_photos, dst_store, dst_photos, src_label: str, dst_label: str, *,
                 replace_ok: bool = False, before_replace: Callable[[], dict] | None = None,
                 log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    pv = transfer_preview(src_store, dst_store, src_label, dst_label, replace_ok=replace_ok)
    if not pv["ok"]:
        raise DataSafetyError(" ".join(pv["problems"]))
    rows = _raw_rows(src_store)
    links = list(dict.fromkeys(iter_links(rows)))
    mapping: dict[str, str] = {}
    warnings: list[str] = []
    src_folder = Path(getattr(src_photos, "folder", "") or ".")
    for i, link in enumerate(links, 1):
        if is_file_link(link):
            p = local_file(link, src_folder)
            if not p:
                warnings.append(f"photo file missing, link kept as is: {link}")
                continue
            if hasattr(dst_photos, "folder"):  # local -> local (not used, but harmless)
                continue
            res = dst_photos.save(f"transfer-{hashlib.sha256(link.encode()).hexdigest()[:24]}",
                                  p.stem.split("_")[0] or "photo", path=str(p))
            mapping[link] = res["link"]
            log(f"uploaded photo {i}/{len(links)}: {p.name}")
        elif "google.com" in link and hasattr(dst_photos, "folder") and hasattr(src_photos, "fetch"):
            from .photos import parse_drive_id

            try:
                fid = parse_drive_id(link)
                data, mime = src_photos.fetch(fid)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"could not download {link}: {e}")
                continue
            ext = mimetypes.guess_extension(mime or "") or ".jpg"
            ext = ".jpg" if ext in (".jpe", ".jpeg") else ext
            folder = Path(dst_photos.folder)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"drive-{fid}{ext}"
            target.write_bytes(data)
            mapping[link] = target.resolve().as_uri()
            log(f"downloaded photo {i}/{len(links)}")
    rewrite_links(rows, mapping)
    acts = rows["Activity"]
    nums = [int(a["act_id"].split("-")[1]) for a in acts if str(a.get("act_id", "")).startswith("A-")
            and str(a["act_id"]).split("-")[1].isdigit()]
    rows["Activity"].append({"act_id": f"A-{max(nums, default=0) + 1:06d}", "request_id": f"transfer-{int(time.time())}",
                             "request_hash": "", "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                             "type": "transfer", "vehicle_id": "", "job_id": "", "part_id": "",
                             "summary": f"Copied {pv['records']} records and {len(mapping)} photo files from "
                                        f"{src_label} to {dst_label} (same IDs)", "source": "transfer"})
    pre = None
    if isinstance(dst_store, SQLiteStore):
        if pv["target_counts"] and sum(pv["target_counts"].values()) and before_replace:
            pre = before_replace()
        dst_store.replace_all(rows)
    else:
        from .store.base import Append

        with dst_store.lock():
            dst_store.commit([Append(tab, r) for tab in TABS for r in rows[tab]])  # one atomic batch
    after = _raw_rows(dst_store)
    ok = all([str(r.get(TABS[tab][0])) for r in after[tab]] == [str(r.get(TABS[tab][0])) for r in rows[tab]]
             for tab in TABS)
    if not ok:
        raise DataSafetyError("transfer check failed: the target does not hold the same records")
    return {"transferred": True, "from": src_label, "to": dst_label, "counts": _counts(after),
            "records": sum(_counts(after).values()), "photos_copied": len(mapping), "warnings": warnings,
            "verified_same_ids": ok, "safety_backup": pre and pre.get("path")}


# ---------------------------------------------------------- using the settings
def backup_current(reason: str = "manual", cfg: dict | None = None) -> dict[str, Any]:
    from .app import build_store

    cfg = cfg or load_config()
    b = cfg["backend"]
    if b == "google":
        raise DataSafetyError("Backups here are for Local (and Demo) data. Your Google records live in your Google "
                              "Sheet, which keeps its own version history (File > Version history). To keep a copy "
                              "on this PC, transfer Google -> Local.")
    store, photos = build_store(cfg)
    return create_backup(store, photos.folder, b, reason, keep=int(cfg["backups"].get("keep", 30)))


def auto_backup_if_due(cfg: dict | None = None) -> dict | None:
    """One automatic backup per day of the Local records (only if there is something to back up)."""
    cfg = cfg or load_config()
    if cfg["backend"] != "local":
        return None
    today = datetime.now().strftime("%Y%m%d")
    if any(backups_dir(cfg).glob(f"torino-local-{today}-*-auto*.zip")):
        return None
    from .app import build_store

    store, _ = build_store(cfg)
    if not any(_counts(_raw_rows(store)).values()):
        return None
    return backup_current("auto", cfg)


def restore_into_local(path: Path | str, confirm: bool = False, cfg: dict | None = None) -> dict[str, Any]:
    from .app import build_store

    cfg = cfg or load_config()
    store, photos = build_store(cfg, "local")
    keep = int(cfg["backups"].get("keep", 30))
    return restore_backup(path, store, photos.folder, confirm=confirm,
                          safety_backup=lambda: create_backup(store, photos.folder, "local", "before-restore", keep=keep))


DIRECTIONS = {"local_to_google": ("local", "google"), "google_to_local": ("google", "local")}


def transfer(direction: str, confirm: bool = False, replace_local: bool = False, cfg: dict | None = None,
             log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    from .app import build_store
    from .config import BACKEND_LABEL

    cfg = cfg or load_config()
    if direction not in DIRECTIONS:
        raise DataSafetyError(f"direction must be one of {', '.join(DIRECTIONS)}")
    if not cfg["google"].get("spreadsheet_id"):
        raise DataSafetyError("Connect Google first (Settings > Google Drive), then come back to transfer.")
    a, b = DIRECTIONS[direction]
    src_store, src_photos = build_store(cfg, a)
    dst_store, dst_photos = build_store(cfg, b)
    replace_ok = replace_local and b == "local"
    if not confirm:
        return {"preview": True, **transfer_preview(src_store, dst_store, BACKEND_LABEL[a], BACKEND_LABEL[b],
                                                    replace_ok=replace_ok)}
    keep = int(cfg["backups"].get("keep", 30))
    return run_transfer(src_store, src_photos, dst_store, dst_photos, BACKEND_LABEL[a], BACKEND_LABEL[b],
                        replace_ok=replace_ok, log=log,
                        before_replace=lambda: create_backup(dst_store, dst_photos.folder, "local", "before-transfer",
                                                             keep=keep))
