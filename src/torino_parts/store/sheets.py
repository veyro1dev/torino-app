"""Google Sheets store: the live source of truth.

* Reads every tab in one ``values.batchGet`` call.
* Writes every operation of a change set in one ``spreadsheets.batchUpdate``
  call.  The Sheets API applies a batchUpdate atomically (if any request fails,
  none are applied), so stock rows and their transaction rows land together.
* Columns are matched by header name, so extra/reordered columns are tolerated.

Avoid sorting the raw tabs in the Sheets UI (use filter views instead): updates
address rows by position, re-read under a lock immediately before writing.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..schema import NUMERIC, TABS
from .base import Append, Store, StoreUnavailable, Update

REAUTH = ("Google needs you to sign in again (the saved sign-in expired or was revoked). "
          "In the Torino app open Settings > Reconnect Google, or run `torino setup-google`.")


def call_google(request):
    """Execute a googleapiclient request, turning auth/network failures into StoreUnavailable."""
    try:
        return request.execute()
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        status = getattr(getattr(e, "resp", None), "status", None)
        if name == "RefreshError" or status == 401:
            raise StoreUnavailable(REAUTH) from e
        if name in ("TransportError", "ServerNotFoundError", "TimeoutError", "ConnectionError") or isinstance(e, OSError):
            raise StoreUnavailable(f"Can't reach Google right now ({name}). Check the internet connection.") from e
        if status == 404:
            raise StoreUnavailable("The inventory workbook was not found or this app lost access to it. "
                                   "Run `torino setup-google` to reconnect.") from e
        raise


def _cell(col: str, value: Any) -> dict:
    if value is None or value == "":
        return {"userEnteredValue": {"stringValue": ""}} if value == "" else {}
    if col in NUMERIC and isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"userEnteredValue": {"numberValue": value}}
    # stringValue is never parsed as a formula, so notes starting with "=" are safe.
    return {"userEnteredValue": {"stringValue": str(value)}}


class SheetsStore(Store):
    cache_ttl = 8.0

    def __init__(self, service, spreadsheet_id: str, lock_path: Path):
        super().__init__(lock_path)
        self._api = threading.RLock()  # googleapiclient/httplib2 objects are not thread-safe
        self.service = service
        self.spreadsheet_id = spreadsheet_id
        self._headers: dict[str, list[str]] = {}
        self._sheet_ids: dict[str, int] = {}

    # -- metadata -----------------------------------------------------------
    def _load_sheet_ids(self) -> None:
        with self._api:
            meta = call_google(self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id, fields="sheets.properties"))
        self._sheet_ids = {
            s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])
        }

    def ensure_schema(self) -> None:
        """Create missing tabs, header rows and missing header columns."""
        self._load_sheet_ids()
        reqs = [
            {"addSheet": {"properties": {"title": tab, "gridProperties": {"frozenRowCount": 1}}}}
            for tab in TABS
            if tab not in self._sheet_ids
        ]
        if reqs:
            with self._api:
                call_google(self.service.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id, body={"requests": reqs}))
            self._load_sheet_ids()
        raw = self._get_values()
        reqs = []
        for tab, cols in TABS.items():
            header = raw[tab][0] if raw[tab] else []
            header = [str(h) for h in header]
            missing = [c for c in cols if c not in header]
            if not missing:
                continue
            new_header = header + missing
            reqs.append({
                "updateCells": {
                    "start": {"sheetId": self._sheet_ids[tab], "rowIndex": 0, "columnIndex": 0},
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": h}, "userEnteredFormat": {"textFormat": {"bold": True}}} for h in new_header]}],
                    "fields": "userEnteredValue,userEnteredFormat.textFormat.bold",
                }
            })
        # Drop the default empty "Sheet1" if we created everything ourselves.
        if "Sheet1" in self._sheet_ids and len(self._sheet_ids) > 1:
            reqs.append({"deleteSheet": {"sheetId": self._sheet_ids["Sheet1"]}})
        if reqs:
            with self._api:
                call_google(self.service.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id, body={"requests": reqs}))
            self._load_sheet_ids()
        self._headers.clear()
        self.invalidate()

    # -- read ---------------------------------------------------------------
    def _get_values(self) -> dict[str, list[list[Any]]]:
        ranges = [f"'{tab}'!A1:ZZ" for tab in TABS]
        with self._api:
            resp = call_google(self.service.spreadsheets().values().batchGet(
                spreadsheetId=self.spreadsheet_id,
                ranges=ranges,
                valueRenderOption="UNFORMATTED_VALUE",
                dateTimeRenderOption="FORMATTED_STRING",
            ))
        out = {}
        for tab, vr in zip(TABS, resp.get("valueRanges", [])):
            out[tab] = vr.get("values", [])
        for tab in TABS:
            out.setdefault(tab, [])
        return out

    def _read(self):
        if not self._sheet_ids:
            self._load_sheet_ids()
        raw = self._get_values()
        data: dict[str, list[dict]] = {}
        for tab, values in raw.items():
            header = [str(h) for h in values[0]] if values else list(TABS[tab])
            self._headers[tab] = header
            rows = []
            for line in values[1:]:
                rec = {h: (line[i] if i < len(line) else "") for i, h in enumerate(header) if h}
                rows.append(rec)
            # Trailing fully blank rows are not data.
            while rows and all(v in ("", None) for v in rows[-1].values()):
                rows.pop()
            data[tab] = rows
        return data

    # -- write --------------------------------------------------------------
    def _commit(self, ops: list[Append | Update]) -> None:
        if not self._headers:
            self._read()
        requests = []
        appended: dict[str, list[dict]] = {}
        for op in ops:
            header = self._headers.get(op.tab) or TABS[op.tab]
            sheet_id = self._sheet_ids[op.tab]
            if isinstance(op, Append):
                row = {"values": [_cell(h, op.record.get(h, "")) for h in header]}
                appended.setdefault(op.tab, []).append(row)
            else:
                # Only touch the columns we know about; leave user columns intact.
                for ci, h in enumerate(header):
                    if h in op.record and not h.startswith("_"):
                        requests.append({
                            "updateCells": {
                                "start": {"sheetId": sheet_id, "rowIndex": op.row + 1, "columnIndex": ci},
                                "rows": [{"values": [_cell(h, op.record[h]) or {"userEnteredValue": {"stringValue": ""}}]}],
                                "fields": "userEnteredValue",
                            }
                        })
        for tab, rows in appended.items():
            requests.append({
                "appendCells": {"sheetId": self._sheet_ids[tab], "rows": rows, "fields": "userEnteredValue"}
            })
        # One batchUpdate == one atomic change.
        with self._api:
            call_google(self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": requests}))

    def describe(self) -> str:
        return f"Google Sheet https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
