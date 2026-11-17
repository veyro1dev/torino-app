"""Label orchestration: queue + preview rendering + printer backend."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .printers import PrinterOffline, PrintFailed, PrintUncertain, make_backend
from .queue import LabelQueue
from .render import LabelContent, barcode_fits, render_label

MESSAGES = {
    "pending": "Queued; not sent to the printer yet.",
    "sending": "Being sent to the printer right now.",
    "printed": "Sent to the printer successfully.",
    "previewed": "Preview rendered (preview mode: nothing sent to a printer).",
    "uncertain": "UNCERTAIN: the printer may have received this job. Check the printer, then "
                 "resolve_label_job with action 'reprint', 'mark_printed' or 'cancel'.",
    "failed": "Not printed. Fix the problem, then resolve_label_job with action 'reprint' or 'cancel'.",
    "cancelled": "Cancelled.",
}


class LabelService:
    def __init__(self, queue: LabelQueue, printer_cfg: dict[str, Any], preview_dir: Path, backend=None):
        self.queue = queue
        self.cfg = printer_cfg
        self.preview_dir = Path(preview_dir)
        self.backend = backend or make_backend(printer_cfg)

    @property
    def live(self) -> bool:
        return bool(getattr(self.backend, "live", False))

    @property
    def auto_send(self) -> bool:
        """Should a new intake label be sent without being asked?"""
        return (not self.live) or bool(self.cfg.get("auto_print", False))

    def startup(self) -> dict[str, Any]:
        recovered = self.queue.recover()
        processed = self.process_pending() if (self.live and self.cfg.get("auto_print")) else []
        return {"marked_uncertain": recovered, "processed": processed}

    # -- public operations ---------------------------------------------------
    def request(self, kind: str, request_id: str, part_id: str, part_name: str, bin_code: str,
                copies: int = 1, send: bool | None = None) -> dict[str, Any]:
        job, created = self.queue.enqueue(kind, request_id, part_id, part_name, bin_code, copies)
        self._ensure_preview(job)
        if send is None:
            send = self.auto_send
        if send:
            if self.live and self.cfg.get("auto_print"):
                # flush older pending jobs first so labels come out in order
                for jid in self.queue.pending_ids():
                    if jid != job["job_id"]:
                        if self._dispatch(jid) == "offline":
                            break
            self._dispatch(job["job_id"])
        return self.view(self.queue.get(job["job_id"]), duplicate=not created)

    def process_pending(self) -> list[dict[str, Any]]:
        out = []
        for jid in self.queue.pending_ids():
            result = self._dispatch(jid)
            out.append(self.view(self.queue.get(jid)))
            if result == "offline":
                break
        return out

    def resolve(self, job_id: str, action: str) -> dict[str, Any]:
        job = self.queue.get(job_id)
        if job is None:
            raise ValueError(f"no label job {job_id}")
        st = job["status"]
        if action == "reprint":
            if st not in ("uncertain", "failed", "pending"):
                raise ValueError(
                    f"job {job_id} is {st}; to print another copy use print_label with a new request_id"
                )
            self.queue.set_status(job_id, "pending", "", only_from=st)
            self._dispatch(job_id)
        elif action == "mark_printed":
            if st not in ("uncertain", "failed", "pending"):
                raise ValueError(f"job {job_id} is {st}; nothing to mark")
            self.queue.set_status(job_id, "printed", "marked printed by user", only_from=st)
        elif action == "cancel":
            if st not in ("uncertain", "failed", "pending"):
                raise ValueError(f"job {job_id} is {st}; cannot cancel")
            self.queue.set_status(job_id, "cancelled", "cancelled by user", only_from=st)
        else:
            raise ValueError("action must be 'reprint', 'mark_printed' or 'cancel'")
        return self.view(self.queue.get(job_id))

    def status(self) -> dict[str, Any]:
        st = self.backend.status()
        st["tape_mm"] = self.cfg["tape_mm"]
        st["barcode_fits_tape"] = barcode_fits(self.cfg["tape_mm"])
        st["jobs_by_status"] = self.queue.counts()
        st["needs_attention"] = [self.view(j) for j in self.queue.list("uncertain")] + [
            self.view(j) for j in self.queue.list("failed")
        ]
        st["pending"] = [self.view(j) for j in self.queue.list("pending")]
        return st

    def jobs(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return [self.view(j) for j in self.queue.list(status, limit)]

    def view(self, job: dict[str, Any] | None, duplicate: bool = False) -> dict[str, Any]:
        if job is None:
            return {}
        v = {
            "job_id": job["job_id"],
            "kind": job["kind"],
            "request_id": job["request_id"],
            "status": job["status"],
            "message": MESSAGES.get(job["status"], ""),
            "part_id": job["part_id"],
            "part_name": job["part_name"],
            "bin_code": job["bin_code"],
            "copies": job["copies"],
            "attempts": job["attempts"],
            "preview_path": job["preview_path"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        }
        if job["last_error"]:
            v["last_error"] = job["last_error"]
        if job["printer"]:
            v["printer"] = job["printer"]
        if duplicate:
            v["duplicate_request"] = True
            v["message"] = "Same request seen before; no new label job was created. " + v["message"]
        return v

    # -- internals -------------------------------------------------------------
    def _ensure_preview(self, job: dict[str, Any]) -> None:
        if job["preview_path"] and Path(job["preview_path"]).exists():
            return
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", f"{job['job_id']}_{job['part_id']}_{job['bin_code']}")
        path = self.preview_dir / f"{safe}.png"
        render_label(LabelContent(job["part_name"], job["part_id"], job["bin_code"]), self.cfg["tape_mm"], path)
        self.queue.set_preview(job["job_id"], str(path))

    def _dispatch(self, job_id: str) -> str:
        job = self.queue.get(job_id)
        if job is None or job["status"] != "pending":
            return "skipped"
        if not self.queue.claim(job_id, self.cfg.get("queue", "") if self.live else "preview", self.cfg["tape_mm"]):
            return "skipped"  # another process claimed it
        if not self.live:
            self._ensure_preview(job)
            self.queue.set_status(job_id, "previewed", only_from="sending")
            return "previewed"
        content = LabelContent(job["part_name"], job["part_id"], job["bin_code"])
        try:
            self.backend.print_label(content, job["copies"])
        except PrinterOffline as e:
            self.queue.set_status(job_id, "pending", f"printer offline: {e}", only_from="sending")
            return "offline"
        except PrintFailed as e:
            self.queue.set_status(job_id, "failed", str(e), only_from="sending")
            return "failed"
        except PrintUncertain as e:
            self.queue.set_status(job_id, "uncertain", str(e), only_from="sending")
            return "uncertain"
        except Exception as e:  # noqa: BLE001 - unknown errors after claim are uncertain
            self.queue.set_status(job_id, "uncertain", f"unexpected error: {e}", only_from="sending")
            return "uncertain"
        self.queue.set_status(job_id, "printed", only_from="sending")
        return "printed"
