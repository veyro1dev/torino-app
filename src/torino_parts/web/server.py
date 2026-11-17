"""The local web server behind the desktop window and the phone scanner.

Three listeners run in one background thread:

* desktop  - http://127.0.0.1:<desktop_port>   (only this PC; every API call needs the session token)
* phone    - https://<lan-ip>:<phone_port>     (paired phones only; device cookie + CSRF header)
* setup    - http://<lan-ip>:<setup_port>      (serves ONLY the public CA certificate + instructions)

All three use the same ``Garage`` service object (same records as MCP and the CLI).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import secrets
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..app import build_inventory, describe, get_active_vehicle, set_active_vehicle
from ..config import BACKEND_LABEL, BACKENDS, data_dir, home_dir, load_config, save_config, set_value, source_key
from ..inventory import SOURCE, InventoryError
from ..store.base import StoreUnavailable
from .pairing import Pairing
from .tls import LocalTLS, lan_ips

log = logging.getLogger("torino_parts.web")
STATIC = Path(__file__).with_name("static")
MAX_UPLOAD = 15 * 1024 * 1024


class NoCacheStatic(StaticFiles):
    """Scripts/styles: the browser must re-check with the server (cheap: ETag -> 304) every time.

    Without this, the desktop window (WebView2) and phone browsers may keep using an older app.js
    after an upgrade - the new menu appears but the new pages don't work."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def _page(path: Path) -> HTMLResponse:
    """The page shell, never cached, with version-stamped script/style URLs so an upgrade can't reuse old files."""
    html = path.read_text(encoding="utf-8")
    for name in ("app.js", "app.css", "phone.js"):
        html = html.replace(f'/static/{name}"', f'/static/{name}?v={__version__}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


class EventBus:
    """In-memory events for the desktop (phone scans, label results). Polled by the window."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._seq = 0

    def publish(self, ev: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            ev = {**ev, "seq": self._seq, "at": time.strftime("%H:%M:%S")}
            self._events.append(ev)
            self._events = self._events[-200:]
            return ev

    def after(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    @property
    def seq(self) -> int:
        return self._seq


class AppContext:
    def __init__(self, phone_host: str = "0.0.0.0", seed_demo: bool = True):
        self.token = secrets.token_urlsafe(24)
        self.events = EventBus()
        self.pairing = Pairing(home_dir() / "devices.json")
        self.tls = LocalTLS(home_dir() / "tls")
        self.phone_host = phone_host
        self.seed_demo = seed_demo
        self._lock = threading.RLock()
        self.inv = None
        self.inv_error: str | None = None
        self.google_flow: dict[str, Any] = {"state": "idle", "log": []}
        self._printer_cache: tuple[float, dict] | None = None
        self._last_scan: dict[str, tuple[str, float, dict]] = {}
        self.ips: list[str] = []
        self.reload()

    @property
    def cfg(self) -> dict[str, Any]:
        return load_config()

    def reload(self) -> None:
        with self._lock:
            if self.inv is not None:
                try:
                    self.inv.labels.queue.close()
                except Exception:  # noqa: BLE001
                    pass
            self.inv, self.inv_error = None, None
            self._printer_cache = None
            cfg = load_config()
            try:
                inv = build_inventory(cfg)
                inv.labels.startup()
                if cfg["backend"] == "demo" and self.seed_demo and not inv.store.read()["Vehicles"]:
                    from ..sample_data import seed_sample_data

                    SOURCE.set("demo")
                    seed_sample_data(inv)
                self.inv = inv
                if cfg["backend"] == "local":
                    threading.Thread(target=self._auto_backup, daemon=True).start()
            except StoreUnavailable as e:
                self.inv_error = str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("could not open inventory")
                self.inv_error = f"{type(e).__name__}: {e}"

    def _auto_backup(self) -> None:
        try:
            from ..datasafety import auto_backup_if_due
            out = auto_backup_if_due()
            if out:
                log.info("automatic backup: %s", out["path"])
        except Exception:  # noqa: BLE001 - never stop the app over a backup
            log.exception("automatic backup failed")

    def get(self):
        with self._lock:
            if self.inv is not None and getattr(self.inv, "source_key", None) != source_key(load_config()):
                self.reload()  # source switched elsewhere (CLI); desktop, phone and MCP stay on the same one
            if self.inv is None:
                raise StoreUnavailable(self.inv_error or "inventory not available")
            return self.inv

    # ------------------------------------------------------------- status
    def printer_status(self, force: bool = False) -> dict[str, Any]:
        if not force and self._printer_cache and time.time() - self._printer_cache[0] < 15:
            return self._printer_cache[1]
        cfg = self.cfg["printer"]
        out: dict[str, Any] = {"mode": cfg["mode"], "tape_mm": cfg["tape_mm"], "auto_print": cfg["auto_print"],
                               "queue": cfg["queue"]}
        try:
            inv = self.get()
            st = inv.labels.backend.status()
            out.update(st)
            counts = inv.labels.queue.counts()
            out["pending"] = counts.get("pending", 0)
            out["uncertain"] = counts.get("uncertain", 0)
            out["failed"] = counts.get("failed", 0)
        except Exception as e:  # noqa: BLE001
            out["detail"] = str(e)
        if cfg["mode"] != "live":
            out.update(ready=False, level="info", text="Preview only (no printer)")
        elif out.get("online") and out.get("template_exists"):
            out.update(ready=True, level="ok", text=f"Ready · {cfg['tape_mm']} mm")
        elif not out.get("bpac_installed", True):
            out.update(ready=False, level="bad", text="b-PAC not installed")
        elif not out.get("template_exists"):
            out.update(ready=False, level="bad", text="No label template")
        else:
            out.update(ready=False, level="warn", text="Printer offline")
        if out.get("uncertain"):
            out.update(level="bad", text=out["text"] + f" · {out['uncertain']} uncertain")
        self._printer_cache = (time.time(), out)
        return out

    def phone_status(self) -> dict[str, Any]:
        devs = self.pairing.devices()
        conn = [d for d in devs if d["connected"]]
        if conn:
            text, level = f"Phone connected ({conn[0]['name']})", "ok"
        elif devs:
            text, level = f"Phone paired ({len(devs)})", "info"
        else:
            text, level = "Phone not paired", "warn"
        return {"paired": len(devs), "connected": bool(conn), "text": text, "level": level,
                "url": self.phone_url(), "enabled": self.cfg["network"]["phone_enabled"]}

    def phone_url(self) -> str:
        ip = self.ips[0] if self.ips else "127.0.0.1"
        return f"https://{ip}:{self.cfg['network']['phone_port']}"

    def setup_url(self) -> str:
        ip = self.ips[0] if self.ips else "127.0.0.1"
        return f"http://{ip}:{self.cfg['network']['setup_port']}"

    def data_version(self) -> str:
        try:
            inv = self.get()
        except StoreUnavailable:
            return "unavailable"
        st = inv.store
        dv = st.data_version()
        if dv is not None:
            v = str(dv)
        else:
            snap = st.read()
            v = "|".join(f"{len(rows)}" for rows in snap.values())
            v += "|" + (snap["Activity"][-1]["act_id"] if snap["Activity"] else "")
            v += "|" + (snap["Transactions"][-1]["txn_id"] if snap["Transactions"] else "")
        q = inv.labels.queue
        row = q._conn().execute("SELECT COUNT(*), MAX(updated_at) FROM jobs").fetchone()
        return f"{v}:{row[0]}:{row[1]}"

    def status(self) -> dict[str, Any]:
        cfg = self.cfg
        b = cfg["backend"]
        out: dict[str, Any] = {"version": __version__, "backend": b, "mode": BACKEND_LABEL.get(b, b),
                               "demo": b == "demo", "local": b == "local",
                               "error": self.inv_error, "google_flow": {k: v for k, v in self.google_flow.items() if k != "log"}}
        if b == "google":
            g = cfg["google"]
            out["workbook"] = {"name": g.get("workbook_name"), "ok": self.inv is not None,
                               "url": f"https://docs.google.com/spreadsheets/d/{g['spreadsheet_id']}" if g.get("spreadsheet_id") else None}
        elif b == "local":
            out["workbook"] = {"name": "Local database on this PC", "ok": self.inv is not None, "url": None}
        else:
            out["workbook"] = {"name": "DEMO data (local, not your real records)", "ok": self.inv is not None, "url": None}
        out["google_configured"] = bool(cfg["google"].get("spreadsheet_id"))
        vid = get_active_vehicle(cfg)
        out["active_vehicle"] = None
        if self.inv is not None:
            vs = self.inv.list_vehicles()["vehicles"]
            out["vehicles"] = [{"vehicle_id": v["vehicle_id"], "label": v["label"]} for v in vs]
            if not vid and vs:
                vid = vs[0]["vehicle_id"]
            if vid:
                v = next((x for x in vs if x["vehicle_id"] == vid), None)
                out["active_vehicle"] = {"vehicle_id": vid, "label": v["label"] if v else vid}
        out["printer"] = self.printer_status()
        out["phone"] = self.phone_status()
        out["data_version"] = self.data_version()
        return out

    # --------------------------------------------------------------- scans
    def handle_scan(self, device: str, code: str, mode: str = "lookup") -> dict[str, Any]:
        code = (code or "").strip()
        if not code:
            raise InventoryError("empty code")
        now = time.time()
        last = self._last_scan.get(device)
        if last and last[0] == code and now - last[1] < 2.5 and mode == "lookup":
            self._last_scan[device] = (code, now, last[2])
            return {**last[2], "repeat": True}
        inv = self.get()
        try:
            res = inv.lookup_code(code)
            out = {"found": True, **scan_summary(res)}
        except InventoryError as e:
            out = {"found": False, "code": code, "message": str(e)}
        self._last_scan[device] = (code, now, out)
        if mode == "lookup":
            name = self.pairing.name_of(device) if device.startswith("D") else device
            self.events.publish({"type": "scan", "device": name, "code": code, "result": out})
        return out


def scan_summary(res: dict[str, Any]) -> dict[str, Any]:
    kind = res["kind"]
    if kind == "part":
        p = res["part"]
        return {"kind": "part", "id": p["part_id"], "title": p["label_name"], "status": p["status"],
                "total_qty": p["total_qty"],
                "entries": [{"stock_id": s["stock_id"], "bin_code": s["bin_code"], "qty": s["qty"],
                             "condition": s["condition"]} for s in p["stock"]]}
    if kind == "bin":
        b = res["bin"]
        return {"kind": "bin", "id": b["bin_code"], "title": b["description"] or f"Bin {b['bin_code']}",
                "total_qty": b["total_qty"],
                "items": [{"part_id": i["part_id"], "name": i["name"], "qty": i["qty"], "condition": i["condition"]}
                          for i in b["items"]]}
    if kind == "job":
        j = res["job"]
        return {"kind": "job", "id": j["job_id"], "title": j["title"], "state": j["state"]}
    if kind == "vehicle":
        v = res["vehicle"]
        return {"kind": "vehicle", "id": v["vehicle_id"], "title": v["label"]}
    r = res["search"]["results"]
    return {"kind": "search", "id": res["code"], "title": f"{len(r)} matching part(s)",
            "results": [{"part_id": p["part_id"], "name": p["label_name"], "total_qty": p["total_qty"]} for p in r[:6]]}


# ======================================================================
# helpers
# ======================================================================
def _err(e: Exception) -> JSONResponse:
    if isinstance(e, InventoryError):
        return JSONResponse(e.to_dict(), status_code=400)
    if isinstance(e, StoreUnavailable):
        return JSONResponse({"error": str(e), "needs_user_action": True}, status_code=503)
    if isinstance(e, (ValueError, KeyError)):
        return JSONResponse({"error": str(e)}, status_code=400)
    log.error("unexpected error: %s", traceback.format_exc())
    return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


async def _body(request: Request) -> dict[str, Any]:
    if request.method != "POST":
        return {}
    raw = await request.body()
    if len(raw) > MAX_UPLOAD:
        raise InventoryError("upload too large (15 MB max)")
    if not raw:
        return {}
    return json.loads(raw)


def _clean(d: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Pick keys, turning '' into None so optional parameters stay optional."""
    out = {}
    for k in keys:
        if k in d:
            v = d[k]
            out[k] = None if v == "" else v
    return out


# ======================================================================
# desktop app (127.0.0.1)
# ======================================================================
def desktop_app(ctx: AppContext) -> Starlette:
    routes: list = []

    def api(path: str, methods=("GET",)):
        def deco(fn: Callable[[Request, dict], Any]):
            async def handler(request: Request):
                if not _desktop_host_ok(request, ctx):
                    return PlainTextResponse("forbidden", status_code=403)
                if not secrets.compare_digest(request.headers.get("x-torino-token", ""), ctx.token):
                    return JSONResponse({"error": "not authorized"}, status_code=401)
                try:
                    body = await _body(request)

                    def run():
                        SOURCE.set("desktop")
                        return fn(request, body)

                    result = await run_in_threadpool(run)
                except Exception as e:  # noqa: BLE001
                    return _err(e)
                if isinstance(result, Response):
                    return result
                return JSONResponse(result)
            routes.append(Route(path, handler, methods=list(methods)))
            return fn
        return deco

    def media(path: str):
        def deco(fn):
            async def handler(request: Request):
                if not _desktop_host_ok(request, ctx) or not secrets.compare_digest(
                        request.query_params.get("t", ""), ctx.token):
                    return PlainTextResponse("forbidden", status_code=403)
                try:
                    return await run_in_threadpool(fn, request)
                except Exception as e:  # noqa: BLE001
                    return _err(e)
            routes.append(Route(path, handler))
            return fn
        return deco

    inv = ctx.get
    q = lambda r, k, d=None: r.query_params.get(k) or d  # noqa: E731

    def _vid(v):
        from ..app import resolve_vehicle
        return resolve_vehicle(inv(), v or None)

    @api("/api/status")
    def status(r, b):
        return ctx.status()

    @api("/api/poll")
    def poll(r, b):
        after = int(q(r, "after", "0"))
        return {"events": ctx.events.after(after), "seq": ctx.events.seq, "data_version": ctx.data_version(),
                "phone": ctx.phone_status(), "google_flow": {k: v for k, v in ctx.google_flow.items() if k != "log"}}

    @api("/api/search")
    def search(r, b):
        code = q(r, "q", "")
        try:
            return inv().lookup_code(code)
        except InventoryError:
            return {"kind": "none", "code": code}

    # vehicles ---------------------------------------------------------
    @api("/api/vehicles")
    def vehicles(r, b):
        return inv().list_vehicles()

    @api("/api/vehicles", ("POST",))
    def add_vehicle(r, b):
        out = inv().add_vehicle(b["request_id"], b.get("year"), b.get("make", ""), b.get("model", ""),
                                b.get("nickname", ""), b.get("trim", ""), b.get("engine", ""), b.get("vin", ""),
                                b.get("notes", ""))
        if b.get("make_active"):
            set_active_vehicle(out["vehicle_id"])
        return out

    @api("/api/active-vehicle", ("POST",))
    def active(r, b):
        v = inv().get_vehicle(b["vehicle_id"])
        set_active_vehicle(v["vehicle_id"])
        return {"vehicle_id": v["vehicle_id"], "label": v["label"]}

    @api("/api/vehicle/{vid}")
    def vehicle(r, b):
        g = inv()
        out = g.vehicle_overview(r.path_params["vid"])
        out["detail"] = g.get_vehicle(r.path_params["vid"])
        return out

    # jobs -----------------------------------------------------------------
    @api("/api/next")
    def nxt(r, b):
        m = q(r, "minutes")
        return inv().what_next(q(r, "vehicle"), int(m) if m else None)

    @api("/api/jobs")
    def jobs(r, b):
        return inv().list_jobs(q(r, "vehicle"), q(r, "status"), q(r, "system"), q(r, "closed") == "1")

    @api("/api/jobs", ("POST",))
    def create_job(r, b):
        steps = [s for s in (b.get("steps") or "").splitlines() if s.strip()] if isinstance(b.get("steps"), str) else b.get("steps")
        return inv().create_job(b["request_id"], b["vehicle_id"], b.get("title", ""), b.get("system") or "other",
                                b.get("goal", ""), b.get("kind") or "assembly", int(b.get("priority") or 3),
                                b.get("estimate_min") or None, steps, b.get("depends_on") or [], b.get("parts") or [],
                                b.get("notes", ""))

    @api("/api/job/{jid}")
    def job(r, b):
        return inv().get_job(r.path_params["jid"])

    @api("/api/job/{jid}", ("POST",))
    def update_job(r, b):
        kw = _clean(b, "title", "system", "goal", "kind", "status", "priority", "estimate_min", "notes",
                    "hold_reason", "add_steps", "steps_done", "steps_undone", "add_depends_on", "remove_depends_on",
                    "add_parts", "note")
        if isinstance(kw.get("add_steps"), str):
            kw["add_steps"] = [s for s in kw["add_steps"].splitlines() if s.strip()]
        return inv().update_job(b["request_id"], r.path_params["jid"], **kw)

    @api("/api/need/{nid}", ("POST",))
    def need(r, b):
        return inv().update_job_part(b["request_id"], r.path_params["nid"],
                                     **_clean(b, "check_status", "part_id", "qty_needed", "check", "notes"))

    @api("/api/job/{jid}/install", ("POST",))
    def install(r, b):
        return inv().record_job_installation(b["request_id"], r.path_params["jid"], **_clean(
            b, "need_id", "part_id", "qty", "bin_code", "stock_id", "notes"))

    @api("/api/removal", ("POST",))
    def removal(r, b):
        kw = _clean(b, "vehicle_id", "bin_code", "qty", "condition", "part_id", "new_part", "removed_from", "job_id",
                    "reinstall_job_id", "check", "photo_base64", "print_label", "notes", "create_bin_if_missing")
        return inv().record_removal(b["request_id"], **kw)

    # parts & bins ------------------------------------------------------------
    @api("/api/parts")
    def parts(r, b):
        return inv().search_parts(q(r, "q", ""), include_out_of_stock=q(r, "all") == "1", limit=50)

    @api("/api/part/{pid}")
    def part(r, b):
        g = inv()
        out = g.get_part(r.path_params["pid"])
        out["photos"] = g.list_photos(part_id=out["part_id"])["photos"]
        out["needed_by"] = [{"need_id": n["need_id"], "job_id": n["job_id"]} for n in g.store.read()["JobParts"]
                            if n["part_id"] == out["part_id"]]
        return out

    @api("/api/bins")
    def bins(r, b):
        return inv().list_bins()

    @api("/api/bins", ("POST",))
    def create_bin(r, b):
        return inv().create_bin(b["request_id"], b["bin_code"], b.get("description", ""), b.get("location", ""),
                                b.get("kind") or "bin")

    @api("/api/bin/{code}")
    def one_bin(r, b):
        return inv().list_bin(r.path_params["code"])

    @api("/api/stock/receive", ("POST",))
    def receive(r, b):
        return inv().receive_stock(b["request_id"], **_clean(b, "bin_code", "qty", "condition", "part_id", "new_part",
                                                             "donor_vehicle_id", "notes", "photo_base64", "print_label",
                                                             "create_bin_if_missing"))

    @api("/api/stock/move", ("POST",))
    def move(r, b):
        return inv().move_stock(b["request_id"], b["part_id"], b["from_bin"], b["to_bin"], int(b.get("qty") or 1),
                                b.get("condition") or None, b.get("stock_id") or None, bool(b.get("print_label")))

    @api("/api/stock/adjust", ("POST",))
    def adjust(r, b):
        return inv().adjust_count(b["request_id"], b.get("reason", ""), **_clean(b, "stock_id", "part_id", "bin_code",
                                                                              "condition", "new_qty", "delta"))

    # photos / specs / activity ----------------------------------------------
    @api("/api/photos")
    def photos(r, b):
        return inv().list_photos(q(r, "vehicle"), q(r, "job"), q(r, "part"), 120)

    @api("/api/photos", ("POST",))
    def add_photo(r, b):
        return inv().attach_photo(b["request_id"], **_clean(b, "vehicle_id", "job_id", "part_id", "caption", "stage",
                                                            "photo_base64"))

    @api("/api/specs")
    def specs(r, b):
        return inv().list_specs(q(r, "vehicle"), q(r, "job"), q(r, "part"), q(r, "q", ""))

    @api("/api/specs", ("POST",))
    def add_spec(r, b):
        return inv().record_spec(b["request_id"], b.get("item", ""), b.get("value", ""), b.get("source", ""),
                                 b.get("unit", ""), **_clean(b, "vehicle_id", "job_id", "part_id", "verified", "notes"))

    @api("/api/activity")
    def activity(r, b):
        return inv().activity_log(q(r, "vehicle"), q(r, "job"), q(r, "part"), int(q(r, "limit", "40")))

    # labels / printer -----------------------------------------------------------
    @api("/api/labels")
    def labels(r, b):
        g = inv()
        return {"jobs": g.labels.jobs(q(r, "status"), int(q(r, "limit", "40"))), "printer": ctx.printer_status(force=True)}

    @api("/api/labels/part", ("POST",))
    def label_part(r, b):
        out = inv().print_label(b["request_id"], b["part_id"], b.get("bin_code") or None, int(b.get("copies") or 1))
        ctx._printer_cache = None
        return out

    @api("/api/labels/bin", ("POST",))
    def label_bin(r, b):
        return inv().print_bin_label(b["request_id"], b["bin_code"], int(b.get("copies") or 1))

    @api("/api/labels/process", ("POST",))
    def label_process(r, b):
        return {"processed": inv().labels.process_pending()}

    @api("/api/labels/{job}/resolve", ("POST",))
    def label_resolve(r, b):
        return {"job": inv().labels.resolve(r.path_params["job"], b["action"])}

    @api("/api/printer")
    def printer(r, b):
        return {"settings": ctx.cfg["printer"], "status": ctx.printer_status(force=True)}

    @api("/api/printer", ("POST",))
    def printer_set(r, b):
        cfg = load_config()
        for k in ("mode", "queue", "template", "tape_mm", "auto_print"):
            if k in b and b[k] is not None:
                if k == "template" and b[k] and not Path(b[k]).is_file():
                    raise InventoryError(f"template file not found: {b[k]}")
                set_value(cfg, f"printer.{k}", str(b[k]))
        save_config(cfg)
        ctx.reload()
        return {"settings": ctx.cfg["printer"], "status": ctx.printer_status(force=True)}

    @api("/api/printer/template", ("POST",))
    def printer_template(r, b):
        """Write a PT-P700 .lbx template for the chosen tape and select it (no P-touch Editor needed)."""
        from ..labels.lbx import check_with_bpac, default_template_path, write_template

        cfg = load_config()
        tape = float(b.get("tape_mm") or cfg["printer"]["tape_mm"])
        set_value(cfg, "printer.tape_mm", str(tape))  # validates the width
        out = write_template(default_template_path(tape), tape, printer_name=cfg["printer"]["queue"])
        try:
            chk = check_with_bpac(out)
        except Exception as e:  # noqa: BLE001 - b-PAC missing: the file is still valid for later
            chk = {"ok": None, "error": str(e)}
        cfg["printer"]["template"] = str(out)
        save_config(cfg)
        ctx.reload()
        return {"template": str(out), "tape_mm": cfg["printer"]["tape_mm"], "bpac_ok": chk.get("ok"),
                "bpac": chk, "settings": ctx.cfg["printer"], "status": ctx.printer_status(force=True)}

    @api("/api/printer/test", ("POST",))
    def printer_test(r, b):
        g = inv()
        rid = b.get("request_id") or f"test-{time.strftime('%Y%m%d-%H%M%S')}"
        job_ = g.labels.request("test", rid, "P-TEST", "TEST Door hinge LH", "B-04", 1, send=True)
        if job_["status"] in ("failed",):
            g.labels.resolve(job_["job_id"], "cancel")
        ctx._printer_cache = None
        return {"job": job_}

    # phone pairing ----------------------------------------------------------------
    @api("/api/pair", ("POST",))
    def pair(r, b):
        import segno

        if not ctx.cfg["network"]["phone_enabled"]:
            raise InventoryError("phone access is turned off in settings")
        code, exp = ctx.pairing.new_code()
        pair_url = f"{ctx.phone_url()}/pair?c={code}"
        setup_url = ctx.setup_url()

        def svg(u):
            buf = io.BytesIO()
            segno.make(u, error="m").save(buf, kind="svg", scale=7, border=2, dark="#000", light="#fff")
            return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()

        return {"pair_url": pair_url, "pair_qr": svg(pair_url), "setup_url": setup_url, "setup_qr": svg(setup_url),
                "expires_in_s": int(exp - time.time()), "ips": ctx.ips,
                "ca_fingerprint": ctx.tls.ca_fingerprint() if ctx.tls.ca_cert_path.exists() else None}

    @api("/api/devices")
    def devices(r, b):
        return {"devices": ctx.pairing.devices()}

    @api("/api/devices/{did}/revoke", ("POST",))
    def revoke(r, b):
        return {"revoked": ctx.pairing.revoke(r.path_params["did"])}

    # google / backend ----------------------------------------------------------------
    @api("/api/google")
    def google(r, b):
        cfg = ctx.cfg
        from ..google_setup import client_secret_path, token_path
        return {"flow": ctx.google_flow, "configured": bool(cfg["google"].get("spreadsheet_id")),
                "has_client_secret": client_secret_path().exists(), "signed_in": token_path().exists(),
                "backend": cfg["backend"], "workbook_name": cfg["google"].get("workbook_name")}

    @api("/api/google/connect", ("POST",))
    def google_connect(r, b):
        from ..google_setup import client_secret_path
        if b.get("client_secret"):
            data = json.loads(b["client_secret"])
            if "installed" not in data:
                raise InventoryError("That is not a Desktop-app OAuth client JSON (it has no 'installed' section).")
            client_secret_path().write_text(json.dumps(data), encoding="utf-8")
        if not client_secret_path().exists():
            raise InventoryError("Upload the OAuth client JSON you downloaded from Google Cloud first.")
        if ctx.google_flow.get("state") == "running":
            return {"flow": ctx.google_flow}
        ctx.google_flow = {"state": "running", "log": ["Opening Google sign-in in your web browser..."]}

        def run():
            from ..google_setup import setup_google
            try:
                setup_google(None, None, log=lambda m: ctx.google_flow["log"].append(str(m)), force_signin=True)
                ctx.google_flow["state"] = "done"
                ctx.reload()
            except Exception as e:  # noqa: BLE001
                ctx.google_flow["state"] = "error"
                ctx.google_flow["log"].append(f"{type(e).__name__}: {e}")
        threading.Thread(target=run, daemon=True).start()
        return {"flow": ctx.google_flow}

    @api("/api/backend", ("POST",))
    def backend(r, b):
        cfg = load_config()
        want = b.get("backend")
        if want not in BACKENDS:
            raise InventoryError(f"backend must be one of {', '.join(BACKENDS)}")
        if want == "google" and not cfg["google"].get("spreadsheet_id"):
            raise InventoryError("Connect Google first (Settings > Connect Google).")
        cfg["backend"] = want
        save_config(cfg)
        ctx.reload()
        return ctx.status()

    # progress / notebook --------------------------------------------------------------
    @api("/api/resume")
    def resume(r, b):
        return inv().resume(_vid(q(r, "vehicle")))

    @api("/api/progress")
    def progress(r, b):
        return inv().progress(_vid(q(r, "vehicle")))

    @api("/api/notebook")
    def notebook(r, b):
        return inv().notebook(q(r, "vehicle") or None, q(r, "q", ""), q(r, "job"), q(r, "part"), q(r, "tag"),
                              limit=int(q(r, "limit", "200")))

    @api("/api/notebook", ("POST",))
    def add_note(r, b):
        kw = _clean(b, "vehicle_id", "title", "tags", "job_id", "part_id", "photo_id")
        if not kw.get("vehicle_id") and not kw.get("job_id") and not kw.get("photo_id"):
            try:
                kw["vehicle_id"] = _vid(None)  # file it under the active project
            except InventoryError:
                pass
        return inv().add_note(b["request_id"], b.get("body", ""), **kw)

    # outside shops ---------------------------------------------------------------------
    @api("/api/away")
    def away(r, b):
        return inv().list_away(q(r, "vehicle"), q(r, "part"), q(r, "all") == "1")

    @api("/api/away/send", ("POST",))
    def send_out(r, b):
        return inv().send_out(b["request_id"], b["part_id"], b.get("holder", ""), int(b.get("qty") or 1),
                              **_clean(b, "reason", "from_bin", "stock_id", "condition", "expected_back", "job_id", "notes"))

    @api("/api/away/{cid}/return", ("POST",))
    def ret(r, b):
        return inv().return_from_shop(b["request_id"], r.path_params["cid"], **_clean(
            b, "qty", "to_bin", "condition", "notes", "create_bin_if_missing"))

    # shopping --------------------------------------------------------------------------
    @api("/api/shopping")
    def shopping(r, b):
        return inv().list_shopping(_vid(q(r, "vehicle")), q(r, "status"))

    @api("/api/shopping", ("POST",))
    def add_item(r, b):
        return inv().add_shopping_item(b["request_id"], _vid(b.get("vehicle_id")), b.get("name", ""),
                                       int(b.get("qty") or 1), b.get("status") or "considering",
                                       **_clean(b, "vendor", "link", "est_price", "part_id", "job_id", "need_id",
                                                "category", "notes"))

    @api("/api/shopping/{iid}", ("POST",))
    def upd_item(r, b):
        return inv().update_shopping_item(b["request_id"], r.path_params["iid"], **_clean(
            b, "status", "name", "qty", "vendor", "link", "est_price", "order_ref", "notes", "category", "job_id",
            "part_id", "paid_amount", "paid_date", "allow_additional_payment"))

    @api("/api/shopping/{iid}/receive", ("POST",))
    def rcv_item(r, b):
        return inv().receive_shopping_item(b["request_id"], r.path_params["iid"], b["bin_code"], **_clean(
            b, "qty", "condition", "part_id", "new_part", "print_label", "create_bin_if_missing", "notes"))

    # money -----------------------------------------------------------------------------
    @api("/api/money")
    def money(r, b):
        out = inv().money_summary(_vid(q(r, "vehicle")))
        out["categories"] = inv().expense_categories()
        return out

    @api("/api/money/expense", ("POST",))
    def expense(r, b):
        return inv().record_expense(b["request_id"], b.get("vehicle_id") or None, b.get("amount"), **_clean(
            b, "vendor", "category", "description", "date", "kind", "job_id", "part_id", "item_id",
            "receipt_base64", "notes", "allow_duplicate"))

    @api("/api/money/budget", ("POST",))
    def budget(r, b):
        return inv().set_budget(b["request_id"], _vid(b.get("vehicle_id")), b.get("amount"),
                                b.get("category") or "", b.get("notes") or "")

    # setup readiness -------------------------------------------------------------------
    @api("/api/setup")
    def setup(r, b):
        return setup_readiness(ctx)

    @api("/api/labels/preview", ("POST",))
    def label_preview(r, b):
        """Render what a label will look like without queueing or printing anything."""
        out = inv().preview_label(b["part_id"], b.get("bin_code") or None)
        out["image"] = "data:image/png;base64," + base64.b64encode(Path(out["preview_path"]).read_bytes()).decode()
        return out

    @api("/api/money/void", ("POST",))
    def void(r, b):
        return inv().void_expense(b["request_id"], b["expense_id"], b.get("reason", ""))

    @api("/api/money/export", ("POST",))
    def money_export(r, b):
        """Save the project's expenses as CSV in Documents\\Torino (works offline; nothing is uploaded)."""
        g = inv()
        vid = _vid(b.get("vehicle_id"))
        import os
        folder = Path(os.environ.get("TORINO_EXPORT_DIR") or Path.home() / "Documents" / "Torino")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{vid}-money-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        path.write_text(g.money_csv(vid), encoding="utf-8-sig")  # BOM: opens cleanly in Excel
        return {"path": str(path)}

    # local data: backups, restore, transfer ----------------------------------------
    @api("/api/data")
    def data_info(r, b):
        from ..config import backups_dir
        from ..datasafety import list_backups

        cfg = ctx.cfg
        return {"backend": cfg["backend"], "mode": BACKEND_LABEL[cfg["backend"]],
                "google_configured": bool(cfg["google"].get("spreadsheet_id")),
                "backup_folder": str(backups_dir(cfg)), "backups": list_backups()[:40]}

    @api("/api/data/backup", ("POST",))
    def data_backup(r, b):
        from ..datasafety import backup_current
        return backup_current("manual")

    @api("/api/data/backup-folder", ("POST",))
    def data_backup_folder(r, b):
        cfg = load_config()
        folder = (b.get("folder") or "").strip()
        if folder and not Path(folder).is_dir():
            raise InventoryError(f"folder not found: {folder}")
        cfg["backups"]["folder"] = folder
        save_config(cfg)
        return {"backup_folder": folder}

    @api("/api/data/restore", ("POST",))
    def data_restore(r, b):
        from ..datasafety import restore_into_local
        out = restore_into_local(b["path"], confirm=bool(b.get("confirm")))
        if out.get("restored"):
            ctx.reload()
        return out

    @api("/api/data/transfer", ("POST",))
    def data_transfer(r, b):
        from ..datasafety import transfer
        out = transfer(b["direction"], confirm=bool(b.get("confirm")), replace_local=bool(b.get("replace_local")))
        if out.get("transferred"):
            ctx.reload()
        return out

    @api("/api/demo/reset", ("POST",))
    def demo_reset(r, b):
        cfg = load_config()
        if cfg["backend"] != "demo":
            raise InventoryError("only demo data can be reset")
        ddir = data_dir(cfg)
        import shutil
        with ctx.get().store.lock():
            (ddir / "workbook.json").unlink(missing_ok=True)
        with ctx.get().labels.queue._tx() as c:  # server threads hold connections: clear rows, keep the file
            c.execute("DELETE FROM jobs")
        for name in ("photos", "label-previews"):
            shutil.rmtree(ddir / name, ignore_errors=True)
        ctx.reload()
        return ctx.status()

    @api("/api/mcp-commands")
    def mcp_commands(r, b):
        return mcp_commands_for_install()

    # media ---------------------------------------------------------------------------
    @media("/media/label/{job_id}")
    def label_png(request):
        j = inv().labels.queue.get(request.path_params["job_id"])
        if not j or not j["preview_path"] or not Path(j["preview_path"]).is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(j["preview_path"], media_type="image/png")

    @media("/media/photo")
    def photo(request):
        return serve_photo(ctx, request.query_params.get("link", ""))

    async def index(request: Request):
        if not _desktop_host_ok(request, ctx):
            return PlainTextResponse("forbidden", status_code=403)
        return _page(STATIC / "desktop" / "index.html")

    routes += [Route("/", index), Mount("/static", NoCacheStatic(directory=STATIC / "desktop"), name="static")]
    return Starlette(routes=routes)


def _bpac_registered() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "bpac.Document"))
        return True
    except OSError:
        return False


