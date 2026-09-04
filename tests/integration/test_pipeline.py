"""Full-batch integration: the whole synthetic dataset, end to end."""

from __future__ import annotations

import pytest

from src.common.types import ConfidenceTier, ExceptionCategory, RazorpayMode
from src.exception_investigator.investigator import triage
from src.pipeline import load_dataset, run
from tests.eval.metrics import evaluate, load_ground_truth


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    from src.contract_compiler.compiler import ContractCompiler, DeterministicBackend
    from src.razorpay_client.client import RazorpayRouteClient

    dataset = load_dataset()
    result = run(
        dataset=dataset,
        db_path=tmp_path_factory.mktemp("db") / "audit.db",
        compiler=ContractCompiler(
            backend=DeterministicBackend(),
            cache_dir=tmp_path_factory.mktemp("policies"),
        ),
        client=RazorpayRouteClient(key_id="", key_secret="", mode=RazorpayMode.MOCK),
    )
    return dataset, result


def test_batch_runs_without_unhandled_errors(batch):
    dataset, result = batch
    assert len(result.outcomes) == len(dataset.orders) == 40
    assert result.records_processed > 200


def test_every_order_gets_a_tier_and_a_reason(batch):
    _, result = batch
    for outcome in result.outcomes:
        d = outcome.decision
        assert d.tier in (ConfidenceTier.AUTO_CLEAR, ConfidenceTier.NEEDS_REVIEW)
        assert d.explanation, f"{d.order_id} has no explanation"
        assert d.evidence, f"{d.order_id} has no evidence"


def test_no_bare_boolean_decisions(batch):
    """Every conclusion is tiered; nothing is a naked pass/fail."""
    _, result = batch
    for outcome in result.outcomes:
        assert isinstance(outcome.decision.tier, ConfidenceTier)


def test_classification_matches_ground_truth_exactly(batch):
    dataset, result = batch
    amounts = {o.order_id: o.gross_amount_paise for o in dataset.orders}
    metrics = evaluate(result, load_ground_truth(), order_amounts=amounts)
    assert metrics.mismatches == [], (
        "misclassified: "
        + ", ".join(f"{m.order_id} {m.expected}->{m.predicted}" for m in metrics.mismatches)
    )
    assert metrics.classification_accuracy == 1.0
    assert metrics.exception_precision == 1.0
    assert metrics.exception_recall == 1.0


def test_unsafe_action_count_is_zero(batch):
    _, result = batch
    assert result.gate_summary["unsafe_action_attempts"] == 0


def test_no_transfer_executes_during_an_analysis_run(batch):
    """Scoring must never move money, not even in test mode."""
    _, result = batch
    assert result.gate_summary["transfers_executed"] == 0


def test_audit_chain_is_intact_after_the_batch(batch):
    _, result = batch
    assert result.audit_ok, result.audit_message


def test_gate_blocked_something_and_quantified_it(batch):
    _, result = batch
    by_decision = result.gate_summary["by_decision"]
    assert by_decision.get("blocked", 0) > 0
    assert by_decision.get("pending_approval", 0) > 0
    assert result.gate_summary["prevented_loss_paise"] > 0


def test_honest_exception_list_is_not_all_one_bucket(batch):
    """A submission where everything auto-clears reads as untested."""
    _, result = batch
    categories = {
        str(o.decision.category)
        for o in result.outcomes
        if o.decision.category != ExceptionCategory.NONE
    }
    assert len(categories) >= 6, f"only found {categories}"

    tiers = result.tier_counts()
    assert tiers["auto_clear"] > 0
    assert tiers["needs_review"] > 0


def test_system_says_i_dont_know_where_it_should(batch):
    """Unresolvable contracts produce refusals, not confident numbers."""
    _, result = batch
    refusals = [
        o for o in result.outcomes
        if o.decision.category
        in (
            ExceptionCategory.CONTRACT_VERSION_CONFLICT,
            ExceptionCategory.AMBIGUOUS_UNRESOLVABLE,
        )
    ]
    assert len(refusals) >= 5
    for outcome in refusals:
        assert outcome.decision.contract_version is None or outcome.decision.evidence


def test_review_queue_is_prioritised_and_actionable(batch):
    _, result = batch
    cases = triage(result.decisions)
    assert len(cases) == 20

    severities = [c.severity for c in cases]
    assert severities == sorted(severities, key=lambda s: ["critical", "high", "medium", "low", "info"].index(s))

    for case in cases:
        assert case.recommended_action
        assert case.owner and case.owner != "—"
        assert case.mechanism


def test_every_decision_records_a_replayable_policy_hash(batch):
    _, result = batch
    for outcome in result.outcomes:
        d = outcome.decision
        if d.contract_version is not None:
            assert d.policy_hash, f"{d.order_id} cannot be replayed"
            assert len(d.policy_hash) == 16
