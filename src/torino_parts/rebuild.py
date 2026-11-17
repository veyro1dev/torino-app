"""Rebuild workflow on top of the inventory: jobs, checklists, dependencies, part needs,
teardown removals, job installations, photos, specs and the activity log.

Everything is stored in the same workbook (tabs Jobs, JobSteps, JobDeps, JobParts, Photos,
Specs, Activity) and goes through ``Inventory._mutate``, so every change is atomic and
idempotent per request_id - exactly like stock changes.
"""

from __future__ import annotations

import re
from typing import Any

from .inventory import (
    SOURCE, Inventory, InventoryError, _Ctx, _int, _s, custody_view, label_name, norm_bin, norm_condition,
)
from .schema import (
    CHECK_STATUSES, CHECKS, JOB_KINDS, JOB_STATUSES, SCOPE_UNIVERSAL, SYSTEMS, USABLE_CONDITIONS,
    UNVERIFIED, VERIFIED,
)
from .projects import ProjectsMixin
from .store.base import Snapshot

OPEN =("planned", "in_progress", "on_hold")
STATE_ORDER = {"in_progress": 0, "ready": 1, "ready_with_checks": 2, "blocked": 3, "on_hold": 4,
               "done": 5, "cancelled": 6}


def _norm_system(system: str | None) -> str:
    s = _s(system).lower()
    if not s:
        return "other"
    aliases = {"brake": "brakes", "electric": "electrical", "wiring": "electrical", "tires": "wheels & tires",
               "wheels": "wheels & tires", "suspension/steering": "suspension", "bodywork": "body",
               "paint": "body", "motor": "engine", "transmission": "drivetrain", "rear end": "drivetrain"}
    return aliases.get(s, s)


def _yes(v: Any) -> bool:
    return v is True or _s(v).lower() in ("yes", "true", "1", "y", "done", "x")


