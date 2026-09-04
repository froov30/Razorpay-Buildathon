"""Unit tests for the LLM fidelity scorer. No API key required."""

from __future__ import annotations

import os

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


# ---------------------------------------------------------------------------
# Live LLM scoring
# ---------------------------------------------------------------------------

NEEDS_KEY = pytest.mark.skipif(
    not any(
        os.getenv(k) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    ),
    reason="live LLM scoring requires ANTHROPIC_API_KEY or GEMINI_API_KEY",
)


def test_llm_cache_dir_is_separate_from_committed_cache():
    """The deterministic cache backs the reproducibility guarantee.

    Overwriting it with LLM output would silently change every headline
    metric in the README, so the runner must use its own directory.
    """
    from tests.eval.run_llm_fidelity import DEFAULT_LLM_CACHE_DIR

    assert "compiled_policies_llm" in str(DEFAULT_LLM_CACHE_DIR)
    assert str(DEFAULT_LLM_CACHE_DIR) != "data/synthetic/compiled_policies"


@pytest.mark.live_llm
@NEEDS_KEY
def test_llm_backend_recovers_terms_and_refuses_correctly(tmp_path):
    """Live scored run. Skipped without a usable key so the suite stays hermetic.

    Two failure modes are deliberately distinguished:

    * The API is unreachable for account reasons — no credit, revoked key,
      rate limit. That is not evidence about extraction quality, so the test
      skips with the reason rather than reporting a fidelity failure.
    * The API answered and the model scored badly. That IS evidence, and the
      test fails loudly.

    Collapsing the two would let a billing problem masquerade as a model
    problem, or worse, let a genuine quality regression hide behind a skip.
    """
    from tests.eval.run_llm_fidelity import compile_with_llm

    # Provider-agnostic: match on what the failure means, not on which SDK
    # raised it, so adding a third backend does not require touching this test.
    ACCOUNT_LEVEL_SIGNALS = (
        "credit balance",
        "quota",
        "rate limit",
        "resource_exhausted",
        "permission denied",
        "api key not valid",
        "unauthenticated",
        "authentication",
    )

    try:
        policies, elapsed = compile_with_llm(cache_dir=tmp_path / "llm", force=False)
    except Exception as exc:  # noqa: BLE001 - classified below, re-raised if unknown
        message = str(exc).lower()
        if any(signal in message for signal in ACCOUNT_LEVEL_SIGNALS):
            pytest.skip(f"LLM API not usable on this account: {exc}")
        raise

    report = build_report(policies, model="live", elapsed_s=elapsed)

    assert report.scored_contracts == 10
    assert report.total_fields == 100
    # Thresholds are deliberately below the observed score: this asserts the
    # backend is usable, not that a specific model version is pinned.
    assert report.field_accuracy >= 0.85, report.to_dict()["field_failures"]
    assert report.refusal_accuracy >= 0.90, report.to_dict()["refusals"]
