"""Unit tests for the LLM fidelity scorer. No API key required."""

from __future__ import annotations

import pytest

from data.generator.contracts import INTENDED_TERMS, build_contract_sources
from src.contract_compiler.compiler import ContractCompiler, DeterministicBackend
from tests.eval.llm_fidelity import (
    POLICY_ACCESSORS,
    build_report,
    score_policy,
    score_refusal,
)


@pytest.fixture(scope="module")
def det_policies(tmp_path_factory):
    """Deterministic-backend policies, used as a known-good reference."""
    compiler = ContractCompiler(
        backend=DeterministicBackend(), cache_dir=tmp_path_factory.mktemp("llmfid")
    )
    return {
        (s.contract_id, s.version): compiler.compile(s) for s in build_contract_sources()
    }


def test_accessors_cover_every_intended_field():
    """Every ground-truth field must have a way to read it off a Policy."""
    expected_fields = set(next(iter(INTENDED_TERMS.values())).keys())
    assert set(POLICY_ACCESSORS) == expected_fields


def test_score_policy_all_correct_for_deterministic_backend(det_policies):
    """The deterministic backend is known to recover CTR-0001 exactly."""
    key = ("CTR-0001", 1)
    results = score_policy(det_policies[key], INTENDED_TERMS[key])
    assert len(results) == 10
    assert all(r.correct for r in results), [r for r in results if not r.correct]


def test_score_policy_flags_a_wrong_field(det_policies):
    """A mismatch must be reported, not silently passed."""
    key = ("CTR-0001", 1)
    tampered = dict(INTENDED_TERMS[key])
    tampered["commission_bps"] = 9999
    results = score_policy(det_policies[key], tampered)
    wrong = [r for r in results if not r.correct]
    assert len(wrong) == 1
    assert wrong[0].field == "commission_bps"
    assert wrong[0].expected == 9999


def test_score_refusal_passes_when_unreadable_contract_refuses(det_policies):
    """CTR-0007 is deliberately unreadable and must refuse."""
    result = score_refusal(det_policies[("CTR-0007", 1)])
    assert result.expectation == "must_refuse"
    assert result.passed


def test_score_refusal_flags_date_ambiguity(det_policies):
    """CTR-0003 v2 must flag both candidate effective dates."""
    result = score_refusal(det_policies[("CTR-0003", 2)])
    assert result.expectation == "must_flag_date_ambiguity"
    assert result.passed


def test_score_refusal_penalises_over_refusal(det_policies):
    """A clean contract that refuses is a false positive."""
    result = score_refusal(det_policies[("CTR-0001", 1)])
    assert result.expectation == "must_not_refuse"
    assert result.passed


def test_build_report_computes_accuracies(det_policies):
    report = build_report(det_policies, model="deterministic", elapsed_s=1.0)
    assert report.total_fields == 100
    assert report.field_accuracy == 1.0
    assert report.refusal_accuracy == 1.0
    assert "field_accuracy" in report.to_dict()