class Garage(ProjectsMixin, Inventory):
    # ==================================================================
    # lookups
    # ==================================================================
    def _job(self, snap: Snapshot, job_id: str) -> dict[str, Any]:
        j = self._find(snap, "Jobs", "job_id", job_id)
        if j is None:
            raise InventoryError(f"no job with id {job_id!r}; use list_jobs to find it")
        return j

    def _need(self, snap: Snapshot, need_id: str) -> dict[str, Any]:
        n = self._find(snap, "JobParts", "need_id", need_id)
        if n is None:
            raise InventoryError(f"no job part need {need_id!r}")
        return n

    def lookup_code(self, code: str) -> dict[str, Any]:
        """Resolve a scanned or typed code: part ID, bin code (or BIN:xx), job ID or vehicle ID."""
        raw = _s(code)
        if not raw:
            raise InventoryError("empty code")
        snap = self.store.read()
        c = raw.upper()
        if c.startswith("BIN:") or c.startswith("BIN "):
            c = c[4:].strip()
            return {"kind": "bin", "code": raw, "bin": self.list_bin(c)}
        p = self._find(snap, "Parts", "part_id", c)
        if p:
            return {"kind": "part", "code": raw, "part": self.part_view(p, snap, include_empty=True)}
        if self._find(snap, "Bins", "bin_code", c.replace(" ", "-")):
            return {"kind": "bin", "code": raw, "bin": self.list_bin(c)}
        j = self._find(snap, "Jobs", "job_id", c)
        if j:
            return {"kind": "job", "code": raw, "job": self._job_summary(snap, j)}
        v = self._find(snap, "Vehicles", "vehicle_id", c)
        if v:
            return {"kind": "vehicle", "code": raw, "vehicle": self._vehicle_view(v, snap)}
        # a part with no leading zeros typed by hand, e.g. "p6" or "P-6"
        m = re.fullmatch(r"P-?0*(\d+)", c)
        if m:
            p = self._find(snap, "Parts", "part_id", f"P-{int(m.group(1)):05d}")
            if p:
                return {"kind": "part", "code": raw, "part": self.part_view(p, snap, include_empty=True)}
        res = self.search_parts(raw, limit=10, include_out_of_stock=True, _snap=snap)
        if res["results"]:
            return {"kind": "search", "code": raw, "search": res}
        raise InventoryError(f"nothing found for {raw!r}", code=raw)

    # ==================================================================
    # job analysis
    # ==================================================================
    def _usable_stock(self, snap: Snapshot, part_id: str) -> list[dict[str, Any]]:
        rows = [s for s in snap["Stock"] if s["part_id"] == part_id and (s["qty"] or 0) > 0]
        return sorted(rows, key=lambda s: (s["condition"] not in USABLE_CONDITIONS, -(s["qty"] or 0)))

    def _demand(self, snap: Snapshot) -> dict[str, list[tuple[str, int]]]:
        """part_id -> [(job_id, remaining qty)] across all open jobs (any vehicle)."""
        open_jobs = {j["job_id"] for j in snap["Jobs"] if (j["status"] or "planned") in OPEN}
        out: dict[str, list[tuple[str, int]]] = {}
        for n in snap["JobParts"]:
            if n["job_id"] in open_jobs and n["part_id"]:
                rem = max(0, (n["qty_needed"] or 0) - (n["qty_installed"] or 0))
                if rem:
                    out.setdefault(n["part_id"], []).append((n["job_id"], rem))
        return out

    def _need_view(self, snap: Snapshot, job: dict, n: dict, vehicle: dict | None,
                   demand: dict[str, list[tuple[str, int]]]) -> dict[str, Any]:
        needed, installed = n["qty_needed"] or 0, n["qty_installed"] or 0
        remaining = max(0, needed - installed)
        v = {"need_id": n["need_id"], "part_id": n["part_id"] or None, "description": n["description"],
             "qty_needed": needed, "qty_installed": installed, "qty_remaining": remaining,
             "check": n["check"] or "none", "check_status": n["check_status"] or "pending"}
        if n["notes"]:
            v["notes"] = n["notes"]
        blockers, checks = [], []
        if not n["part_id"]:
            v["name"] = n["description"] or "(unnamed part)"
            v["on_hand"] = 0
            v["pull_from"] = []
            if remaining:
                v["status"] = "missing"
                blockers.append({"type": "part_unknown", "need_id": n["need_id"],
                                 "text": f"No part record for '{v['name']}' x{remaining}",
                                 "clear": f"Identify it in inventory or buy it, then link the part to need {n['need_id']}"})
            else:
                v["status"] = "installed"
            return {**v, "_blockers": blockers, "_checks": checks}
        part = self._find(snap, "Parts", "part_id", n["part_id"])
        if part is None:
            v.update(name=n["description"] or n["part_id"], status="missing", on_hand=0, pull_from=[])
            blockers.append({"type": "part_unknown", "need_id": n["need_id"],
                             "text": f"Linked part {n['part_id']} no longer exists", "clear": "Re-link this need"})
            return {**v, "_blockers": blockers, "_checks": checks}
        v["name"] = label_name(part)
        stock = self._usable_stock(snap, part["part_id"])
        usable = [s for s in stock if s["condition"] in USABLE_CONDITIONS]
        on_hand = sum(s["qty"] for s in usable)
        v["on_hand"] = on_hand
        v["pull_from"] = [{"bin_code": s["bin_code"], "qty": s["qty"], "condition": s["condition"],
                           "stock_id": s["stock_id"]} for s in usable]
        unusable = [s for s in stock if s["condition"] not in USABLE_CONDITIONS]
        if unusable:
            v["not_usable"] = [{"bin_code": s["bin_code"], "qty": s["qty"], "condition": s["condition"]} for s in unusable]
        # fitment for this job's vehicle
        fm = None
        if vehicle:
            fits = [f for f in snap["Fitment"] if f["part_id"] == part["part_id"]]
            fm = self._fitment_match(part, fits, vehicle.get("year"), vehicle.get("make", ""), vehicle.get("model", ""))
            if fm != VERIFIED and any(s["part_id"] == part["part_id"] and s["donor_vehicle_id"] == vehicle["vehicle_id"]
                                      for s in snap["Stock"]):
                fm = VERIFIED  # it came off this very vehicle
                v["fitment_note"] = "came off this vehicle"
            v["fitment"] = fm or "not_listed"
        others = [(jid, q) for jid, q in demand.get(part["part_id"], []) if jid != job["job_id"]]
        if others and sum(q for _, q in others) + remaining > on_hand:
            v["shared_with"] = [{"job_id": jid, "qty": q} for jid, q in others]
        status_check = v["check_status"]
        away = [c for c in snap.get("Custody", []) if c["part_id"] == part["part_id"]
                and (c["qty_out"] or 0) > (c["qty_returned"] or 0)]
        if away:
            v["away"] = [custody_view(c) for c in away]
        if remaining == 0:
            v["status"] = "installed"
        elif on_hand < remaining:
            short = remaining - on_hand
            away_qty = sum((c["qty_out"] or 0) - (c["qty_returned"] or 0) for c in away)
            from_away = min(short, away_qty)
            still_missing = short - from_away
            v["status"] = "away" if not still_missing else ("missing" if on_hand == 0 else "short")
            for c in away if from_away else []:
                cv = custody_view(c)
                back = f", expected back {cv['expected_back']}" if cv.get("expected_back") else ""
                late = " - OVERDUE" if cv.get("overdue") else ""
                blockers.append({"type": "away", "need_id": n["need_id"], "part_id": part["part_id"],
                                 "custody_id": c["custody_id"],
                                 "text": f"{cv['qty_away']} x {v['name']} ({part['part_id']}) is away at {c['holder']}"
                                         f"{' for ' + c['reason'] if c['reason'] else ''} since {str(c['sent_at'])[:10]}"
                                         f"{back}{late}",
                                 "clear": f"Get it back from {c['holder']}, then record the return into a bin ({c['custody_id']})"})
            if still_missing:
                extra = f" (only {', '.join(s['condition'] for s in unusable)} on hand)" if unusable and on_hand == 0 else ""
                blockers.append({"type": "part", "need_id": n["need_id"], "part_id": part["part_id"],
                                 "text": f"Missing {still_missing} x {v['name']} ({part['part_id']}){extra}",
                                 "clear": f"Buy or find {still_missing} more, then receive into a bin"})
        else:
            v["status"] = "ready"
        if remaining and status_check == "failed":
            blockers.append({"type": "failed_check", "need_id": n["need_id"], "part_id": part["part_id"],
                             "text": f"{v['name']} failed inspection/fitment check",
                             "clear": f"Replace or repair it, then mark need {n['need_id']} passed"})
        elif remaining and status_check != "passed":
            if (n["check"] or "none") == "inspect":
                checks.append({"type": "inspect", "need_id": n["need_id"], "part_id": part["part_id"],
                               "text": f"Inspect {v['name']} ({part['part_id']}) before installing",
                               "clear": f"Mark need {n['need_id']} passed or failed"})
            if (n["check"] or "none") == "verify_fitment" or fm in (UNVERIFIED, "unknown", None):
                why = {UNVERIFIED: "fitment is unverified", "unknown": "fitment is unknown",
                       None: "fitment does not list this vehicle"}.get(fm, "fitment check requested")
                checks.append({"type": "fitment", "need_id": n["need_id"], "part_id": part["part_id"],
                               "text": f"Verify {v['name']} ({part['part_id']}) fits: {why}",
                               "clear": f"Test-fit or check the manual, then mark need {n['need_id']} passed"})
            if v["status"] == "ready" and checks and any(c["need_id"] == n["need_id"] for c in checks):
                v["status"] = "needs_check"
        return {**v, "_blockers": blockers, "_checks": checks}

    @staticmethod
    def _shopping_for_need(snap: Snapshot, n: dict) -> list[dict[str, Any]]:
        return [it for it in snap.get("Shopping", []) if it["status"] in ("considering", "ready", "ordered")
                and (it["need_id"] == n["need_id"]
                     or (n["part_id"] and it["part_id"] == n["part_id"] and it["job_id"] in ("", n["job_id"])))]

    def _with_shopping(self, snap: Snapshot, n: dict, view: dict) -> dict:
        """A missing part that is already on the shopping list / on order says so, with the next action."""
        items = self._shopping_for_need(snap, n)
        if not items:
            return view
        view["shopping"] = [{"item_id": it["item_id"], "status": it["status"], "qty": it["qty"],
                             "qty_received": it["qty_received"] or 0, "vendor": it["vendor"]} for it in items]
        for b in view["_blockers"]:
            if b["type"] not in ("part", "part_unknown"):
                continue
            ordered = [it for it in items if it["status"] == "ordered"]
            if ordered:
                it = ordered[0]
                left = (it["qty"] or 0) - (it["qty_received"] or 0)
                b["text"] += f" - on order: {it['item_id']} ({left} to come{' from ' + it['vendor'] if it['vendor'] else ''})"
                b["clear"] = f"When it arrives, receive {it['item_id']} into a bin (Shopping)"
                b["type_detail"] = "on_order"
            else:
                it = items[0]
                b["text"] += f" - on the shopping list: {it['item_id']} ({it['status']})"
                b["clear"] = f"Order {it['item_id']}, then receive it into a bin (Shopping)"
                b["type_detail"] = "on_shopping_list"
        return view

    def _analyze(self, snap: Snapshot, job: dict, demand=None) -> dict[str, Any]:
        demand = demand if demand is not None else self._demand(snap)
        vehicle = self._find(snap, "Vehicles", "vehicle_id", job["vehicle_id"])
        status = job["status"] or "planned"
        needs = [self._with_shopping(snap, n, self._need_view(snap, job, n, vehicle, demand))
                 for n in snap["JobParts"] if n["job_id"] == job["job_id"]]
        blockers = [b for n in needs for b in n.pop("_blockers")]
        checks = [c for n in needs for c in n.pop("_checks")]
        for d in snap["JobDeps"]:
            if d["job_id"] != job["job_id"]:
                continue
            dep = self._find(snap, "Jobs", "job_id", d["depends_on"])
            if dep is None or (dep["status"] or "planned") == "done":
                continue
            if (dep["status"] or "planned") == "cancelled":
                continue
            blockers.insert(0, {"type": "job", "job_id": dep["job_id"],
                                "text": f"Waiting on {dep['job_id']} '{dep['title']}' ({dep['status'] or 'planned'})",
                                "clear": f"Finish {dep['job_id']} '{dep['title']}'"})
        if status == "on_hold":
            blockers.insert(0, {"type": "hold", "text": f"On hold: {job['hold_reason'] or 'no reason given'}",
                                "clear": "Resume the job when that is resolved"})
        if status in ("done", "cancelled"):
            state = status
        elif status == "on_hold":
            state = "on_hold"
        elif blockers:
            state = "blocked"
        elif status == "in_progress":
            state = "in_progress"
        elif checks:
            state = "ready_with_checks"
        else:
            state = "ready"
        steps = sorted([s for s in snap["JobSteps"] if s["job_id"] == job["job_id"]], key=lambda s: (s["seq"] or 0, s["step_id"]))
        done = sum(1 for s in steps if _yes(s["done"]))
        est = job["estimate_min"]
        remaining = None
        if est:
            remaining = round(est * (len(steps) - done) / len(steps)) if steps else est
            if status in ("done", "cancelled"):
                remaining = 0
        return {"state": state, "blockers": blockers, "checks": checks, "needs": needs, "steps": steps,
                "steps_done": done, "remaining_min": remaining, "vehicle": vehicle}

    def _job_summary(self, snap: Snapshot, job: dict, demand=None, analysis=None) -> dict[str, Any]:
        a = analysis or self._analyze(snap, job, demand)
        out = {"job_id": job["job_id"], "vehicle_id": job["vehicle_id"], "title": job["title"],
               "system": job["system"], "kind": job["kind"] or "assembly", "status": job["status"] or "planned",
               "state": a["state"], "priority": job["priority"] or 3, "estimate_min": job["estimate_min"],
               "remaining_min": a["remaining_min"], "steps": f"{a['steps_done']}/{len(a['steps'])}",
               "blockers": [b["text"] for b in a["blockers"]], "checks": [c["text"] for c in a["checks"]],
               "parts": {"needed": len(a["needs"]),
                         "missing": sum(1 for n in a["needs"] if n["status"] in ("missing", "short")),
                         "away": sum(1 for n in a["needs"] if n["status"] == "away"),
                         "to_check": sum(1 for n in a["needs"] if n["status"] == "needs_check"),
                         "ready": sum(1 for n in a["needs"] if n["status"] == "ready"),
                         "installed": sum(1 for n in a["needs"] if n["status"] == "installed")}}
        if job["goal"]:
            out["goal"] = job["goal"]
        return out

    # ==================================================================
    # queries
    # ==================================================================
    def list_jobs(self, vehicle_id: str | None = None, status: str | None = None, system: str | None = None,
                  include_closed: bool = False) -> dict[str, Any]:
        snap = self.store.read()
        demand = self._demand(snap)
        jobs = snap["Jobs"]
        if vehicle_id:
            v = self._vehicle(snap, vehicle_id)
            jobs = [j for j in jobs if j["vehicle_id"] == v["vehicle_id"]]
        if status:
            jobs = [j for j in jobs if (j["status"] or "planned") == status]
        elif not include_closed:
            jobs = [j for j in jobs if (j["status"] or "planned") in OPEN]
        if system:
            jobs = [j for j in jobs if j["system"] == _norm_system(system)]
        out = [self._job_summary(snap, j, demand) for j in jobs]
        out.sort(key=lambda j: (STATE_ORDER[j["state"]], j["priority"], j["job_id"]))
        return {"count": len(out), "jobs": out}

    def get_job(self, job_id: str, activity_limit: int = 25) -> dict[str, Any]:
        snap = self.store.read()
        job = self._job(snap, job_id)
        a = self._analyze(snap, job)
        out = self._job_summary(snap, job, analysis=a)
        out["vehicle"] = self.vehicle_label(a["vehicle"])
        out["notes"] = job["notes"]
        out["hold_reason"] = job["hold_reason"] or None
        out["timestamps"] = {k: job[k] for k in ("created_at", "updated_at", "started_at", "completed_at") if job[k]}
        out["checklist"] = [{"step_id": s["step_id"], "text": s["text"], "done": _yes(s["done"]),
                             **({"done_at": s["done_at"]} if s["done_at"] else {})} for s in a["steps"]]
        out["depends_on"] = []
        for d in snap["JobDeps"]:
            if d["job_id"] == job["job_id"] and d["depends_on"]:
                dep = self._find(snap, "Jobs", "job_id", d["depends_on"]) or {}
                out["depends_on"].append({"job_id": d["depends_on"], "title": dep.get("title"),
                                          "status": dep.get("status") or "planned", "dep_id": d["dep_id"]})
        out["unblocks"] = [{"job_id": d["job_id"], "title": (self._find(snap, "Jobs", "job_id", d["job_id"]) or {}).get("title")}
                           for d in snap["JobDeps"] if d["depends_on"] == job["job_id"]]
        out["blockers"] = a["blockers"]
        out["checks"] = a["checks"]
        out["parts_needed"] = a["needs"]
        out["pull_list"] = [{"need_id": n["need_id"], "part_id": n["part_id"], "name": n["name"],
                             "take": min(n["qty_remaining"], n["on_hand"]), "from": n["pull_from"]}
                            for n in a["needs"] if n["qty_remaining"] and n["on_hand"]]
        out["installed"] = [{"install_id": i["install_id"], "part_id": i["part_id"], "qty": i["qty"],
                             "from_bin": i["bin_code"], "installed_at": i["installed_at"]}
                            for i in snap["Installations"] if i["job_id"] == job["job_id"]]
        out["photos"] = self._photos(snap, job_id=job["job_id"])
        out["specs"] = [self._spec_view(sp) for sp in snap["Specs"] if sp["job_id"] == job["job_id"]]
        acts = [x for x in snap["Activity"] if x["job_id"] == job["job_id"]]
        out["activity"] = [self._act_view(x) for x in acts[-activity_limit:][::-1]]
        nxt = next((s for s in out["checklist"] if not s["done"]), None)
        out["where_you_left_off"] = {
            "last_activity": out["activity"][0] if out["activity"] else None,
            "next_step": nxt,
        }
        return out

    def vehicle_overview(self, vehicle_id: str) -> dict[str, Any]:
        snap = self.store.read()
        v = self._vehicle(snap, vehicle_id)
        demand = self._demand(snap)
        jobs = [self._job_summary(snap, j, demand) for j in snap["Jobs"] if j["vehicle_id"] == v["vehicle_id"]]
        by_state: dict[str, int] = {}
        for j in jobs:
            by_state[j["state"]] = by_state.get(j["state"], 0) + 1
        out = self._vehicle_view(v, snap)
        out["jobs_by_state"] = by_state
        out["next"] = self.what_next(v["vehicle_id"], _snap=snap)
        out["recent_activity"] = [self._act_view(a) for a in [x for x in snap["Activity"] if x["vehicle_id"] == v["vehicle_id"]][-15:][::-1]]
        out["installed_parts"] = len([i for i in snap["Installations"] if i["vehicle_id"] == v["vehicle_id"]])
        systems: dict[str, dict[str, int]] = {}
        for j in jobs:
            s = systems.setdefault(j["system"] or "other", {"open": 0, "done": 0})
            s["done" if j["state"] == "done" else "open"] += 0 if j["state"] == "cancelled" else 1
        out["systems"] = systems
        return out

    def what_next(self, vehicle_id: str, minutes: int | None = None, _snap: Snapshot | None = None) -> dict[str, Any]:
        """Ready jobs (respecting dependencies and parts on hand), blockers with what clears them,
        and - when minutes is given - what fits in that time using the job estimates.

        With ``minutes``: ``fits_in_time`` = ready jobs that fit; ``do_next`` = the other ready jobs.
        Every job is listed once."""
        snap = _snap or self.store.read()
        v = self._vehicle(snap, vehicle_id)
        demand = self._demand(snap)
        jobs = [j for j in snap["Jobs"] if j["vehicle_id"] == v["vehicle_id"] and (j["status"] or "planned") in OPEN]
        analyses = {j["job_id"]: self._analyze(snap, j, demand) for j in jobs}
        summaries = {j["job_id"]: self._job_summary(snap, j, analysis=analyses[j["job_id"]]) for j in jobs}
        # how many blocked jobs each job would (help) unblock
        unblocks: dict[str, list[str]] = {}
        for jid, a in analyses.items():
            for b in a["blockers"]:
                if b["type"] == "job":
                    unblocks.setdefault(b["job_id"], []).append(jid)
        for jid, s in summaries.items():
            if unblocks.get(jid):
                s["unblocks"] = unblocks[jid]
        workable = [s for s in summaries.values() if s["state"] in ("in_progress", "ready", "ready_with_checks")]
        workable.sort(key=lambda s: (s["state"] != "in_progress", s["state"] == "ready_with_checks", s["priority"],
                                     -len(s.get("unblocks", [])), s["remaining_min"] if s["remaining_min"] is not None else 10**6,
                                     s["job_id"]))
        blocked = []
        for jid, a in analyses.items():
            if a["state"] in ("blocked", "on_hold"):
                blocked.append({**summaries[jid], "blockers": a["blockers"]})
        blocked.sort(key=lambda s: (s["priority"], s["job_id"]))
        out: dict[str, Any] = {"vehicle": self.vehicle_label(v), "vehicle_id": v["vehicle_id"],
                               "do_next": workable, "blocked": blocked}
        for w in workable:
            a = analyses[w["job_id"]]
            w["pull_list"] = [{"part_id": n["part_id"], "name": n["name"], "take": min(n["qty_remaining"], n["on_hand"]),
                               "bins": [f"{p['bin_code']} ({p['qty']} {p['condition']})" for p in n["pull_from"]]}
                              for n in a["needs"] if n["qty_remaining"] and n["on_hand"]]
            nxt = next((s for s in a["steps"] if not _yes(s["done"])), None)
            if nxt:
                w["next_step"] = nxt["text"]
        if minutes is not None:
            m = _int(minutes, "minutes", 1)
            fits = [w for w in workable if w["remaining_min"] is not None and w["remaining_min"] <= m]
            out["time_available_min"] = m
            out["fits_in_time"] = fits
            # each job appears once: do_next now holds only the ready jobs that do NOT fit (or have no estimate)
            fit_ids = {w["job_id"] for w in fits}
            out["do_next"] = [w for w in workable if w["job_id"] not in fit_ids]
            out["no_estimate"] = [w["job_id"] for w in workable if w["remaining_min"] is None]
            quick = [c for w in workable for c in analyses[w["job_id"]]["checks"]]
            if quick:
                out["quick_checks"] = [c["text"] for c in quick]
            if not fits:
                out["note"] = (f"No ready job has an estimate of {m} min or less. "
                               "Add estimate_min to jobs, or pick a checklist step from a ready job.")
        # clear-the-blocker suggestions: which single actions unblock the most work
        clears: dict[str, set[str]] = {}
        for b in blocked:
            for x in b["blockers"]:
                clears.setdefault(x["clear"], set()).add(b["job_id"])
        out["unblock_actions"] = [{"action": k, "unblocks": sorted(v_)} for k, v_ in
                                  sorted(clears.items(), key=lambda kv: -len(kv[1]))][:10]
        return out

    def activity_log(self, vehicle_id: str | None = None, job_id: str | None = None, part_id: str | None = None,
                     limit: int = 30) -> dict[str, Any]:
        snap = self.store.read()
        rows = snap["Activity"]
        if vehicle_id:
            rows = [r for r in rows if r["vehicle_id"].upper() == vehicle_id.upper()]
        if job_id:
            rows = [r for r in rows if r["job_id"].upper() == job_id.upper()]
        if part_id:
            rows = [r for r in rows if r["part_id"].upper() == part_id.upper()]
        return {"activity": [self._act_view(r) for r in rows[-limit:][::-1]]}

    @staticmethod
    def _act_view(a: dict) -> dict[str, Any]:
        return {k: a[k] for k in ("act_id", "timestamp", "type", "summary", "vehicle_id", "job_id", "part_id", "source")
                if a.get(k)}

    @staticmethod
    def _spec_view(sp: dict) -> dict[str, Any]:
        return {k: sp[k] for k in ("spec_id", "item", "value", "unit", "source", "verified", "notes", "part_id", "job_id",
                                   "vehicle_id") if sp.get(k) not in ("", None)}

    def _photos(self, snap: Snapshot, vehicle_id=None, job_id=None, part_id=None) -> list[dict[str, Any]]:
        out = []
        for ph in snap["Photos"]:
            if vehicle_id and ph["vehicle_id"] != vehicle_id:
                continue
            if job_id and ph["job_id"] != job_id:
                continue
            if part_id and ph["part_id"] != part_id:
                continue
            out.append({k: ph[k] for k in ("photo_id", "link", "vehicle_id", "job_id", "part_id", "caption", "stage",
                                           "source", "taken_at") if ph.get(k)})
        return out[::-1]

    def list_photos(self, vehicle_id: str | None = None, job_id: str | None = None, part_id: str | None = None,
                    limit: int = 60) -> dict[str, Any]:
        snap = self.store.read()
        return {"photos": self._photos(snap, vehicle_id and vehicle_id.upper(), job_id and job_id.upper(),
                                       part_id and part_id.upper())[:limit]}

    def list_specs(self, vehicle_id: str | None = None, job_id: str | None = None, part_id: str | None = None,
                   query: str = "") -> dict[str, Any]:
        snap = self.store.read()
        rows = snap["Specs"]
        if vehicle_id:
            rows = [r for r in rows if r["vehicle_id"] == vehicle_id.upper()]
        if job_id:
            rows = [r for r in rows if r["job_id"] == job_id.upper()]
        if part_id:
            rows = [r for r in rows if r["part_id"] == part_id.upper()]
        if query:
            q = query.lower()
            rows = [r for r in rows if q in f"{r['item']} {r['notes']}".lower()]
        return {"specs": [self._spec_view(r) for r in rows]}

    # ==================================================================
    # job mutations
    # ==================================================================
    def _validate_need(self, snap: Snapshot, item: dict[str, Any]) -> dict[str, Any]:
        pid = _s(item.get("part_id")).upper()
        desc = _s(item.get("description"))
        if not pid and not desc:
            raise InventoryError("each part need needs a part_id or a description")
        if pid:
            self._part(snap, pid)
        check = _s(item.get("check")).lower() or "none"
        if check not in CHECKS:
            raise InventoryError(f"check must be one of {CHECKS}")
        return {"part_id": pid, "description": desc, "qty_needed": _int(item.get("qty", item.get("qty_needed", 1)), "qty", 1),
                "check": check, "notes": _s(item.get("notes"))}

    def _add_need(self, ctx: _Ctx, job: dict, nd: dict[str, Any]) -> str:
        nid = ctx.next_id("JobParts", "need_id", "N", 5)
        ctx.append("JobParts", {"need_id": nid, "job_id": job["job_id"], "part_id": nd["part_id"],
                                "description": nd["description"], "qty_needed": nd["qty_needed"], "qty_installed": 0,
                                "check": nd["check"], "check_status": "pending" if nd["check"] != "none" else "",
                                "notes": nd["notes"], "created_at": ctx.now, "updated_at": ctx.now})
        return nid

    def _add_steps(self, ctx: _Ctx, job_id: str, steps: list[str]) -> list[str]:
        existing = [s["seq"] or 0 for s in ctx.snap["JobSteps"] if s["job_id"] == job_id]
        seq = max(existing, default=0)
        ids = []
        for text in steps:
            t = _s(text)
            if not t:
                continue
            seq += 1
            sid = ctx.next_id("JobSteps", "step_id", "ST", 5)
            ctx.append("JobSteps", {"step_id": sid, "job_id": job_id, "seq": seq, "text": t, "done": "", "done_at": ""})
            ids.append(sid)
        return ids

    def _check_cycle(self, snap: Snapshot, job_id: str, depends_on: str, pending: list[tuple[str, str]] = ()) -> None:
        edges: dict[str, set[str]] = {}
        for d in snap["JobDeps"]:
            edges.setdefault(d["job_id"], set()).add(d["depends_on"])
        for a, b in pending:
            edges.setdefault(a, set()).add(b)
        stack, seen = [depends_on], set()
        while stack:
            cur = stack.pop()
            if cur == job_id:
                raise InventoryError(f"{job_id} depending on {depends_on} would create a circular dependency")
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(edges.get(cur, ()))

    def create_job(self, request_id: str, vehicle_id: str, title: str, system: str = "other", goal: str = "",
                   kind: str = "assembly", priority: int = 3, estimate_min: int | None = None,
                   steps: list[str] | None = None, depends_on: list[str] | None = None,
                   parts: list[dict[str, Any]] | None = None, notes: str = "", status: str = "planned") -> dict[str, Any]:
        if not _s(title):
            raise InventoryError("title is required")
        kind = _s(kind).lower() or "assembly"
        if kind not in JOB_KINDS:
            raise InventoryError(f"kind must be one of {JOB_KINDS}")
        status = _s(status).lower() or "planned"
        if status not in ("planned", "in_progress", "on_hold"):
            raise InventoryError("new jobs start as planned, in_progress or on_hold")
        pr = _int(priority, "priority", 1)
        if pr > 5:
            raise InventoryError("priority is 1 (highest) to 5 (lowest)")
        est = _int(estimate_min, "estimate_min", 1) if estimate_min not in (None, "") else None
        params = {"vehicle_id": _s(vehicle_id).upper(), "title": _s(title), "system": _norm_system(system),
                  "goal": _s(goal), "kind": kind, "priority": pr, "estimate_min": est, "steps": [_s(x) for x in steps or []],
                  "depends_on": [_s(x).upper() for x in depends_on or []], "parts": parts or [], "notes": _s(notes),
                  "status": status}

        def build(ctx: _Ctx):
            snap = ctx.snap
            v = self._vehicle(snap, vehicle_id)
            jid = ctx.next_id("Jobs", "job_id", "J", 5)
            for d in params["depends_on"]:
                self._job(snap, d)
            needs = [self._validate_need(snap, it) for it in params["parts"]]
            job = {"job_id": jid, "vehicle_id": v["vehicle_id"], "system": params["system"], "title": params["title"],
                   "goal": params["goal"], "kind": kind, "status": status, "priority": pr, "estimate_min": est,
                   "notes": params["notes"], "hold_reason": "", "created_at": ctx.now, "updated_at": ctx.now,
                   "started_at": ctx.now if status == "in_progress" else "", "completed_at": ""}
            ctx.append("Jobs", job)
            self._add_steps(ctx, jid, params["steps"])
            for d in params["depends_on"]:
                ctx.append("JobDeps", {"dep_id": ctx.next_id("JobDeps", "dep_id", "D", 5), "job_id": jid,
                                       "depends_on": d, "note": "", "created_at": ctx.now})
            for nd in needs:
                self._add_need(ctx, job, nd)
            ctx.activity("job_created", f"{jid} created: {params['title']} [{params['system']}]", v["vehicle_id"], jid)
            return {"job_id": jid}

        out = self._mutate("create_job", request_id, params, build,
                           lambda prior, snap: {"job_id": prior[0]["job_id"]})
        out["status"] = "created"
        out["job"] = self.get_job(out["job_id"])
        out["message"] = out.get("message", "") + f"Job {out['job_id']} '{params['title']}' saved."
        return out

    def update_job(self, request_id: str, job_id: str, title: str | None = None, system: str | None = None,
                   goal: str | None = None, kind: str | None = None, status: str | None = None,
                   priority: int | None = None, estimate_min: int | None = None, notes: str | None = None,
                   hold_reason: str | None = None, add_steps: list[str] | None = None,
                   steps_done: list[str] | None = None, steps_undone: list[str] | None = None,
                   add_depends_on: list[str] | None = None, remove_depends_on: list[str] | None = None,
                   add_parts: list[dict[str, Any]] | None = None, note: str | None = None) -> dict[str, Any]:
        """Change job fields, tick checklist steps, add dependencies/part needs, or log a note."""
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = _s(title)
        if system is not None:
            changes["system"] = _norm_system(system)
        if goal is not None:
            changes["goal"] = _s(goal)
        if kind is not None:
            if _s(kind).lower() not in JOB_KINDS:
                raise InventoryError(f"kind must be one of {JOB_KINDS}")
            changes["kind"] = _s(kind).lower()
        if status is not None:
            st = _s(status).lower()
            if st not in JOB_STATUSES:
                raise InventoryError(f"status must be one of {JOB_STATUSES}")
            changes["status"] = st
        if priority is not None:
            pr = _int(priority, "priority", 1)
            if pr > 5:
                raise InventoryError("priority is 1 (highest) to 5 (lowest)")
            changes["priority"] = pr
        if estimate_min is not None:
            changes["estimate_min"] = _int(estimate_min, "estimate_min", 1)
        if notes is not None:
            changes["notes"] = _s(notes)
        if hold_reason is not None:
            changes["hold_reason"] = _s(hold_reason)
        params = {"job_id": _s(job_id).upper(), **changes, "add_steps": add_steps or [],
                  "steps_done": [x.upper() for x in steps_done or []], "steps_undone": [x.upper() for x in steps_undone or []],
                  "add_dep": [x.upper() for x in add_depends_on or []], "rm_dep": [x.upper() for x in remove_depends_on or []],
                  "add_parts": add_parts or [], "note": _s(note)}
        if not any(v for k, v in params.items() if k != "job_id") and not changes:
            raise InventoryError("nothing to update")

        def build(ctx: _Ctx):
            snap = ctx.snap
            job = self._job(snap, job_id)
            jid, vid = job["job_id"], job["vehicle_id"]
            summary: list[str] = []
            warnings: list[str] = []
            ch = dict(changes)
            st = ch.get("status")
            if st and st != (job["status"] or "planned"):
                if st == "in_progress" and not job["started_at"]:
                    ch["started_at"] = ctx.now
                if st == "done":
                    ch["completed_at"] = ctx.now
                    a = self._analyze(snap, job)
                    undone = [s["text"] for s in a["steps"] if not _yes(s["done"]) and s["step_id"] not in params["steps_done"]]
                    if undone:
                        warnings.append(f"{len(undone)} checklist step(s) not ticked")
                    left = [n["name"] for n in a["needs"] if n["qty_remaining"]]
                    if left:
                        warnings.append("parts not recorded as installed: " + ", ".join(left))
                if st == "on_hold" and not (ch.get("hold_reason") or job["hold_reason"]):
                    raise InventoryError("give hold_reason when putting a job on hold")
                if st != "on_hold" and job["hold_reason"] and "hold_reason" not in ch:
                    ch["hold_reason"] = ""
                summary.append(f"status {job['status'] or 'planned'} -> {st}")
            for k in ("title", "system", "goal", "kind", "priority", "estimate_min", "notes", "hold_reason"):
                if k in ch and k != "hold_reason":
                    summary.append(f"{k} updated")
            if ch:
                ch["updated_at"] = ctx.now
                ctx.update("Jobs", job, ch)
            if params["add_steps"]:
                ids = self._add_steps(ctx, jid, params["add_steps"])
                summary.append(f"added {len(ids)} checklist step(s)")
            for sid in params["steps_done"] + params["steps_undone"]:
                step = self._find(snap, "JobSteps", "step_id", sid)
                if step is None or step["job_id"] != jid:
                    raise InventoryError(f"step {sid} is not on job {jid}")
            for sid in params["steps_done"]:
                step = self._find(snap, "JobSteps", "step_id", sid)
                ctx.update("JobSteps", step, {"done": "yes", "done_at": ctx.now})
                summary.append(f"done: {step['text']}")
            for sid in params["steps_undone"]:
                step = self._find(snap, "JobSteps", "step_id", sid)
                ctx.update("JobSteps", step, {"done": "", "done_at": ""})
                summary.append(f"reopened: {step['text']}")
            pending_edges: list[tuple[str, str]] = []
            for d in params["add_dep"]:
                self._job(snap, d)
                if d == jid:
                    raise InventoryError("a job cannot depend on itself")
                if any(x["job_id"] == jid and x["depends_on"] == d for x in snap["JobDeps"]):
                    continue
                self._check_cycle(snap, jid, d, pending_edges)
                pending_edges.append((jid, d))
                ctx.append("JobDeps", {"dep_id": ctx.next_id("JobDeps", "dep_id", "D", 5), "job_id": jid,
                                       "depends_on": d, "note": "", "created_at": ctx.now})
                summary.append(f"now waits on {d}")
            if params["rm_dep"]:
                # Rows can't be deleted atomically with other ops; mark the dependency as removed instead.
                for d in snap["JobDeps"]:
                    if d["job_id"] == jid and d["depends_on"] in params["rm_dep"]:
                        ctx.update("JobDeps", d, {"depends_on": "", "note": f"removed (was {d['depends_on']}) {ctx.now}"})
                        summary.append(f"no longer waits on {d['depends_on']}")
            for it in params["add_parts"]:
                nid = self._add_need(ctx, job, self._validate_need(snap, it))
                summary.append(f"needs part {nid}")
            if params["note"]:
                # job notes also go into the project notebook (searchable, linked to the job)
                self._append_note(ctx, params["note"], vid, job_id=jid)
                ctx.activity("note", params["note"], vid, jid)
            if summary:
                ctx.activity("job_updated", f"{jid}: " + "; ".join(summary), vid, jid)
            return {"job_id": jid, "changes": summary, "warnings": warnings}

        out = self._mutate("update_job", request_id, params, build,
                           lambda prior, snap: {"job_id": prior[0]["job_id"], "changes": [], "warnings": []})
        out["status"] = "updated"
        out["job"] = self.get_job(out["job_id"])
        return out

    def add_job_note(self, request_id: str, job_id: str, note: str) -> dict[str, Any]:
        if not _s(note):
            raise InventoryError("note is empty")
        return self.update_job(request_id, job_id, note=note)

    def update_job_part(self, request_id: str, need_id: str, check_status: str | None = None,
                        part_id: str | None = None, qty_needed: int | None = None, check: str | None = None,
                        notes: str | None = None) -> dict[str, Any]:
        """Record an inspection/fitment result, link an identified part, or change the quantity."""
        ch: dict[str, Any] = {}
        if check_status is not None:
            cs = _s(check_status).lower()
            if cs not in CHECK_STATUSES:
                raise InventoryError(f"check_status must be one of {CHECK_STATUSES}")
            ch["check_status"] = cs
        if check is not None:
            c = _s(check).lower()
            if c not in CHECKS:
                raise InventoryError(f"check must be one of {CHECKS}")
            ch["check"] = c
        if part_id is not None:
            ch["part_id"] = _s(part_id).upper()
        if qty_needed is not None:
            ch["qty_needed"] = _int(qty_needed, "qty_needed", 1)
        if notes is not None:
            ch["notes"] = _s(notes)
        if not ch:
            raise InventoryError("nothing to update")
        params = {"need_id": _s(need_id).upper(), **ch}

        def build(ctx: _Ctx):
            n = self._need(ctx.snap, need_id)
            job = self._job(ctx.snap, n["job_id"])
            if ch.get("part_id"):
                self._part(ctx.snap, ch["part_id"])
            c = dict(ch)
            c["updated_at"] = ctx.now
            ctx.update("JobParts", n, c)
            what = ", ".join(f"{k}={v}" for k, v in ch.items())
            ctx.activity("part_need_updated", f"{n['need_id']} ({n['description'] or n['part_id']}): {what}",
                         job["vehicle_id"], job["job_id"], ch.get("part_id") or n["part_id"])
            return {"need_id": n["need_id"], "job_id": job["job_id"]}

        out = self._mutate("update_job_part", request_id, params, build,
                           lambda prior, snap: {"need_id": params["need_id"], "job_id": prior[0]["job_id"]})
        out["status"] = "updated"
        out["job"] = self.get_job(out["job_id"])
        return out

    def record_job_installation(self, request_id: str, job_id: str, need_id: str | None = None,
                                part_id: str | None = None, qty: int = 1, bin_code: str | None = None,
                                stock_id: str | None = None, condition: str | None = None, notes: str = "",
                                take_from_stock: bool = True) -> dict[str, Any]:
        """The user reports they installed a part for a job: stock, the vehicle's installation history,
        the job's part need and the activity log are updated together in one atomic write."""
        rid_job = _s(job_id).upper()
        snap = self.store.read()
        job = self._job(snap, rid_job)
        if not need_id and not part_id:
            raise InventoryError("give need_id or part_id")
        need = None
        if need_id:
            need = self._need(snap, need_id)
            if need["job_id"] != job["job_id"]:
                raise InventoryError(f"need {need_id} belongs to {need['job_id']}, not {job['job_id']}")
            if not need["part_id"]:
                raise InventoryError(f"need {need['need_id']} has no linked part yet; link it with update_job_part")
            part_id = need["part_id"]
        else:
            need = next((n for n in snap["JobParts"] if n["job_id"] == job["job_id"] and n["part_id"] == _s(part_id).upper()), None)
        q = _int(qty, "qty", 1)
        need_key = need["need_id"] if need else ""

        def extra(ctx: _Ctx, part, veh, iid):
            j = self._job(ctx.snap, job["job_id"])
            n = self._find(ctx.snap, "JobParts", "need_id", need_key) if need_key else None
            if n is not None:
                ctx.update("JobParts", n, {"qty_installed": (n["qty_installed"] or 0) + q, "updated_at": ctx.now})
            if (j["status"] or "planned") == "planned":
                ctx.update("Jobs", j, {"status": "in_progress", "started_at": j["started_at"] or ctx.now, "updated_at": ctx.now})
            elif (j["status"] or "planned") in ("done", "cancelled"):
                raise InventoryError(f"job {j['job_id']} is {j['status']}; reopen it before recording installs")
            else:
                ctx.update("Jobs", j, {"updated_at": ctx.now})
            ctx.activity("installed", f"Installed {q} x {label_name(part)} ({part['part_id']}) on {self.vehicle_label(veh)} "
                         f"for {j['job_id']} [{iid}]" + (f": {_s(notes)}" if _s(notes) else ""),
                         veh["vehicle_id"], j["job_id"], part["part_id"])
            return {"job_id": j["job_id"], "need_id": need_key or None}

        out = self.record_installation(request_id, part_id, job["vehicle_id"], q, bin_code, condition, stock_id,
                                       take_from_stock, notes, job_id=job["job_id"], _kind="record_job_installation",
                                       _extra=extra, _extra_params={"need": need_key},
                                       _extra_replay=lambda prior, snap: {"job_id": job["job_id"], "need_id": need_key or None})
        out["job"] = self.get_job(job["job_id"])
        return out

    def record_removal(self, request_id: str, vehicle_id: str, bin_code: str, qty: int = 1, condition: str = "used",
                       part_id: str | None = None, new_part: dict[str, Any] | None = None, removed_from: str = "",
                       job_id: str | None = None, reinstall_job_id: str | None = None, check: str = "inspect",
                       photo_path: str | None = None, photo_base64: str | None = None, photo_drive_ref: str | None = None,
                       print_label: bool = True, notes: str = "", create_bin_if_missing: bool = False) -> dict[str, Any]:
        """Teardown: a part came off this vehicle. Stores it (donor = this vehicle, removed_from = where),
        photographs it in place, links it to the job where it goes back (as a part need to inspect) and
        labels its bag/bin - all in one call."""
        snap = self.store.read()
        veh = self._vehicle(snap, vehicle_id)
        tj = self._job(snap, job_id) if job_id else None
        rj = self._job(snap, reinstall_job_id) if reinstall_job_id else None
        for j in (tj, rj):
            if j and j["vehicle_id"] != veh["vehicle_id"]:
                raise InventoryError(f"job {j['job_id']} belongs to {j['vehicle_id']}, not {veh['vehicle_id']}")
        chk = _s(check).lower() or "inspect"
        if chk not in CHECKS:
            raise InventoryError(f"check must be one of {CHECKS}")
        q = _int(qty, "qty", 1)

        def extra(ctx: _Ctx, part, sid, photo):
            res: dict[str, Any] = {}
            if photo:
                phid = ctx.next_id("Photos", "photo_id", "PH", 5)
                ctx.append("Photos", {"photo_id": phid, "link": photo["link"], "vehicle_id": veh["vehicle_id"],
                                      "job_id": (tj or rj or {}).get("job_id", ""), "part_id": part["part_id"],
                                      "caption": f"{part['name']} in place" + (f" ({removed_from})" if removed_from else ""),
                                      "stage": "in place", "source": SOURCE.get(), "taken_at": ctx.now,
                                      "request_id": ctx.request_id})
                res["photo_id"] = phid
            if rj:
                nid = self._add_need(ctx, rj, {"part_id": part["part_id"], "description": part["name"], "qty_needed": q,
                                               "check": chk, "notes": f"removed from {removed_from}".strip() if removed_from else ""})
                res["reinstall_need_id"] = nid
                res["reinstall_job_id"] = rj["job_id"]
            if tj and (tj["status"] or "planned") == "planned":
                ctx.update("Jobs", tj, {"status": "in_progress", "started_at": ctx.now, "updated_at": ctx.now})
            ctx.activity("removed", f"Removed {q} x {label_name(part)} ({part['part_id']}) from {veh['vehicle_id']}"
                         + (f" ({removed_from})" if removed_from else "") + f" -> bin {norm_bin(bin_code)}"
                         + (f"; goes back in {rj['job_id']}" if rj else ""),
                         veh["vehicle_id"], (tj or rj or {}).get("job_id", ""), part["part_id"])
            return res

        def replay(prior, snap2):
            res: dict[str, Any] = {}
            ph = next((p for p in snap2["Photos"] if p["request_id"] == _s(request_id)), None)
            if ph:
                res["photo_id"] = ph["photo_id"]
            if rj:
                rec = next((t for t in prior if t.get("type") == "receive"), None)
                n = next((n for n in snap2["JobParts"] if rec and n["job_id"] == rj["job_id"] and n["part_id"] == rec["part_id"]), None)
                if n:
                    res["reinstall_need_id"] = n["need_id"]
                    res["reinstall_job_id"] = rj["job_id"]
            return res

        out = self.receive_stock(request_id, bin_code, q, condition, part_id=part_id, new_part=new_part,
                                 donor_vehicle_id=veh["vehicle_id"], notes=notes, photo_path=photo_path,
                                 photo_base64=photo_base64, photo_drive_ref=photo_drive_ref, print_label=print_label,
                                 create_bin_if_missing=create_bin_if_missing, removed_from=removed_from,
                                 _kind="record_removal", _extra=extra, _extra_replay=replay,
                                 _extra_params={"job": (tj or {}).get("job_id", ""), "reinstall": (rj or {}).get("job_id", ""),
                                                "check": chk})
        out["status"] = "removed_and_stored"
        out["message"] = out["message"].replace("Saved", "Removed and stored", 1)
        return out

    def attach_photo(self, request_id: str, vehicle_id: str | None = None, job_id: str | None = None,
                     part_id: str | None = None, caption: str = "", stage: str = "", photo_path: str | None = None,
                     photo_base64: str | None = None, photo_drive_ref: str | None = None) -> dict[str, Any]:
        if not (photo_path or photo_base64 or photo_drive_ref):
            raise InventoryError("give photo_path, photo_drive_ref or photo_base64")
        if not (vehicle_id or job_id or part_id):
            raise InventoryError("attach the photo to a vehicle, job and/or part")
        if self.photos is None:
            raise InventoryError("photo storage is not configured")
        params = {"vehicle_id": _s(vehicle_id).upper(), "job_id": _s(job_id).upper(), "part_id": _s(part_id).upper(),
                  "caption": _s(caption), "stage": _s(stage)}

        def build(ctx: _Ctx):
            snap = ctx.snap
            job = self._job(snap, job_id) if job_id else None
            part = self._part(snap, part_id) if part_id else None
            vid = params["vehicle_id"] or (job["vehicle_id"] if job else "")
            if vid:
                self._vehicle(snap, vid)
            photo = self.photos.save(ctx.request_id, part["part_id"] if part else (job["job_id"] if job else vid),
                                     path=photo_path, base64_data=photo_base64, drive_ref=photo_drive_ref)
            phid = ctx.next_id("Photos", "photo_id", "PH", 5)
            ctx.append("Photos", {"photo_id": phid, "link": photo["link"], "vehicle_id": vid,
                                  "job_id": job["job_id"] if job else "", "part_id": part["part_id"] if part else "",
                                  "caption": params["caption"], "stage": params["stage"], "source": SOURCE.get(),
                                  "taken_at": ctx.now, "request_id": ctx.request_id})
            if part:
                links = [x for x in (part.get("photo_links") or "").split("\n") if x.strip()]
                if photo["link"] not in links:
                    links.append(photo["link"])
                    ctx.update("Parts", part, {"photo_links": "\n".join(links), "updated_at": ctx.now})
            target = " / ".join(x for x in (job and job["job_id"], part and part["part_id"], vid) if x)
            ctx.activity("photo", f"Photo {phid} added to {target}" + (f": {params['caption']}" if params["caption"] else ""),
                         vid, job["job_id"] if job else "", part["part_id"] if part else "")
            return {"photo_id": phid, "link": photo["link"]}

        def replay(prior, snap):
            ph = next(p for p in snap["Photos"] if p["request_id"] == _s(request_id))
            return {"photo_id": ph["photo_id"], "link": ph["link"]}

        out = self._mutate("attach_photo", request_id, params, build, replay)
        out["status"] = "photo_saved"
        return out

    def record_spec(self, request_id: str, item: str, value: str, source: str, unit: str = "",
                    vehicle_id: str | None = None, job_id: str | None = None, part_id: str | None = None,
                    verified: bool = False, notes: str = "") -> dict[str, Any]:
        """Save a repair specification (torque, clearance, fluid capacity, fitment fact) WITH its source."""
        if not _s(item) or not _s(value):
            raise InventoryError("item and value are required")
        if not _s(source):
            raise InventoryError("source is required (e.g. '1970 Ford Car Shop Manual, Vol. 2, p. 4-12')")
        params = {"item": _s(item), "value": _s(value), "unit": _s(unit), "source": _s(source),
                  "vehicle_id": _s(vehicle_id).upper(), "job_id": _s(job_id).upper(), "part_id": _s(part_id).upper(),
                  "verified": bool(verified), "notes": _s(notes)}

        def build(ctx: _Ctx):
            snap = ctx.snap
            job = self._job(snap, params["job_id"]) if params["job_id"] else None
            vid = params["vehicle_id"] or (job["vehicle_id"] if job else "")
            if vid:
                self._vehicle(snap, vid)
            if params["part_id"]:
                self._part(snap, params["part_id"])
            spid = ctx.next_id("Specs", "spec_id", "SP", 5)
            ctx.append("Specs", {"spec_id": spid, "vehicle_id": vid, "job_id": params["job_id"], "part_id": params["part_id"],
                                 "item": params["item"], "value": params["value"], "unit": params["unit"],
                                 "source": params["source"], "verified": VERIFIED if verified else UNVERIFIED,
                                 "notes": params["notes"], "created_at": ctx.now})
            ctx.activity("spec", f"{spid} {params['item']}: {params['value']} {params['unit']} (source: {params['source']})".strip(),
                         vid, params["job_id"], params["part_id"])
            return {"spec_id": spid}

        out = self._mutate("record_spec", request_id, params, build,
                           lambda prior, snap: {"spec_id": prior[0]["summary"].split()[0]})
        out["status"] = "saved"
        return out

    def preview_label(self, part_id: str, bin_code: str | None = None) -> dict[str, Any]:
        """Render what a part label will look like on the configured tape. Nothing is queued or printed and
        inventory is not changed. bin_code defaults to the part's main bin."""
        from .labels.render import LabelContent, PRINTABLE_DOTS, barcode_fits, render_label

        if self.labels is None:
            raise InventoryError("labels are not configured")
        snap = self.store.read()
        p = self._part(snap, part_id)
        code = norm_bin(bin_code) if bin_code else ""
        if not code:
            stock = sorted((s for s in snap["Stock"] if s["part_id"] == p["part_id"] and (s["qty"] or 0) > 0),
                           key=lambda s: -(s["qty"] or 0))
            code = stock[0]["bin_code"] if stock else ""
        tape = float(self.labels.cfg["tape_mm"])
        tape = int(tape) if tape.is_integer() else tape
        out = self.labels.preview_dir / f"preview-{p['part_id']}-{code or 'nobin'}-{tape:g}mm.png"
        content = LabelContent(label_name(p), p["part_id"], code)
        render_label(content, tape, out)
        return {"status": "previewed", "printed": False, "inventory_changed": False, "preview_path": str(out),
                "tape_mm": tape, "printable_dots": PRINTABLE_DOTS[tape], "barcode": barcode_fits(tape),
                "lines": {"name": content.short_name(), "part_id": p["part_id"], "bin": code},
                "printer_mode": self.labels.cfg.get("mode")}

    def print_bin_label(self, request_id: str, bin_code: str, copies: int = 1) -> dict[str, Any]:
        """Label for a bin/bag. Its barcode is BIN:<code> so a scan opens the bin."""
        snap = self.store.read()
        code = norm_bin(bin_code)
        b = self._bin(snap, code)
        if self.labels is None:
            raise InventoryError("labels are not configured")
        name = b["description"] or "Storage bin"
        job = self.labels.request("bin", _s(request_id), f"BIN:{code}", name, code, _int(copies, "copies", 1), send=True)
        return {"status": "label_requested", "label": job, "inventory_changed": False}


__all__ = ["Garage", "SYSTEMS", "JOB_STATUSES", "JOB_KINDS", "CHECKS", "SCOPE_UNIVERSAL"]
