"""Tabular store abstraction.

A store holds the tabs listed in :mod:`torino_parts.schema`.  Reads return every
row of every tab as dicts (with a private ``_row`` key holding the 0-based data
row index).  Writes are expressed as a list of operations committed *atomically*:
either every append/update in the list lands, or none do.  That is what keeps a
stock change and its transaction-history row together.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from filelock import FileLock

from ..schema import NUMERIC, TABS


@dataclass
class Append:
    tab: str
    record: dict[str, Any]


@dataclass
class Update:
    tab: str
    row: int  # 0-based data row index (``_row`` from a read)
    record: dict[str, Any]


Snapshot = dict[str, list[dict[str, Any]]]


def normalize_cell(col: str, value: Any) -> Any:
    """Bring a cell read from storage into canonical python form."""
    if value is None:
        return "" if col not in NUMERIC else None
    if col in NUMERIC:
        if value == "":
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return int(f) if f.is_integer() else f
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


class StoreUnavailable(RuntimeError):
    """The backing store can't be reached or needs the user to sign in again."""


class Store(ABC):
    """Base class; subclasses implement ``_read`` and ``_commit``.

    ``read()`` may serve a short-lived cached snapshot (``cache_ttl`` seconds) so a
    desktop UI that refreshes every few seconds doesn't exhaust API quotas.
    Mutations always use ``read(fresh=True)`` under the lock.
    """

    cache_ttl: float = 0.0

    def __init__(self, lock_path: Path):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Cross-process lock: the desktop app, Codex and Claude Code may each run a server.
        # Per-thread file lock (filelock default) + an in-process lock: threads of the desktop
        # server are serialized here, other processes by the OS file lock.
        self._lock = FileLock(str(lock_path), timeout=60)
        self._tlock = threading.RLock()
        self._cache: tuple[float, Snapshot] | None = None
        self._cache_lock = threading.Lock()
        self.generation = 0  # bumps on every commit made by this process

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._tlock, self._lock:
            yield

    def read(self, fresh: bool = False) -> Snapshot:
        if not fresh and self.cache_ttl > 0:
            with self._cache_lock:
                if self._cache and time.monotonic() - self._cache[0] < self.cache_ttl:
                    return self._cache[1]
        snap = self._read_snapshot()
        if not fresh and self.cache_ttl > 0:
            with self._cache_lock:
                self._cache = (time.monotonic(), snap)
        return snap

    def invalidate(self) -> None:
        with self._cache_lock:
            self._cache = None

    def data_version(self) -> Any:
        """Cheap value that changes whenever any process commits (None: not available)."""
        return None

    def _read_snapshot(self) -> Snapshot:
        raw = self._read()
        snap: Snapshot = {}
        for tab, cols in TABS.items():
            rows = []
            for i, r in enumerate(raw.get(tab, [])):
                rec = {c: normalize_cell(c, r.get(c)) for c in cols}
                for extra, v in r.items():  # keep user-added columns visible
                    if extra not in rec and not extra.startswith("_"):
                        rec[extra] = v
                rec["_row"] = i
                rows.append(rec)
            snap[tab] = rows
        return snap

    def commit(self, ops: list[Append | Update]) -> None:
        for op in ops:
            if op.tab not in TABS:
                raise ValueError(f"unknown tab {op.tab}")
        if ops:
            try:
                self._commit(ops)
            finally:
                self.invalidate()
            self.generation += 1

    @abstractmethod
    def _read(self) -> dict[str, list[dict[str, Any]]]: ...

    @abstractmethod
    def _commit(self, ops: list[Append | Update]) -> None: ...

    @abstractmethod
    def ensure_schema(self) -> None: ...

    def describe(self) -> str:
        return type(self).__name__