def setup_readiness(ctx: AppContext) -> dict[str, Any]:
    """Google, printer and phone: is it ready, and the ONE next thing to do if not.

    ``next.act`` names a desktop action (button) and ``next.go`` a page."""
    cfg = ctx.cfg
    out: dict[str, Any] = {}

    # --- Google (optional) ---------------------------------------------------------
    from ..google_setup import client_secret_path, token_path
    g = cfg["google"]
    if cfg["backend"] == "google" and ctx.inv_error:
        out["google"] = {"ready": False, "level": "bad", "text": f"Google needs attention: {ctx.inv_error}",
                         "next": {"label": "Reconnect Google", "go": "#/settings/google"}}
    elif not client_secret_path().exists():
        out["google"] = {"ready": False, "level": "info", "optional": True,
                         "text": "Not set up (optional — Local mode works without Google).",
                         "next": {"label": "Choose the Desktop-app OAuth JSON from Google Cloud", "go": "#/settings/google"}}
    elif not (token_path().exists() and g.get("spreadsheet_id")):
        out["google"] = {"ready": False, "level": "warn", "text": "OAuth file saved; you have not signed in yet.",
                         "next": {"label": "Connect Google (sign in)", "go": "#/settings/google"}}
    elif cfg["backend"] != "google":
        out["google"] = {"ready": True, "level": "ok",
                         "text": f"Connected (workbook '{g.get('workbook_name')}'), not in use right now.",
                         "next": {"label": "Transfer Local → Google or switch source", "go": "#/settings/data"}}
    else:
        out["google"] = {"ready": True, "level": "ok", "text": f"In use: {g.get('workbook_name')}", "next": None}

    # --- printer ---------------------------------------------------------------------
    p = cfg["printer"]
    from ..labels.printers import _windows_queue_info
    qi = _windows_queue_info(p["queue"]) if sys.platform == "win32" else {"exists": None}
    tpl = p.get("template") or ""
    st = ctx.printer_status() if p["mode"] == "live" else {}
    pr: dict[str, Any] = {"mode": p["mode"], "queue": p["queue"], "queue_exists": qi.get("exists"),
                          "bpac": _bpac_registered(), "template": tpl, "template_exists": bool(tpl) and Path(tpl).is_file(),
                          "tape_mm": p["tape_mm"], "tape_detected": st.get("tape_detected"), "uncertain": st.get("uncertain", 0)}
    if pr["uncertain"]:
        pr.update(ready=False, level="bad", text=f"{pr['uncertain']} label(s) may or may not have printed.",
                  next={"label": "Decide on uncertain labels", "go": "#/labels"})
    elif not pr["bpac"]:
        pr.update(ready=False, level="warn", text="Brother b-PAC 3.4 (64-bit) is not installed.",
                  next={"label": "Install b-PAC (see setup guide, step 3)", "go": "#/labels"})
    elif qi.get("exists") is False:
        others = qi.get("brother_queues") or []
        if others:
            pr.update(ready=False, level="warn", text=f"No printer named '{p['queue']}'. Found: {', '.join(others)}.",
                      next={"label": f"Use '{others[0]}'", "act": "useQueue", "arg": others[0]})
        else:
            pr.update(ready=False, level="warn",
                      text="Windows has no PT-P700 printer yet. Labels are previewed and kept until it is ready.",
                      next={"label": "Plug in the PT-P700, switch it on, hold the Editor Lite button until its green "
                                     "light goes off", "go": "#/labels"})
    elif not pr["template_exists"]:
        pr.update(ready=False, level="warn", text="No label template for the loaded tape.",
                  next={"label": f"Create the {p['tape_mm']} mm template", "act": "makeTemplate"})
    elif p["mode"] != "live":
        pr.update(ready=False, level="info", text="Printer set up; Torino is in preview mode (nothing prints).",
                  next={"label": "Switch to Live printing", "act": "goLive"})
    elif not st.get("online"):
        pr.update(ready=False, level="warn", text="Printer is offline. Labels wait in the queue until it is back.",
                  next={"label": "Switch the PT-P700 on and check the USB cable", "go": None})
    elif st.get("tape_mm_detected") and abs(float(st["tape_mm_detected"]) - float(p["tape_mm"])) > 0.01:
        pr.update(ready=False, level="warn",
                  text=f"Loaded tape is {st['tape_detected']}, template is for {p['tape_mm']} mm.",
                  next={"label": f"Create a template for {st['tape_mm_detected']:g} mm", "act": "makeTemplate",
                        "arg": str(st["tape_mm_detected"])})
    else:
        pr.update(ready=True, level="ok", text=f"Ready · {st.get('tape_detected') or str(p['tape_mm']) + ' mm'}",
                  next={"label": "Print one test label", "act": "testLabel"} if not st.get("pending") else
                  {"label": f"Send {st['pending']} waiting label(s)", "act": "processQueue"})
    out["printer"] = pr

    # --- phone -----------------------------------------------------------------------
    ph = ctx.phone_status()
    if not ph["enabled"]:
        out["phone"] = {"ready": False, "level": "info", "text": "Phone access is turned off.", "next": None}
    elif ph["connected"]:
        out["phone"] = {"ready": True, "level": "ok", "text": ph["text"], "next": None}
    elif ph["paired"]:
        out["phone"] = {"ready": True, "level": "info", "text": ph["text"] + " — not open right now.",
                        "next": {"label": "Open the bookmarked Torino page on the phone (same Wi-Fi)", "go": None}}
    else:
        out["phone"] = {"ready": False, "level": "warn", "text": "No phone paired yet.",
                        "next": {"label": "Pair Phone", "act": "pair"}}
    out["phone"]["url"] = ph["url"]
    return out


