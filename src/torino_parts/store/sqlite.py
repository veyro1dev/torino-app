"""Local SQLite database: the "Local" backend (this PC only, works offline, no Google account).

One table per workbook tab (``t_<Tab>``) with one column per schema field plus ``_extra`` (JSON) for
columns the schema doesn't know, and ``_row`` giving the stable row order the store API is built on.

* A change set is one ``BEGIN IMMEDIATE`` ... ``COMMIT`` transaction, so it lands completely or not at all.
* WAL journal: the desktop app, the phone pages and several MCP servers (Codex, Claude Code) read while
  one of them writes; the cross-process file lock in :class:`Store` serializes the writers.
* ``meta.version`` increases on every commit; reads reuse a cached snapshot while it is unchanged, and the
  desktop app uses it to notice changes made by Codex.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..schema import SCHEMA_VERSION, TABS
from .base import Append, Snapshot, Store, Update


def _t(tab: str) -> str:
    return f'"t_{tab}"'


def _q(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


def _to_db(v: Any) -> Any:
    if isinstance(v, bool):
        return "yes" if v else ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


class SQLiteStore(Store):
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(self.path.with_suffix(".lock"))
        self._snap_cache: tuple[int, Snapshot] | None = None
        self.ensure_schema()

    # ------------------------------------------------------------ connection
    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA foreign_keys=OFF")
        return c

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as c:
            yield c

    # --------------------------------------------------------------- schema
    def ensure_schema(self) -> None:
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for k, v in (("version", "0"), ("created_at", now), ("app", "torino")):
                    c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (k, v))
                c.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
                for tab, cols in TABS.items():
                    c.execute(f"CREATE TABLE IF NOT EXISTS {_t(tab)} (_row INTEGER PRIMARY KEY, _extra TEXT)")
                    have = {r[1] for r in c.execute(f"PRAGMA table_info({_t(tab)})")}
                    for col in cols:
                        if col not in have:  # upgrade: add new columns, never drop or rename
                            c.execute(f"ALTER TABLE {_t(tab)} ADD COLUMN {_q(col)}")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    # ----------------------------------------------------------------- read
    def data_version(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0])

    def _read_with_version(self) -> tuple[int, dict[str, list[dict[str, Any]]]]:
        with self._conn() as c:
            c.execute("BEGIN")  # one consistent snapshot across all tables (WAL read transaction)
            try:
                version = int(c.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0])
                data: dict[str, list[dict[str, Any]]] = {}
                for tab, cols in TABS.items():
                    cur = c.execute(f"SELECT * FROM {_t(tab)} ORDER BY _row")
                    names = [d[0] for d in cur.description]
                    rows = []
                    for line in cur.fetchall():
                        raw = dict(zip(names, line))
                        rec = {k: raw.get(k) for k in cols}
                        extra = raw.get("_extra")
                        if extra:
                            try:
                                for k, v in json.loads(extra).items():
                                    rec.setdefault(k, v)
                            except ValueError:
                                pass
                        rows.append(rec)
                    data[tab] = rows
            finally:
                c.execute("COMMIT")
        return version, data

    def _read(self):
        return self._read_with_version()[1]

    def read(self, fresh: bool = False) -> Snapshot:
        if not fresh:
            v = self.data_version()
            with self._cache_lock:
                if self._snap_cache and self._snap_cache[0] == v:
                    return self._snap_cache[1]
        version, raw = self._read_with_version()
        snap = self._normalize(raw)
        if not fresh:  # fresh snapshots are handed to writers, which modify their rows
            with self._cache_lock:
                self._snap_cache = (version, snap)
        return snap

    def _normalize(self, raw) -> Snapshot:
        from .base import normalize_cell

        snap: Snapshot = {}
        for tab, cols in TABS.items():
            rows = []
            for i, r in enumerate(raw.get(tab, [])):
                rec = {c: normalize_cell(c, r.get(c)) for c in cols}
                for extra, v in r.items():
                    if extra not in rec and not extra.startswith("_"):
                        rec[extra] = v
                rec["_row"] = i
                rows.append(rec)
            snap[tab] = rows
        return snap

    def invalidate(self) -> None:
        super().invalidate()
        with self._cache_lock:
            self._snap_cache = None

    # ---------------------------------------------------------------- write
    def _write_ops(self, c: sqlite3.Connection, ops: list[Append | Update]) -> None:
        for op in ops:
            cols = TABS[op.tab]
            rec = {k: _to_db(v) for k, v in op.record.items() if not k.startswith("_")}
            known = {k: v for k, v in rec.items() if k in cols}
            extra = {k: v for k, v in rec.items() if k not in cols}
            if isinstance(op, Append):
                nxt = c.execute(f"SELECT COALESCE(MAX(_row) + 1, 0) FROM {_t(op.tab)}").fetchone()[0]
                names = ["_row", *known, "_extra"]
                vals = [nxt, *known.values(), json.dumps(extra, ensure_ascii=False) if extra else None]
                c.execute(f"INSERT INTO {_t(op.tab)} ({', '.join(_q(n) for n in names)}) "
                          f"VALUES ({', '.join('?' for _ in names)})", vals)
            else:
                row = c.execute(f"SELECT _row, _extra FROM {_t(op.tab)} ORDER BY _row LIMIT 1 OFFSET ?",
                                (op.row,)).fetchone()
                if row is None:
                    raise IndexError(f"{op.tab} row {op.row} does not exist")
                sets, vals = [], []
                for k, v in known.items():
                    sets.append(f"{_q(k)} = ?")
                    vals.append(v)
                if extra:
                    merged = json.loads(row[1]) if row[1] else {}
                    merged.update(extra)
                    sets.append("_extra = ?")
                    vals.append(json.dumps(merged, ensure_ascii=False))
                if sets:
                    c.execute(f"UPDATE {_t(op.tab)} SET {', '.join(sets)} WHERE _row = ?", (*vals, row[0]))

    def _commit(self, ops: list[Append | Update]) -> None:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                self._write_ops(c, ops)
                c.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='version'")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def replace_all(self, rows_by_tab: dict[str, list[dict[str, Any]]]) -> None:
        """Atomically replace every record (restore from backup / transfer into Local).

        Works while other processes have the database open: they simply see the new contents."""
        with self.lock(), self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                for tab in TABS:
                    c.execute(f"DELETE FROM {_t(tab)}")
                self._write_ops(c, [Append(tab, r) for tab in TABS for r in rows_by_tab.get(tab, [])])
                c.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='version'")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        self.invalidate()
        self.generation += 1

    def backup_to(self, dest: Path) -> None:
        """Consistent copy of the live database (SQLite online backup API)."""
        dest = Path(dest)
        dest.unlink(missing_ok=True)
        with self._conn() as src, closing(sqlite3.connect(dest)) as dst:
            src.backup(dst)

    def integrity_ok(self) -> bool:
        with self._conn() as c:
            return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    def describe(self) -> str:
        return f"local database {self.path}"
