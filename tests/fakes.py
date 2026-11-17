"""In-memory stand-ins for the Google Sheets service and b-PAC COM objects."""

from __future__ import annotations

import copy
import re


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeSheets:
    """Implements the subset of Sheets API v4 used by SheetsStore, with real semantics:

    * batchUpdate is atomic (all requests or none),
    * batchGet trims trailing empty cells/rows like the real API,
    * appendCells appends after the last row containing data.
    """

    def __init__(self):
        self.grids: dict[str, list[list]] = {"Sheet1": []}
        self.ids: dict[str, int] = {"Sheet1": 0}
        self.batch_calls = 0
        self.fail_next_batch = False

    # service.spreadsheets()
    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, fields=None):
        return _Exec(lambda: {"sheets": [{"properties": {"title": t, "sheetId": i}} for t, i in self.ids.items()]})

    def batchGet(self, spreadsheetId, ranges, valueRenderOption=None, dateTimeRenderOption=None):
        def run():
            out = []
            for r in ranges:
                title = re.match(r"'(.+)'!", r).group(1)
                rows = [list(row) for row in self.grids.get(title, [])]
                for row in rows:
                    while row and row[-1] in ("", None):
                        row.pop()
                while rows and not rows[-1]:
                    rows.pop()
                vr = {"range": r}
                if rows:
                    vr["values"] = rows
                out.append(vr)
            return {"valueRanges": out}
        return _Exec(run)

    def batchUpdate(self, spreadsheetId, body):
        def run():
            self.batch_calls += 1
            if self.fail_next_batch:
                self.fail_next_batch = False
                raise RuntimeError("simulated network failure")
            grids, ids = copy.deepcopy(self.grids), dict(self.ids)
            by_id = lambda sid: next(t for t, i in ids.items() if i == sid)  # noqa: E731
            for req in body["requests"]:
                (kind, arg), = req.items()
                if kind == "addSheet":
                    title = arg["properties"]["title"]
                    if title in ids:
                        raise ValueError("duplicate sheet")
                    ids[title] = max(ids.values(), default=0) + 1
                    grids[title] = []
                elif kind == "deleteSheet":
                    t = by_id(arg["sheetId"])
                    del ids[t]
                    del grids[t]
                elif kind == "updateCells":
                    g = grids[by_id(arg["start"]["sheetId"])]
                    r0, c0 = arg["start"]["rowIndex"], arg["start"]["columnIndex"]
                    for dr, row in enumerate(arg["rows"]):
                        for dc, cell in enumerate(row["values"]):
                            self._set(g, r0 + dr, c0 + dc, cell)
                elif kind == "appendCells":
                    g = grids[by_id(arg["sheetId"])]
                    last = max((i for i, row in enumerate(g) if any(v not in ("", None) for v in row)), default=-1)
                    for k, row in enumerate(arg["rows"]):
                        for c, cell in enumerate(row["values"]):
                            self._set(g, last + 1 + k, c, cell)
                else:
                    raise ValueError(f"unsupported request {kind}")
            self.grids, self.ids = grids, ids
            return {}
        return _Exec(run)

    @staticmethod
    def _set(g, r, c, cell):
        while len(g) <= r:
            g.append([])
        row = g[r]
        while len(row) <= c:
            row.append("")
        uev = cell.get("userEnteredValue", {})
        if "numberValue" in uev:
            v = uev["numberValue"]
            row[c] = int(v) if float(v).is_integer() else v
        elif "stringValue" in uev:
            row[c] = uev["stringValue"]
        else:
            row[c] = ""


class FakeBpacObject:
    def __init__(self):
        self.Text = ""


class FakeBpacPrinter:
    def __init__(self, world):
        self.world = world

    def IsPrinterOnline(self, name):
        return self.world.online and name == self.world.queue

    def GetMediaName(self):
        return self.world.media


class FakeBpacDocument:
    def __init__(self, world):
        self.world = world
        self.Printer = FakeBpacPrinter(world)
        self.ErrorCode = 0
        self.objects = {}
        self.barcodes = {}

    def Open(self, path):
        self.objects = {n: FakeBpacObject() for n in self.world.template_objects}
        return True

    def GetObject(self, name):
        return self.objects.get(name)

    def GetBarcodeIndex(self, name):
        return 0 if name in self.world.barcode_objects else -1

    def SetBarcodeData(self, idx, data):
        self.barcodes[idx] = data
        return True

    def SetPrinter(self, name, fit):
        return True

    def StartPrint(self, name, opts):
        return True

    def PrintOut(self, copies, opts):
        self.world.buffered = (copies, {n: o.Text for n, o in self.objects.items()}, dict(self.barcodes))
        return True

    def EndPrint(self):
        if self.world.hang_in_endprint:
            import time
            time.sleep(self.world.hang_in_endprint)
        if self.world.endprint_raises:
            raise RuntimeError("COM error 0x800706BA")
        if not self.world.endprint_ok:
            self.ErrorCode = 7
            return False
        self.world.printed.append(self.world.buffered)
        return True

    def Close(self):
        pass


class LateBoundPrinter(FakeBpacPrinter):
    """Like real b-PAC through pywin32 dynamic dispatch: zero-argument methods run on attribute access."""

    @property
    def GetMediaName(self):  # noqa: N802
        return self.world.media


class LateBoundDocument(FakeBpacDocument):
    def __init__(self, world):
        super().__init__(world)
        self.Printer = LateBoundPrinter(world)

    @property
    def EndPrint(self):  # noqa: N802
        return FakeBpacDocument.EndPrint(self)

    @property
    def Close(self):  # noqa: N802
        return True


class FakeBpacWorld:
    def __init__(self, queue="Brother PT-P700"):
        self.queue = queue
        self.online = True
        self.media = "24mm"
        self.template_objects = ["PartName", "PartID", "BinCode"]
        self.barcode_objects = ["Barcode"]
        self.endprint_ok = True
        self.endprint_raises = False
        self.hang_in_endprint = 0
        self.printed: list = []
        self.buffered = None
        # real b-PAC is reached through pywin32 late binding; keep that the default for every test
        self.late_bound = True

    def dispatch(self, progid):
        assert progid == "bpac.Document"
        return LateBoundDocument(self) if self.late_bound else FakeBpacDocument(self)
