"""Append-only, hash-chained audit log.

Every decision the system makes and every money movement it proposes, allows, or
refuses is recorded here before anything else happens. Two properties matter:

**Append-only.** There is no update or delete API. The table is guarded by SQLite
triggers that raise on ``UPDATE`` and ``DELETE``, so tampering fails at the
storage layer rather than relying on application discipline.

**Hash-chained.** Each entry stores ``prev_hash`` and an ``entry_hash`` computed
over its own content plus its predecessor. Altering or removing any historical
entry breaks every subsequent hash, and :func:`AuditLog.verify_chain` detects it.
This is what makes "unsafe action count = 0" an auditable claim rather than an
assertion — the absence of an unsafe action is only meaningful if the log could
not have been edited after the fact.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at  TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    subject_id   TEXT    NOT NULL,
    outcome      TEXT    NOT NULL,
    payload_json TEXT    NOT NULL,
    prev_hash    TEXT    NOT NULL,
    entry_hash   TEXT    NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit_log(subject_id);
CREATE INDEX IF NOT EXISTS idx_audit_action  ON audit_log(action);

-- Storage-layer immutability. Application code cannot rewrite history even by
-- mistake; these triggers make it a database error.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is forbidden');
END;
"""


@dataclass(frozen=True, slots=True)
class AuditEntry:
    seq: int
    recorded_at: str
    actor: str
    action: str
    subject_id: str
    outcome: str
    payload: dict[str, Any]
    prev_hash: str
    entry_hash: str


def _hash_entry(
    *,
    recorded_at: str,
    actor: str,
    action: str,
    subject_id: str,
    outcome: str,
    payload_json: str,
    prev_hash: str,
) -> str:
    material = "|".join(
        [recorded_at, actor, action, subject_id, outcome, payload_json, prev_hash]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AuditLog:
    """Hash-chained audit trail over SQLite."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing ----------------------------------------------------------

    def record(
        self,
        *,
        actor: str,
        action: str,
        subject_id: str,
        outcome: str,
        payload: dict[str, Any] | None = None,
        recorded_at: datetime | None = None,
    ) -> AuditEntry:
        """Append one entry. There is deliberately no way to amend or remove it."""
        ts = (recorded_at or datetime.now(timezone.utc)).isoformat()
        payload_json = json.dumps(
            payload or {}, sort_keys=True, separators=(",", ":"), default=str
        )
        prev_hash = self.head_hash()
        entry_hash = _hash_entry(
            recorded_at=ts,
            actor=actor,
            action=action,
            subject_id=subject_id,
            outcome=outcome,
            payload_json=payload_json,
            prev_hash=prev_hash,
        )
        cur = self._conn.execute(
            "INSERT INTO audit_log "
            "(recorded_at, actor, action, subject_id, outcome, payload_json, "
            " prev_hash, entry_hash) VALUES (?,?,?,?,?,?,?,?)",
            (ts, actor, action, subject_id, outcome, payload_json, prev_hash, entry_hash),
        )
        self._conn.commit()
        return AuditEntry(
            seq=int(cur.lastrowid or 0),
            recorded_at=ts,
            actor=actor,
            action=action,
            subject_id=subject_id,
            outcome=outcome,
            payload=json.loads(payload_json),
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )

    # -- reading ----------------------------------------------------------

    def head_hash(self) -> str:
        row = self._conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row["entry_hash"] if row else GENESIS_HASH

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
        return int(row["n"])

    def entries(
        self, *, action: str | None = None, subject_id: str | None = None
    ) -> Iterator[AuditEntry]:
        sql = "SELECT * FROM audit_log"
        clauses, params = [], []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if subject_id:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq"
        for row in self._conn.execute(sql, params):
            yield AuditEntry(
                seq=row["seq"],
                recorded_at=row["recorded_at"],
                actor=row["actor"],
                action=row["action"],
                subject_id=row["subject_id"],
                outcome=row["outcome"],
                payload=json.loads(row["payload_json"]),
                prev_hash=row["prev_hash"],
                entry_hash=row["entry_hash"],
            )

    def count(self, *, action: str | None = None, outcome: str | None = None) -> int:
        sql, params = "SELECT COUNT(*) AS n FROM audit_log", []
        clauses = []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return int(self._conn.execute(sql, params).fetchone()["n"])

    # -- integrity --------------------------------------------------------

    def verify_chain(self) -> tuple[bool, str]:
        """Recompute the whole chain. Returns ``(ok, message)``.

        Called by the evaluation harness before it reports the unsafe-action
        count, because that metric means nothing if the log is not intact.
        """
        prev = GENESIS_HASH
        checked = 0
        for row in self._conn.execute("SELECT * FROM audit_log ORDER BY seq"):
            expected = _hash_entry(
                recorded_at=row["recorded_at"],
                actor=row["actor"],
                action=row["action"],
                subject_id=row["subject_id"],
                outcome=row["outcome"],
                payload_json=row["payload_json"],
                prev_hash=row["prev_hash"],
            )
            if row["prev_hash"] != prev:
                return False, (
                    f"chain break at seq={row['seq']}: prev_hash "
                    f"{row['prev_hash'][:12]} != expected {prev[:12]}"
                )
            if row["entry_hash"] != expected:
                return False, (
                    f"content tampered at seq={row['seq']}: stored hash "
                    f"{row['entry_hash'][:12]} != recomputed {expected[:12]}"
                )
            prev = row["entry_hash"]
            checked += 1
        return True, f"chain intact across {checked} entries"
