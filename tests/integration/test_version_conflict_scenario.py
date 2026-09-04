"""The "what broke" scenario, traced end-to-end.

This is the test a judge should read first. It follows the planted
contract-version conflict all the way through the system and asserts the
behaviour the whole submission is arguing for: **when the contract is genuinely
ambiguous, the system refuses to decide, explains why, and holds the money.**

The scenario
------------
Seller SLR-0003's agreement (CTR-0003) drops the seller's share from 70% to 65%.
The amendment says it takes effect "from the commencement of the current billing
month" but was executed on 12 February. Both 1 February and 12 February are
defensible start dates, and nothing in the document ranks them.

Orders placed 1-11 February therefore have two defensible commission rates. A
system that picks one is wrong roughly half the time and confident every time.

Demo location for the pitch video
---------------------------------
Orders **ORD-1011, ORD-1012, ORD-1013** — visible in the dashboard's exception
queue under ``contract_version_conflict``.
"""

from __future__ import annotations

import pytest

from src.common.types import (
    ConfidenceTier,
    ExceptionCategory,
    GateDecision,
    RazorpayMode,
)
from src.exception_investigator.investigator import investigate
from src.pipeline import load_dataset, run

CONFLICT_ORDERS = {"ORD-1011", "ORD-1012", "ORD-1013"}
CLEAN_BEFORE = "ORD-1010"   # 20 Jan — before the amendment
CLEAN_AFTER = "ORD-1014"    # 18 Feb — after both candidate dates


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    from src.contract_compiler.compiler import ContractCompiler, DeterministicBackend
    from src.razorpay_client.client import RazorpayRouteClient

    result = run(
        dataset=load_dataset(),
        db_path=tmp_path_factory.mktemp("db") / "audit.db",
        compiler=ContractCompiler(
            backend=DeterministicBackend(),
            cache_dir=tmp_path_factory.mktemp("pol"),
        ),
        client=RazorpayRouteClient(key_id="", key_secret="", mode=RazorpayMode.MOCK),
    )
    return {o.order_id: o for o in result.outcomes}


class TestTheSystemRefusesToGuess:
    @pytest.mark.parametrize("order_id", sorted(CONFLICT_ORDERS))
    def test_conflicted_orders_route_to_human_review(self, outcomes, order_id):
        d = outcomes[order_id].decision
        assert d.category is ExceptionCategory.CONTRACT_VERSION_CONFLICT
        assert d.tier is ConfidenceTier.NEEDS_REVIEW

    @pytest.mark.parametrize("order_id", sorted(CONFLICT_ORDERS))
    def test_no_entitlement_figure_is_asserted(self, outcomes, order_id):
        """The point of the exercise: no number is invented."""
        d = outcomes[order_id].decision
        assert d.contract_version is None
        assert d.expected == {}

    @pytest.mark.parametrize("order_id", sorted(CONFLICT_ORDERS))
    def test_the_gate_holds_the_money(self, outcomes, order_id):
        verdict = outcomes[order_id].gate_verdict
        assert verdict is not None
        assert verdict.decision is GateDecision.PENDING_APPROVAL
        assert verdict.token is None, "no token means no transfer can fire"
        assert verdict.amount_at_risk_paise > 0


class TestTheRefusalIsExplained:
    @pytest.mark.parametrize("order_id", sorted(CONFLICT_ORDERS))
    def test_both_candidate_dates_are_surfaced(self, outcomes, order_id):
        evidence = " ".join(outcomes[order_id].decision.evidence)
        assert "2026-02-01" in evidence
        assert "2026-02-12" in evidence

    @pytest.mark.parametrize("order_id", sorted(CONFLICT_ORDERS))
    def test_both_competing_rates_are_surfaced(self, outcomes, order_id):
        evidence = " ".join(outcomes[order_id].decision.evidence)
        assert "30.00%" in evidence and "35.00%" in evidence

    @pytest.mark.parametrize("order_id", sorted(CONFLICT_ORDERS))
    def test_the_clause_text_is_quoted_as_evidence(self, outcomes, order_id):
        evidence = " ".join(outcomes[order_id].decision.evidence)
        assert "billing month" in evidence.lower()
        assert "Executed on" in evidence

    @pytest.mark.parametrize("order_id", sorted(CONFLICT_ORDERS))
    def test_the_reviewer_gets_an_owner_and_an_action(self, outcomes, order_id):
        case = investigate(outcomes[order_id].decision)
        assert case.severity == "critical"
        assert case.hold_funds is True
        assert "Do not auto-resolve" in case.recommended_action
        assert "Seller Contracting" in case.owner
        assert case.amount_at_stake_paise > 0


class TestTheConflictWindowIsBounded:
    """An ambiguous amendment must not poison the whole relationship."""

    def test_orders_before_the_amendment_settle_normally(self, outcomes):
        d = outcomes[CLEAN_BEFORE].decision
        assert d.category is ExceptionCategory.NONE
        assert d.tier is ConfidenceTier.AUTO_CLEAR
        assert d.contract_version == 1

    def test_orders_after_both_dates_settle_normally(self, outcomes):
        d = outcomes[CLEAN_AFTER].decision
        assert d.category is ExceptionCategory.NONE
        assert d.tier is ConfidenceTier.AUTO_CLEAR
        assert d.contract_version == 2, "the amendment does govern, unambiguously"

    def test_only_the_boundary_window_is_conflicted(self, outcomes):
        conflicted = {
            oid
            for oid, o in outcomes.items()
            if o.decision.category is ExceptionCategory.CONTRACT_VERSION_CONFLICT
        }
        assert conflicted == CONFLICT_ORDERS


def test_the_scenario_is_documented_for_the_judge():
    """The failure narrative is a submission deliverable, not just code."""
    from pathlib import Path

    changelog = Path("docs/CHANGELOG.md")
    assert changelog.exists(), "docs/CHANGELOG.md is a required deliverable"
    text = changelog.read_text(encoding="utf-8")
    assert "ORD-1011" in text, "the demo location must be findable"
    assert "billing month" in text.lower()
