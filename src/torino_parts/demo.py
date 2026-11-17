"""End-to-end demo on DEMO data (no Google account, no printer needed).

Resets the local demo workbook, seeds the sample garage (1970 Torino rebuild, 1987 Monte Carlo,
donor Montego) and walks through inventory + rebuild operations with checks.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

from .app import build_inventory, set_active_vehicle
from .config import data_dir, load_config, save_config
from .inventory import SOURCE
from .labels.printers import PreviewBackend
from .sample_data import _photo, seed_sample_data


class _Report:
    def __init__(self):
        self.lines: list[str] = []
        self.checks: list[tuple[bool, str]] = []

    def h(self, text: str) -> None:
        self.say(f"\n## {text}")

    def say(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def check(self, ok: bool, text: str) -> None:
        self.checks.append((ok, text))
        self.say(f"[{'OK' if ok else 'FAIL'}] {text}")


def reset_demo_data(cfg: dict) -> Path:
    ddir = data_dir(cfg)
    for name in ("workbook.json", "workbook.lock"):
        (ddir / name).unlink(missing_ok=True)
    db = ddir / "labels.db"
    if db.exists():
        import sqlite3
        with sqlite3.connect(db) as c:
            c.execute("DELETE FROM jobs")
    for name in ("photos", "label-previews", "sample-photos"):
        shutil.rmtree(ddir / name, ignore_errors=True)
    return ddir


def run_demo(out_dir: Path | None = None) -> bool:
    SOURCE.set("demo")
    cfg = copy.deepcopy(load_config())
    cfg["backend"] = "demo"
    cfg["printer"] = {**cfg["printer"], "mode": "preview", "tape_mm": 24}  # the demo never prints physically
    reset_demo_data(cfg)
    out_dir = Path(out_dir or Path.cwd() / "demo-output")
    out_dir.mkdir(parents=True, exist_ok=True)
    g = build_inventory(cfg, backend=PreviewBackend(cfg["printer"]))
    g.labels.startup()
    r = _Report()
    r.say("# Torino garage demo (DEMO data)")
    r.say(f"Data: {g.store.describe()} | labels: preview, 24 mm TZe")

    ids = seed_sample_data(g)
    T = ids["torino"]
    real_cfg = load_config()
    real_cfg.setdefault("ui", {}).setdefault("active_vehicle", {})["demo"] = T
    save_config(real_cfg)
    r.h("1. Vehicles (different makes) and the sample Torino rebuild")
    for v in g.list_vehicles()["vehicles"]:
        r.say(f"  {v['label']}")
    H = ids["hinge_lh"]
    p = g.get_part(H)
    r.check(len(p["fitment"]) == 3 and len({s["bin_code"] for s in p["stock"]}) == 3
            and {"new", "used"} <= set(p["total_by_condition"]),
            f"{H} has 3 fitment entries and stock split across 3 bins (new and used kept separate)")

    r.h('2. "Where are the left door hinges?"')
    res = g.search_parts("Where are the left door hinges?")
    for x in res["results"]:
        r.say(f"  {x['part_id']} {x['label_name']}: " + "; ".join(f"{s['bin_code']} {s['qty']} {s['condition']}" for s in x["stock"]))
    r.check([x["part_id"] for x in res["results"]] == [H], "only the left hinge is returned")

    r.h("3. What can I do next on the Torino? (and in 30 minutes)")
    n = g.what_next(T, minutes=30)
    for j in n["do_next"]:
        r.say(f"  NEXT  {j['job_id']} {j['title']} [{j['state']}] next step: {j.get('next_step')}")
        for pl in j.get("pull_list", []):
            r.say(f"        pull {pl['take']} x {pl['name']} from {', '.join(pl['bins'])}")
    for j in n["blocked"]:
        r.say(f"  BLOCKED {j['job_id']} {j['title']}: " + " | ".join(f"{b['text']} -> {b['clear']}" for b in j["blockers"]))
    r.say(f"  Fits in 30 min: {[j['job_id'] for j in n['fits_in_time']]}")
    r.check(any(j["job_id"] == ids["job_rotors"] for j in n["blocked"]), "rotor job is blocked by the caliper job + missing rotors")

    r.h('4. Photo intake: "Add this to B-04 and label it." (search first, then one call)')
    photo = _photo(out_dir / "demo-photo-window-crank.jpg", "crank")
    found = g.search_parts("window crank handle")
    crank = found["results"][0]["part_id"]
    r.say(f"  identified: window crank handle -> existing record {crank}; storing 1 more in B-04")
    args = dict(request_id="demo-intake-b04-crank", bin_code="B-04", qty=1, condition="used", part_id=crank,
                photo_path=str(photo), print_label=True)
    first = g.receive_stock(**args)
    second = g.receive_stock(**args)
    snap = g.store.read()
    receipts = [t for t in snap["Transactions"] if t["request_id"] == args["request_id"] and t["type"] == "receive"]
    jobs = [j for j in g.labels.queue.list(limit=500) if j["request_id"] == args["request_id"]]
    r.say(f"  label: {first['label']['status']} -> {first['label']['preview_path']}")
    r.check(second["duplicate_request"] and len(receipts) == 1 and len(jobs) == 1 and jobs[0]["attempts"] == 1,
            "repeating the intake left ONE stock receipt and ONE label job")

    r.h("5. Move, inspect, install for a job, reprint")
    mv = g.move_stock("demo-move-hinge", H, "A-02", "B-04", 1)
    r.say(f"  {mv['message']}")
    door = g.get_job(ids["job_door"])
    hinge_need = next(x for x in door["parts_needed"] if x["part_id"] == H)
    g.update_job("demo-door-finish-teardown", ids["job_teardown"], status="done")
    g.update_job_part("demo-hinge-inspected", hinge_need["need_id"], check_status="passed")
    inst = g.record_job_installation("demo-install-hinge", ids["job_door"], need_id=hinge_need["need_id"], bin_code="A-01")
    again = g.record_job_installation("demo-install-hinge", ids["job_door"], need_id=hinge_need["need_id"], bin_code="A-01")
    r.say(f"  {inst['message']} (left in A-01: {inst['qty_left_in_bin']})")
    r.check(again["duplicate_request"] and inst["job"]["parts_needed"][0]["qty_installed"] == 1
            and g.get_job(ids["job_door"])["status"] == "in_progress", "installation updated stock, vehicle history and job once")
    before = g.get_part(H)["total_qty"]
    rp = g.print_label("demo-reprint-hinge", H, "B-04")
    r.check(g.get_part(H)["total_qty"] == before, f"reprint {rp['label']['job_id']} did not change inventory")

    r.h("6. Recent activity on the Torino")
    for a in g.activity_log(T, limit=8)["activity"]:
        r.say(f"  {a['timestamp'][:16]} [{a.get('source', '')}] {a['summary']}")

    for j in g.labels.jobs(limit=50):
        if j["preview_path"] and Path(j["preview_path"]).is_file():
            shutil.copyfile(j["preview_path"], out_dir / Path(j["preview_path"]).name)
    ok = all(c for c, _ in r.checks)
    r.say(f"\n{sum(c for c, _ in r.checks)}/{len(r.checks)} checks passed. Report and label previews: {out_dir}")
    (out_dir / "demo-report.md").write_text("\n".join(r.lines), encoding="utf-8")
    g.labels.queue.close()
    return ok
