"""Persistent label job queue (SQLite on this PC, survives restarts).

Job states::

    pending    -> waiting for the printer (offline, live auto-print off, or not yet sent)
    sending    -> handed to b-PAC right now
    printed    -> b-PAC confirmed the job was sent to the printer
    previewed  -> preview mode: PNG rendered, nothing sent to a printer
    uncertain  -> the printer MAY have received the job; a human decides (reprint / mark_printed / cancel)
    failed     -> definitely not printed (e.g. template missing, wrong tape); fix and resolve
    cancelled

Each job is keyed by (kind, request_id), so retrying the same request never
creates a second job and never sends a second print.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

STATES = {"pending", "sending", "printed", "previewed", "uncertain", "failed", "cancelled"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class LabelConflict(ValueError):
    pass


class LabelQueue:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._tx() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    part_id TEXT NOT NULL,
                    part_name TEXT NOT NULL,
                    bin_code TEXT NOT NULL,
                    copies INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    preview_path TEXT NOT NULL DEFAULT '',
                    printer TEXT NOT NULL DEFAULT '',
                    tape_mm REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT '',
                    owner_pid INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(kind, request_id)
                )"""
            )

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            self._local.conn = c
        return c

    @contextmanager
    def _tx(self):
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    @staticmethod
    def job_id_for(kind: str, request_id: str) -> str:
        return "L-" + hashlib.sha256(f"{kind}:{request_id}".encode()).hexdigest()[:10].upper()

    def enqueue(self, kind: str, request_id: str, part_id: str, part_name: str,
                bin_code: str, copies: int = 1) -> tuple[dict[str, Any], bool]:
        """Create a job, or return the existing one for this request. Returns (job, created)."""
        job_id = self.job_id_for(kind, request_id)
        now = _now()
        with self._tx() as c:
            row = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is not None:
                job = dict(row)
                if (job["part_id"], job["bin_code"], job["copies"]) != (part_id, bin_code, copies):
                    raise LabelConflict(
                        f"request_id {request_id!r} was already used for a label of "
                        f"{job['part_id']} in {job['bin_code']}; use a new request_id"
                    )
                return job, False
            c.execute(
                "INSERT INTO jobs (job_id, kind, request_id, part_id, part_name, bin_code, copies, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, kind, request_id, part_id, part_name, bin_code, copies, "pending", now, now),
            )
            return dict(c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()), True

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def find(self, kind: str, request_id: str) -> dict[str, Any] | None:
        return self.get(self.job_id_for(kind, request_id))

    def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            rows = self._conn().execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC, rowid DESC LIMIT ?", (status, limit)
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self._conn().execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def pending_ids(self) -> list[str]:
        rows = self._conn().execute(
            "SELECT job_id FROM jobs WHERE status='pending' ORDER BY created_at, rowid"
        ).fetchall()
        return [r["job_id"] for r in rows]

    def claim(self, job_id: str, printer: str, tape_mm: float) -> bool:
        """Atomically move pending -> sending. Only one process can win."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE jobs SET status='sending', attempts=attempts+1, printer=?, tape_mm=?, updated_at=?, sent_at=?,"
                " owner_pid=? WHERE job_id=? AND status='pending'",
                (printer, tape_mm, _now(), _now(), os.getpid(), job_id),
            )
            return cur.rowcount == 1

    def set_status(self, job_id: str, status: str, error: str = "", only_from: str | None = None) -> None:
        assert status in STATES
        with self._tx() as c:
            if only_from:
                c.execute(
                    "UPDATE jobs SET status=?, last_error=?, updated_at=? WHERE job_id=? AND status=?",
                    (status, error, _now(), job_id, only_from),
                )
            else:
                c.execute(
                    "UPDATE jobs SET status=?, last_error=?, updated_at=? WHERE job_id=?",
                    (status, error, _now(), job_id),
                )

    def note_error(self, job_id: str, error: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE jobs SET last_error=?, updated_at=? WHERE job_id=?", (error, _now(), job_id))

    def set_preview(self, job_id: str, path: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE jobs SET preview_path=? WHERE job_id=?", (path, job_id))

    def recover(self) -> list[str]:
        """After a restart, a job left 'sending' by a dead process may or may not have printed.

        Jobs owned by a still-running process (e.g. the other AI client's server) are left alone.
        """
        stale = []
        with self._tx() as c:
            rows = c.execute("SELECT job_id, owner_pid FROM jobs WHERE status='sending'").fetchall()
            for r in rows:
                if r["owner_pid"] == os.getpid() or not _pid_alive(r["owner_pid"]):
                    stale.append(r["job_id"])
                    c.execute(
                        "UPDATE jobs SET status='uncertain', last_error=?, updated_at=? WHERE job_id=?",
                        ("interrupted while sending to the printer; check the printer output", _now(), r["job_id"]),
                    )
        return stale


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
