"""Adversarial tests against the entitlement gate.

The project's central safety claim is ``unsafe_action_count == 0``. Observing
that number to be zero after a clean run proves almost nothing — a clean run
never tries anything unsafe. These tests attack the gate on purpose and assert
that each attack is refused, that nothing executes, and that the refusal is
recorded.

Attacks covered
---------------
1. Calling the Razorpay client directly with no token at all.
2. Replaying a token that has already been spent.
3. Mutating an approved proposal's amount before executing it.
4. Forging a token with a signature the gate never issued.
5. Substituting a token issued for a *different* proposal.
6. Sending a premature payout through the gate and trying to execute the
   refusal anyway.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from src.common.types import ConfidenceTier, ExceptionCategory, GateDecision, PartyRole
from src.razorpay_client.client import (
    ApprovalToken,
    LiveKeyRefused,
    RazorpayRouteClient,
    TransferProposal,
    UnsafeActionError,
)


def test_direct_call_without_token_is_refused(client, seller_proposal):
    """Attack 1: bypass the gate entirely."""
    with pytest.raises(UnsafeActionError, match="no gate token"):
        client.execute_transfer(seller_proposal, None)

    assert client.unsafe_action_attempts == 1
    assert client.executed == [], "no transfer may exist after a refused call"


def test_replayed_token_is_refused(gate, client, simple_ctx, seller_proposal):
    """Attack 2: spend the same approval twice."""
    verdict = gate.submit(seller_proposal, simple_ctx)
    assert verdict.decision is GateDecision.ALLOWED

    first = client.execute_transfer(seller_proposal, verdict.token)
    assert first["id"].startswith("trf_")

    with pytest.raises(UnsafeActionError, match="already been used"):
        client.execute_transfer(seller_proposal, verdict.token)

    assert client.unsafe_action_attempts == 1
    assert len(client.executed) == 1, "replay must not produce a second transfer"


def test_mutated_proposal_invalidates_token(gate, client, simple_ctx, seller_proposal):
    """Attack 3: get approval for ₹800, then try to send ₹80,000."""
    verdict = gate.submit(seller_proposal, simple_ctx)
    assert verdict.decision is GateDecision.ALLOWED

    inflated = replace(seller_proposal, amount_paise=8_000_000)

    with pytest.raises(UnsafeActionError, match="altered after approval"):
        client.execute_transfer(inflated, verdict.token)

    assert client.unsafe_action_attempts == 1
    assert client.executed == []


def test_forged_token_is_refused(client, seller_proposal):
    """Attack 4: fabricate a token without the gate's secret."""
    forged = ApprovalToken(
        token_id="tok_forged",
        proposal_hash=seller_proposal.content_hash(),
        issued_at=None,  # type: ignore[arg-type]
        approver="attacker",
        signature="0" * 64,
    )
    with pytest.raises(UnsafeActionError):
        client.execute_transfer(seller_proposal, forged)

    assert client.executed == []


def test_token_for_another_proposal_is_refused(gate, client, simple_ctx, seller_proposal):
    """Attack 5: reuse a valid token against a different payee."""
    verdict = gate.submit(seller_proposal, simple_ctx)

    other = TransferProposal(
        proposal_id="PRP-OTHER",
        order_id="ORD-TEST",
        party_role=PartyRole.SELLER,
        party_account_id="acc_ATTACKER",
        amount_paise=80_000,
    )
    with pytest.raises(UnsafeActionError, match="altered after approval"):
        client.execute_transfer(other, verdict.token)

    assert client.executed == []


def test_premature_payout_is_blocked_and_cannot_execute(gate, client, simple_ctx, seller_proposal):
    """Attack 6: a payout inside the contractual hold window."""
    delivered = simple_ctx.deliveries[0].occurred_at
    early_ctx = replace(simple_ctx, as_of=delivered + timedelta(hours=3))

    verdict = gate.submit(seller_proposal, early_ctx)

    assert verdict.decision is GateDecision.BLOCKED
    assert verdict.tier is ConfidenceTier.BLOCKED
    assert verdict.category is ExceptionCategory.PREMATURE_PAYOUT
    assert verdict.token is None
    assert verdict.amount_at_risk_paise == seller_proposal.amount_paise

    with pytest.raises(UnsafeActionError):
        client.execute_transfer(verdict.proposal, verdict.token)

    assert client.executed == []


def test_wrong_amount_is_blocked_with_only_excess_at_risk(gate, simple_ctx):
    """Overpayment: entitled money is not counted as prevented loss."""
    overpay = TransferProposal(
        proposal_id="PRP-OVER",
        order_id="ORD-TEST",
        party_role=PartyRole.SELLER,
        party_account_id="acc_SLR-TEST",
        amount_paise=95_000,  # entitled to 80,000
    )
    verdict = gate.submit(overpay, simple_ctx)

    assert verdict.decision is GateDecision.BLOCKED
    assert verdict.expected_paise == 80_000
    assert verdict.amount_at_risk_paise == 15_000, "only the excess is loss"


def test_human_override_issues_a_fresh_token_and_audits_both(gate, client, simple_ctx, audit):
    """Maker-checker: the block stays in the log even after an override."""
    early = replace(simple_ctx, as_of=simple_ctx.deliveries[0].occurred_at)
    proposal = TransferProposal(
        proposal_id="PRP-OVERRIDE",
        order_id="ORD-TEST",
        party_role=PartyRole.SELLER,
        party_account_id="acc_SLR-TEST",
        amount_paise=80_000,
    )
    blocked = gate.submit(proposal, early)
    assert blocked.decision is GateDecision.BLOCKED

    approved = gate.approve_by_human(
        blocked, approver="controller@example.com", justification="Delivery confirmed by phone."
    )
    assert approved.decision is GateDecision.APPROVED_BY_HUMAN
    assert approved.token is not None

    result = client.execute_transfer(approved.proposal, approved.token)
    assert result["id"].startswith("trf_")

    actions = [e.action for e in audit.entries(subject_id="ORD-TEST")]
    assert "gate.decision" in actions
    assert "gate.human_override" in actions

    overrides = list(audit.entries(action="gate.human_override"))
    assert overrides[0].payload["approver"] == "controller@example.com"
    assert overrides[0].payload["original_decision"] == "blocked"


def test_overridden_proposal_is_excluded_from_prevented_loss(gate, simple_ctx):
    """Money a human released did move — it was not prevented."""
    early = replace(simple_ctx, as_of=simple_ctx.deliveries[0].occurred_at)
    proposal = TransferProposal(
        proposal_id="PRP-COUNT",
        order_id="ORD-TEST",
        party_role=PartyRole.SELLER,
        party_account_id="acc_SLR-TEST",
        amount_paise=80_000,
    )
    blocked = gate.submit(proposal, early)
    assert gate.prevented_loss_paise() == 80_000

    gate.approve_by_human(blocked, approver="controller", justification="verified")
    assert gate.prevented_loss_paise() == 0


def test_live_keys_are_refused_unconditionally():
    """No flag, no environment variable, no override releases this."""
    with pytest.raises(LiveKeyRefused, match="LIVE key"):
        RazorpayRouteClient(key_id="rzp_live_abc123", key_secret="secret")
