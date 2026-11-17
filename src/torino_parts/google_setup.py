"""Google desktop OAuth (drive.file scope) and workbook/folder provisioning.

The OAuth client secret and token are stored under the Torino home directory
(%APPDATA%\\TorinoInventory by default), never in the repository.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .config import home_dir, load_config, save_config

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def token_path() -> Path:
    return home_dir() / "google-token.json"


def client_secret_path() -> Path:
    return home_dir() / "google-client-secret.json"


def load_credentials(interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    tp = token_path()
    if tp.exists():
        creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            tp.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:  # noqa: BLE001
            if not interactive:
                raise RuntimeError(
                    "Google sign-in expired or was revoked. Run `torino setup-google` again."
                )
    if not interactive:
        raise RuntimeError("Not signed in to Google. Run `torino setup-google --client-secret <file>`.")
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not client_secret_path().exists():
        raise RuntimeError("No OAuth client secret saved. Pass --client-secret <downloaded json>.")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path()), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", open_browser=True, timeout_seconds=600,
                                  success_message="Torino is connected to Google. You can close this tab.")
    tp.write_text(creds.to_json(), encoding="utf-8")
    return creds


def services(creds=None):
    from googleapiclient.discovery import build

    creds = creds or load_credentials()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return sheets, drive


def _accessible(drive, file_id: str) -> bool:
    if not file_id:
        return False
    try:
        f = drive.files().get(fileId=file_id, fields="id,trashed").execute()
        return not f.get("trashed", False)
    except Exception:  # noqa: BLE001
        return False


def setup_google(client_secret: str | None, workbook_name: str | None = None, log=print,
                 force_signin: bool = False) -> dict[str, Any]:
    if client_secret:
        src = Path(client_secret).expanduser()
        if not src.is_file():
            raise FileNotFoundError(client_secret)
        shutil.copyfile(src, client_secret_path())
        log(f"Saved OAuth client secret to {client_secret_path()}")
    if token_path().exists() and (client_secret or force_signin):
        token_path().unlink()  # new client or explicit reconnect -> new consent
    creds = load_credentials(interactive=True)
    log(f"Signed in. Token stored at {token_path()} (scope: drive.file only)")
    sheets, drive = services(creds)

    cfg = load_config()
    g = cfg["google"]
    name = workbook_name or g.get("workbook_name") or "Torino Parts Inventory"

    if not _accessible(drive, g.get("folder_id", "")):
        folder = drive.files().create(body={"name": name, "mimeType": FOLDER_MIME}, fields="id").execute()
        g["folder_id"] = folder["id"]
        log(f"Created private Drive folder '{name}'")
    if not _accessible(drive, g.get("photos_folder_id", "")):
        photos = drive.files().create(
            body={"name": "Photos", "mimeType": FOLDER_MIME, "parents": [g["folder_id"]]}, fields="id"
        ).execute()
        g["photos_folder_id"] = photos["id"]
        log("Created private 'Photos' folder")
    if not _accessible(drive, g.get("spreadsheet_id", "")):
        sheet = drive.files().create(
            body={"name": name, "mimeType": SHEET_MIME, "parents": [g["folder_id"]]}, fields="id"
        ).execute()
        g["spreadsheet_id"] = sheet["id"]
        log(f"Created inventory workbook '{name}'")
    g["workbook_name"] = name

    from .store.sheets import SheetsStore

    store = SheetsStore(sheets, g["spreadsheet_id"], home_dir() / "google" / "sheet.lock")
    store.ensure_schema()
    cfg["backend"] = "google"
    save_config(cfg)
    url = f"https://docs.google.com/spreadsheets/d/{g['spreadsheet_id']}"
    log(f"Workbook ready: {url}")
    log("Backend switched to 'google'. Use `torino use demo` to switch back to demo data.")
    return {"spreadsheet_url": url, **g}