def _desktop_host_ok(request: Request, ctx: AppContext) -> bool:
    """Defends against DNS-rebinding: only accept requests addressed to the loopback host."""
    host = request.headers.get("host", "")
    port = ctx.cfg["network"]["desktop_port"]
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def serve_photo(ctx: AppContext, link: str) -> Response:
    link = unquote(link)
    inv = ctx.get()
    if link.startswith("file:"):
        from urllib.request import url2pathname
        path = Path(url2pathname(urlparse(link).path)).resolve()
        root = Path(getattr(inv.photos, "folder", Path("/nonexistent"))).resolve()
        if root not in path.parents or not path.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(path)
    from ..photos import parse_drive_id
    try:
        fid = parse_drive_id(link)
    except ValueError:
        return PlainTextResponse("not found", status_code=404)
    cache = data_dir(ctx.cfg) / "photo-cache" / f"{fid}.jpg"
    if not cache.exists():
        data, mime = inv.photos.fetch(fid)
        if not (mime or "").startswith("image/"):  # e.g. a PDF receipt
            return Response(data, media_type=mime or "application/octet-stream")
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.thumbnail((1400, 1400))
        cache.parent.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(cache, quality=85)
    return FileResponse(cache, media_type="image/jpeg")


def mcp_commands_for_install() -> dict[str, Any]:
    if getattr(sys, "frozen", False):
        cli = Path(sys.executable).with_name("torino-cli.exe")
    else:
        cli = Path(sys.executable).with_name("torino.exe")
        if not cli.exists():
            cli = Path(sys.executable).parent / "Scripts" / "torino.exe"
    exe = str(cli)
    esc = exe.replace("\\", "\\\\")
    return {
        "cli": exe,
        "codex": f'codex mcp add torino-parts -- "{exe}" serve --source codex',
        "claude": f'claude mcp add torino-parts --scope user -- "{exe}" serve --source claude',
        "codex_toml": f'[mcp_servers.torino-parts]\ncommand = "{esc}"\nargs = ["serve", "--source", "codex"]\n'
                      'startup_timeout_sec = 30\ntool_timeout_sec = 120',
    }


