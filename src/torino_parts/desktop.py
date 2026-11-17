"""Torino.exe: the desktop app.

Starts the local web server (desktop UI + phone scanner) and opens it in a native window
(Microsoft Edge WebView2 via pywebview). Closing the window stops the server.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

WINDOW_TITLE = "Torino Garage"


def _setup_logging() -> Path:
    from .config import home_dir

    logdir = home_dir() / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / "torino-app.log"
    if logfile.exists() and logfile.stat().st_size > 5_000_000:
        logfile.replace(logdir / "torino-app.old.log")
    # A windowed .exe has no console: send stray prints to the log file.
    if sys.stdout is None or sys.stderr is None:
        f = open(logfile, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
    logging.basicConfig(level=logging.INFO, filename=str(logfile), encoding="utf-8",
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return logfile


def _run_file() -> Path:
    from .config import home_dir

    return home_dir() / "run.json"


def _already_running() -> bool:
    """If another Torino window is open, bring it to the front and return True."""
    rf = _run_file()
    if not rf.exists():
        return False
    try:
        info = json.loads(rf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    from .labels.queue import _pid_alive

    if not _pid_alive(int(info.get("pid", 0))) or info.get("pid") == os.getpid():
        return False
    if sys.platform == "win32":
        try:
            import win32con
            import win32gui

            hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
            if hwnd:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
    return True


def _message(text: str, title: str = "Torino") -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    else:
        print(text)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="Torino")
    ap.add_argument("--headless", action="store_true", help="run the server without a window (testing)")
    ap.add_argument("--local-only", action="store_true", help="don't listen on the home network (no phone)")
    ap.add_argument("--print-url", action="store_true", help="print the desktop URL (with session token) to stdout")
    args = ap.parse_args(argv)

    logfile = _setup_logging()
    log = logging.getLogger("torino_parts.desktop")
    if _already_running():
        log.info("another instance is running; focused it")
        return

    from .web.server import AppContext, WebServer

    try:
        ctx = AppContext(phone_host="127.0.0.1" if args.local_only else "0.0.0.0")
        server = WebServer(ctx)
        server.start()
    except Exception as e:  # noqa: BLE001
        log.exception("startup failed")
        _message(f"Torino could not start:\n\n{e}\n\nDetails: {logfile}")
        sys.exit(1)
    _run_file().write_text(json.dumps({"pid": os.getpid(), "port": ctx.cfg["network"]["desktop_port"],
                                       "started": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
    log.info("server started: desktop on 127.0.0.1:%s, phone on %s", ctx.cfg["network"]["desktop_port"], ctx.phone_url())
    if args.print_url:
        print(server.desktop_url, flush=True)
    try:
        if args.headless:
            while True:
                time.sleep(1)
        else:
            _open_window(server, log)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        try:
            _run_file().unlink()
        except OSError:
            pass
        log.info("stopped")


PICK_TYPES = {
    "template": ("P-touch label template (*.lbx)",),
    "backup": ("Torino backup (*.zip)",),
    "folder": (),
}


class Bridge:
    """Called from the page as window.pywebview.api.* (native Windows dialogs)."""

    def pick_file(self, kind: str = "template", start: str = ""):
        import webview

        win = webview.windows[0]
        dialog = getattr(getattr(webview, "FileDialog", None), "FOLDER" if kind == "folder" else "OPEN", None)
        if dialog is None:  # older pywebview
            dialog = webview.FOLDER_DIALOG if kind == "folder" else webview.OPEN_DIALOG
        kw = {"directory": start} if start and Path(start).exists() else {}
        if kind != "folder":
            kw["file_types"] = PICK_TYPES.get(kind, ()) + ("All files (*.*)",)
        res = win.create_file_dialog(dialog, allow_multiple=False, **kw)
        if not res:
            return None
        return res[0] if isinstance(res, (list, tuple)) else str(res)


def _open_window(server, log) -> None:
    from .config import home_dir

    try:
        import webview

        webview.create_window(WINDOW_TITLE, server.desktop_url, width=1480, height=940, min_size=(1000, 680),
                              text_select=True, zoomable=True, js_api=Bridge())
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(home_dir() / "webview"))
    except Exception:  # noqa: BLE001 - no WebView2: fall back to the default browser
        log.exception("native window unavailable; using the default browser")
        import webbrowser

        webbrowser.open(server.desktop_url)
        _message("Torino is open in your web browser.\n\nKeep this message open while you work; click OK to quit Torino.")


if __name__ == "__main__":
    main()
