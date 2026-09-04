"""Version resolution, including the planted contract-version conflict.

The load-bearing property: the conflict window is *finite*. An ambiguous
amendment does not poison every subsequent order — only those where the
competing readings actually elect different versions.
"""

from __future__ import annotations

import pytest

from data.generator.contracts import build_contract_sources
from src.contract_compiler.resolver import group_by_contract, resolve
from tests.conftest import utc


@pytest.fixture(scope="module")
def versions(tmp_path_factory):
    from src.contract_compiler.compiler import ContractCompiler, DeterministicBackend

    compiler = ContractCompiler(
        backend=DeterministicBackend(), cache_dir=tmp_path_factory.mktemp("pol")
    )
    policies = [compiler.compile(s) for s in build_contract_sources()]
    return group_by_contract(policies)


class TestVersionConflictWindow:
    """CTR-0003: seller share 70% -> 65%, amendment executed 12 Feb but stated
    as effective from the start of the billing month (1 Feb)."""

    @pytest.mark.parametrize("date", ["2026-01-02", "2026-01-15", "2026-01-31"])
    def test_before_any_candidate_date_resolves_to_v1(self, versions, date):
        r = resolve(versions["CTR-0003"], utc(date))
        assert r.is_resolved
        assert r.policy.version == 1
        assert r.policy.commission.rate_bps == 3000

    @pytest.mark.parametrize("date", ["2026-02-01", "2026-02-05", "2026-02-11"])
    def test_inside_the_window_refuses_to_choose(self, versions, date):
        r = resolve(versions["CTR-0003"], utc(date))
        assert r.is_conflicted
        assert r.policy is None, "must not fall back to a 'best guess' version"
        assert {c.policy.version for c in r.candidates} == {1, 2}
        assert "30.00%" in r.conflict_reason and "35.00%" in r.conflict_reason

    @pytest.mark.parametrize("date", ["2026-02-12", "2026-02-20", "2026-06-01"])
    def test_after_every_candidate_date_resolves_to_v2(self, versions, date):
        """Both readings elect v2, so the ambiguity is immaterial here."""
        r = resolve(versions["CTR-0003"], utc(date))
        assert r.is_resolved
        assert r.policy.version == 2
        assert r.policy.commission.rate_bps == 3500

    def test_conflict_carries_the_evidence_a_human_needs(self, versions):
        r = resolve(versions["CTR-0003"], utc("2026-02-05"))
        assert r.ambiguities
        amb = r.ambiguities[0]
        assert set(amb.candidates) == {"2026-02-01", "2026-02-12"}
        assert "v1" in r.candidate_summary() and "v2" in r.candidate_summary()

    def test_never_silently_prefers_the_newest_version(self, versions):
        """Picking the highest version number is a plausible heuristic that
        would quietly under-pay the seller across the whole window."""
        for date in ("2026-02-01", "2026-02-06", "2026-02-11"):
            r = resolve(versions["CTR-0003"], utc(date))
            assert r.policy is None


class TestSingleVersionContracts:
    @pytest.mark.parametrize(
        "contract_id", ["CTR-0001", "CTR-0002", "CTR-0004", "CTR-0006"]
    )
    def test_resolve_cleanly(self, versions, contract_id):
        r = resolve(versions[contract_id], utc("2026-01-15"))
        assert r.is_resolved
        assert r.policy.version == 1

    def test_unreadable_terms_are_refused_even_with_one_version(self, versions):
        r = resolve(versions["CTR-0007"], utc("2026-01-15"))
        assert r.is_conflicted
        assert "promotion funding" in r.conflict_reason.lower()

    def test_order_before_any_effective_date(self, versions):
        r = resolve(versions["CTR-0001"], utc("2025-06-01"))
        assert not r.is_resolved
        assert "covers this order date" in r.conflict_reason

    def test_no_versions_at_all(self):
        r = resolve([], utc("2026-01-15"), contract_id="CTR-NONE")
        assert not r.is_resolved
        assert "No contract version exists" in r.conflict_reason
