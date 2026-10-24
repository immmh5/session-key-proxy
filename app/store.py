"""Session-bound API key store backed by SQLite (WAL mode).

This module is the single source of truth for *which API key belongs to which
session*. It is deliberately small and persistence-only: the HTTP layer in
``proxy.py`` decides *when* to call these methods, while this store guarantees
the invariants:

* At most one key is bound to a given session id.
* A key is only ever bound to one session at a time.
* The pool is seeded from ``keys.json`` exactly once, on a fresh DB.
* A key that is exhausted or invalid is never silently requeued.
* All pick/requeue operations are atomic under concurrency
  (``BEGIN IMMEDIATE`` serializes writers even across processes/threads).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, util

# Key lifecycle states -------------------------------------------------------
K_AVAILABLE = "available"
K_BOUND = "bound"
K_COOLDOWN = "cooldown"
K_EXHAUSTED = "exhausted"
K_INVALID = "invalid"

# Terminal states: once a key lands here it must be reset by an admin, never
# automatically recovered by GC.
_TERMINAL = (K_EXHAUSTED, K_INVALID)

_ALL_STATUSES = (K_AVAILABLE, K_BOUND, K_COOLDOWN, K_EXHAUSTED, K_INVALID)


@dataclass(frozen=True)
class Binding:
    """A session's permanent binding to a single API key."""

    session_id: str
    key_id: str
    bound_at: float
    last_seen: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id             TEXT PRIMARY KEY,
    secret         TEXT NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'available',
    cooldown_until REAL,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bindings (
    session_id TEXT PRIMARY KEY,
    key_id     TEXT NOT NULL REFERENCES keys(id) ON DELETE CASCADE,
    bound_at   REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_keys_status   ON keys(status);
CREATE INDEX IF NOT EXISTS idx_keys_avail    ON keys(status, cooldown_until);
CREATE INDEX IF NOT EXISTS idx_bindings_key  ON bindings(key_id);
"""


class Store:
    """Session-bound API key store.

    ``db_path`` is the SQLite file. ``keys_file`` (optional) is the JSON seed
    read only when the key pool is empty (i.e. a fresh DB). ``session_ttl``
    and ``cooldown`` tune GC and cooldown behaviour respectively. ``now`` is an
    injectable clock (default ``app.util.now``) so tests can be deterministic.
    """

    def __init__(
        self,
        db_path,
        keys_file=None,
        session_ttl: float = config.SESSION_TTL_SECONDS,
        cooldown: float = config.COOLDOWN_SECONDS,
        now=util.now,
    ) -> None:
        self.db_path = str(db_path)
        self.keys_file = str(keys_file) if keys_file is not None else None
        self.session_ttl = float(session_ttl)
        self.cooldown = float(cooldown)
        self._now = now
        self._lock = threading.RLock()
        self._local = threading.local()

    # ---- connection management -----------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close this thread's connection (idempotent)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---- lifecycle ------------------------------------------------------
    def init(self) -> "Store":
        """Create tables (idempotent) and seed the pool from keys_file."""
        with self._lock:
            conn = self._conn()
            conn.executescript(_SCHEMA)
            conn.commit()
            self._seed(conn)
        return self

    def _seed(self, conn: sqlite3.Connection) -> None:
        if self.keys_file is None:
            return
        existing = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        if existing:
            return  # Only seed a fresh (empty) pool.
        raw = Path(self.keys_file).read_text(encoding="utf-8")
        entries = json.loads(raw).get("keys", [])
        ts = self._now()
        rows = [
            (e["id"], e["key"], e.get("label", ""), K_AVAILABLE, ts) for e in entries
        ]
        if rows:
            conn.executemany(
                "INSERT INTO keys (id, secret, label, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()

    # ---- key pool -------------------------------------------------------
    def get_key(self, key_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT id, secret, label, status, cooldown_until, updated_at "
            "FROM keys WHERE id = ?",
            (key_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_key(row)

    def list_keys(self) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT id, secret, label, status, cooldown_until, updated_at "
            "FROM keys ORDER BY id"
        ).fetchall()
        return [self._row_to_key(r) for r in rows]

    def count_by_status(self) -> Dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) FROM keys GROUP BY status"
        ).fetchall()
        counts = {status: 0 for status in _ALL_STATUSES}
        for status, n in rows:
            counts[status] = n
        return counts

    def set_key_state(
        self,
        key_id: str,
        status: str,
        cooldown_until: Optional[float] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Set a key's lifecycle state. Returns True if the key existed."""
        ts = self._now() if now is None else now
        if status not in _ALL_STATUSES:
            raise ValueError(f"unknown key status: {status!r}")
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                "UPDATE keys SET status=?, cooldown_until=?, updated_at=? WHERE id=?",
                (status, cooldown_until, ts, key_id),
            )
            conn.commit()
        return cur.rowcount > 0

    def add_key(
        self,
        key_id: str,
        secret: str,
        label: str = "",
        now: Optional[float] = None,
    ) -> bool:
        """Insert a new key into the pool. Returns True if inserted (False if id exists)."""
        ts = self._now() if now is None else now
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO keys (id, secret, label, status, cooldown_until, updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?)",
                    (key_id, secret, label, K_AVAILABLE, ts),
                )
                conn.commit()
                return cur.rowcount > 0
            except sqlite3.IntegrityError:
                conn.rollback()
                return False

    def update_key(
        self,
        key_id: str,
        secret: Optional[str] = None,
        label: Optional[str] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Update a key's secret and/or label. Returns True if the key existed."""
        ts = self._now() if now is None else now
        updates, params = [], []
        if secret is not None:
            updates.append("secret = ?")
            params.append(secret)
        if label is not None:
            updates.append("label = ?")
            params.append(label)
        if not updates:
            return False
        updates.append("updated_at = ?")
        params.append(ts)
        params.append(key_id)
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                f"UPDATE keys SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()
        return cur.rowcount > 0

    def delete_key(self, key_id: str) -> bool:
        """Delete a key and its bindings (cascade). Returns True if deleted."""
        with self._lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM keys WHERE id = ?", (key_id,))
            conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row_to_key(row: Tuple) -> Dict[str, Any]:
        key_id, secret, label, status, cooldown_until, updated_at = row
        return {
            "id": key_id,
            "secret": secret,
            "masked": util.mask_key(secret),
            "label": label,
            "status": status,
            "cooldown_until": cooldown_until,
            "updated_at": updated_at,
        }

    # ---- session bindings ----------------------------------------------
    def resolve(self, session_id: str, now: Optional[float] = None) -> Optional[Binding]:
        """Return the binding for ``session_id``, creating one if needed.

        If the session is already bound, its ``last_seen`` is refreshed and the
        existing key returned — keys are **never** swapped between sessions.

        Otherwise an available key is picked atomically and permanently bound
        to the session. Returns ``None`` when no key is available (e.g. every
        key is in cooldown / exhausted / invalid).
        """
        ts = self._now() if now is None else now
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT key_id, bound_at, last_seen FROM bindings WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is not None:
                    key_id, bound_at, last_seen = row
                    key = self.get_key(key_id)
                    # Only invalidate the binding if the key is in a state where it
                    # cannot be used for this session — i.e. cooldown (temporary
                    # unavailability) or terminal states (exhausted / invalid).
                    # A key that is simply BOUND to this session is still valid.
                    if key is not None and key["status"] in (K_COOLDOWN,) + _TERMINAL:
                        # Key is no longer available (cooldown / exhausted / invalid) —
                        # invalidate the stale binding and return None so the caller
                        # picks a fresh key.
                        conn.execute(
                            "DELETE FROM bindings WHERE session_id = ?",
                            (session_id,),
                        )
                        conn.rollback()
                        return None
                    else:
                        conn.execute(
                            "UPDATE keys SET status = ?, updated_at = ? WHERE id = ?",
                            (K_BOUND, ts, key_id),
                        )
                        conn.execute(
                            "UPDATE bindings SET last_seen = ? WHERE session_id = ?",
                            (ts, session_id),
                        )
                        conn.commit()
                        return Binding(session_id, key_id, bound_at, ts)
                # No binding yet: atomically grab an available key.
                key = conn.execute(
                    "SELECT id FROM keys "
                    "WHERE (status = ? OR "
                    "(status = ? AND cooldown_until IS NOT NULL AND cooldown_until <= ?)) "
                    "AND (cooldown_until IS NULL OR cooldown_until <= ?) "
                    "ORDER BY id LIMIT 1",
                    (K_AVAILABLE, K_COOLDOWN, ts, ts),
                ).fetchone()
                if key is None:
                    conn.rollback()
                    return None
                key_id = key[0]
                conn.execute(
                    "UPDATE keys SET status = ?, cooldown_until = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (K_BOUND, ts, key_id),
                )
                conn.execute(
                    "INSERT INTO bindings (session_id, key_id, bound_at, last_seen) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, key_id, ts, ts),
                )
                conn.commit()
                return Binding(session_id, key_id, ts, ts)
            except sqlite3.Error:
                conn.rollback()
                raise

    def get_bound_key(self, session_id: str) -> Optional[str]:
        """Return the key id bound to ``session_id``, or ``None``."""
        row = self._conn().execute(
            "SELECT key_id FROM bindings WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0] if row else None

    def list_bindings(self) -> List[Binding]:
        rows = self._conn().execute(
            "SELECT session_id, key_id, bound_at, last_seen FROM bindings ORDER BY session_id"
        ).fetchall()
        return [Binding(r[0], r[1], r[2], r[3]) for r in rows]

    def unbind(self, session_id: str) -> bool:
        """Release a session's binding and requeue its key. Returns True if a
        binding existed and was removed."""
        ts = self._now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT key_id FROM bindings WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return False
                key_id = row[0]
                conn.execute("DELETE FROM bindings WHERE session_id = ?", (session_id,))
                conn.execute(
                    "UPDATE keys SET status = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    (K_AVAILABLE, ts, key_id, K_BOUND),
                )
                conn.commit()
                return True
            except sqlite3.Error:
                conn.rollback()
                raise

    def gc(self, now: Optional[float] = None) -> int:
        """Housekeeping pass.

        Deletes bindings idle longer than the session TTL and requeues their
        keys (only keys still in ``bound`` state — exhausted/invalid keys are
        never auto-recovered) **and** requeues keys whose cooldown has expired
        (``status = 'cooldown'`` with ``cooldown_until <= now``).

        Returns the number of stale sessions collected.
        """
        ts = self._now() if now is None else now
        cutoff = ts - self.session_ttl
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT session_id, key_id FROM bindings WHERE last_seen <= ?",
                    (cutoff,),
                ).fetchall()
                for session_id, key_id in rows:
                    conn.execute(
                        "DELETE FROM bindings WHERE session_id = ?", (session_id,)
                    )
                    conn.execute(
                        "UPDATE keys SET status = ?, cooldown_until = NULL, "
                        "updated_at = ? WHERE id = ? AND status = ?",
                        (K_AVAILABLE, ts, key_id, K_BOUND),
                    )
                # Requeue keys whose cooldown has expired.
                conn.execute(
                    "UPDATE keys SET status = ?, cooldown_until = NULL, "
                    "updated_at = ? WHERE status = ? AND cooldown_until IS NOT NULL "
                    "AND cooldown_until <= ?",
                    (K_AVAILABLE, ts, K_COOLDOWN, ts),
                )
                conn.commit()
                return len(rows)
            except sqlite3.Error:
                conn.rollback()
                raise
