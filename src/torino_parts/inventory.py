"""Inventory service: all business rules live here; MCP tools and CLI are thin wrappers.

Idempotency
-----------
Every mutation takes a caller-supplied ``request_id``.  Under the store lock we
look for Transactions rows with that request_id.  If found (and the request
fingerprint matches) we return the original outcome instead of applying the
change again, so retries never add stock twice.  A matching label job is keyed
by the same request_id in the label queue, so retries never print twice.

Atomicity
---------
Stock/Parts/Fitment/Installations rows and their Transactions rows are written
in one ``store.commit`` (one Sheets batchUpdate), so they land together.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .schema import (
    BIN_KINDS, CONDITION_ALIASES, CONDITIONS, IDENTIFIED, SCOPE_SPECIFIC, SCOPE_UNIVERSAL, SCOPE_UNKNOWN,
    SCOPES, UNIDENTIFIED, UNVERIFIED, VERIFIED,
)
from .store.base import Append, Snapshot, Store, Update


class InventoryError(ValueError):
    """User-facing error (bad input, not found, ambiguous...)."""

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": str(self), **self.details}


# Who is making a change: "desktop", "phone", "codex" (MCP), "cli", "demo"... recorded in Activity.
SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar("torino_source", default="app")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fingerprint(kind: str, params: dict[str, Any]) -> str:
    blob = json.dumps({"kind": kind, **params}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def norm_bin(code: str) -> str:
    if code is None or not str(code).strip():
        raise InventoryError("bin_code is required")
    c = re.sub(r"\s+", "-", str(code).strip().upper())
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]{0,31}", c):
        raise InventoryError(f"invalid bin code {code!r}: use letters, digits, '-', '.', '/' (e.g. B-04)")
    return c


def norm_condition(cond: str | None) -> str:
    c = (cond or "used").strip().lower()
    c = CONDITION_ALIASES.get(c, c)
    if c not in CONDITIONS:
        raise InventoryError(f"condition must be one of {sorted(CONDITIONS)}", given=cond)
    return c


def _int(value: Any, name: str, minimum: int | None = None) -> int:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise InventoryError(f"{name} must be a whole number") from None
    if not f.is_integer():
        raise InventoryError(f"{name} must be a whole number")
    i = int(f)
    if minimum is not None and i < minimum:
        raise InventoryError(f"{name} must be at least {minimum}")
    return i


def _year(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    y = _int(value, name)
    if not 1886 <= y <= 2100:
        raise InventoryError(f"{name} {y} is not a plausible model year")
    return y


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


SIDE_WORDS = {
    "left": "left", "lh": "left", "l/h": "left", "driver": "left", "drivers": "left", "ds": "left",
    "right": "right", "rh": "right", "r/h": "right", "passenger": "right", "passengers": "right", "ps": "right",
    "front": "front", "fr": "front", "frt": "front", "rear": "rear", "rr": "rear", "back": "rear",
    "upper": "upper", "top": "upper", "lower": "lower", "bottom": "lower",
    "inner": "inner", "outer": "outer", "center": "center", "centre": "center",
}
SIDE_OPPOSITE = {"left": "right", "right": "left", "front": "rear", "rear": "front",
                 "upper": "lower", "lower": "upper", "inner": "outer", "outer": "inner"}
SIDE_ABBR = {"left": "LH", "right": "RH", "front": "FR", "rear": "RR", "upper": "UPR",
             "lower": "LWR", "inner": "INR", "outer": "OTR", "center": "CTR"}
STOPWORDS = {
    "where", "are", "is", "the", "my", "a", "an", "do", "i", "we", "have", "any", "what", "which",
    "in", "for", "of", "find", "show", "me", "located", "location", "stored", "keep", "kept", "part",
    "parts", "got", "there", "some", "all", "and", "or", "to", "from", "on", "with", "side", "please",
    "can", "you", "list", "whats", "what's", "it", "they", "those", "these", "our", "bin", "bins",
}


def _singular(t: str) -> str:
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("es") and t[-3] in "sxz":
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def _tokens(text: str) -> list[str]:
    return [_singular(t) for t in re.findall(r"[a-z0-9/]+", (text or "").lower())]


def side_set(text: str) -> set[str]:
    return {SIDE_WORDS[t] for t in re.findall(r"[a-z/]+", (text or "").lower()) if t in SIDE_WORDS}


def custody_view(c: dict[str, Any]) -> dict[str, Any]:
    """A part (or some of it) that is away from the shop: who has it, why, since when, back when."""
    out_q, ret = c["qty_out"] or 0, c["qty_returned"] or 0
    v: dict[str, Any] = {"custody_id": c["custody_id"], "part_id": c["part_id"], "holder": c["holder"],
                         "reason": c["reason"], "qty_out": out_q, "qty_returned": ret, "qty_away": out_q - ret,
                         "sent_at": c["sent_at"], "from_bin": c["bin_code"], "condition": c["condition"],
                         "status": c["status"] or ("returned" if ret >= out_q else "partial" if ret else "out")}
    for k in ("expected_back", "returned_at", "job_id", "vehicle_id", "notes"):
        if c.get(k):
            v[k] = c[k]
    try:
        v["days_away"] = (datetime.now().astimezone() - datetime.fromisoformat(str(c["sent_at"]))).days
    except (TypeError, ValueError):
        pass
    if c.get("expected_back") and out_q > ret:
        v["overdue"] = str(c["expected_back"])[:10] < datetime.now().strftime("%Y-%m-%d")
    return v


def label_name(part: dict[str, Any]) -> str:
    """Short name printed on the label: name + abbreviated side/position."""
    name = part["name"] or "Part"
    sides = []
    for t in re.findall(r"[a-z/]+", (part.get("side_position") or "").lower()):
        s = SIDE_WORDS.get(t)
        if s and SIDE_ABBR[s] not in sides:
            sides.append(SIDE_ABBR[s])
    if sides and not any(a.lower() in name.lower().split() for a in sides):
        name = f"{name} {' '.join(sides)}"
    return name


@dataclass
class _Ctx:
    snap: Snapshot
    request_id: str
    request_hash: str
    now: str
    ops: list = field(default_factory=list)
    _alloc: dict[str, int] = field(default_factory=dict)
    txns: list[str] = field(default_factory=list)
    acts: list[str] = field(default_factory=list)

    def activity(self, type_: str, summary: str, vehicle_id: str = "", job_id: str = "", part_id: str = "") -> str:
        """Append a human-readable Activity row (the rebuild log). Carries the request_id for idempotency."""
        aid = self.next_id("Activity", "act_id", "A", 6)
        self.append("Activity", {"act_id": aid, "request_id": self.request_id, "request_hash": self.request_hash,
                                 "timestamp": self.now, "type": type_, "vehicle_id": vehicle_id or "",
                                 "job_id": job_id or "", "part_id": part_id or "", "summary": summary,
                                 "source": SOURCE.get()})
        self.acts.append(aid)
        return aid

    def next_id(self, tab: str, col: str, prefix: str, width: int) -> str:
        if prefix not in self._alloc:
            pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
            nums = [int(m.group(1)) for r in self.snap[tab] if (m := pat.match(str(r.get(col) or "")))]
            self._alloc[prefix] = max(nums, default=0)
        self._alloc[prefix] += 1
        return f"{prefix}-{self._alloc[prefix]:0{width}d}"

    def append(self, tab: str, rec: dict[str, Any]) -> dict[str, Any]:
        self.ops.append(Append(tab, rec))
        return rec

    def update(self, tab: str, row: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
        row.update(changes)
        self.ops.append(Update(tab, row["_row"], dict(changes)))
        return row

    def txn(self, type_: str, **fields: Any) -> str:
        tid = self.next_id("Transactions", "txn_id", "T", 6)
        rec = {"txn_id": tid, "request_id": self.request_id, "request_hash": self.request_hash,
               "timestamp": self.now, "type": type_}
        for k in ("part_id", "stock_id", "bin_code", "qty_delta", "qty_after", "condition",
                  "vehicle_id", "related_bin", "notes"):
            rec[k] = fields.get(k, "" if k not in ("qty_delta", "qty_after") else None)
        self.append("Transactions", rec)
        self.txns.append(tid)
        return tid


class Inventory:
    def __init__(self, store: Store, labels=None, photos=None):
        self.store = store
        self.labels = labels
        self.photos = photos

    # ======================================================================
    # helpers
    # ======================================================================
    def _mutate(self, kind: str, request_id: str, params: dict[str, Any],
                build: Callable[[_Ctx], dict[str, Any]],
                replay: Callable[[list[dict], Snapshot], dict[str, Any]]) -> dict[str, Any]:
        rid = _s(request_id)
        if not rid:
            raise InventoryError(
                "request_id is required: generate a new unique id for each new request and reuse it only when retrying"
            )
        if len(rid) > 120:
            raise InventoryError("request_id is too long (max 120 characters)")
        h = _fingerprint(kind, params)
        with self.store.lock():
            snap = self.store.read(fresh=True)
            prior = ([t for t in snap["Transactions"] if t["request_id"] == rid]
                     + [a for a in snap["Activity"] if a["request_id"] == rid])
            if prior:
                if prior[0]["request_hash"] != h:
                    raise InventoryError(
                        f"request_id {rid!r} was already used for a different request "
                        f"({prior[0]['type']} at {prior[0]['timestamp']}); use a new request_id",
                        conflicting_records=[t.get("txn_id") or t.get("act_id") for t in prior],
                    )
                out = replay(prior, snap)
                out["duplicate_request"] = True
                out["message"] = "This request_id was already applied; nothing was changed again. " + out.get("message", "")
                out["transaction_ids"] = [t["txn_id"] for t in prior if t.get("txn_id")]
                out["activity_ids"] = [t["act_id"] for t in prior if t.get("act_id")]
                return out
            ctx = _Ctx(snap, rid, h, _now())
            out = build(ctx)
            self.store.commit(ctx.ops)
        out["request_id"] = rid
        out["transaction_ids"] = ctx.txns
        out["activity_ids"] = ctx.acts
        out["duplicate_request"] = False
        return out

    @staticmethod
    def _find(snap: Snapshot, tab: str, col: str, value: str) -> dict[str, Any] | None:
        v = _s(value).upper()
        for r in snap[tab]:
            if _s(r.get(col)).upper() == v:
                return r
        return None

    def _part(self, snap: Snapshot, part_id: str) -> dict[str, Any]:
        p = self._find(snap, "Parts", "part_id", part_id)
        if p is None:
            raise InventoryError(f"no part with id {part_id!r}; use search_parts to find the id")
        return p

    def _vehicle(self, snap: Snapshot, vehicle_id: str) -> dict[str, Any]:
        v = self._find(snap, "Vehicles", "vehicle_id", vehicle_id)
        if v is None:
            raise InventoryError(f"no vehicle with id {vehicle_id!r}; use list_vehicles or add_vehicle")
        return v

    def _bin(self, snap: Snapshot, code: str) -> dict[str, Any]:
        b = self._find(snap, "Bins", "bin_code", code)
        if b is None:
            raise InventoryError(
                f"bin {code} does not exist yet. Ask the user to confirm, then call create_bin "
                f"(or pass create_bin_if_missing=true).",
                missing_bin=code,
            )
        return b

    @staticmethod
    def vehicle_label(v: dict[str, Any] | None) -> str:
        if not v:
            return ""
        parts = [str(v.get("year") or ""), v.get("make", ""), v.get("model", ""), v.get("trim", "")]
        s = " ".join(p for p in parts if p)
        if v.get("nickname"):
            s += f' "{v["nickname"]}"'
        return f"{v['vehicle_id']} {s}".strip()

    def _stock_view(self, s: dict[str, Any], snap: Snapshot) -> dict[str, Any]:
        out = {"stock_id": s["stock_id"], "bin_code": s["bin_code"], "condition": s["condition"],
               "qty": s["qty"] or 0}
        b = self._find(snap, "Bins", "bin_code", s["bin_code"])
        if b is not None and (b.get("kind") or "bin") != "bin":
            out["place"] = b.get("kind")
            if b.get("description"):
                out["place_name"] = b["description"]
        if s.get("donor_vehicle_id"):
            out["donor_vehicle"] = self.vehicle_label(self._find(snap, "Vehicles", "vehicle_id", s["donor_vehicle_id"])) or s["donor_vehicle_id"]
        if s.get("notes"):
            out["notes"] = s["notes"]
        return out

    def _fitment_view(self, f: dict[str, Any]) -> dict[str, Any]:
        yrs = ""
        if f["year_from"] or f["year_to"]:
            a, b = f["year_from"] or "", f["year_to"] or ""
            yrs = str(a) if a == b else f"{a}-{b}"
        desc = " ".join(str(x) for x in (yrs, f["make"], f["model"] or "(all models)", f["trim"], f["engine"]) if x)
        return {"fitment_id": f["fitment_id"], "fits": desc, "verified": f["verified"] or UNVERIFIED,
                **({"notes": f["notes"]} if f.get("notes") else {}),
                **({"source": f["source"]} if f.get("source") else {})}

    def part_view(self, p: dict[str, Any], snap: Snapshot, include_empty: bool = False) -> dict[str, Any]:
        stock = [s for s in snap["Stock"] if s["part_id"] == p["part_id"] and (include_empty or (s["qty"] or 0) > 0)]
        fit = [f for f in snap["Fitment"] if f["part_id"] == p["part_id"]]
        inst = [i for i in snap["Installations"] if i["part_id"] == p["part_id"]]
        out = {
            "part_id": p["part_id"],
            "name": p["name"],
            "status": p["status"] or IDENTIFIED,
            "side_position": p["side_position"],
            "label_name": label_name(p),
        }
        for k in ("manufacturer", "oem_number", "stamped_number", "category", "notes"):
            if p.get(k):
                out[k] = p[k]
        out["photo_links"] = [x for x in (p.get("photo_links") or "").split("\n") if x.strip()]
        out["fitment_scope"] = p["fitment_scope"] or SCOPE_UNKNOWN
        out["fitment"] = [self._fitment_view(f) for f in fit]
        out["stock"] = [self._stock_view(s, snap) for s in stock]
        out["total_qty"] = sum((s["qty"] or 0) for s in stock)
        out["total_by_condition"] = {}
        for s in stock:
            out["total_by_condition"][s["condition"]] = out["total_by_condition"].get(s["condition"], 0) + (s["qty"] or 0)
        away = [c for c in snap.get("Custody", []) if c["part_id"] == p["part_id"]
                and (c["qty_out"] or 0) > (c["qty_returned"] or 0)]
        out["in_shop_qty"] = out["total_qty"]
        out["away_qty"] = sum((c["qty_out"] or 0) - (c["qty_returned"] or 0) for c in away)
        if away:
            out["away"] = [custody_view(c) for c in away]
        if inst:
            out["installed_on"] = [
                {"vehicle": self.vehicle_label(self._find(snap, "Vehicles", "vehicle_id", i["vehicle_id"])) or i["vehicle_id"],
                 "qty": i["qty"], "installed_at": i["installed_at"], "install_id": i["install_id"]}
                for i in inst
            ]
        return out

    def _request_label(self, kind: str, request_id: str, part: dict[str, Any], bin_code: str,
                       copies: int = 1, send: bool | None = None) -> dict[str, Any]:
        if self.labels is None:
            return {"status": "not_configured"}
        try:
            return self.labels.request(kind, request_id, part["part_id"], label_name(part), bin_code, copies, send)
        except Exception as e:  # noqa: BLE001 - inventory change already succeeded
            return {"status": "error", "error": str(e),
                    "message": "Inventory was saved, but the label could not be queued. Retry with print_label."}

    # ======================================================================
    # queries
    # ======================================================================
    def get_part(self, part_id: str) -> dict[str, Any]:
        snap = self.store.read()
        return self.part_view(self._part(snap, part_id), snap, include_empty=True)

    def list_bins(self) -> dict[str, Any]:
        snap = self.store.read()
        out = []
        for b in snap["Bins"]:
            rows = [s for s in snap["Stock"] if s["bin_code"] == b["bin_code"] and (s["qty"] or 0) > 0]
            out.append({"bin_code": b["bin_code"], "description": b["description"], "location": b["location"],
                        "kind": b.get("kind") or "bin", "entries": len(rows), "total_qty": sum(s["qty"] for s in rows)})
        return {"bins": out}

    def list_bin(self, bin_code: str) -> dict[str, Any]:
        snap = self.store.read()
        code = norm_bin(bin_code)
        b = self._bin(snap, code)
        items = []
        for s in snap["Stock"]:
            if s["bin_code"] == code and (s["qty"] or 0) > 0:
                p = self._find(snap, "Parts", "part_id", s["part_id"]) or {"name": "?", "side_position": ""}
                items.append({"part_id": s["part_id"], "name": p.get("name"), "side_position": p.get("side_position"),
                              **self._stock_view(s, snap)})
        return {"bin_code": code, "description": b["description"], "location": b["location"],
                "kind": b.get("kind") or "bin", "items": items, "total_qty": sum(i["qty"] for i in items)}

    def list_vehicles(self) -> dict[str, Any]:
        snap = self.store.read()
        return {"vehicles": [self._vehicle_view(v, snap) for v in snap["Vehicles"]]}

    def get_vehicle(self, vehicle_id: str) -> dict[str, Any]:
        snap = self.store.read()
        v = self._vehicle(snap, vehicle_id)
        out = self._vehicle_view(v, snap)
        out["installed_parts"] = [
            {"part_id": i["part_id"], "name": (self._find(snap, "Parts", "part_id", i["part_id"]) or {}).get("name"),
             "qty": i["qty"], "installed_at": i["installed_at"], "from_bin": i["bin_code"], "condition": i["condition"]}
            for i in snap["Installations"] if i["vehicle_id"] == v["vehicle_id"]
        ]
        out["donated_stock"] = [
            {"part_id": s["part_id"], "name": (self._find(snap, "Parts", "part_id", s["part_id"]) or {}).get("name"),
             **self._stock_view(s, snap)}
            for s in snap["Stock"] if s["donor_vehicle_id"] == v["vehicle_id"] and (s["qty"] or 0) > 0
        ]
        out["parts_that_fit"] = [r["part_id"] for r in self.search_parts(vehicle_id=v["vehicle_id"], limit=200, _snap=snap)["results"]
                                 if r.get("fitment_match") in ("verified", "unverified")]
        return out

    def _vehicle_view(self, v: dict[str, Any], snap: Snapshot) -> dict[str, Any]:
        out = {k: v[k] for k in ("vehicle_id", "nickname", "year", "make", "model", "trim", "engine", "vin", "notes") if v.get(k) not in ("", None)}
        out["label"] = self.vehicle_label(v)
        return out

    def history(self, part_id: str | None = None, bin_code: str | None = None, vehicle_id: str | None = None,
                request_id: str | None = None, limit: int = 30) -> dict[str, Any]:
        snap = self.store.read()
        rows = snap["Transactions"]
        if part_id:
            rows = [r for r in rows if r["part_id"].upper() == part_id.upper()]
        if bin_code:
            c = norm_bin(bin_code)
            rows = [r for r in rows if c in (r["bin_code"], r["related_bin"])]
        if vehicle_id:
            rows = [r for r in rows if r["vehicle_id"].upper() == vehicle_id.upper()]
        if request_id:
            rows = [r for r in rows if r["request_id"] == request_id]
        rows = rows[-limit:][::-1]
        return {"transactions": [{k: v for k, v in r.items() if k not in ("_row", "request_hash") and v not in ("", None)} for r in rows]}

    # ---------------------------------------------------------------- search
    def _fitment_match(self, part: dict, fits: list[dict], year: int | None, make: str, model: str) -> str | None:
        scope = part["fitment_scope"] or SCOPE_UNKNOWN
        if scope == SCOPE_UNIVERSAL:
            return "universal"
        best = None
        for f in fits:
            if make and f["make"].lower() != make.lower():
                continue
            if model and f["model"] and f["model"].lower() != model.lower():
                continue
            if year is not None:
                if f["year_from"] and year < f["year_from"]:
                    continue
                if f["year_to"] and year > f["year_to"]:
                    continue
            status = f["verified"] or UNVERIFIED
            if status == VERIFIED:
                return VERIFIED
            best = UNVERIFIED
        if best:
            return best
        if not fits:
            return "unknown"
        return None

    def search_parts(self, query: str = "", make: str | None = None, model: str | None = None,
                     year: int | None = None, vehicle_id: str | None = None, bin_code: str | None = None,
                     include_out_of_stock: bool = False, limit: int = 20, _snap: Snapshot | None = None) -> dict[str, Any]:
        snap = _snap or self.store.read()
        make, model = _s(make), _s(model)
        year = _year(year, "year") if year not in (None, "") else None
        interpreted: dict[str, Any] = {}
        if vehicle_id:
            v = self._vehicle(snap, vehicle_id)
            make, model, year = make or v["make"], model or v["model"], year or v["year"]
            interpreted["vehicle"] = self.vehicle_label(v)

        q_sides = side_set(query)
        raw_tokens = [t for t in re.findall(r"[a-z0-9/]+", (query or "").lower())]
        terms = []
        for t in raw_tokens:
            if t in STOPWORDS or t in SIDE_WORDS:
                continue
            if re.fullmatch(r"(18|19|20)\d\d", t) and year is None:
                year = int(t)
                continue
            terms.append(_singular(t))
        interpreted.update(terms=terms, side=sorted(q_sides), year=year, make=make or None, model=model or None)
        compact_q = re.sub(r"[^a-z0-9]", "", (query or "").lower())

        fit_by_part: dict[str, list[dict]] = {}
        for f in snap["Fitment"]:
            fit_by_part.setdefault(f["part_id"], []).append(f)
        bin_filter = norm_bin(bin_code) if bin_code else None

        scored = []
        for p in snap["Parts"]:
            fits = fit_by_part.get(p["part_id"], [])
            p_sides = side_set(p["side_position"]) | side_set(p["name"])
            # Exclude explicit opposite sides (asking for left never returns right-hand parts).
            if any(SIDE_OPPOSITE.get(s) in p_sides and s not in p_sides for s in q_sides):
                continue
            fm = None
            if make or model or year is not None:
                fm = self._fitment_match(p, fits, year, make, model)
                if fm is None:
                    continue
            name_toks = set(_tokens(p["name"]))
            other = " ".join([p["category"], p["notes"], p["manufacturer"], p["oem_number"], p["stamped_number"],
                              p["part_id"], p["status"]] + [f"{f['make']} {f['model']} {f['trim']} {f['engine']}" for f in fits])
            other_toks = set(_tokens(other))
            score, hits = 0.0, 0
            for t in terms:
                if any(n == t or (len(t) >= 3 and n.startswith(t)) for n in name_toks):
                    score += 3
                    hits += 1
                elif any(n == t or (len(t) >= 3 and n.startswith(t)) for n in other_toks):
                    score += 1
                    hits += 1
            for code in (p["oem_number"], p["stamped_number"], p["part_id"]):
                c = re.sub(r"[^a-z0-9]", "", (code or "").lower())
                if c and len(c) >= 4 and c in compact_q:
                    score += 6
                    hits = len(terms)
            if terms and hits == 0:
                continue
            quality = "full" if hits == len(terms) else "partial"
            if quality == "partial" and hits < max(1, len(terms) // 2):
                continue
            if q_sides:
                if q_sides <= p_sides:
                    score += 2
                elif not p_sides:
                    quality = "side_unspecified" if quality == "full" else quality
            if fm == VERIFIED:
                score += 1
            view = self.part_view(p, snap)
            if bin_filter:
                view["stock"] = [s for s in view["stock"] if s["bin_code"] == bin_filter]
                view["total_qty"] = sum(s["qty"] for s in view["stock"])
                if not view["stock"]:
                    continue
            view["match"] = quality
            if fm:
                view["fitment_match"] = fm
            # Head noun: "door hinges" asks for hinges, not hinge *bolts*. The query's last term should be
            # the last word of the name's main part (before a comma/parenthesis).
            head_toks = _tokens(re.split(r"[,(]", p["name"])[0])
            view["_head"] = bool(terms) and bool(head_toks) and (
                head_toks[-1] == terms[-1] or (len(terms[-1]) >= 4 and head_toks[-1].startswith(terms[-1])))
            rank = {"full": 0, "side_unspecified": 1, "partial": 2}[quality]
            scored.append((rank, -score, -view["total_qty"], p["part_id"], view))
        scored.sort(key=lambda x: x[:4])
        results = [x[-1] for x in scored]
        in_stock = [r for r in results if r["total_qty"] > 0]
        note = None
        if not include_out_of_stock:
            if in_stock:
                results = in_stock
            elif results:
                note = "No matching parts are in stock; showing matching records with zero quantity."
        if any(r["match"] != "partial" for r in results):
            results = [r for r in results if r["match"] != "partial"]  # drop weak matches when good ones exist
        related = []
        if any(r["_head"] for r in results) and not all(r["_head"] for r in results):
            related = [{"part_id": r["part_id"], "name": r["label_name"], "total_qty": r["total_qty"]}
                       for r in results if not r["_head"]]
            results = [r for r in results if r["_head"]]
        for r in results:
            r.pop("_head", None)
        out = {"query": query, "interpreted": interpreted, "count": len(results), "results": results[:limit]}
        if related:
            out["related"] = related[:10]
        if note:
            out["note"] = note
        if not results:
            out["note"] = "No matching parts found."
        return out

    # ======================================================================
    # mutations
    # ======================================================================
    def create_bin(self, request_id: str, bin_code: str, description: str = "", location: str = "",
                   kind: str = "bin") -> dict[str, Any]:
        """A place in the shop: a bin/box/bag (kind=bin) or a work area such as the bench (kind=work_area)."""
        code = norm_bin(bin_code)
        kind = _s(kind).lower().replace(" ", "_") or "bin"
        if kind not in BIN_KINDS:
            raise InventoryError(f"kind must be one of {BIN_KINDS}")
        params = {"bin_code": code, "description": _s(description), "location": _s(location)}
        if kind != "bin":
            params["kind"] = kind  # (kept out of the fingerprint for plain bins: retries from older clients match)

        def build(ctx: _Ctx):
            if self._find(ctx.snap, "Bins", "bin_code", code):
                raise InventoryError(f"bin {code} already exists", bin_code=code)
            ctx.append("Bins", {"bin_code": code, "description": params["description"],
                                "location": params["location"], "created_at": ctx.now, "kind": kind})
            ctx.txn("create_bin", bin_code=code, notes=params["description"])
            return {"status": "created", "bin_code": code}

        return self._mutate("create_bin", request_id, params, build,
                            lambda prior, snap: {"status": "created", "bin_code": code})

    def add_vehicle(self, request_id: str, year: int, make: str, model: str, nickname: str = "",
                    trim: str = "", engine: str = "", vin: str = "", notes: str = "") -> dict[str, Any]:
        y = _year(year, "year")
        if y is None or not _s(make) or not _s(model):
            raise InventoryError("year, make and model are required for a vehicle")
        vin_n = re.sub(r"\s", "", _s(vin)).upper()
        if vin_n and (len(vin_n) > 17 or not re.fullmatch(r"[A-HJ-NPR-Z0-9]+", vin_n)):
            raise InventoryError("VIN may only contain letters (not I, O, Q) and digits, max 17 characters")
        params = {"year": y, "make": _s(make), "model": _s(model), "nickname": _s(nickname), "trim": _s(trim),
                  "engine": _s(engine), "vin": vin_n, "notes": _s(notes)}

        def build(ctx: _Ctx):
            if vin_n:
                dup = self._find(ctx.snap, "Vehicles", "vin", vin_n)
                if dup:
                    raise InventoryError(f"a vehicle with VIN {vin_n} already exists: {self.vehicle_label(dup)}",
                                         vehicle_id=dup["vehicle_id"])
            vid = ctx.next_id("Vehicles", "vehicle_id", "V", 4)
            rec = {"vehicle_id": vid, **params, "created_at": ctx.now}
            ctx.append("Vehicles", rec)
            ctx.txn("add_vehicle", vehicle_id=vid, notes=self.vehicle_label(rec))
            return {"status": "created", "vehicle_id": vid, "vehicle": self.vehicle_label(rec)}

        def replay(prior, snap):
            v = self._find(snap, "Vehicles", "vehicle_id", prior[0]["vehicle_id"])
            return {"status": "created", "vehicle_id": prior[0]["vehicle_id"], "vehicle": self.vehicle_label(v)}

        return self._mutate("add_vehicle", request_id, params, build, replay)

    def _validate_fitment(self, f: dict[str, Any]) -> dict[str, Any]:
        make = _s(f.get("make"))
        if not make:
            raise InventoryError("each fitment entry needs a make (use fitment_scope='universal' for universal parts)")
        yf = _year(f.get("year_from", f.get("year")), "year_from")
        yt = _year(f.get("year_to", f.get("year", f.get("year_from"))), "year_to")
        if yf and yt and yf > yt:
            raise InventoryError(f"year_from {yf} is after year_to {yt}")
        v = f.get("verified", False)
        verified = VERIFIED if (v is True or _s(v).lower() in ("true", "yes", "verified")) else UNVERIFIED
        return {"make": make, "model": _s(f.get("model")), "year_from": yf, "year_to": yt,
                "trim": _s(f.get("trim")), "engine": _s(f.get("engine")), "verified": verified,
                "source": _s(f.get("source")), "notes": _s(f.get("notes"))}

    def _append_fitment(self, ctx: _Ctx, part_id: str, f: dict[str, Any]) -> str:
        fid = ctx.next_id("Fitment", "fitment_id", "F", 5)
        ctx.append("Fitment", {"fitment_id": fid, "part_id": part_id, **f, "created_at": ctx.now})
        ctx.txn("add_fitment", part_id=part_id,
                notes=f"{fid}: {f['year_from'] or ''}-{f['year_to'] or ''} {f['make']} {f['model']} ({f['verified']})")
        return fid

    def _new_part_record(self, ctx: _Ctx, np: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        unidentified = bool(np.get("unidentified"))
        name = _s(np.get("name"))
        if not name:
            if not unidentified:
                raise InventoryError("new_part.name is required (or set unidentified=true)")
            name = "Unidentified part"
        fit = [self._validate_fitment(f) for f in (np.get("fitment") or [])]
        scope = _s(np.get("fitment_scope")).lower() or (SCOPE_SPECIFIC if fit else SCOPE_UNKNOWN)
        if scope not in SCOPES:
            raise InventoryError(f"fitment_scope must be one of {sorted(SCOPES)}")
        if scope == SCOPE_UNIVERSAL and fit:
            raise InventoryError("a universal part should not also list specific fitment entries")
        if scope == SCOPE_SPECIFIC and not fit:
            scope = SCOPE_UNKNOWN
        oem = _s(np.get("oem_number"))
        if oem:
            norm = re.sub(r"[^A-Z0-9]", "", oem.upper())
            for p in ctx.snap["Parts"]:
                if norm and re.sub(r"[^A-Z0-9]", "", p["oem_number"].upper()) == norm and not np.get("allow_duplicate"):
                    raise InventoryError(
                        f"part {p['part_id']} ({p['name']}) already has OEM number {p['oem_number']}. "
                        "Use part_id to add stock to it, or pass new_part.allow_duplicate=true if it is really different.",
                        existing_part_id=p["part_id"],
                    )
        pid = ctx.next_id("Parts", "part_id", "P", 5)
        rec = {
            "part_id": pid, "name": name, "status": UNIDENTIFIED if unidentified else IDENTIFIED,
            "side_position": _s(np.get("side_position")), "manufacturer": _s(np.get("manufacturer")),
            "oem_number": oem, "stamped_number": _s(np.get("stamped_number")), "fitment_scope": scope,
            "category": _s(np.get("category")), "notes": _s(np.get("notes")), "photo_links": "",
            "created_at": ctx.now, "updated_at": ctx.now,
        }
        return rec, fit

    def _stock_row(self, snap: Snapshot, part_id: str, bin_code: str, condition: str, donor: str) -> dict[str, Any] | None:
        for s in snap["Stock"]:
            if (s["part_id"] == part_id and s["bin_code"] == bin_code and s["condition"] == condition
                    and _s(s["donor_vehicle_id"]) == donor):
                return s
        return None

    def receive_stock(self, request_id: str, bin_code: str, qty: int = 1, condition: str = "used",
                      part_id: str | None = None, new_part: dict[str, Any] | None = None,
                      donor_vehicle_id: str | None = None, notes: str = "",
                      photo_path: str | None = None, photo_base64: str | None = None,
                      photo_drive_ref: str | None = None, print_label: bool = True,
                      create_bin_if_missing: bool = False, removed_from: str = "",
                      _kind: str = "receive_stock", _extra=None, _extra_params: dict | None = None,
                      _extra_replay=None, _fingerprint: dict | None = None) -> dict[str, Any]:
        """Save stock for an existing or new part, then (optionally) queue its label.

        ``_extra(ctx, part, stock_id, photo)`` lets the rebuild workflow add job/photo/activity rows
        to the same atomic commit (teardown removals)."""
        code = norm_bin(bin_code)
        q = _int(qty, "qty", 1)
        cond = norm_condition(condition)
        if bool(part_id) == bool(new_part):
            raise InventoryError("give exactly one of part_id (existing part) or new_part (new record)")
        donor = _s(donor_vehicle_id).upper()
        params = {
            "bin": code, "qty": q, "condition": cond, "part_id": _s(part_id).upper(), "new_part": new_part or {},
            "donor": donor, "notes": _s(notes), "label": bool(print_label), "removed_from": _s(removed_from),
            **(_extra_params or {}),
            # The photo source is deliberately NOT part of the fingerprint: a client retry may
            # hand over the same photo via a different temp path. Photo saves are keyed by request_id.
        }
        has_photo = bool(photo_path or photo_base64 or photo_drive_ref)
        if has_photo and self.photos is None:
            raise InventoryError("photo storage is not configured")

        def build(ctx: _Ctx):
            snap = ctx.snap
            if not self._find(snap, "Bins", "bin_code", code):
                if not create_bin_if_missing:
                    self._bin(snap, code)  # raises with guidance
                ctx.append("Bins", {"bin_code": code, "description": "", "location": "", "created_at": ctx.now})
                ctx.txn("create_bin", bin_code=code, notes="created during intake")
            if donor:
                self._vehicle(snap, donor)
            created = False
            if new_part:
                part, fit = self._new_part_record(ctx, new_part)
                created = True
            else:
                part, fit = self._part(snap, part_id), []
            photo = None
            if has_photo:
                photo = self.photos.save(ctx.request_id, part["part_id"], path=photo_path,
                                         base64_data=photo_base64, drive_ref=photo_drive_ref)
                links = [x for x in (part.get("photo_links") or "").split("\n") if x.strip()]
                if photo["link"] not in links:
                    links.append(photo["link"])
                if created:
                    part["photo_links"] = "\n".join(links)
                else:
                    ctx.update("Parts", part, {"photo_links": "\n".join(links), "updated_at": ctx.now})
            if created:
                ctx.append("Parts", part)
                ctx.txn("create_part", part_id=part["part_id"], notes=f"{part['name']} ({part['status']})")
                for f in fit:
                    self._append_fitment(ctx, part["part_id"], f)
            row = self._stock_row(snap, part["part_id"], code, cond, donor)
            if row:
                after = (row["qty"] or 0) + q
                ctx.update("Stock", row, {"qty": after, "updated_at": ctx.now})
                sid = row["stock_id"]
            else:
                sid = ctx.next_id("Stock", "stock_id", "S", 5)
                after = q
                ctx.append("Stock", {"stock_id": sid, "part_id": part["part_id"], "bin_code": code, "condition": cond,
                                     "qty": q, "donor_vehicle_id": donor, "notes": _s(notes), "updated_at": ctx.now,
                                     "removed_from": _s(removed_from)})
            ctx.txn("receive", part_id=part["part_id"], stock_id=sid, bin_code=code, qty_delta=q, qty_after=after,
                    condition=cond, vehicle_id=donor,
                    notes=_s(notes) or (f"removed from {removed_from}" if removed_from else ""))
            out = {"part": part, "stock_id": sid, "qty_after": after, "created_part": created, "photo": photo}
            if _extra:
                out["extra"] = _extra(ctx, part, sid, photo)
            return out

        def replay(prior, snap):
            rec = next(t for t in prior if t["type"] == "receive")
            part = self._find(snap, "Parts", "part_id", rec["part_id"])
            out = {"part": part, "stock_id": rec["stock_id"], "qty_after": rec["qty_after"],
                   "created_part": any(t["type"] == "create_part" for t in prior), "photo": None}
            if _extra_replay:
                out["extra"] = _extra_replay(prior, snap)
            return out

        # _fingerprint: callers whose derived arguments change after the first success (e.g. a shopping item that
        # got linked to the part it created) fingerprint the caller's own request so a retry still matches.
        out = self._mutate(_kind, request_id, _fingerprint if _fingerprint is not None else params, build, replay)
        part = out.pop("part")
        snap = self.store.read()
        res = {
            "status": "received",
            "message": out.get("message", "") + f"Saved {q} x {part['name']} ({cond}) in bin {code}.",
            "part": self.part_view(self._part(snap, part["part_id"]), snap),
            "stock_id": out["stock_id"], "bin_code": code, "qty_received": q, "condition": cond,
            "qty_in_entry_after_receipt": out["qty_after"], "created_part": out["created_part"],
            "request_id": out["request_id"] if "request_id" in out else _s(request_id),
            "transaction_ids": out["transaction_ids"], "duplicate_request": out["duplicate_request"],
        }
        if out.get("photo"):
            res["photo"] = out["photo"]
        if out.get("activity_ids"):
            res["activity_ids"] = out["activity_ids"]
        if out.get("extra"):
            res.update(out["extra"])
        res["label"] = (self._request_label("intake", _s(request_id), part, code)
                        if print_label else {"requested": False})
        return res

    def _pick_source(self, snap: Snapshot, part_id: str, bin_code: str | None, condition: str | None,
                     stock_id: str | None, donor: str | None = None) -> dict[str, Any]:
        if stock_id:
            s = self._find(snap, "Stock", "stock_id", stock_id)
            if s is None:
                raise InventoryError(f"no stock entry {stock_id}")
            if part_id and s["part_id"] != part_id.upper():
                raise InventoryError(f"stock entry {stock_id} is part {s['part_id']}, not {part_id}")
            return s
        cands = [s for s in snap["Stock"] if s["part_id"] == part_id.upper() and (s["qty"] or 0) > 0]
        where = [self._stock_view(s, snap) for s in cands]
        if bin_code:
            cands = [s for s in cands if s["bin_code"] == bin_code]
        if condition:
            cands = [s for s in cands if s["condition"] == condition]
        if donor:
            cands = [s for s in cands if s["donor_vehicle_id"] == donor]
        if not cands:
            raise InventoryError(f"part {part_id} has no matching stock" + (f" in bin {bin_code}" if bin_code else ""),
                                 stock_on_hand=where)
        if len(cands) > 1:
            raise InventoryError("more than one stock entry matches; pass stock_id (or bin_code/condition) to choose",
                                 options=[self._stock_view(s, snap) for s in cands])
        return cands[0]

    def move_stock(self, request_id: str, part_id: str, from_bin: str, to_bin: str, qty: int = 1,
                   condition: str | None = None, stock_id: str | None = None,
                   print_label: bool = False) -> dict[str, Any]:
        src_code, dst_code = norm_bin(from_bin), norm_bin(to_bin)
        if src_code == dst_code:
            raise InventoryError("from_bin and to_bin are the same")
        q = _int(qty, "qty", 1)
        cond = norm_condition(condition) if condition else None
        params = {"part_id": _s(part_id).upper(), "from": src_code, "to": dst_code, "qty": q,
                  "condition": cond, "stock_id": _s(stock_id).upper(), "label": bool(print_label)}

        def build(ctx: _Ctx):
            snap = ctx.snap
            part = self._part(snap, part_id)
            self._bin(snap, dst_code)
            src = self._pick_source(snap, part["part_id"], src_code, cond, stock_id)
            if src["bin_code"] != src_code:
                raise InventoryError(f"stock entry {src['stock_id']} is in {src['bin_code']}, not {src_code}")
            have = src["qty"] or 0
            if q > have:
                raise InventoryError(f"only {have} in {src_code} ({src['condition']}); cannot move {q}")
            ctx.update("Stock", src, {"qty": have - q, "updated_at": ctx.now})
            dst = self._stock_row(snap, part["part_id"], dst_code, src["condition"], _s(src["donor_vehicle_id"]))
            if dst:
                dst_after = (dst["qty"] or 0) + q
                ctx.update("Stock", dst, {"qty": dst_after, "updated_at": ctx.now})
                dst_id = dst["stock_id"]
            else:
                dst_id = ctx.next_id("Stock", "stock_id", "S", 5)
                dst_after = q
                ctx.append("Stock", {"stock_id": dst_id, "part_id": part["part_id"], "bin_code": dst_code,
                                     "condition": src["condition"], "qty": q, "donor_vehicle_id": src["donor_vehicle_id"],
                                     "notes": src["notes"], "updated_at": ctx.now})
            ctx.txn("move_out", part_id=part["part_id"], stock_id=src["stock_id"], bin_code=src_code, qty_delta=-q,
                    qty_after=have - q, condition=src["condition"], related_bin=dst_code)
            ctx.txn("move_in", part_id=part["part_id"], stock_id=dst_id, bin_code=dst_code, qty_delta=q,
                    qty_after=dst_after, condition=src["condition"], related_bin=src_code)
            return {"part": part, "from": {"bin_code": src_code, "stock_id": src["stock_id"], "qty_after": have - q},
                    "to": {"bin_code": dst_code, "stock_id": dst_id, "qty_after": dst_after}, "condition": src["condition"]}

        def replay(prior, snap):
            o = next(t for t in prior if t["type"] == "move_out")
            i = next(t for t in prior if t["type"] == "move_in")
            return {"part": self._find(snap, "Parts", "part_id", o["part_id"]),
                    "from": {"bin_code": o["bin_code"], "stock_id": o["stock_id"], "qty_after": o["qty_after"]},
                    "to": {"bin_code": i["bin_code"], "stock_id": i["stock_id"], "qty_after": i["qty_after"]},
                    "condition": o["condition"]}

        out = self._mutate("move_stock", request_id, params, build, replay)
        part = out.pop("part")
        out["status"] = "moved"
        out["message"] = out.get("message", "") + f"Moved {q} x {part['name']} from {src_code} to {dst_code}."
        out["part_id"] = part["part_id"]
        out["qty_moved"] = q
        if print_label:
            out["label"] = self._request_label("move", _s(request_id), part, dst_code, send=True)
        return out

    def adjust_count(self, request_id: str, reason: str, stock_id: str | None = None, part_id: str | None = None,
                     bin_code: str | None = None, condition: str | None = None, new_qty: int | None = None,
                     delta: int | None = None) -> dict[str, Any]:
        if (new_qty is None) == (delta is None):
            raise InventoryError("give exactly one of new_qty (counted quantity) or delta (+/- change)")
        if not _s(reason):
            raise InventoryError("reason is required for a count correction (e.g. 'cycle count', 'found damaged')")
        nq = _int(new_qty, "new_qty", 0) if new_qty is not None else None
        dl = _int(delta, "delta") if delta is not None else None
        code = norm_bin(bin_code) if bin_code else None
        cond = norm_condition(condition) if condition else None
        params = {"stock_id": _s(stock_id).upper(), "part_id": _s(part_id).upper(), "bin": code, "condition": cond,
                  "new_qty": nq, "delta": dl, "reason": _s(reason)}

        def build(ctx: _Ctx):
            snap = ctx.snap
            row = None
            if stock_id:
                row = self._pick_source(snap, part_id or "", None, None, stock_id)
            else:
                if not part_id or not code:
                    raise InventoryError("give stock_id, or part_id + bin_code (+ condition)")
                part = self._part(snap, part_id)
                self._bin(snap, code)
                rows = [s for s in snap["Stock"] if s["part_id"] == part["part_id"] and s["bin_code"] == code
                        and (cond is None or s["condition"] == cond)]
                if len(rows) > 1:
                    nonzero = [s for s in rows if (s["qty"] or 0) > 0]
                    rows = nonzero if len(nonzero) == 1 else rows
                if len(rows) > 1:
                    raise InventoryError("several stock entries match; pass stock_id",
                                         options=[self._stock_view(s, snap) for s in rows])
                row = rows[0] if rows else None
            before = (row["qty"] or 0) if row else 0
            after = nq if nq is not None else before + dl
            if after < 0:
                raise InventoryError(f"count would go negative ({before} {dl:+d})")
            if row:
                ctx.update("Stock", row, {"qty": after, "updated_at": ctx.now})
                sid, b, c, pid = row["stock_id"], row["bin_code"], row["condition"], row["part_id"]
            else:
                if after == 0:
                    raise InventoryError("no stock entry exists there and the new count is 0; nothing to do")
                c = cond or "used"
                sid = ctx.next_id("Stock", "stock_id", "S", 5)
                b, pid = code, part["part_id"]
                ctx.append("Stock", {"stock_id": sid, "part_id": pid, "bin_code": b, "condition": c, "qty": after,
                                     "donor_vehicle_id": "", "notes": "", "updated_at": ctx.now})
            ctx.txn("adjust", part_id=pid, stock_id=sid, bin_code=b, qty_delta=after - before, qty_after=after,
                    condition=c, notes=_s(reason))
            return {"stock_id": sid, "part_id": pid, "bin_code": b, "condition": c, "qty_before": before, "qty_after": after}

        def replay(prior, snap):
            t = prior[0]
            return {"stock_id": t["stock_id"], "part_id": t["part_id"], "bin_code": t["bin_code"],
                    "condition": t["condition"], "qty_before": (t["qty_after"] or 0) - (t["qty_delta"] or 0),
                    "qty_after": t["qty_after"]}

        out = self._mutate("adjust_count", request_id, params, build, replay)
        out["status"] = "adjusted"
        out["message"] = out.get("message", "") + f"Count for {out['part_id']} in {out['bin_code']} is now {out['qty_after']}."
        return out

    def record_installation(self, request_id: str, part_id: str, vehicle_id: str, qty: int = 1,
                            bin_code: str | None = None, condition: str | None = None, stock_id: str | None = None,
                            take_from_stock: bool = True, notes: str = "", installed_on: str | None = None,
                            job_id: str = "", _kind: str = "record_installation", _extra=None,
                            _extra_params: dict | None = None, _extra_replay=None) -> dict[str, Any]:
        q = _int(qty, "qty", 1)
        code = norm_bin(bin_code) if bin_code else None
        cond = norm_condition(condition) if condition else None
        params = {"part_id": _s(part_id).upper(), "vehicle_id": _s(vehicle_id).upper(), "qty": q, "bin": code,
                  "condition": cond, "stock_id": _s(stock_id).upper(), "take": bool(take_from_stock),
                  "notes": _s(notes), "installed_on": _s(installed_on), "job_id": _s(job_id),
                  **(_extra_params or {})}

        def build(ctx: _Ctx):
            snap = ctx.snap
            part = self._part(snap, part_id)
            veh = self._vehicle(snap, vehicle_id)
            when = _s(installed_on) or ctx.now
            iid = ctx.next_id("Installations", "install_id", "I", 5)
            if take_from_stock:
                src = self._pick_source(snap, part["part_id"], code, cond, stock_id)
                have = src["qty"] or 0
                if q > have:
                    raise InventoryError(f"only {have} in {src['bin_code']}; cannot install {q} from stock")
                ctx.update("Stock", src, {"qty": have - q, "updated_at": ctx.now})
                ctx.append("Installations", {"install_id": iid, "part_id": part["part_id"], "vehicle_id": veh["vehicle_id"],
                                             "qty": q, "stock_id": src["stock_id"], "bin_code": src["bin_code"],
                                             "condition": src["condition"], "donor_vehicle_id": src["donor_vehicle_id"],
                                             "installed_at": when, "notes": _s(notes), "request_id": ctx.request_id,
                                             "job_id": _s(job_id)})
                ctx.txn("install", part_id=part["part_id"], stock_id=src["stock_id"], bin_code=src["bin_code"],
                        qty_delta=-q, qty_after=have - q, condition=src["condition"], vehicle_id=veh["vehicle_id"],
                        notes=f"{iid} {_s(notes)}".strip())
                from_bin, left = src["bin_code"], have - q
            else:
                ctx.append("Installations", {"install_id": iid, "part_id": part["part_id"], "vehicle_id": veh["vehicle_id"],
                                             "qty": q, "stock_id": "", "bin_code": "", "condition": cond or "",
                                             "donor_vehicle_id": "", "installed_at": when, "notes": _s(notes),
                                             "request_id": ctx.request_id, "job_id": _s(job_id)})
                ctx.txn("install", part_id=part["part_id"], qty_delta=0, vehicle_id=veh["vehicle_id"],
                        notes=f"{iid} not taken from stock {_s(notes)}".strip())
                from_bin, left = None, None
            out = {"install_id": iid, "part": part, "vehicle": self.vehicle_label(veh), "from_bin": from_bin,
                   "qty_left_in_bin": left, "vehicle_id": veh["vehicle_id"]}
            if _extra:
                out.update(_extra(ctx, part, veh, iid) or {})
            return out

        def replay(prior, snap):
            t = next(x for x in prior if x.get("type") == "install")
            iid = t["notes"].split()[0]
            out = {"install_id": iid, "part": self._find(snap, "Parts", "part_id", t["part_id"]),
                   "vehicle": self.vehicle_label(self._find(snap, "Vehicles", "vehicle_id", t["vehicle_id"])),
                   "from_bin": t["bin_code"] or None, "qty_left_in_bin": t["qty_after"], "vehicle_id": t["vehicle_id"]}
            if _extra_replay:
                out.update(_extra_replay(prior, snap) or {})
            return out

        out = self._mutate(_kind, request_id, params, build, replay)
        part = out.pop("part")
        out.update(status="installed", part_id=part["part_id"], qty=q)
        out["message"] = out.get("message", "") + f"Recorded {q} x {part['name']} installed on {out['vehicle']}."
        return out

    def add_fitment(self, request_id: str, part_id: str, make: str, model: str = "", year_from: int | None = None,
                    year_to: int | None = None, trim: str = "", engine: str = "", verified: bool = False,
                    source: str = "", notes: str = "") -> dict[str, Any]:
        f = self._validate_fitment({"make": make, "model": model, "year_from": year_from, "year_to": year_to,
                                    "trim": trim, "engine": engine, "verified": verified, "source": source, "notes": notes})
        params = {"part_id": _s(part_id).upper(), **f}

        def build(ctx: _Ctx):
            part = self._part(ctx.snap, part_id)
            if part["fitment_scope"] == SCOPE_UNIVERSAL:
                raise InventoryError(f"{part['part_id']} is marked universal; change fitment_scope with update_part first")
            fid = self._append_fitment(ctx, part["part_id"], f)
            if part["fitment_scope"] != SCOPE_SPECIFIC:
                ctx.update("Parts", part, {"fitment_scope": SCOPE_SPECIFIC, "updated_at": ctx.now})
            return {"status": "added", "fitment_id": fid, "part_id": part["part_id"]}

        def replay(prior, snap):
            return {"status": "added", "fitment_id": prior[0]["notes"].split(":")[0], "part_id": prior[0]["part_id"]}

        return self._mutate("add_fitment", request_id, params, build, replay)

    def set_fitment_verified(self, request_id: str, fitment_id: str, verified: bool) -> dict[str, Any]:
        status = VERIFIED if verified else UNVERIFIED
        params = {"fitment_id": _s(fitment_id).upper(), "verified": status}

        def build(ctx: _Ctx):
            f = self._find(ctx.snap, "Fitment", "fitment_id", fitment_id)
            if f is None:
                raise InventoryError(f"no fitment entry {fitment_id}")
            ctx.update("Fitment", f, {"verified": status})
            ctx.txn("update_fitment", part_id=f["part_id"], notes=f"{f['fitment_id']} -> {status}")
            return {"status": "updated", "fitment_id": f["fitment_id"], "verified": status}

        return self._mutate("set_fitment_verified", request_id, params, build,
                            lambda prior, snap: {"status": "updated", "fitment_id": params["fitment_id"], "verified": status})

    PART_FIELDS = ("name", "side_position", "manufacturer", "oem_number", "stamped_number", "category", "notes",
                   "fitment_scope")

    def update_part(self, request_id: str, part_id: str, identified: bool | None = None,
                    photo_path: str | None = None, photo_base64: str | None = None, photo_drive_ref: str | None = None,
                    **fields: Any) -> dict[str, Any]:
        bad = set(fields) - set(self.PART_FIELDS)
        if bad:
            raise InventoryError(f"unknown part fields: {sorted(bad)}")
        changes = {k: _s(v) for k, v in fields.items() if v is not None}
        if "fitment_scope" in changes:
            changes["fitment_scope"] = changes["fitment_scope"].lower()
            if changes["fitment_scope"] not in SCOPES:
                raise InventoryError(f"fitment_scope must be one of {sorted(SCOPES)}")
        if identified is not None:
            changes["status"] = IDENTIFIED if identified else UNIDENTIFIED
        has_photo = bool(photo_path or photo_base64 or photo_drive_ref)
        if not changes and not has_photo:
            raise InventoryError("nothing to update")
        params = {"part_id": _s(part_id).upper(), **changes, "photo": has_photo}

        def build(ctx: _Ctx):
            part = self._part(ctx.snap, part_id)
            ch = dict(changes)
            if ch.get("fitment_scope") == SCOPE_UNIVERSAL and any(f["part_id"] == part["part_id"] for f in ctx.snap["Fitment"]):
                raise InventoryError("this part has specific fitment entries; it cannot also be universal")
            if has_photo:
                photo = self.photos.save(ctx.request_id, part["part_id"], path=photo_path,
                                         base64_data=photo_base64, drive_ref=photo_drive_ref)
                links = [x for x in (part.get("photo_links") or "").split("\n") if x.strip()]
                if photo["link"] not in links:
                    links.append(photo["link"])
                ch["photo_links"] = "\n".join(links)
            ch["updated_at"] = ctx.now
            ctx.update("Parts", part, ch)
            ctx.txn("update_part", part_id=part["part_id"],
                    notes=", ".join(f"{k}={v}" for k, v in ch.items() if k not in ("updated_at", "photo_links"))
                    + (" +photo" if has_photo else ""))
            return {"status": "updated", "part_id": part["part_id"]}

        out = self._mutate("update_part", request_id, params, build,
                           lambda prior, snap: {"status": "updated", "part_id": prior[0]["part_id"]})
        out["part"] = self.get_part(out["part_id"])
        return out

    # ======================================================================
    # labels
    # ======================================================================
    def print_label(self, request_id: str, part_id: str, bin_code: str | None = None, copies: int = 1) -> dict[str, Any]:
        """Print or reprint a label. Never changes inventory."""
        rid = _s(request_id)
        if not rid:
            raise InventoryError("request_id is required (reuse it only when retrying the same print request)")
        c = _int(copies, "copies", 1)
        if c > 20:
            raise InventoryError("copies is limited to 20 per request")
        snap = self.store.read()
        part = self._part(snap, part_id)
        bins = sorted({s["bin_code"] for s in snap["Stock"] if s["part_id"] == part["part_id"] and (s["qty"] or 0) > 0})
        if bin_code:
            code = norm_bin(bin_code)
            self._bin(snap, code)
        elif len(bins) == 1:
            code = bins[0]
        else:
            raise InventoryError(
                "which bin should the label show?" if bins else "this part is not in any bin; pass bin_code",
                bins_with_stock=bins,
            )
        warning = None
        if code not in bins:
            warning = f"{part['part_id']} currently has no stock in {code}"
        if self.labels is None:
            raise InventoryError("labels are not configured")
        job = self.labels.request("reprint", rid, part["part_id"], label_name(part), code, c, send=True)
        out = {"status": "label_requested", "label": job, "inventory_changed": False}
        if warning:
            out["warning"] = warning
        return out