# ======================================================================
# phone app (HTTPS on the LAN)
# ======================================================================
COOKIE = "torino_dev"


def phone_app(ctx: AppContext) -> Starlette:
    routes: list = []

    def papi(path: str, methods=("GET",), raw: bool = False):
        def deco(fn):
            async def handler(request: Request):
                device = ctx.pairing.check(request.cookies.get(COOKIE))
                if not device:
                    return JSONResponse({"error": "This phone is not paired. On the laptop click Pair Phone."},
                                        status_code=401)
                if request.method == "POST" and request.headers.get("x-torino") != "1":
                    return JSONResponse({"error": "bad request"}, status_code=403)
                try:
                    if raw:
                        body = await request.body()
                        if len(body) > MAX_UPLOAD:
                            raise InventoryError("upload too large")
                    else:
                        body = await _body(request)

                    def run():
                        SOURCE.set("phone")
                        return fn(request, body, device)

                    return JSONResponse(await run_in_threadpool(run))
                except Exception as e:  # noqa: BLE001
                    return _err(e)
            routes.append(Route(path, handler, methods=list(methods)))
            return fn
        return deco

    @papi("/p/api/me")
    def me(r, b, device):
        st = ctx.status()
        return {"device": ctx.pairing.name_of(device), "demo": st["demo"], "mode": st["mode"],
                "workbook": st["workbook"]["name"], "active_vehicle": st["active_vehicle"]}

    @papi("/p/api/scan", ("POST",))
    def scan(r, b, device):
        return ctx.handle_scan(device, b.get("code", ""), b.get("mode", "lookup"))

    @papi("/p/api/frame", ("POST",), raw=True)
    def frame(r, body, device):
        code = decode_barcode(body)
        if not code:
            return {"decoded": None}
        mode = r.query_params.get("mode", "lookup")
        return {"decoded": code, "result": ctx.handle_scan(device, code, mode)}

    @papi("/p/api/jobs")
    def pjobs(r, b, device):
        vid = get_active_vehicle(ctx.cfg)
        if not vid:
            return {"jobs": []}
        return {"jobs": [{"job_id": j["job_id"], "title": j["title"], "state": j["state"]}
                         for j in ctx.get().list_jobs(vid)["jobs"]]}

    @papi("/p/api/bins")
    def pbins(r, b, device):
        return ctx.get().list_bins()

    @papi("/p/api/photo", ("POST",))
    def pphoto(r, b, device):
        vid = get_active_vehicle(ctx.cfg) or None
        out = ctx.get().attach_photo(b["request_id"], vehicle_id=vid if b.get("job_id") or not b.get("part_id") else None,
                                     job_id=b.get("job_id") or None, part_id=b.get("part_id") or None,
                                     caption=b.get("caption", ""), stage=b.get("stage", ""),
                                     photo_base64=b["image"])
        ctx.events.publish({"type": "photo", "device": ctx.pairing.name_of(device), "photo_id": out["photo_id"],
                            "part_id": b.get("part_id"), "job_id": b.get("job_id")})
        return out

    @papi("/p/api/move", ("POST",))
    def pmove(r, b, device):
        out = ctx.get().move_stock(b["request_id"], b["part_id"], b["from_bin"], b["to_bin"], int(b.get("qty") or 1),
                                   None, b.get("stock_id") or None, False)
        ctx.events.publish({"type": "move", "device": ctx.pairing.name_of(device), "message": out["message"],
                            "part_id": b["part_id"]})
        return out

    async def pair(request: Request):
        code = request.query_params.get("c", "")
        ua = request.headers.get("user-agent", "")
        name = "iPhone" if "iPhone" in ua else "Android phone" if "Android" in ua else "Phone"
        res = ctx.pairing.redeem(code, name)
        if not res:
            return HTMLResponse(_simple_page("Pairing code expired",
                                             "This pairing code was already used or has expired. On the laptop click "
                                             "<b>Pair Phone</b> again and scan the new QR code."), status_code=400)
        device_id, cookie = res
        ctx.events.publish({"type": "paired", "device": name})
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, cookie, max_age=365 * 24 * 3600, httponly=True, secure=True, samesite="strict")
        return resp

    async def index(request: Request):
        return _page(STATIC / "phone" / "index.html")

    routes += [Route("/", index), Route("/pair", pair),
               Mount("/static", NoCacheStatic(directory=STATIC / "phone"), name="pstatic")]
    return Starlette(routes=routes)


