"""SQLite persistence for dynamic state: proposals, acks, GCP call evidence.

Production entities (scenes/crew/cast/locations) are small and static for a
single shooting day — they live in CSV and memory (see importers). SQLite
stores what changes at runtime. Fine for one production, per plan §3.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    incident_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    group_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS acks (
    plan_id TEXT NOT NULL,
    token TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    acked_at TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (plan_id, token)
);
CREATE TABLE IF NOT EXISTS gcp_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    model TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    meta_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS token_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    epoch INTEGER NOT NULL,
    issued_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # RLock: helpers like bump_token_epoch call get_token_meta under the lock.
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Backfill group_id on databases created before the sandbox feature.
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(plans)")}
            if "group_id" not in cols:
                self._conn.execute(
                    "ALTER TABLE plans ADD COLUMN group_id TEXT NOT NULL DEFAULT ''"
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- plans
    def save_plan(self, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO plans "
                "(id, created_at, status, incident_json, payload_json, group_id) "
                "VALUES (?,?,?,?,?,?)",
                (
                    payload["id"],
                    payload["created_at"],
                    payload.get("status", "proposed"),
                    json.dumps(payload.get("incident", {})),
                    json.dumps(payload),
                    payload.get("group_id", ""),
                ),
            )
            self._conn.commit()

    def plans_in_group(self, group_id: str) -> list[dict]:
        """All option payloads of a sandbox group, in insertion order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json, status FROM plans WHERE group_id = ? "
                "ORDER BY rowid ASC",
                (group_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["status"] = row["status"]
            out.append(payload)
        return out

    def group_has_published(self, group_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM plans WHERE group_id = ? AND status LIKE 'published%' "
                "LIMIT 1",
                (group_id,),
            ).fetchone()
        return row is not None

    def get_plan(self, plan_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json, status, group_id FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        payload["status"] = row["status"]
        payload["group_id"] = row["group_id"]
        return payload

    def set_plan_status(self, plan_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE plans SET status = ? WHERE id = ?", (status, plan_id)
            )
            self._conn.commit()

    def latest_published_plan(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM plans WHERE status LIKE 'published%' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def previous_published_plan(self) -> dict | None:
        """The most recently published plan that was later superseded — the
        rollback target. Returns None when there is nothing to revert to."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json, status, group_id FROM plans "
                "WHERE status = 'superseded' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        payload["status"] = row["status"]
        payload["group_id"] = row["group_id"]
        return payload

    def list_plans(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, status FROM plans "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- acks
    def record_ack(
        self, plan_id: str, token: str, subject_id: str, display_name: str, message: str = ""
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO acks "
                "(plan_id, token, subject_id, display_name, acked_at, message) "
                "VALUES (?,?,?,?,?,?)",
                (plan_id, token, subject_id, display_name, utc_now_iso(), message),
            )
            self._conn.commit()

    def acks_for_plan(self, plan_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM acks WHERE plan_id = ? ORDER BY acked_at", (plan_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def has_acked(self, plan_id: str, subject_id: str) -> bool:
        """Key by subject, not tokens: link rotation must not orphan acks."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM acks WHERE plan_id = ? AND subject_id = ?",
                (plan_id, subject_id),
            ).fetchone()
        return row is not None

    # --------------------------------------------------------- token meta
    def get_token_meta(self) -> tuple[int, str]:
        """Current link (epoch, issued_at ISO); lazily initialized to (0, now)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT epoch, issued_at FROM token_meta WHERE id = 1"
            ).fetchone()
            if row is not None:
                return int(row["epoch"]), str(row["issued_at"])
            issued = utc_now_iso()
            # Two processes sharing one fresh DB may both reach this INSERT;
            # IGNORE makes initialization converge instead of crashing one.
            self._conn.execute(
                "INSERT OR IGNORE INTO token_meta (id, epoch, issued_at) "
                "VALUES (1, 0, ?)",
                (issued,),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT epoch, issued_at FROM token_meta WHERE id = 1"
            ).fetchone()
            return int(row["epoch"]), str(row["issued_at"])

    def bump_token_epoch(self) -> tuple[int, str]:
        """Rotate every crew/cast link: new epoch, fresh issue time."""
        with self._lock:
            epoch, _ = self.get_token_meta()
            epoch += 1
            issued = utc_now_iso()
            self._conn.execute(
                "UPDATE token_meta SET epoch = ?, issued_at = ? WHERE id = 1",
                (epoch, issued),
            )
            self._conn.commit()
            return epoch, issued

    def set_token_issued_at(self, issued_at_iso: str) -> None:
        """Ops/test hook: backdate the issue time to exercise expiry."""
        with self._lock:
            self._conn.execute(
                "UPDATE token_meta SET issued_at = ? WHERE id = 1", (issued_at_iso,)
            )
            self._conn.commit()

    # ---------------------------------------------------------- gcp calls
    def log_gcp_call(
        self, kind: str, model: str, latency_ms: int, ok: bool, meta: dict | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO gcp_calls (ts, kind, model, latency_ms, ok, meta_json) "
                "VALUES (?,?,?,?,?,?)",
                (utc_now_iso(), kind, model, latency_ms, int(ok), json.dumps(meta or {})),
            )
            self._conn.commit()

    def recent_gcp_calls(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM gcp_calls ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
