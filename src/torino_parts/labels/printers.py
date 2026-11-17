"""Printer backends.

``PreviewBackend`` writes nothing to a printer (demo / no hardware).
``BpacBackend`` drives a Brother PT-P700 through the Brother b-PAC 3.4 COM
client and a P-touch Editor ``.lbx`` template, using the Windows printer queue.

Outcomes are reported with three exception types so the queue can tell
"definitely not printed" from "maybe printed":

* ``PrinterOffline`` - nothing was sent; job stays pending for a later retry.
* ``PrintFailed``    - nothing was sent; job needs attention (template, tape...).
* ``PrintUncertain`` - the job may have reached the printer; a human decides.
"""

from __future__ import annotations

import re
import sys
import threading
from pathlib import Path
from typing import Any

from .render import LabelContent


class PrinterOffline(Exception):
    pass


class PrintFailed(Exception):
    pass


class PrintUncertain(Exception):
    pass


class PreviewBackend:
    name = "preview"
    live = False

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def status(self) -> dict[str, Any]:
        return {
            "mode": "preview",
            "online": None,
            "detail": "Preview mode: labels are rendered as PNG files; nothing is sent to a printer.",
        }

    def print_label(self, content: LabelContent, copies: int) -> None:  # pragma: no cover - never called
        raise PrintFailed("preview backend cannot print")


# b-PAC PrintOptionConstants
BPO_AUTOCUT = 0x1


def _windows_queue_info(queue: str) -> dict[str, Any]:
    """Look up the Windows printer queue via the spooler."""
    try:
        import win32print
    except ImportError:
        return {"exists": None, "detail": "pywin32 not installed"}
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    names = [p[2] for p in win32print.EnumPrinters(flags)]
    if queue not in names:
        brother = [n for n in names if "brother" in n.lower() or "pt-p700" in n.lower()]
        return {"exists": False, "available_queues": names, "brother_queues": brother}
    info: dict[str, Any] = {"exists": True}
    try:
        h = win32print.OpenPrinter(queue)
        try:
            p2 = win32print.GetPrinter(h, 2)
        finally:
            win32print.ClosePrinter(h)
        status = p2.get("Status", 0)
        attrs = p2.get("Attributes", 0)
        info.update(
            driver=p2.get("pDriverName"),
            port=p2.get("pPortName"),
            spooler_offline=bool(status & 0x80) or bool(attrs & 0x400),
            spooler_error=bool(status & 0x2),
            jobs_in_queue=p2.get("cJobs", 0),
        )
    except Exception as e:  # noqa: BLE001
        info["detail"] = f"could not query spooler: {e}"
    return info


CO_E_SERVER_STOPPING = -2146959352  # 0x80080008: bpacHost.exe is exiting after the previous use


def bpac_dispatch(progid: str = "bpac.Document", attempts: int = 8, delay_s: float = 0.4):
    """``win32com.client.Dispatch`` that rides out b-PAC's out-of-process server shutting down.

    Nothing has been sent to the printer at this point, so retrying is always safe.
    """
    import time

    import pywintypes
    import win32com.client

    for i in range(attempts):
        try:
            return win32com.client.Dispatch(progid)
        except pywintypes.com_error as e:
            if e.hresult != CO_E_SERVER_STOPPING or i == attempts - 1:
                raise
            time.sleep(delay_s)


def com_call0(obj, name: str):
    """Call a zero-argument b-PAC method under late *or* early binding.

    pywin32's dynamic dispatch has no type info for b-PAC, so it invokes a zero-argument member as
    soon as it is accessed (``doc.EndPrint`` already *is* the result) and calling that result raises
    ``TypeError``. Early-bound wrappers and test fakes return a bound method instead.
    """
    v = getattr(obj, name)
    return v() if callable(v) else v