def decode_barcode(data: bytes) -> str | None:
    import zxingcpp
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:  # noqa: BLE001
        return None
    for b in zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.Code128):
        if b.text:
            return b.text.strip()
    return None


def _simple_page(title: str, body: str) -> str:
    return (f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><body style='font:20px system-ui;padding:24px;max-width:640px'>"
            f"<h1 style='font-size:28px'>{title}</h1><p>{body}</p></body>")


# ======================================================================
# setup app (plain HTTP: the public CA certificate only)
# ======================================================================
def setup_app(ctx: AppContext) -> Starlette:
    async def index(request: Request):
        fp = ctx.tls.ca_fingerprint()
        return HTMLResponse(_simple_page("Torino phone setup (one time)", f"""
Install the Torino certificate so your phone trusts the laptop's private HTTPS page.<br><br>
<a href="/torino-ca.crt" style="display:inline-block;background:#0b5;color:#fff;padding:16px 22px;border-radius:10px;
text-decoration:none;font-weight:700">Download certificate</a>
<h2 style="font-size:22px">iPhone</h2><ol>
<li>Tap Download certificate, then <b>Allow</b>.</li>
<li>Settings &rarr; <b>Profile Downloaded</b> (or General &rarr; VPN &amp; Device Management) &rarr; Install.</li>
<li>Settings &rarr; General &rarr; About &rarr; <b>Certificate Trust Settings</b> &rarr; turn on <i>Torino Garage local CA</i>.</li></ol>
<h2 style="font-size:22px">Android</h2><ol>
<li>Tap Download certificate.</li>
<li>Settings &rarr; Security &amp; privacy &rarr; More security settings &rarr; Encryption &amp; credentials &rarr;
<b>Install a certificate</b> &rarr; <b>CA certificate</b> &rarr; Install anyway &rarr; pick <i>torino-ca.crt</i>.</li></ol>
<p>Then scan the <b>Pair</b> QR code shown on the laptop.</p>
<p style="font-size:14px;color:#555">Certificate fingerprint (SHA-256):<br><code style="word-break:break-all">{fp}</code><br>
This certificate only works for private home-network addresses; it cannot be used for public websites.</p>"""))

    async def ca(request: Request):
        return Response(ctx.tls.ca_der(), media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="torino-ca.crt"'})

    return Starlette(routes=[Route("/", index), Route("/torino-ca.crt", ca)])


# ======================================================================
# runner
# ======================================================================
class WebServer:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.servers = []
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def start(self, timeout: float = 20) -> None:
        import uvicorn

        net = self.ctx.cfg["network"]
        servers = [uvicorn.Server(uvicorn.Config(desktop_app(self.ctx), host="127.0.0.1", port=net["desktop_port"],
                                                 log_level="warning", lifespan="off", access_log=False))]
        if net["phone_enabled"]:
            self.ctx.ips = lan_ips() if self.ctx.phone_host == "0.0.0.0" else []
            cert, key, _ips = self.ctx.tls.ensure_server_cert(self.ctx.ips)
            servers.append(uvicorn.Server(uvicorn.Config(phone_app(self.ctx), host=self.ctx.phone_host,
                                                         port=net["phone_port"], ssl_certfile=str(cert),
                                                         ssl_keyfile=str(key), log_level="warning", lifespan="off",
                                                         access_log=False)))
            servers.append(uvicorn.Server(uvicorn.Config(setup_app(self.ctx), host=self.ctx.phone_host,
                                                         port=net["setup_port"], log_level="warning", lifespan="off",
                                                         access_log=False)))
        self.servers = servers

        def run():
            try:
                asyncio.run(self._serve())
            except BaseException as e:  # noqa: BLE001
                self.error = e
                log.exception("web server stopped")

        self.thread = threading.Thread(target=run, name="torino-web", daemon=True)
        self.thread.start()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.error or not self.thread.is_alive():
                raise RuntimeError(f"web server failed to start: {self.error}")
            if all(s.started for s in self.servers):
                return
            time.sleep(0.05)
        raise RuntimeError("web server did not start in time")

    async def _serve(self):
        await asyncio.gather(*(s.serve() for s in self.servers))
        if any(not s.started for s in self.servers):
            raise RuntimeError("a listener could not start (is the port already in use?)")

    def stop(self) -> None:
        for s in self.servers:
            s.should_exit = True
        if self.thread:
            self.thread.join(10)

    @property
    def desktop_url(self) -> str:
        return f"http://127.0.0.1:{self.ctx.cfg['network']['desktop_port']}/?t={quote(self.ctx.token)}"
