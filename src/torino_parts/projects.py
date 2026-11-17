"""Project features on top of the rebuild workflow (schema v3):

* custody   - parts at an outside shop (machine shop, plater...), partial returns, job blockers
* shopping  - per-project list: considering -> ready -> ordered -> received (partial deliveries via intake)
* notebook  - searchable notes linked to jobs, parts and photos
* progress  - completed / active / blocked work, where you left off, resume with next step and pull list
* money     - budget, expenses, refunds, receipts; actual vs outstanding orders vs possible purchases

Every write goes through ``Inventory._mutate``: one atomic commit, idempotent per request_id, with an
Activity row - exactly like stock changes. Checkout/payment happens outside the app; Torino only records it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .inventory import SOURCE, InventoryError, _Ctx, _int, _s, custody_view, label_name, norm_bin, norm_condition
from .schema import EXPENSE_CATEGORIES, EXPENSE_KINDS, SHOPPING_STATUSES
from .store.base import Snapshot

OPEN_SHOP = ("considering", "ready", "ordered")


def _money(v: Any, name: str = "amount", allow_zero: bool = False) -> float:
    try:
        f = round(float(str(v).replace("$", "").replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        raise InventoryError(f"{name} must be a number, e.g. 42.50") from None
    if f < 0 or (f == 0 and not allow_zero):
        raise InventoryError(f"{name} must be more than 0 (record money back as a refund)")
    return f


def _opt_money(v: Any, name: str) -> float | None:
    return None if v in (None, "") else _money(v, name, allow_zero=True)


def _date(v: Any, name: str = "date") -> str:
    s = _s(v)
    if not s:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        return datetime.fromisoformat(s[:10]).strftime("%Y-%m-%d")
    except ValueError:
        raise InventoryError(f"{name} must look like 2026-11-12") from None


def _opt_date(v: Any, name: str) -> str:
    return _date(v, name) if _s(v) else ""


def _cat(v: Any) -> str:
    c = _s(v).lower()
    return c or "parts"


class ProjectsMixin:
    """Mixed into :class:`torino_parts.rebuild.Garage`."""

    # ==================================================================
    # custody: parts away from the shop
    # ==================================================================
    def _custody(self, snap: Snapshot, custody_id: str) -> dict[str, Any]:
        c = self._find(snap, "Custody", "custody_id", custody_id)
        if c is None:
            raise InventoryError(f"no outside-shop record {custody_id!r}; use list_away to find it")
        return c

    def send_out(self, request_id: str, part_id: str, holder: str, qty: int = 1, reason: str = "",
                 from_bin: str | None = None, stock_id: str | None = None, condition: str | None = None,
                 expected_back: str = "", job_id: str | None = None, notes: str = "") -> dict[str, Any]:
        """A part leaves the shop (machine shop, plater, upholsterer, a friend...). Stock in the bin goes down;
        the part shows as away with who has it, why, since when and when it is expected back."""
        if not _s(holder):
            raise InventoryError("holder is required: who has it (e.g. 'Smith Machine Shop')")
        q = _int(qty, "qty", 1)
        cond = norm_condition(condition) if condition else None
        params = {"part_id": _s(part_id).upper(), "holder": _s(holder), "qty": q, "reason": _s(reason),
                  "from": norm_bin(from_bin) if from_bin else "", "stock_id": _s(stock_id).upper(), "condition": cond,
                  "expected_back": _opt_date(expected_back, "expected_back"), "job_id": _s(job_id).upper(),
                  "notes": _s(notes)}

        def build(ctx: _Ctx):
            snap = ctx.snap
            part = self._part(snap, part_id)
            src = self._pick_source(snap, part["part_id"], params["from"] or None, cond, stock_id)
            have = src["qty"] or 0
            if q > have:
                raise InventoryError(f"only {have} in {src['bin_code']} ({src['condition']}); cannot send {q}")
            job = self._job(snap, params["job_id"]) if params["job_id"] else None
            vid = job["vehicle_id"] if job else ""
            ctx.update("Stock", src, {"qty": have - q, "updated_at": ctx.now})
            cid = ctx.next_id("Custody", "custody_id", "C", 5)
            ctx.append("Custody", {"custody_id": cid, "part_id": part["part_id"], "stock_id": src["stock_id"],
                                   "bin_code": src["bin_code"], "condition": src["condition"], "qty_out": q,
                                   "qty_returned": 0, "holder": params["holder"], "reason": params["reason"],
                                   "vehicle_id": vid, "job_id": params["job_id"], "sent_at": ctx.now,
                                   "expected_back": params["expected_back"], "returned_at": "", "status": "out",
                                   "notes": params["notes"], "created_at": ctx.now, "updated_at": ctx.now})
            ctx.txn("send_out", part_id=part["part_id"], stock_id=src["stock_id"], bin_code=src["bin_code"],
                    qty_delta=-q, qty_after=have - q, condition=src["condition"],
                    notes=f"to {params['holder']}" + (f": {params['reason']}" if params["reason"] else ""))
            ctx.activity("sent_out", f"[{cid}] Sent {q} x {label_name(part)} ({part['part_id']}) from {src['bin_code']} "
                         f"to {params['holder']}" + (f" for {params['reason']}" if params["reason"] else "")
                         + (f", expected back {params['expected_back']}" if params["expected_back"] else ""),
                         vid, params["job_id"], part["part_id"])
            return {"custody_id": cid, "part_id": part["part_id"]}

        def replay(prior, snap):
            act = next(a for a in prior if a.get("act_id"))
            return {"custody_id": re.match(r"\[(C-\d+)\]", act["summary"]).group(1), "part_id": act["part_id"]}

        out = self._mutate("send_out", request_id, params, build, replay)
        snap = self.store.read()
        out["status"] = "sent_out"
        out["custody"] = custody_view(self._custody(snap, out["custody_id"]))
        out["part"] = self.part_view(self._part(snap, out["part_id"]), snap)
        out["message"] = out.get("message", "") + f"{q} x {out['part']['label_name']} is now away at {params['holder']}."
        return out

    def return_from_shop(self, request_id: str, custody_id: str, qty: int | None = None, to_bin: str | None = None,
                         condition: str | None = None, notes: str = "", create_bin_if_missing: bool = False) -> dict[str, Any]:
        """Some or all of an away part came back. It goes into ``to_bin`` (default: the bin it left from),
        optionally with a new condition (e.g. remanufactured after a rebuild)."""
        cond = norm_condition(condition) if condition else ""
        params = {"custody_id": _s(custody_id).upper(), "qty": qty if qty in (None, "") else _int(qty, "qty", 1),
                  "to": norm_bin(to_bin) if to_bin else "", "condition": cond, "notes": _s(notes)}

        def build(ctx: _Ctx):
            snap = ctx.snap
            c = self._custody(snap, params["custody_id"])
            away = (c["qty_out"] or 0) - (c["qty_returned"] or 0)
            if away <= 0:
                raise InventoryError(f"{c['custody_id']} is already fully returned")
            q = away if params["qty"] in (None, "") else params["qty"]
            if q > away:
                raise InventoryError(f"only {away} still at {c['holder']} on {c['custody_id']}; cannot return {q}")
            part = self._part(snap, c["part_id"])
            code = params["to"] or c["bin_code"]
            if not self._find(snap, "Bins", "bin_code", code):
                if not create_bin_if_missing:
                    self._bin(snap, code)
                ctx.append("Bins", {"bin_code": code, "description": "", "location": "", "created_at": ctx.now, "kind": "bin"})
                ctx.txn("create_bin", bin_code=code, notes="created on return")
            orig = self._find(snap, "Stock", "stock_id", c["stock_id"]) or {}
            donor = _s(orig.get("donor_vehicle_id"))
            rc = params["condition"] or c["condition"] or "used"
            row = self._stock_row(snap, part["part_id"], code, rc, donor)
            if row:
                after = (row["qty"] or 0) + q
                ctx.update("Stock", row, {"qty": after, "updated_at": ctx.now})
                sid = row["stock_id"]
            else:
                sid, after = ctx.next_id("Stock", "stock_id", "S", 5), q
                ctx.append("Stock", {"stock_id": sid, "part_id": part["part_id"], "bin_code": code, "condition": rc,
                                     "qty": q, "donor_vehicle_id": donor, "notes": orig.get("notes", ""),
                                     "updated_at": ctx.now, "removed_from": orig.get("removed_from", "")})
            ret = (c["qty_returned"] or 0) + q
            done = ret >= (c["qty_out"] or 0)
            ch = {"qty_returned": ret, "status": "returned" if done else "partial", "updated_at": ctx.now}
            if done:
                ch["returned_at"] = ctx.now
            if params["notes"]:
                ch["notes"] = (c["notes"] + "\n" if c["notes"] else "") + params["notes"]
            ctx.update("Custody", c, ch)
            ctx.txn("return", part_id=part["part_id"], stock_id=sid, bin_code=code, qty_delta=q, qty_after=after,
                    condition=rc, notes=f"back from {c['holder']} ({c['custody_id']})")
            left = (c["qty_out"] or 0) - ret
            ctx.activity("returned", f"[{c['custody_id']}] {q} x {label_name(part)} ({part['part_id']}) back from "
                         f"{c['holder']} into {code}" + (f" as {rc}" if params["condition"] else "")
                         + (f"; {left} still at {c['holder']}" if left else "; all back"),
                         c["vehicle_id"], c["job_id"], part["part_id"])
            return {"custody_id": c["custody_id"], "bin_code": code, "stock_id": sid, "qty_returned": q,
                    "still_away": left}

        def replay(prior, snap):
            t = next(x for x in prior if x.get("type") == "return")
            c = self._custody(snap, params["custody_id"])
            return {"custody_id": c["custody_id"], "bin_code": t["bin_code"], "stock_id": t["stock_id"],
                    "qty_returned": t["qty_delta"], "still_away": (c["qty_out"] or 0) - (c["qty_returned"] or 0)}

        out = self._mutate("return_from_shop", request_id, params, build, replay)
        snap = self.store.read()
        c = self._custody(snap, out["custody_id"])
        out["status"] = "returned" if not out["still_away"] else "partially_returned"
        out["custody"] = custody_view(c)
        out["part"] = self.part_view(self._part(snap, c["part_id"]), snap)
        out["message"] = out.get("message", "") + (
            f"{out['qty_returned']} back in {out['bin_code']}" + (f"; {out['still_away']} still at {c['holder']}."
                                                                   if out["still_away"] else "; nothing left away."))
        return out

    def list_away(self, vehicle_id: str | None = None, part_id: str | None = None,
                  include_returned: bool = False) -> dict[str, Any]:
        snap = self.store.read()
        rows = snap["Custody"]
        if part_id:
            rows = [c for c in rows if c["part_id"] == _s(part_id).upper()]
        if vehicle_id:
            v = _s(vehicle_id).upper()
            needed = {n["part_id"] for n in snap["JobParts"] for j in snap["Jobs"]
                      if n["job_id"] == j["job_id"] and j["vehicle_id"] == v}
            rows = [c for c in rows if c["vehicle_id"] == v or c["part_id"] in needed]
        if not include_returned:
            rows = [c for c in rows if (c["qty_out"] or 0) > (c["qty_returned"] or 0)]
        out = []
        for c in rows:
            cv = custody_view(c)
            p = self._find(snap, "Parts", "part_id", c["part_id"])
            cv["name"] = label_name(p) if p else c["part_id"]
            cv["blocks_jobs"] = sorted({n["job_id"] for n in snap["JobParts"] if n["part_id"] == c["part_id"]
                                        and (n["qty_needed"] or 0) > (n["qty_installed"] or 0)
                                        and (self._find(snap, "Jobs", "job_id", n["job_id"]) or {}).get("status")
                                        not in ("done", "cancelled")})
            out.append(cv)
        return {"away": out, "count": len(out), "qty_away": sum(c["qty_away"] for c in out)}

    # ==================================================================
    # notebook
    # ==================================================================
    def _append_note(self, ctx: _Ctx, body: str, vehicle_id: str = "", title: str = "", tags: str = "",
                     job_id: str = "", part_id: str = "", photo_id: str = "") -> str:
        nid = ctx.next_id("Notes", "note_id", "NB", 5)
        ctx.append("Notes", {"note_id": nid, "vehicle_id": vehicle_id, "title": title, "body": body, "tags": tags,
                             "job_id": job_id, "part_id": part_id, "photo_id": photo_id, "source": SOURCE.get(),
                             "created_at": ctx.now, "updated_at": ctx.now, "request_id": ctx.request_id})
        return nid

    def add_note(self, request_id: str, body: str, vehicle_id: str | None = None, title: str = "", tags: str = "",
                 job_id: str | None = None, part_id: str | None = None, photo_id: str | None = None) -> dict[str, Any]:
        """Add a notebook entry (what you did, found, measured or decided), linked to a job, part and/or photo."""
        if not _s(body):
            raise InventoryError("the note is empty")
        tag_s = ", ".join(t.strip().lower() for t in re.split(r"[,#]", _s(tags)) if t.strip())
        params = {"body": _s(body), "vehicle_id": _s(vehicle_id).upper(), "title": _s(title), "tags": tag_s,
                  "job_id": _s(job_id).upper(), "part_id": _s(part_id).upper(), "photo_id": _s(photo_id).upper()}

        def build(ctx: _Ctx):
            snap = ctx.snap
            job = self._job(snap, params["job_id"]) if params["job_id"] else None
            if params["part_id"]:
                self._part(snap, params["part_id"])
            ph = None
            if params["photo_id"]:
                ph = self._find(snap, "Photos", "photo_id", params["photo_id"])
                if ph is None:
                    raise InventoryError(f"no photo {params['photo_id']}")
            vid = params["vehicle_id"] or (job["vehicle_id"] if job else "") or (ph["vehicle_id"] if ph else "")
            if vid:
                self._vehicle(snap, vid)
            nid = self._append_note(ctx, params["body"], vid, params["title"], params["tags"], params["job_id"],
                                    params["part_id"], params["photo_id"])
            ctx.activity("note", f"[{nid}] " + (params["title"] + ": " if params["title"] else "") + params["body"][:300],
                         vid, params["job_id"], params["part_id"])
            return {"note_id": nid}

        out = self._mutate("add_note", request_id, params, build,
                           lambda prior, snap: {"note_id": next(n["note_id"] for n in snap["Notes"]
                                                                if n["request_id"] == _s(request_id))})
        out["status"] = "saved"
        out["note"] = self.notebook(note_id=out["note_id"])["notes"][0]
        return out

    def _note_view(self, snap: Snapshot, n: dict[str, Any]) -> dict[str, Any]:
        v = {k: n[k] for k in ("note_id", "vehicle_id", "title", "body", "tags", "job_id", "part_id", "photo_id",
                               "source", "created_at") if n.get(k)}
        if n.get("job_id"):
            j = self._find(snap, "Jobs", "job_id", n["job_id"])
            v["job"] = {"job_id": n["job_id"], "title": j["title"] if j else "(missing job)"}
        if n.get("part_id"):
            p = self._find(snap, "Parts", "part_id", n["part_id"])
            v["part"] = {"part_id": n["part_id"], "name": label_name(p) if p else "(missing part)"}
        if n.get("photo_id"):
            ph = self._find(snap, "Photos", "photo_id", n["photo_id"])
            if ph:
                v["photo"] = {"photo_id": ph["photo_id"], "link": ph["link"], "caption": ph["caption"]}
        return v

    def notebook(self, vehicle_id: str | None = None, query: str = "", job_id: str | None = None,
                 part_id: str | None = None, tag: str | None = None, note_id: str | None = None,
                 limit: int = 100) -> dict[str, Any]:
        """Search the project notebook (newest first). Notes recorded on jobs before v0.3 are included."""
        snap = self.store.read()
        notes = [dict(n) for n in snap["Notes"]]
        have = {n["request_id"] for n in notes if n.get("request_id")}
        for a in snap["Activity"]:  # older job notes (activity log only)
            if a["type"] == "note" and a["request_id"] not in have:
                notes.append({"note_id": a["act_id"], "vehicle_id": a["vehicle_id"], "title": "", "body": a["summary"],
                              "tags": "", "job_id": a["job_id"], "part_id": a["part_id"], "photo_id": "",
                              "source": a["source"], "created_at": a["timestamp"]})
        if note_id:
            notes = [n for n in notes if n["note_id"] == _s(note_id).upper()]
        if vehicle_id:  # general notes (no vehicle) belong to every project's notebook
            notes = [n for n in notes if n["vehicle_id"] in (_s(vehicle_id).upper(), "")]
        if job_id:
            notes = [n for n in notes if n["job_id"] == _s(job_id).upper()]
        if part_id:
            notes = [n for n in notes if n["part_id"] == _s(part_id).upper()]
        if tag:
            t = _s(tag).lower().lstrip("#")
            notes = [n for n in notes if t in [x.strip() for x in (n.get("tags") or "").split(",")]]
        views = [self._note_view(snap, n) for n in notes]
        for i, v_ in enumerate(views):
            v_["_seq"] = i
        if _s(query):
            words = [w for w in re.findall(r"[\w-]+", query.lower()) if w]
            def hay(v):
                return " ".join(str(x) for x in (v.get("title"), v.get("body"), v.get("tags"), v.get("note_id"),
                                                 (v.get("job") or {}).get("title"), v.get("job_id"),
                                                 (v.get("part") or {}).get("name"), v.get("part_id"),
                                                 (v.get("photo") or {}).get("caption"))).lower()
            views = [v for v in views if all(w in hay(v) for w in words)]
        views.sort(key=lambda v: (str(v.get("created_at") or ""), v["_seq"]), reverse=True)
        for v_ in views:
            v_.pop("_seq", None)
        return {"count": len(views), "notes": views[: _int(limit, "limit", 1)]}

    # ==================================================================
    # progress and resume
    # ==================================================================
    def resume(self, vehicle_id: str, _snap: Snapshot | None = None) -> dict[str, Any]:
        """Where you left off: the job you were last working on, its next step, what to pull from which bin,
        and what blocks it."""
        snap = _snap or self.store.read()
        v = self._vehicle(snap, vehicle_id)
        acts = [a for a in snap["Activity"] if a["vehicle_id"] == v["vehicle_id"]]
        open_jobs = {j["job_id"]: j for j in snap["Jobs"] if j["vehicle_id"] == v["vehicle_id"]
                     and (j["status"] or "planned") in ("planned", "in_progress", "on_hold")}
        focus = None
        for a in reversed(acts):
            if a["job_id"] in open_jobs and a["type"] not in ("job_created",):
                focus = open_jobs[a["job_id"]]
                break
        if focus is None:
            inprog = [j for j in open_jobs.values() if j["status"] == "in_progress"]
            focus = max(inprog, key=lambda j: str(j["updated_at"]), default=None)
        notes = self.notebook(v["vehicle_id"], limit=1)["notes"]
        out: dict[str, Any] = {"vehicle_id": v["vehicle_id"], "vehicle": self.vehicle_label(v),
                               "last_activity": self._act_view(acts[-1]) if acts else None,
                               "last_note": notes[0] if notes else None}
        if focus:
            j = self.get_job(focus["job_id"], activity_limit=5)
            out["job"] = {"job_id": j["job_id"], "title": j["title"], "state": j["state"], "steps": j["steps"],
                          "system": j["system"]}
            out["next_step"] = j["where_you_left_off"]["next_step"]
            out["pull_list"] = j["pull_list"]
            out["blockers"] = j["blockers"]
            out["checks"] = j["checks"]
            out["last_on_this_job"] = j["activity"][0] if j["activity"] else None
        nxt = self.what_next(v["vehicle_id"], _snap=snap)
        out["then"] = [{"job_id": w["job_id"], "title": w["title"], "state": w["state"], "next_step": w.get("next_step"),
                        "pull_list": w.get("pull_list", [])} for w in nxt["do_next"]
                       if not focus or w["job_id"] != focus["job_id"]][:3]
        if not focus and not out["then"]:
            out["message"] = "Nothing is in progress or ready. See what's blocking on the Rebuild page."
        return out

    def progress(self, vehicle_id: str) -> dict[str, Any]:
        """Completed work, active work, blockers and where you left off - for one project."""
        snap = self.store.read()
        v = self._vehicle(snap, vehicle_id)
        demand = self._demand(snap)
        jobs = [j for j in snap["Jobs"] if j["vehicle_id"] == v["vehicle_id"]]
        an = {j["job_id"]: self._analyze(snap, j, demand) for j in jobs}
        summ = {j["job_id"]: self._job_summary(snap, j, analysis=an[j["job_id"]]) for j in jobs}
        done = sorted((j for j in jobs if j["status"] == "done"), key=lambda j: str(j["completed_at"]), reverse=True)
        active = [summ[j["job_id"]] for j in jobs if j["status"] == "in_progress"]
        from .rebuild import _yes

        for s in active:
            nxt = next((st for st in an[s["job_id"]]["steps"] if not _yes(st["done"])), None)
            if nxt:
                s["next_step"] = nxt["text"]
        blocked = [{**summ[j["job_id"]], "blockers": an[j["job_id"]]["blockers"]} for j in jobs
                   if an[j["job_id"]]["state"] in ("blocked", "on_hold")]
        live = [j for j in jobs if j["status"] != "cancelled"]
        steps_total = sum(len(an[j["job_id"]]["steps"]) for j in live)
        steps_done = sum(an[j["job_id"]]["steps_done"] for j in live)
        systems: dict[str, dict[str, int]] = {}
        for j in live:
            s = systems.setdefault(j["system"] or "other", {"done": 0, "open": 0})
            s["done" if j["status"] == "done" else "open"] += 1
        milestones = [self._act_view(a) for a in snap["Activity"] if a["vehicle_id"] == v["vehicle_id"]
                      and a["type"] in ("installed", "removed", "returned", "sent_out", "shopping_received", "note")
                      or (a["vehicle_id"] == v["vehicle_id"] and a["type"] == "job_updated" and "-> done" in a["summary"])]
        return {"vehicle_id": v["vehicle_id"], "vehicle": self.vehicle_label(v),
                "totals": {"jobs": len(live), "done": len(done), "in_progress": len(active), "blocked": len(blocked),
                           "percent_jobs_done": round(100 * len(done) / len(live)) if live else 0,
                           "steps_done": steps_done, "steps_total": steps_total,
                           "parts_installed": sum((i["qty"] or 0) for i in snap["Installations"]
                                                  if i["vehicle_id"] == v["vehicle_id"])},
                "completed": [{"job_id": j["job_id"], "title": j["title"], "system": j["system"],
                               "completed_at": j["completed_at"], "steps": summ[j["job_id"]]["steps"]} for j in done],
                "active": active, "blocked": blocked, "by_system": systems,
                "away": self.list_away(v["vehicle_id"])["away"],
                "recent": milestones[-15:][::-1], "resume": self.resume(v["vehicle_id"], _snap=snap)}

    # ==================================================================
    # shopping list
    # ==================================================================
    def _item(self, snap: Snapshot, item_id: str) -> dict[str, Any]:
        it = self._find(snap, "Shopping", "item_id", item_id)
        if it is None:
            raise InventoryError(f"no shopping item {item_id!r}; use list_shopping to find it")
        return it

    def _item_view(self, snap: Snapshot, it: dict[str, Any]) -> dict[str, Any]:
        qty, got = it["qty"] or 0, it["qty_received"] or 0
        v = {k: it[k] for k in ("item_id", "vehicle_id", "status", "name", "part_id", "job_id", "need_id", "vendor",
                                "link", "category", "order_ref", "ordered_at", "received_at", "notes")
             if it.get(k) not in ("", None)}
        v["qty"], v["qty_received"] = qty, got
        if it["est_price"] is not None:
            v["est_price"] = it["est_price"]
            v["est_total"] = round(it["est_price"] * qty, 2)
        if it["status"] == "ordered" and got:
            v["partially_received"] = True
            v["qty_to_come"] = qty - got
        pays = [e for e in snap["Expenses"] if e["item_id"] == it["item_id"] and not e.get("voided")]
        if pays:
            v["paid"] = round(sum((e["amount"] or 0) * (-1 if e["kind"] == "refund" else 1) for e in pays), 2)
            v["expense_ids"] = [e["expense_id"] for e in pays]
        if it.get("job_id"):
            j = self._find(snap, "Jobs", "job_id", it["job_id"])
            v["job_title"] = j["title"] if j else None
        return v

    def add_shopping_item(self, request_id: str, vehicle_id: str, name: str = "", qty: int = 1,
                          status: str = "considering", vendor: str = "", link: str = "", est_price: Any = None,
                          part_id: str | None = None, job_id: str | None = None, need_id: str | None = None,
                          category: str = "parts", notes: str = "") -> dict[str, Any]:
        """Put something on the project's shopping list (it is NOT bought by the app; checkout stays outside)."""
        st = _s(status).lower() or "considering"
        if st not in OPEN_SHOP:
            raise InventoryError(f"new items start as one of {list(OPEN_SHOP)}")
        params = {"vehicle_id": _s(vehicle_id).upper(), "name": _s(name), "qty": _int(qty, "qty", 1), "status": st,
                  "vendor": _s(vendor), "link": _s(link), "est_price": _opt_money(est_price, "est_price"),
                  "part_id": _s(part_id).upper(), "job_id": _s(job_id).upper(), "need_id": _s(need_id).upper(),
                  "category": _cat(category), "notes": _s(notes)}

        def build(ctx: _Ctx):
            snap = ctx.snap
            v = self._vehicle(snap, params["vehicle_id"])
            part = self._part(snap, params["part_id"]) if params["part_id"] else None
            need = self._need(snap, params["need_id"]) if params["need_id"] else None
            jid = params["job_id"] or (need["job_id"] if need else "")
            job = self._job(snap, jid) if jid else None
            if need and need["job_id"] != jid:
                raise InventoryError(f"need {need['need_id']} belongs to {need['job_id']}, not {jid}")
            if job and job["vehicle_id"] != v["vehicle_id"]:
                raise InventoryError(f"job {jid} belongs to {job['vehicle_id']}, not {v['vehicle_id']}")
            if not part and need and need["part_id"]:
                part = self._find(snap, "Parts", "part_id", need["part_id"])
            name = params["name"] or (part and part["name"]) or (need and need["description"])
            if not name:
                raise InventoryError("name is required (or link a part / job part need)")
            iid = ctx.next_id("Shopping", "item_id", "SH", 5)
            ctx.append("Shopping", {"item_id": iid, "vehicle_id": v["vehicle_id"], "status": st, "name": name,
                                    "part_id": part["part_id"] if part else "", "job_id": jid,
                                    "need_id": params["need_id"], "vendor": params["vendor"], "link": params["link"],
                                    "est_price": params["est_price"], "qty": params["qty"], "qty_received": 0,
                                    "category": params["category"], "order_ref": "",
                                    "ordered_at": ctx.now if st == "ordered" else "", "received_at": "",
                                    "notes": params["notes"], "created_at": ctx.now, "updated_at": ctx.now})
            ctx.activity("shopping_added", f"[{iid}] Shopping list ({st}): {params['qty']} x {name}"
                         + (f" from {params['vendor']}" if params["vendor"] else "")
                         + (f" ~${params['est_price']:.2f} each" if params["est_price"] is not None else ""),
                         v["vehicle_id"], jid, part["part_id"] if part else "")
            return {"item_id": iid}

        out = self._mutate("add_shopping_item", request_id, params, build,
                           lambda prior, snap: {"item_id": re.match(r"\[(SH-\d+)\]", prior[0]["summary"]).group(1)})
        out["status"] = "added"
        snap = self.store.read()
        out["item"] = self._item_view(snap, self._item(snap, out["item_id"]))
        return out

    def update_shopping_item(self, request_id: str, item_id: str, status: str | None = None, name: str | None = None,
                             qty: int | None = None, vendor: str | None = None, link: str | None = None,
                             est_price: Any = None, order_ref: str | None = None, notes: str | None = None,
                             category: str | None = None, job_id: str | None = None, part_id: str | None = None,
                             paid_amount: Any = None, paid_date: str | None = None,
                             allow_additional_payment: bool = False) -> dict[str, Any]:
        """Change an item or move it along: considering -> ready -> ordered (optionally recording what you paid)
        -> cancelled. Receiving goes through receive_shopping_item (puts it in a bin and prints its label)."""
        ch: dict[str, Any] = {}
        if status is not None:
            st = _s(status).lower()
            if st not in SHOPPING_STATUSES:
                raise InventoryError(f"status must be one of {SHOPPING_STATUSES}")
            ch["status"] = st
        for k, val in (("name", name), ("vendor", vendor), ("link", link), ("order_ref", order_ref), ("notes", notes)):
            if val is not None:
                ch[k] = _s(val)
        if category is not None:
            ch["category"] = _cat(category)
        if qty is not None:
            ch["qty"] = _int(qty, "qty", 1)
        if est_price not in (None, ""):
            ch["est_price"] = _money(est_price, "est_price", allow_zero=True)
        if job_id is not None:
            ch["job_id"] = _s(job_id).upper()
        if part_id is not None:
            ch["part_id"] = _s(part_id).upper()
        paid = _money(paid_amount, "paid_amount") if paid_amount not in (None, "") else None
        if not ch and paid is None:
            raise InventoryError("nothing to update")
        params = {"item_id": _s(item_id).upper(), **ch, "paid": paid, "paid_date": _opt_date(paid_date, "paid_date"),
                  "allow_more": bool(allow_additional_payment)}

        def build(ctx: _Ctx):
            snap = ctx.snap
            it = self._item(snap, params["item_id"])
            c = dict(ch)
            if c.get("job_id"):
                self._job(snap, c["job_id"])
            if c.get("part_id"):
                self._part(snap, c["part_id"])
            if c.get("qty") is not None and c["qty"] < (it["qty_received"] or 0):
                raise InventoryError(f"{it['qty_received']} already received; qty can't be lower")
            st = c.get("status")
            msg = []
            if st == "received" and (it["qty_received"] or 0) < (c.get("qty") or it["qty"] or 0):
                msg.append("marked received without adding stock (use receive_shopping_item to put parts in a bin)")
            if st == "ordered" and not it["ordered_at"]:
                c["ordered_at"] = ctx.now
            if st == "received" and not it["received_at"]:
                c["received_at"] = ctx.now
            c["updated_at"] = ctx.now
            ctx.update("Shopping", it, c)
            eid = None
            if paid is not None:
                prior = [e for e in snap["Expenses"] if e["item_id"] == it["item_id"] and e["kind"] != "refund"
                         and not e.get("voided")]
                if prior and not params["allow_more"]:
                    raise InventoryError(
                        f"a payment is already recorded for {it['item_id']} ({', '.join(e['expense_id'] for e in prior)}: "
                        f"${sum(e['amount'] or 0 for e in prior):.2f}). Not adding it again. If this really is a "
                        "second payment, pass allow_additional_payment=true.",
                        existing_expenses=[e["expense_id"] for e in prior])
                eid = self._append_expense(ctx, it["vehicle_id"], paid, "expense", c.get("vendor", it["vendor"]),
                                           c.get("category", it["category"]) or "parts",
                                           f"{c.get('name', it['name'])} x{c.get('qty', it['qty'])}",
                                           params["paid_date"] or ctx.now[:10], c.get("job_id", it["job_id"]),
                                           c.get("part_id", it["part_id"]), it["item_id"], "",
                                           f"order {c.get('order_ref', it['order_ref'])}".strip() if c.get("order_ref", it["order_ref"]) else "")
            what = ", ".join(f"{k}={v}" for k, v in ch.items())
            ctx.activity("shopping_updated", f"[{it['item_id']}] {it['name']}: {what}".rstrip(": ")
                         + (f"; paid ${paid:.2f} [{eid}]" if eid else ""), it["vehicle_id"],
                         c.get("job_id", it["job_id"]), c.get("part_id", it["part_id"]))
            return {"item_id": it["item_id"], "expense_id": eid, "notes": msg}

        def replay(prior, snap):
            m = re.search(r"\[(E-\d+)\]", prior[0]["summary"])
            return {"item_id": params["item_id"], "expense_id": m.group(1) if m else None, "notes": []}

        out = self._mutate("update_shopping_item", request_id, params, build, replay)
        out["status"] = "updated"
        snap = self.store.read()
        out["item"] = self._item_view(snap, self._item(snap, out["item_id"]))
        return out

    def receive_shopping_item(self, request_id: str, item_id: str, bin_code: str, qty: int | None = None,
                              condition: str = "new", part_id: str | None = None, new_part: dict | None = None,
                              print_label: bool = True, create_bin_if_missing: bool = False,
                              notes: str = "") -> dict[str, Any]:
        """A delivery arrived: put it in a bin through the normal intake (stock + transaction + label), mark the
        shopping item received (or partly received) and link the part to the item and its job need. Does NOT
        record a payment again - payments are recorded once, when ordered or with record_expense."""
        snap = self.store.read(fresh=True)
        it = self._item(snap, item_id)
        iid = it["item_id"]
        # fingerprint = what the caller asked for, so a retry matches even after the item changed
        fp = {"item": iid, "bin": norm_bin(bin_code), "qty": None if qty in (None, "") else _int(qty, "qty", 1),
              "condition": norm_condition(condition), "part_id": _s(part_id).upper(), "new_part": new_part or {},
              "label": bool(print_label), "notes": _s(notes)}
        prior = next((t for t in snap["Transactions"] if t["request_id"] == _s(request_id) and t["type"] == "receive"), None)
        if prior:  # retry of a receive that already happened: replay it, skip the "still to come" checks
            q, pid, new_part = prior["qty_delta"] or 1, prior["part_id"], None
        else:
            if it["status"] == "cancelled":
                raise InventoryError(f"{iid} is cancelled")
            left = (it["qty"] or 0) - (it["qty_received"] or 0)
            q = left if fp["qty"] is None else fp["qty"]
            if left <= 0:
                raise InventoryError(f"all {it['qty']} of {iid} are already received")
            if q > left:
                raise InventoryError(f"only {left} of {iid} still to come; raise the item's qty first if more arrived")
            pid = _s(part_id).upper() or it["part_id"]
            if not pid and not new_part:
                new_part = {"name": it["name"], "category": it["category"] if it["category"] not in ("parts", "") else ""}

        def extra(ctx: _Ctx, part, sid, photo):
            cur = self._item(ctx.snap, iid)
            got = (cur["qty_received"] or 0) + q
            complete = got >= (cur["qty"] or 0)
            ch = {"qty_received": got, "status": "received" if complete else "ordered", "part_id": part["part_id"],
                  "updated_at": ctx.now}
            if not cur["ordered_at"]:
                ch["ordered_at"] = ctx.now
            if complete:
                ch["received_at"] = ctx.now
            ctx.update("Shopping", cur, ch)
            need = self._find(ctx.snap, "JobParts", "need_id", cur["need_id"]) if cur["need_id"] else None
            if need is not None and not need["part_id"]:
                ctx.update("JobParts", need, {"part_id": part["part_id"], "updated_at": ctx.now})
            ctx.activity("shopping_received", f"[{iid}] Received {q} x {cur['name']} into {norm_bin(bin_code)}"
                         + (" - order complete" if complete else f" - {cur['qty'] - got} still to come"),
                         cur["vehicle_id"], cur["job_id"], part["part_id"])
            return {"item_id": iid, "qty_still_to_come": max(0, (cur["qty"] or 0) - got)}

        def replay(prior, snap2):
            cur = self._item(snap2, iid)
            return {"item_id": iid, "qty_still_to_come": max(0, (cur["qty"] or 0) - (cur["qty_received"] or 0))}

        out = self.receive_stock(request_id, bin_code, q, condition, part_id=pid or None,
                                 new_part=None if pid else new_part, notes=notes or f"shopping {iid}",
                                 print_label=print_label, create_bin_if_missing=create_bin_if_missing,
                                 _kind="receive_shopping_item", _extra=extra, _extra_params={"item": iid},
                                 _extra_replay=replay, _fingerprint=fp)
        snap = self.store.read()
        out["item"] = self._item_view(snap, self._item(snap, iid))
        out["status"] = "received" if not out["qty_still_to_come"] else "partially_received"
        return out

    def list_shopping(self, vehicle_id: str, status: str | None = None) -> dict[str, Any]:
        snap = self.store.read()
        v = self._vehicle(snap, vehicle_id)
        items = [self._item_view(snap, it) for it in snap["Shopping"] if it["vehicle_id"] == v["vehicle_id"]]
        if status:
            items = [i for i in items if i["status"] == _s(status).lower()]
        groups = {s: [i for i in items if i["status"] == s] for s in SHOPPING_STATUSES}
        totals = {s: round(sum(i.get("est_total", 0) for i in g), 2) for s, g in groups.items()}
        return {"vehicle_id": v["vehicle_id"], "vehicle": self.vehicle_label(v), "items": items, "by_status": groups,
                "est_totals": totals, "count": len(items)}

    # ==================================================================
    # money
    # ==================================================================
    def _append_expense(self, ctx: _Ctx, vehicle_id: str, amount: float, kind: str, vendor: str, category: str,
                        description: str, date: str, job_id: str, part_id: str, item_id: str, receipt_link: str,
                        notes: str) -> str:
        eid = ctx.next_id("Expenses", "expense_id", "E", 5)
        ctx.append("Expenses", {"expense_id": eid, "vehicle_id": vehicle_id, "date": date, "kind": kind,
                                "amount": amount, "vendor": vendor, "category": category, "description": description,
                                "job_id": job_id or "", "part_id": part_id or "", "item_id": item_id or "",
                                "receipt_link": receipt_link, "notes": notes, "created_at": ctx.now})
        return eid

    def record_expense(self, request_id: str, vehicle_id: str | None, amount: Any, vendor: str = "",
                       category: str = "parts", description: str = "", date: str | None = None, kind: str = "expense",
                       job_id: str | None = None, part_id: str | None = None, item_id: str | None = None,
                       receipt_path: str | None = None, receipt_base64: str | None = None, notes: str = "",
                       allow_duplicate: bool = False) -> dict[str, Any]:
        """Record money spent (kind=expense) or money back (kind=refund) on a project, with an optional receipt
        file (photo or PDF). A payment that looks identical to one already recorded is refused unless
        allow_duplicate=true, so a re-entered or retried payment is never counted twice."""
        k = _s(kind).lower() or "expense"
        if k not in EXPENSE_KINDS:
            raise InventoryError(f"kind must be one of {EXPENSE_KINDS}")
        amt = _money(amount)
        params = {"vehicle_id": _s(vehicle_id).upper(), "amount": amt, "kind": k, "vendor": _s(vendor),
                  "category": _cat(category), "description": _s(description), "date": _date(date),
                  "job_id": _s(job_id).upper(), "part_id": _s(part_id).upper(), "item_id": _s(item_id).upper(),
                  "notes": _s(notes), "allow_duplicate": bool(allow_duplicate)}
        has_receipt = bool(receipt_path or receipt_base64)
        if has_receipt and self.photos is None:
            raise InventoryError("receipt storage is not configured")

        def build(ctx: _Ctx):
            snap = ctx.snap
            it = self._item(snap, params["item_id"]) if params["item_id"] else None
            job = self._job(snap, params["job_id"]) if params["job_id"] else None
            if params["part_id"]:
                self._part(snap, params["part_id"])
            vid = params["vehicle_id"] or (it["vehicle_id"] if it else "") or (job["vehicle_id"] if job else "")
            if not vid:
                raise InventoryError("which project? give vehicle_id (or a job / shopping item)")
            self._vehicle(snap, vid)
            if not params["allow_duplicate"]:
                for e in snap["Expenses"]:
                    if e.get("voided"):
                        continue
                    same = (e["vehicle_id"] == vid and e["kind"] == k and abs((e["amount"] or 0) - amt) < 0.005
                            and e["vendor"].lower() == params["vendor"].lower() and str(e["date"])[:10] == params["date"])
                    paid_item = (it is not None and k == "expense" and e["item_id"] == it["item_id"] and e["kind"] == "expense")
                    if same or paid_item:
                        raise InventoryError(
                            f"this looks like a payment that is already recorded: {e['expense_id']} "
                            f"(${e['amount']:.2f} {e['vendor']} {e['date']}{' for ' + e['item_id'] if e['item_id'] else ''}). "
                            "Not counting it twice. If it really is another payment, pass allow_duplicate=true.",
                            existing_expense=e["expense_id"])
            link = ""
            if has_receipt:
                link = self.photos.save(ctx.request_id, "receipt", path=receipt_path, base64_data=receipt_base64,
                                        receipt=True)["link"]
            eid = self._append_expense(ctx, vid, amt, k, params["vendor"],
                                       params["category"] if params["category"] != "parts" or not it else (it["category"] or "parts"),
                                       params["description"] or (it["name"] if it else ""), params["date"],
                                       params["job_id"] or (it["job_id"] if it else ""),
                                       params["part_id"] or (it["part_id"] if it else ""), params["item_id"], link,
                                       params["notes"])
            sign = "-" if k == "refund" else ""
            ctx.activity("expense" if k == "expense" else "refund",
                         f"[{eid}] {'Refund' if k == 'refund' else 'Spent'} {sign}${amt:.2f}"
                         + (f" at {params['vendor']}" if params["vendor"] else "")
                         + (f": {params['description'] or (it['name'] if it else '')}" if params["description"] or it else "")
                         + (f" ({params['item_id']})" if params["item_id"] else ""),
                         vid, params["job_id"] or (it["job_id"] if it else ""), params["part_id"])
            return {"expense_id": eid}

        out = self._mutate("record_expense", request_id, params, build,
                           lambda prior, snap: {"expense_id": re.match(r"\[(E-\d+)\]", prior[0]["summary"]).group(1)})
        snap = self.store.read()
        e = self._find(snap, "Expenses", "expense_id", out["expense_id"])
        out["status"] = "recorded"
        out["expense"] = self._expense_view(e)
        out["totals"] = {k2: v2 for k2, v2 in self.money_summary(e["vehicle_id"], _snap=snap).items()
                         if k2 in ("budget", "spent", "remaining", "outstanding_orders_total", "possible_purchases_total")}
        return out

    @staticmethod
    def _expense_view(e: dict[str, Any]) -> dict[str, Any]:
        v = {k: e[k] for k in ("expense_id", "vehicle_id", "date", "kind", "amount", "vendor", "category", "description",
                               "job_id", "part_id", "item_id", "receipt_link", "notes", "voided") if e.get(k) not in ("", None)}
        v["net"] = 0.0 if e.get("voided") else round(-(e["amount"] or 0) if e["kind"] == "refund" else (e["amount"] or 0), 2)
        return v

    def set_budget(self, request_id: str, vehicle_id: str, amount: Any, category: str = "",
                   notes: str = "") -> dict[str, Any]:
        """Set the project budget (category empty) or a budget for one category."""
        amt = _money(amount, allow_zero=True)
        params = {"vehicle_id": _s(vehicle_id).upper(), "amount": amt, "category": _s(category).lower(),
                  "notes": _s(notes)}

        def build(ctx: _Ctx):
            v = self._vehicle(ctx.snap, params["vehicle_id"])
            row = next((b for b in ctx.snap["Budgets"] if b["vehicle_id"] == v["vehicle_id"]
                        and b["category"] == params["category"]), None)
            if row:
                ctx.update("Budgets", row, {"amount": amt, "notes": params["notes"] or row["notes"], "updated_at": ctx.now})
                bid = row["budget_id"]
            else:
                bid = ctx.next_id("Budgets", "budget_id", "BU", 4)
                ctx.append("Budgets", {"budget_id": bid, "vehicle_id": v["vehicle_id"], "category": params["category"],
                                       "amount": amt, "notes": params["notes"], "updated_at": ctx.now})
            ctx.activity("budget", f"[{bid}] Budget{' for ' + params['category'] if params['category'] else ''} set to "
                         f"${amt:,.2f}", v["vehicle_id"])
            return {"budget_id": bid}

        out = self._mutate("set_budget", request_id, params, build,
                           lambda prior, snap: {"budget_id": re.match(r"\[(BU-\d+)\]", prior[0]["summary"]).group(1)})
        out["status"] = "saved"
        out["money"] = self.money_summary(params["vehicle_id"])
        return out

    def money_summary(self, vehicle_id: str, _snap: Snapshot | None = None, include_expenses: bool = True) -> dict[str, Any]:
        """Actual spending (expenses minus refunds), outstanding orders (ordered, no payment recorded yet) and
        possible purchases (considering / ready) as three separate totals, plus spending by category, job and
        vendor against the budget."""
        snap = _snap or self.store.read()
        v = self._vehicle(snap, vehicle_id)
        vid = v["vehicle_id"]
        all_exp = [e for e in snap["Expenses"] if e["vehicle_id"] == vid]
        exp = [e for e in all_exp if not e.get("voided")]  # voided = entered by mistake; kept, never counted
        net = lambda e: -(e["amount"] or 0) if e["kind"] == "refund" else (e["amount"] or 0)  # noqa: E731
        spent = round(sum(net(e) for e in exp), 2)

        def group(key, label=None):
            d: dict[str, float] = {}
            for e in exp:
                d[e[key] or "(none)"] = d.get(e[key] or "(none)", 0) + net(e)
            rows = [{key: k, "spent": round(x, 2)} for k, x in sorted(d.items(), key=lambda kv: -kv[1])]
            if label:
                for r in rows:
                    j = self._find(snap, "Jobs", "job_id", r[key]) if r[key] != "(none)" else None
                    r["title"] = j["title"] if j else "not linked to a job"
            return rows

        budgets = [b for b in snap["Budgets"] if b["vehicle_id"] == vid]
        total_budget = next((b["amount"] for b in budgets if not b["category"]), None)
        by_cat = group("category")
        cat_budget = [{"category": b["category"], "budget": b["amount"],
                       "spent": next((c["spent"] for c in by_cat if c["category"] == b["category"]), 0.0)}
                      for b in budgets if b["category"]]
        for c in cat_budget:
            c["remaining"] = round(c["budget"] - c["spent"], 2)
        items = [it for it in snap["Shopping"] if it["vehicle_id"] == vid]
        paid_items = {e["item_id"] for e in exp if e["item_id"] and e["kind"] == "expense"}
        est = lambda it: round((it["est_price"] or 0) * (it["qty"] or 0), 2)  # noqa: E731
        outstanding = [it for it in items if it["status"] == "ordered" and it["item_id"] not in paid_items]
        possible = [it for it in items if it["status"] in ("considering", "ready")]
        unpaid_received = [it for it in items if it["status"] == "received" and it["item_id"] not in paid_items
                           and it["est_price"]]
        out_total = round(sum(est(i) for i in outstanding), 2)
        pos_total = round(sum(est(i) for i in possible), 2)
        res: dict[str, Any] = {
            "vehicle_id": vid, "vehicle": self.vehicle_label(v), "currency": "USD",
            "budget": total_budget, "spent": spent,
            "remaining": round(total_budget - spent, 2) if total_budget is not None else None,
            "outstanding_orders_total": out_total, "possible_purchases_total": pos_total,
            "remaining_after_orders": round(total_budget - spent - out_total, 2) if total_budget is not None else None,
            "outstanding_orders": [self._item_view(snap, i) for i in outstanding],
            "possible_purchases": [self._item_view(snap, i) for i in possible],
            "by_category": by_cat, "by_job": group("job_id", label=True), "by_vendor": group("vendor"),
            "category_budgets": cat_budget,
            "refunds_total": round(sum(e["amount"] or 0 for e in exp if e["kind"] == "refund"), 2),
            "explain": "spent = expenses minus refunds; outstanding orders = ordered items with no payment recorded "
                       "(estimated price); possible purchases = items you are considering or ready to buy (estimated). "
                       "The three are never added together.",
        }
        if unpaid_received:
            res["received_without_payment"] = [self._item_view(snap, i) for i in unpaid_received]
        # actual spending over time (one point per day, running total) - orders/plans are NOT in it
        days: dict[str, float] = {}
        for e in exp:
            d = str(e["date"])[:10] or str(e["created_at"])[:10]
            days[d] = days.get(d, 0) + net(e)
        run = 0.0
        res["timeline"] = []
        for d in sorted(days):
            run += days[d]
            res["timeline"].append({"date": d, "net": round(days[d], 2), "cumulative": round(run, 2)})
        if include_expenses:
            res["expenses"] = [self._expense_view(e) for e in sorted(all_exp, key=lambda e: (str(e["date"]), e["expense_id"]),
                                                                       reverse=True)]
        return res

    def void_expense(self, request_id: str, expense_id: str, reason: str) -> dict[str, Any]:
        """Mark an expense/refund as entered by mistake. It stays in the record (with the reason) but is no longer
        counted anywhere; a shopping item it paid for shows as unpaid again."""
        if not _s(reason):
            raise InventoryError("give a reason (e.g. 'entered twice', 'wrong amount - re-entered as E-00012')")
        params = {"expense_id": _s(expense_id).upper(), "reason": _s(reason)}

        def build(ctx: _Ctx):
            e = self._find(ctx.snap, "Expenses", "expense_id", params["expense_id"])
            if e is None:
                raise InventoryError(f"no expense {params['expense_id']}")
            if e.get("voided"):
                raise InventoryError(f"{e['expense_id']} is already voided ({e['voided']})")
            ctx.update("Expenses", e, {"voided": f"{ctx.now[:10]}: {params['reason']}"})
            ctx.activity("expense_voided", f"[{e['expense_id']}] voided ${e['amount']:.2f} {e['vendor']}: {params['reason']}",
                         e["vehicle_id"], e["job_id"], e["part_id"])
            return {"expense_id": e["expense_id"], "vehicle_id": e["vehicle_id"]}

        out = self._mutate("void_expense", request_id, params, build,
                           lambda prior, snap: {"expense_id": params["expense_id"],
                                                "vehicle_id": prior[0]["vehicle_id"]})
        out["status"] = "voided"
        m = self.money_summary(out["vehicle_id"], include_expenses=False)
        out["totals"] = {k: m[k] for k in ("budget", "spent", "remaining", "outstanding_orders_total",
                                           "possible_purchases_total")}
        return out

    def money_csv(self, vehicle_id: str) -> str:
        """All expenses and refunds of a project as CSV (for records/taxes); voided rows are marked, not dropped."""
        import csv
        import io

        snap = self.store.read()
        v = self._vehicle(snap, vehicle_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["expense_id", "date", "kind", "amount", "net", "vendor", "category", "description", "job_id",
                    "part_id", "shopping_item", "receipt", "notes", "voided"])
        rows = sorted((e for e in snap["Expenses"] if e["vehicle_id"] == v["vehicle_id"]),
                      key=lambda e: (str(e["date"]), e["expense_id"]))
        for e in rows:
            amt = e["amount"] or 0
            netv = 0 if e.get("voided") else (-amt if e["kind"] == "refund" else amt)
            w.writerow([e["expense_id"], e["date"], e["kind"], f"{amt:.2f}", f"{netv:.2f}", e["vendor"], e["category"],
                        e["description"], e["job_id"], e["part_id"], e["item_id"],
                        "yes" if e["receipt_link"] else "", e["notes"], e.get("voided") or ""])
        return buf.getvalue()

    @staticmethod
    def expense_categories() -> list[str]:
        return list(EXPENSE_CATEGORIES)
