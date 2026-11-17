"""Verify an EXTRACTED release folder end to end (uses a throwaway Torino home; never your real data).

    python packaging\\verify_release.py <folder containing Torino.exe>

Checks: CLI + demo, desktop API, phone pairing over trusted HTTPS, phone scan reaching the desktop,
camera-frame decoding, MCP stdio (inventory + rebuild tools), install recorded via MCP visible to the
desktop API, restart persistence of job history and label queue, and the native Torino.exe window.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

RESULTS: list[tuple[bool, str]] = []
_STARTED: list = []


def _popen(*a, **k):
    """subprocess.Popen that is always cleaned up, even if a check crashes half way."""
    p = subprocess.Popen(*a, **k)
    _STARTED.append(p)
    return p


@__import__("atexit").register
def _cleanup() -> None:
    for p in _STARTED:
        if p.poll() is None:
            p.kill()


def check(ok: bool, what: str) -> None:
    RESULTS.append((bool(ok), what))
    print(f"[{'OK' if ok else 'FAIL'}] {what}", flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main(folder: str) -> int:
    app = Path(folder)
    cli, gui = app / "torino-cli.exe", app / "Torino.exe"
    check(cli.is_file() and gui.is_file(), f"release contains Torino.exe and torino-cli.exe ({app})")
    home = Path(tempfile.mkdtemp(prefix="torino-verify-"))
    env = {**os.environ, "TORINO_HOME": str(home), "PYTHONIOENCODING": "utf-8"}
    run = lambda *a, **k: subprocess.run([str(cli), *a], env=env, capture_output=True, text=True, timeout=300, **k)  # noqa: E731

    r = run("--help")
    check(r.returncode == 0 and "serve" in r.stdout and "app" in r.stdout, "torino-cli --help lists serve/app/demo")
    r = run("demo", "--out", str(home / "demo-out"))
    check(r.returncode == 0 and "6/6 checks passed" in r.stdout, "torino-cli demo: 6/6 checks on DEMO data")

    cfg = json.loads((home / "config.json").read_text()) if (home / "config.json").exists() else {}
    cfg.setdefault("network", {}).update(desktop_port=free_port(), phone_port=free_port(), setup_port=free_port())
    (home / "config.json").write_text(json.dumps(cfg))

    # ---- MCP stdio against the packaged server: inventory tool + rebuild tool, then an install
    from mcp.client.client import Client
    from mcp.client.stdio import StdioServerParameters

    async def mcp_session():
        params = StdioServerParameters(command=str(cli), args=["serve", "--source", "codex"], env=env)
        async with Client(params, read_timeout_seconds=90) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            out = {"names": names}
            for name, args in [("search_parts", {"query": "left door hinges"}),
                               ("get_job", {"job_id": "J-00003"}),
                               ("what_next", {"minutes": 30}),
                               ("record_job_installation", {"request_id": "verify-install-sway", "job_id": "J-00005",
                                                            "need_id": "N-00006", "qty": 1}),
                               ("record_job_installation", {"request_id": "verify-install-sway", "job_id": "J-00005",
                                                            "need_id": "N-00006", "qty": 1})]:
                res = await c.call_tool(name, args)
                sc = res.structured_content
                out.setdefault(name, []).append(sc if sc is not None else json.loads(res.content[0].text))
            return out

    m = asyncio.run(mcp_session())
    check({"search_parts", "what_next", "get_job", "record_job_installation"} <= m["names"], f"MCP lists {len(m['names'])} tools incl. inventory + rebuild")
    check(m["search_parts"][0]["results"][0]["part_id"] == "P-00001", "MCP inventory tool: left door hinges -> P-00001 with bins")
    blk = [b["text"] for b in m["get_job"][0]["blockers"]]
    check(any("J-00002" in b for b in blk) and any("rotor" in b.lower() for b in blk), f"MCP rebuild tool: J-00003 blockers {blk}")
    first, second = m["record_job_installation"]
    check(first["status"] == "installed" and second["duplicate_request"] is True, "MCP install recorded once (repeat = duplicate)")

    # ---- the packaged app server (headless) sees what Codex recorded
    proc = _popen([str(cli), "app", "--headless", "--local-only", "--print-url"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = proc.stdout.readline().strip()
    base, token = url.split("/?t=")
    token = urllib.parse.unquote(token)

    def desk(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Torino-Token": token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    job5 = desk("/api/job/J-00005")
    check(job5["parts_needed"][0]["status"] == "installed" and job5["activity"][0]["source"] == "codex",
          "desktop API shows the install Codex recorded (job + activity, source=codex)")
    part = desk("/api/part/P-00006")
    check(part["total_qty"] == 0 and part["installed_on"][0]["vehicle"].startswith("V-0001"), "stock deducted and vehicle history updated")
    lbl = desk("/api/labels/part", {"request_id": "verify-label-1", "part_id": "P-00001", "bin_code": "A-01"})
    check(lbl["label"]["status"] == "previewed", "label preview from the packaged app")

    # phone: pair over trusted HTTPS and scan the label we just rendered
    pair = desk("/api/pair", {})
    phone_base = pair["pair_url"].split("/pair")[0].replace(pair["pair_url"].split("//")[1].split(":")[0], "127.0.0.1")
    sslctx = ssl.create_default_context(cafile=str(home / "tls" / "torino-ca.crt"))
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=sslctx), urllib.request.HTTPCookieProcessor(jar))
    code = pair["pair_url"].split("c=")[1]
    opener.open(f"{phone_base}/pair?c={code}", timeout=30).read()
    seq = desk("/api/poll?after=0")["seq"]
    from PIL import Image
    img = Image.open(lbl["label"]["preview_path"]).convert("RGB")
    canvas = Image.new("RGB", (img.width + 200, img.height + 300), (80, 80, 80))
    canvas.paste(img, (100, 150))
    buf = io.BytesIO()
    canvas.rotate(6, fillcolor=(80, 80, 80)).resize((960, 960 * canvas.height // canvas.width)).save(buf, "JPEG", quality=75)
    req = urllib.request.Request(f"{phone_base}/p/api/frame?mode=lookup", data=buf.getvalue(),
                                 headers={"X-Torino": "1", "Content-Type": "image/jpeg"})
    fr = json.loads(opener.open(req, timeout=30).read())
    check(fr["decoded"] == "P-00001" and fr["result"]["found"], "phone camera frame decoded on the laptop: P-00001")
    evs = [e for e in desk(f"/api/poll?after={seq}")["events"] if e["type"] == "scan"]
    check(len(evs) == 1 and evs[0]["result"]["entries"], "scan appeared on the desktop with qty/condition/bins")
    jobs_before = [j["job_id"] for j in desk("/api/labels")["jobs"]]
    acts_before = len(desk("/api/activity?vehicle=V-0001&limit=500")["activity"])
    proc.terminate()
    proc.wait(30)

    # ---- restart: history and label queue persist
    proc = _popen([str(cli), "app", "--headless", "--local-only", "--print-url"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = proc.stdout.readline().strip()
    base, token = url.split("/?t=")
    token = urllib.parse.unquote(token)
    jobs_after = [j["job_id"] for j in desk("/api/labels")["jobs"]]
    acts_after = len(desk("/api/activity?vehicle=V-0001&limit=500")["activity"])
    check(jobs_after == jobs_before and acts_after == acts_before and acts_after > 0,
          f"after restart: {len(jobs_after)} label jobs and {acts_after} activity entries still there")
    proc.terminate()
    proc.wait(30)

    # ---- v0.3: Local mode (SQLite), MCP -> desktop consistency, retries, offline label queue, backup/restore
    tpl = home / "tpl" / "torino-24mm.lbx"
    r = run("printer", "make-template", "--tape", "24", "--out", str(tpl))
    check(r.returncode == 0 and tpl.is_file(), "packaged CLI wrote a 24 mm label template")
    cfg = json.loads((home / "config.json").read_text())
    cfg["backend"] = "local"
    cfg["printer"].update(mode="live", template=str(tpl), tape_mm=24, queue="Brother PT-P700")
    (home / "config.json").write_text(json.dumps(cfg))

    async def local_session():
        params = StdioServerParameters(command=str(cli), args=["serve", "--source", "codex"], env=env)
        async with Client(params, read_timeout_seconds=90) as c:
            async def call(_t, **a):
                res = await c.call_tool(_t, a)
                if res.is_error:
                    return {"__error__": res.content[0].text}
                sc = res.structured_content
                return sc if sc is not None else json.loads(res.content[0].text)
            o = {"info": await call("inventory_info")}
            v = (await call("add_vehicle", request_id="lv", year=1970, make="Ford", model="Torino"))["vehicle_id"]
            await call("set_active_vehicle", vehicle_id=v)
            await call("create_bin", request_id="lb", bin_code="A-01")
            o["part"] = await call("receive_stock", request_id="lr", bin_code="A-01", qty=2, condition="used",
                                   new_part={"name": "Door hinge, upper", "side_position": "left"}, print_label=True)
            o["label2"] = await call("print_label", request_id="lp", part_id=o["part"]["part"]["part_id"], bin_code="A-01")
            o["label2b"] = await call("print_label", request_id="lp", part_id=o["part"]["part"]["part_id"], bin_code="A-01")
            o["note"] = await call("add_note", request_id="ln", body="Hinge pins worn; bushings ordered", tags="body")
            o["e1"] = await call("record_expense", request_id="le", amount=42.5, vendor="Ford parts", description="Hinge bushings")
            o["e1b"] = await call("record_expense", request_id="le", amount=42.5, vendor="Ford parts", description="Hinge bushings")
            o["e2"] = await call("record_expense", request_id="le-again", amount=42.5, vendor="Ford parts", description="Hinge bushings")
            o["money"] = await call("money_summary")
            o["preview"] = await call("preview_label", part_id=o["part"]["part"]["part_id"], bin_code="A-01")
            o["labels_after"] = await call("label_status")
            return o

    lo = asyncio.run(local_session())
    check(lo["info"]["mode"] == "Local", "packaged MCP server uses the Local database after the switch")
    lab = lo["label2"]["label"]
    check(lab["status"] == "pending" and "offline" in (lab.get("last_error") or ""),
          f"live mode, no PT-P700 queue: real b-PAC reports offline -> label kept pending ({lab['status']})")
    check(lo["label2b"]["label"]["job_id"] == lab["job_id"] and lo["label2b"]["label"].get("duplicate_request"),
          "repeating the label request_id returns the same label job (no second label)")
    check(lo["e1b"].get("duplicate_request") and "already recorded" in lo["e2"].get("__error__", ""),
          "repeated expense request_id is a duplicate; a re-entered identical payment is refused")
    check(lo["money"]["spent"] == 42.5 and len(lo["money"]["expenses"]) == 1, "money readback: $42.50 spent, one expense")
    pv = lo["preview"]
    from PIL import Image
    check(pv.get("printed") is False and Path(pv["preview_path"]).is_file() and Image.open(pv["preview_path"]).height == 170 * 3
          and len(lo["labels_after"]["recent_jobs"]) == 2,
          f"MCP preview_label: 24 mm PNG rendered ({pv['lines']['name']} / {pv['lines']['bin']}), nothing queued or printed")

    proc = _popen([str(cli), "app", "--headless", "--local-only", "--print-url"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = proc.stdout.readline().strip()
    base, token = url.split("/?t=")
    token = urllib.parse.unquote(token)
    st = desk("/api/status")
    nb = desk("/api/notebook")
    mon = desk("/api/money")
    labs = [j for j in desk("/api/labels")["jobs"] if j["part_id"] == lo["part"]["part"]["part_id"]]
    check(st["mode"] == "Local" and nb["count"] == 1 and nb["notes"][0]["note_id"] == lo["note"]["note_id"]
          and mon["spent"] == 42.5 and len(mon["expenses"]) == 1 and lab["job_id"] in [j["job_id"] for j in labs]
          and len(labs) == 2, "desktop app shows the same note, spending and label jobs Codex created")
    # shopping -> order -> payment -> partial delivery, through the desktop API of the packaged app
    vid = st["active_vehicle"]["vehicle_id"]
    it = desk("/api/shopping", {"request_id": "vs-1", "vehicle_id": vid, "name": "Hinge pin kit", "qty": 2,
                                "est_price": "18.50", "vendor": "NPD", "status": "ready"})["item"]
    o = desk(f"/api/shopping/{it['item_id']}", {"request_id": "vs-2", "status": "ordered", "order_ref": "NPD-1",
                                                "paid_amount": "39.80"})
    o2 = desk(f"/api/shopping/{it['item_id']}", {"request_id": "vs-2", "status": "ordered", "order_ref": "NPD-1",
                                                 "paid_amount": "39.80"})
    rc = desk(f"/api/shopping/{it['item_id']}/receive", {"request_id": "vs-3", "bin_code": "A-01", "qty": 1,
                                                         "print_label": False})
    mon2 = desk(f"/api/money?vehicle={vid}")
    check(o["expense_id"] and o2.get("duplicate_request") and rc["status"] == "partially_received"
          and mon2["spent"] == round(42.5 + 39.8, 2) and len([e for e in mon2["expenses"] if e.get("item_id")]) == 1
          and mon2["outstanding_orders_total"] == 0 and mon2["timeline"][-1]["cumulative"] == mon2["spent"],
          f"shopping -> paid order -> partial delivery: one expense, spent ${mon2['spent']}, graph total matches")
    # the desktop pages themselves, rendered by a real browser engine (headless Chrome) against the packaged app
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if chrome.is_file():
        desk("/api/notebook", {"request_id": "vs-4", "body": "Pins measured worn 0.004 in"})
        expect = {"progress": ["Jobs done", "Checklist steps"], "shopping": ["Ready to buy", "Hinge pin kit"],
                  "money": ["Spending over time", "Spending by category", "Not in the line", "<svg"],
                  "notebook": ["Pins measured worn", "New note"]}
        for page, needles in expect.items():
            dom = subprocess.run([str(chrome), "--headless=new", "--disable-gpu", "--virtual-time-budget=8000",
                                  f"--user-data-dir={home / 'chrome'}", "--dump-dom",
                                  f"{base}/?t={urllib.parse.quote(token)}#/{page}"],
                                 capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace").stdout
            ok = all(n in dom for n in needles) and "Couldn't load this page" not in dom
            check(ok, f"desktop page '{page}' renders ({', '.join(n for n in needles if n != '<svg')})"
                      + ("" if ok else f" MISSING {[n for n in needles if n not in dom]}"))
    else:
        print("     (Chrome not found: page rendering checks skipped)")
    rd = desk("/api/setup")
    check(rd["printer"]["queue_exists"] is False and rd["printer"]["next"],
          f"printer readiness: {rd['printer']['text']} -> next: {rd['printer']['next']['label']}")
    notes_at_backup = desk("/api/notebook")["count"]
    bk = desk("/api/data/backup", {})
    desk("/api/notebook", {"request_id": "after-backup", "body": "written after the backup"})
    rs = desk("/api/data/restore", {"path": bk["path"], "confirm": True})
    check(rs["restored"] and desk("/api/notebook")["count"] == notes_at_backup and Path(rs["safety_backup"]).is_file(),
          f"backup ({bk['records']} records) and restore in the packaged app; previous data saved first")
    proc.terminate()
    proc.wait(30)
    proc = _popen([str(cli), "app", "--headless", "--local-only", "--print-url"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = proc.stdout.readline().strip()
    base, token = url.split("/?t=")
    token = urllib.parse.unquote(token)
    again = {j["job_id"]: j["status"] for j in desk("/api/labels")["jobs"]}
    check(again.get(lab["job_id"]) == "pending", "after restart the offline label is still queued (pending), not lost")
    proc.terminate()
    proc.wait(30)

    # ---- the double-clickable window
    if sys.platform == "win32":
        import win32con
        import win32gui
        import win32process
        g = _popen([str(gui), "--local-only"], env=env)

        def own_window() -> int:
            # only a window of the process we started: never touch a Torino the user has open
            found: list[int] = []

            def cb(h, _):
                if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == "Torino Garage" \
                        and win32process.GetWindowThreadProcessId(h)[1] == g.pid:
                    found.append(h)
                return True
            win32gui.EnumWindows(cb, None)
            return found[0] if found else 0

        hwnd = 0
        for _ in range(80):
            hwnd = own_window()
            if hwnd:
                break
            time.sleep(0.25)
        check(bool(hwnd), "Torino.exe opened its window")
        if hwnd:
            time.sleep(4)
            try:
                from PIL import ImageGrab
                win32gui.SetForegroundWindow(hwnd)
                time.sleep(1)
                ImageGrab.grab(bbox=win32gui.GetWindowRect(hwnd), all_screens=True).save(home / "window.png")
                print(f"     screenshot: {home / 'window.png'}")
            except Exception as e:  # noqa: BLE001
                print("     (screenshot skipped:", e, ")")
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        try:
            g.wait(30)
            check(g.returncode == 0, "Torino.exe exited cleanly when the window was closed")
        except subprocess.TimeoutExpired:
            g.kill()
            check(False, "Torino.exe exited cleanly when the window was closed")

    ok = all(o for o, _ in RESULTS)
    print(f"\n{sum(o for o, _ in RESULTS)}/{len(RESULTS)} release checks passed (scratch data: {home})")
    return 0 if ok else 1


if __name__ == "__main__":
    import urllib.parse  # noqa: F401
    sys.exit(main(sys.argv[1]))
