"""Photo intake: accessible local files, base64 data, or authorized Drive files.

Photo saves are idempotent per request_id so a retried intake never uploads twice.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import shutil
import threading
from pathlib import Path
from typing import Any

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff"}
MAX_BYTES = 25 * 1024 * 1024


class PhotoError(ValueError):
    pass


def parse_drive_id(ref: str) -> str:
    """Accept a raw Drive file ID or a Drive/Docs URL."""
    ref = ref.strip()
    for pat in (r"/d/([A-Za-z0-9_-]{10,})", r"[?&]id=([A-Za-z0-9_-]{10,})"):
        m = re.search(pat, ref)
        if m:
            return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", ref):
        return ref
    raise PhotoError(f"not a Drive file id or link: {ref!r}")


def _check_local(path: str, receipt: bool = False) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise PhotoError(f"photo file not found or not accessible: {path}")
    if p.suffix.lower() not in IMAGE_EXT and not (receipt and p.suffix.lower() == ".pdf"):
        raise PhotoError(f"not an image file type: {p.suffix}")
    if p.stat().st_size > MAX_BYTES:
        raise PhotoError("photo is larger than 25 MB")
    return p


def _decode_b64(data: str) -> tuple[bytes, str]:
    ext = ".jpg"
    m = re.match(r"data:(?:image|application)/([\w+.-]+);base64,(.*)", data, re.S)
    if m:
        ext = "." + m.group(1).replace("jpeg", "jpg")
        data = m.group(2)
    raw = base64.b64decode(data, validate=False)
    if raw[:5] == b"%PDF-":
        ext = ".pdf"
    if len(raw) > MAX_BYTES:
        raise PhotoError("photo is larger than 25 MB")
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    return raw, ext


def _stem(request_id: str, part_id: str) -> str:
    return f"{part_id}_{hashlib.sha256(request_id.encode()).hexdigest()[:8]}"


class LocalPhotoStore:
    """Demo mode: copy photos into the local data folder."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def save(self, request_id: str, part_id: str, *, path: str | None = None,
             base64_data: str | None = None, drive_ref: str | None = None, receipt: bool = False) -> dict[str, Any]:
        self.folder.mkdir(parents=True, exist_ok=True)
        stem = _stem(request_id, part_id)
        if drive_ref:
            raise PhotoError("Drive photos need Google mode (run `torino setup-google`)")
        if path:
            src = _check_local(path, receipt)
            dest = self.folder / f"{stem}{src.suffix.lower()}"
            if not dest.exists():
                shutil.copyfile(src, dest)
        elif base64_data:
            raw, ext = _decode_b64(base64_data)
            dest = self.folder / f"{stem}{ext}"
            if not dest.exists():
                dest.write_bytes(raw)
        else:
            raise PhotoError("no photo given")
        return {"link": dest.resolve().as_uri(), "stored": "local"}


class DrivePhotoStore:
    """Google mode: upload to the private Photos folder created by setup (drive.file scope)."""

    def __init__(self, drive, photos_folder_id: str):
        self.drive = drive
        self.folder_id = photos_folder_id
        self._api = threading.RLock()

    def fetch(self, file_id: str) -> tuple[bytes, str]:
        """Download an app-accessible image (for thumbnails in the desktop app)."""
        from .store.sheets import call_google

        with self._api:
            meta = call_google(self.drive.files().get(fileId=file_id, fields="mimeType"))
            data = call_google(self.drive.files().get_media(fileId=file_id))
        return data, meta.get("mimeType", "image/jpeg")

    def _existing(self, request_id: str):
        q = (
            f"'{self.folder_id}' in parents and trashed = false and "
            f"appProperties has {{ key='torino_request' and value='{hashlib.sha256(request_id.encode()).hexdigest()[:32]}' }}"
        )
        res = self.drive.files().list(q=q, fields="files(id,webViewLink)", spaces="drive").execute()
        files = res.get("files", [])
        return files[0] if files else None

    def save(self, request_id: str, part_id: str, *, path: str | None = None,
             base64_data: str | None = None, drive_ref: str | None = None, receipt: bool = False) -> dict[str, Any]:
        with self._api:
            return self._save(request_id, part_id, path=path, base64_data=base64_data, drive_ref=drive_ref,
                              receipt=receipt)

    def _save(self, request_id: str, part_id: str, *, path: str | None = None,
              base64_data: str | None = None, drive_ref: str | None = None, receipt: bool = False) -> dict[str, Any]:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload, MediaInMemoryUpload

        if drive_ref:
            file_id = parse_drive_id(drive_ref)
            try:
                f = self.drive.files().get(fileId=file_id, fields="id,name,mimeType,webViewLink").execute()
            except HttpError as e:
                raise PhotoError(
                    "That Drive file is not available to this app. With the drive.file scope the app can "
                    "only open files it created or files you authorized for it. Download the photo and "
                    "pass it as a local file path instead (it will be uploaded to the inventory Photos folder)."
                ) from e
            if not f.get("mimeType", "").startswith("image/") and not (receipt and f.get("mimeType") == "application/pdf"):
                raise PhotoError(f"Drive file {f.get('name')} is not an image ({f.get('mimeType')})")
            return {"link": f["webViewLink"], "stored": "drive-linked", "drive_file_id": f["id"]}

        existing = self._existing(request_id)
        if existing:
            return {"link": existing["webViewLink"], "stored": "drive", "drive_file_id": existing["id"], "reused": True}
        stem = _stem(request_id, part_id)
        if path:
            src = _check_local(path, receipt)
            mime = mimetypes.guess_type(src.name)[0] or "image/jpeg"
            media = MediaFileUpload(str(src), mimetype=mime, resumable=False)
            name = f"{stem}{src.suffix.lower()}"
        elif base64_data:
            raw, ext = _decode_b64(base64_data)
            mime = mimetypes.guess_type("x" + ext)[0] or "image/jpeg"
            media = MediaInMemoryUpload(raw, mimetype=mime, resumable=False)
            name = f"{stem}{ext}"
        else:
            raise PhotoError("no photo given")
        body = {
            "name": name,
            "parents": [self.folder_id],
            "appProperties": {"torino_request": hashlib.sha256(request_id.encode()).hexdigest()[:32], "part_id": part_id},
        }
        f = self.drive.files().create(body=body, media_body=media, fields="id,webViewLink").execute()
        return {"link": f["webViewLink"], "stored": "drive", "drive_file_id": f["id"]}