def _parse_tape_mm(media_name: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm", media_name or "", re.I)
    return float(m.group(1)) if m else None


class BpacBackend:
    """Brother b-PAC 3.4 (COM ProgID ``bpac.Document``)."""

    name = "bpac"
    live = True

    def __init__(self, cfg: dict[str, Any], dispatch=None):
        self.cfg = cfg
        self._dispatch = dispatch  # injectable for tests

    # -- COM helpers ----------------------------------------------------------
    def _new_document(self):
        if self._dispatch is not None:
            return self._dispatch("bpac.Document")
        if sys.platform != "win32":
            raise PrintFailed("b-PAC is only available on Windows")
        try:
            import win32com.client  # noqa: F401
        except ImportError as e:
            raise PrintFailed("pywin32 is not installed") from e
        try:
            return bpac_dispatch("bpac.Document")
        except Exception as e:  # noqa: BLE001
            raise PrintFailed(
                "b-PAC 3.4 client component is not installed/registered for this Python "
                f"bitness ({'64' if sys.maxsize > 2**32 else '32'}-bit): {e}"
            ) from e

    @staticmethod
    def _com_init():
        try:
            import pythoncom

            pythoncom.CoInitialize()
            return pythoncom.CoUninitialize
        except ImportError:
            return lambda: None

    # -- status ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        queue = self.cfg["queue"]
        template = self.cfg.get("template") or ""
        out: dict[str, Any] = {
            "mode": "live",
            "queue": queue,
            "template": template,
            "template_exists": bool(template) and Path(template).is_file(),
            "tape_mm_configured": self.cfg["tape_mm"],
            "auto_print": self.cfg.get("auto_print", False),
        }
        if self._dispatch is None:
            out["windows_queue"] = _windows_queue_info(queue)
        uninit = self._com_init()
        doc = printer = None
        try:
            doc = self._new_document()
            printer = doc.Printer
            out["bpac_installed"] = True
            out["online"] = bool(printer.IsPrinterOnline(queue))
            try:
                media = com_call0(printer, "GetMediaName") if out["online"] else ""
                out["tape_detected"] = media or None
                out["tape_mm_detected"] = _parse_tape_mm(media)
            except Exception:  # noqa: BLE001
                out["tape_detected"] = None
        except PrintFailed as e:
            out["bpac_installed"] = False
            out["online"] = False
            out["detail"] = str(e)
        except Exception as e:  # noqa: BLE001
            out["online"] = False
            out["detail"] = f"b-PAC error: {e}"
        finally:
            doc = printer = None  # release COM objects before COM is shut down on this thread
            uninit()
        return out

    # -- print ----------------------------------------------------------------
    def print_label(self, content: LabelContent, copies: int) -> None:
        state: dict[str, Any] = {"phase": "init", "error": None, "cancel": False}
        t = threading.Thread(target=self._print_thread, args=(content, copies, state), daemon=True)
        t.start()
        t.join(self.cfg.get("timeout_s", 45))
        if t.is_alive():
            state["cancel"] = True  # the thread aborts before PrintOut if it has not reached it
            if state["phase"] in ("printout", "endprint", "done"):
                raise PrintUncertain(
                    f"b-PAC did not finish within {self.cfg.get('timeout_s', 45)} s during "
                    f"'{state['phase']}'; the label may or may not have printed"
                )
            raise PrintFailed(f"b-PAC timed out during '{state['phase']}' before sending; nothing printed")
        err = state["error"]
        if err is not None:
            raise err

    def _print_thread(self, content: LabelContent, copies: int, state: dict) -> None:
        uninit = self._com_init()
        doc = None
        try:
            queue = self.cfg["queue"]
            template = self.cfg.get("template") or ""
            fields = self.cfg["fields"]
            state["phase"] = "open"
            if not template or not Path(template).is_file():
                raise PrintFailed(f"label template not found: {template!r} (set printer.template)")
            doc = self._new_document()
            if not doc.Printer.IsPrinterOnline(queue):
                raise PrinterOffline(f"printer queue {queue!r} is offline or not connected")
            if self.cfg.get("check_tape", True):
                try:
                    detected = _parse_tape_mm(com_call0(doc.Printer, "GetMediaName"))
                except Exception:  # noqa: BLE001
                    detected = None
                if detected is not None and abs(detected - float(self.cfg["tape_mm"])) > 0.01:
                    raise PrintFailed(
                        f"installed tape is {detected:g} mm but printer.tape_mm/template is "
                        f"{self.cfg['tape_mm']} mm; load the right tape or switch template"
                    )
            if not doc.Open(str(template)):
                raise PrintFailed(f"b-PAC could not open template {template} (error {doc.ErrorCode})")
            state["phase"] = "fill"
            self._set_text(doc, fields.get("name"), content.short_name(), required=True)
            id_line = "scan to open bin" if content.part_id.startswith("BIN:") else content.part_id  # as preview
            self._set_text(doc, fields.get("part_id"), id_line, required=True)
            self._set_text(doc, fields.get("bin"), content.bin_code, required=True)
            self._set_barcode(doc, fields.get("barcode"), content.part_id)
            state["phase"] = "setprinter"
            if not doc.SetPrinter(queue, True):
                raise PrintFailed(f"b-PAC could not select printer queue {queue!r}")
            if not doc.StartPrint("", BPO_AUTOCUT):
                raise PrintFailed(f"b-PAC StartPrint failed (error {doc.ErrorCode})")
            if state["cancel"]:
                raise PrintFailed("cancelled after timeout before sending")
            state["phase"] = "printout"
            # PrintOut buffers; EndPrint hands the job to the spooler.
            if not doc.PrintOut(int(copies), BPO_AUTOCUT):
                raise PrintFailed(f"b-PAC PrintOut failed (error {doc.ErrorCode}); nothing sent")
            state["phase"] = "endprint"
            try:
                ok = com_call0(doc, "EndPrint")
            except Exception as e:  # noqa: BLE001
                raise PrintUncertain(f"b-PAC EndPrint raised {e}; the label may have printed") from e
            if not ok:
                raise PrintUncertain(
                    f"b-PAC EndPrint reported failure (error {doc.ErrorCode}); the job may have reached the printer"
                )
            state["phase"] = "done"
        except (PrinterOffline, PrintFailed, PrintUncertain) as e:
            state["error"] = e
        except Exception as e:  # noqa: BLE001
            if state["phase"] in ("printout", "endprint", "done"):
                state["error"] = PrintUncertain(f"unexpected b-PAC error during {state['phase']}: {e}")
            else:
                state["error"] = PrintFailed(f"unexpected b-PAC error during {state['phase']}: {e}")
        finally:
            if doc is not None:
                try:
                    com_call0(doc, "Close")
                except Exception:  # noqa: BLE001
                    pass
            doc = None  # release COM objects before COM is shut down on this thread
            uninit()

    @staticmethod
    def _set_text(doc, obj_name: str | None, value: str, required: bool) -> None:
        if not obj_name:
            return
        obj = doc.GetObject(obj_name)
        if obj is None:
            if required:
                raise PrintFailed(f"template has no object named {obj_name!r}")
            return
        obj.Text = value

    @staticmethod
    def _set_barcode(doc, obj_name: str | None, value: str) -> None:
        """Barcode is optional: narrow-tape templates simply omit the object."""
        if not obj_name:
            return
        try:
            idx = doc.GetBarcodeIndex(obj_name)
        except Exception:  # noqa: BLE001
            idx = -1
        if idx is not None and idx >= 0:
            doc.SetBarcodeData(idx, value)
            return
        obj = doc.GetObject(obj_name)
        if obj is not None:
            obj.Text = value


def make_backend(cfg: dict[str, Any]):
    return BpacBackend(cfg) if cfg.get("mode") == "live" else PreviewBackend(cfg)
