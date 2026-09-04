"""The audit log's immutability and tamper-evidence.

"Unsafe action count = 0" is only a meaningful claim if the log recording it
could not have been edited afterwards. These tests attack the log directly.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.audit.log import GENESIS_HASH, AuditLog


def test_chain_links_each_entry_to_its_predecessor(audit):
    first = audit.record(actor="a", action="x", subject_id="s1", outcome="ok")
    second = audit.record(actor="a", action="x", subject_id="s2", outcome="ok")

    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.entry_hash
    assert audit.head_hash() == second.entry_hash


def test_clean_chain_verifies(audit):
    for i in range(20):
        audit.record(actor="a", action="x", subject_id=f"s{i}", outcome="ok")
    ok, message = audit.verify_chain()
    assert ok
    assert "20 entries" in message


class TestImmutability:
    """Enforced by SQLite triggers, not by application discipline."""

    def test_update_is_blocked_at_the_storage_layer(self, audit):
        audit.record(actor="a", action="x", subject_id="s1", outcome="ok")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            audit._conn.execute("UPDATE audit_log SET outcome = 'tampered'")

    def test_delete_is_blocked_at_the_storage_layer(self, audit):
        audit.record(actor="a", action="x", subject_id="s1", outcome="ok")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            audit._conn.execute("DELETE FROM audit_log")

    def test_the_log_exposes_no_mutation_api(self):
        for forbidden in ("update", "delete", "edit", "amend", "remove"):
            assert not hasattr(AuditLog, forbidden)


class TestTamperDetection:
    """Even a direct file edit that bypasses the triggers is detected."""

    def test_content_tampering_breaks_verification(self, tmp_path):
        path = tmp_path / "audit.db"
        log = AuditLog(path)
        for i in range(5):
            log.record(actor="a", action="x", subject_id=f"s{i}", outcome="ok")
        assert log.verify_chain()[0]
        log.close()

        # Drop the triggers and rewrite history, the way an attacker with file
        # access would have to.
        conn = sqlite3.connect(str(path))
        conn.execute("DROP TRIGGER audit_log_no_update")
        conn.execute("UPDATE audit_log SET outcome = 'clean' WHERE seq = 3")
        conn.commit()
        conn.close()

        reopened = AuditLog(path)
        ok, message = reopened.verify_chain()
        assert not ok
        assert "tampered at seq=3" in message
        reopened.close()

    def test_deleting_an_entry_breaks_the_chain(self, tmp_path):
        path = tmp_path / "audit.db"
        log = AuditLog(path)
        for i in range(5):
            log.record(actor="a", action="x", subject_id=f"s{i}", outcome="ok")
        log.close()

        conn = sqlite3.connect(str(path))
        conn.execute("DROP TRIGGER audit_log_no_delete")
        conn.execute("DELETE FROM audit_log WHERE seq = 3")
        conn.commit()
        conn.close()

        reopened = AuditLog(path)
        ok, message = reopened.verify_chain()
        assert not ok
        assert "chain break" in message
        reopened.close()


def test_payload_is_queryable_by_subject_and_action(audit):
    audit.record(actor="gate", action="gate.decision", subject_id="ORD-1",
                 outcome="blocked", payload={"amount_paise": 500})
    audit.record(actor="gate", action="razorpay.transfer", subject_id="ORD-1",
                 outcome="executed")
    audit.record(actor="gate", action="gate.decision", subject_id="ORD-2",
                 outcome="allowed")

    assert len(list(audit.entries(subject_id="ORD-1"))) == 2
    assert len(list(audit.entries(action="gate.decision"))) == 2
    assert audit.count(action="gate.decision", outcome="blocked") == 1

    entry = next(audit.entries(subject_id="ORD-1"))
    assert entry.payload["amount_paise"] == 500
