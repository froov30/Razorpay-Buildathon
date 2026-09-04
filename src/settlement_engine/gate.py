"""The entitlement gate — maker-checker control in front of every money movement.

Nothing in this system calls Razorpay directly. A proposed transfer is submitted
here first; the gate re-derives the entitlement from the compiled contract and
either issues a single-use :class:`ApprovalToken` or refuses. The Razorpay client
will not execute without that token, so "no unsafe action can fire" is enforced
by construction rather than by convention.

Verdicts
--------
``ALLOWED``
    Amount matches entitlement to the paise and every condition is satisfied.
    A token is issued immediately.

``BLOCKED``
    The proposal contradicts the contract. No token. A human must explicitly
    approve to override, which creates a second audit entry naming them.

``PENDING_APPROVAL``
    The contract could not be read unambiguously (e.g. a version conflict), so
    the gate declines to assert either way. Routed to the review queue.

Prevented loss
--------------
Every refusal records ``amount_at_risk_paise``, and their sum is the dashboard's
headline figure. The convention, stated so the number is defensible:

* **Wrong amount** — at risk is the *excess* over entitlement (the part that
  should not have moved).
* **Premature or unresolvable** — at risk is the *entire* proposal, because at
  that moment the party was owed nothing at all.

Money the seller was genuinely owed is never counted as prevented loss.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.audit.log import AuditLog
from src.common.money import format_inr
from src.common.types import (
    ConfidenceTier,
    ExceptionCategory,
    GateDecision,
    PartyRole,
)
from src.contract_compiler.resolver import Resolution
from src.razorpay_client.client import (
    ApprovalToken,
    RazorpayRouteClient,
    TransferProposal,
)
from src.settlement_engine.compute import OrderContext, compute_entitlements


@dataclass(slots=True)
class GateVerdict:
    """Outcome of submitting one proposal to the gate."""

    proposal: TransferProposal
    decision: GateDecision
    tier: ConfidenceTier
    reason: str
    token: ApprovalToken | None = None
    expected_paise: int | None = None
    amount_at_risk_paise: int = 0
    category: ExceptionCategory = ExceptionCategory.NONE
    evidence: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.decision in (GateDecision.ALLOWED, GateDecision.APPROVED_BY_HUMAN)


class EntitlementGate:
    """Re-derives entitlement and authorises (or refuses) money movement."""

    def __init__(
        self,
        client: RazorpayRouteClient,
        audit: AuditLog,
        *,
        actor: str = "entitlegraph.gate",
    ) -> None:
        self.client = client
        self.audit = audit
        self.actor = actor
        self.verdicts: list[GateVerdict] = []

    # -- token issuance ----------------------------------------------------

    def _issue_token(self, proposal: TransferProposal, approver: str) -> ApprovalToken:
        token_id = "tok_" + uuid.uuid4().hex[:16] + secrets.token_hex(4)
        proposal_hash = proposal.content_hash()
        return ApprovalToken(
            token_id=token_id,
            proposal_hash=proposal_hash,
            issued_at=datetime.now(timezone.utc),
            approver=approver,
            signature=ApprovalToken.sign(
                token_id, proposal_hash, self.client.token_secret
            ),
        )

    # -- the check ---------------------------------------------------------

    def submit(
        self,
        proposal: TransferProposal,
        ctx: OrderContext | None,
        resolution: Resolution | None = None,
    ) -> GateVerdict:
        """Check a proposal against the contract. Only path to a token."""
        verdict = self._evaluate(proposal, ctx, resolution)
        self.verdicts.append(verdict)
        self.audit.record(
            actor=self.actor,
            action="gate.decision",
            subject_id=proposal.order_id,
            outcome=str(verdict.decision),
            payload={
                "proposal_id": proposal.proposal_id,
                "proposal_hash": proposal.content_hash(),
                "party_role": str(proposal.party_role),
                "amount_paise": proposal.amount_paise,
                "expected_paise": verdict.expected_paise,
                "amount_at_risk_paise": verdict.amount_at_risk_paise,
                "category": str(verdict.category),
                "tier": str(verdict.tier),
                "reason": verdict.reason,
                "token_id": verdict.token.token_id if verdict.token else None,
                "razorpay_mode": str(self.client.mode),
            },
        )
        return verdict

    def _evaluate(
        self,
        proposal: TransferProposal,
        ctx: OrderContext | None,
        resolution: Resolution | None,
    ) -> GateVerdict:
        # 1. Unresolvable contract -> refuse to assert either way.
        if resolution is not None and not resolution.is_resolved:
            is_version_conflict = (
                len({c.policy.version for c in resolution.candidates}) > 1
            )
            return GateVerdict(
                proposal=proposal,
                decision=GateDecision.PENDING_APPROVAL,
                tier=ConfidenceTier.NEEDS_REVIEW,
                category=(
                    ExceptionCategory.CONTRACT_VERSION_CONFLICT
                    if is_version_conflict
                    else ExceptionCategory.AMBIGUOUS_UNRESOLVABLE
                ),
                reason=(
                    "Cannot authorise: the governing contract terms are ambiguous. "
                    + resolution.conflict_reason
                ),
                amount_at_risk_paise=proposal.amount_paise,
                evidence=[resolution.conflict_reason]
                + ([f"Candidates: {resolution.candidate_summary()}"] if resolution.candidates else []),
            )

        if ctx is None:
            return GateVerdict(
                proposal=proposal,
                decision=GateDecision.BLOCKED,
                tier=ConfidenceTier.BLOCKED,
                category=ExceptionCategory.AMBIGUOUS_UNRESOLVABLE,
                reason="Cannot authorise: no order context supplied for this proposal.",
                amount_at_risk_paise=proposal.amount_paise,
            )

        # 2. Compute what this party is actually owed, right now.
        computation = compute_entitlements(ctx)
        entitlement = computation.entitlements.get(proposal.party_role)

        if entitlement is None:
            return GateVerdict(
                proposal=proposal,
                decision=GateDecision.BLOCKED,
                tier=ConfidenceTier.BLOCKED,
                category=ExceptionCategory.AMBIGUOUS_UNRESOLVABLE,
                reason=(
                    f"Cannot authorise: contract grants no entitlement to "
                    f"{proposal.party_role} on order {proposal.order_id}."
                ),
                amount_at_risk_paise=proposal.amount_paise,
                evidence=computation.derivation,
            )

        # 3. Timing condition — the premature-payout guard.
        if not entitlement.entitled_now:
            blocker = next(
                (r for r in entitlement.reasons if r.startswith("NOT YET PAYABLE")),
                "a contractual condition is not yet satisfied",
            )
            return GateVerdict(
                proposal=proposal,
                decision=GateDecision.BLOCKED,
                tier=ConfidenceTier.BLOCKED,
                category=ExceptionCategory.PREMATURE_PAYOUT,
                reason=(
                    f"BLOCKED: {format_inr(proposal.amount_paise)} to "
                    f"{proposal.party_role} is not yet payable. {blocker}"
                ),
                expected_paise=entitlement.entitled_amount_paise,
                # Nothing was owed at this moment, so the whole amount was at risk.
                amount_at_risk_paise=proposal.amount_paise,
                evidence=list(entitlement.reasons),
            )

        # 4. Amount condition.
        expected = entitlement.entitled_amount_paise
        if proposal.amount_paise != expected:
            excess = proposal.amount_paise - expected
            return GateVerdict(
                proposal=proposal,
                decision=GateDecision.BLOCKED,
                tier=ConfidenceTier.BLOCKED,
                category=ExceptionCategory.RATE_MISMATCH,
                reason=(
                    f"BLOCKED: proposed {format_inr(proposal.amount_paise)} to "
                    f"{proposal.party_role} but contract "
                    f"v{ctx.policy.version} entitles {format_inr(expected)} "
                    f"(difference {format_inr(excess)})."
                ),
                expected_paise=expected,
                # Only the excess is loss; the entitled part was owed anyway.
                amount_at_risk_paise=max(0, excess),
                evidence=computation.derivation,
            )

        # 5. Clean.
        token = self._issue_token(proposal, approver=self.actor)
        return GateVerdict(
            proposal=proposal,
            decision=GateDecision.ALLOWED,
            tier=ConfidenceTier.AUTO_CLEAR,
            category=ExceptionCategory.NONE,
            reason=(
                f"Authorised: {format_inr(proposal.amount_paise)} to "
                f"{proposal.party_role} matches contract v{ctx.policy.version} "
                f"entitlement exactly and all conditions are satisfied."
            ),
            token=token,
            expected_paise=expected,
            evidence=computation.derivation,
        )

    # -- human override ----------------------------------------------------

    def approve_by_human(
        self, verdict: GateVerdict, *, approver: str, justification: str
    ) -> GateVerdict:
        """Second-person approval for a blocked or pending proposal.

        The maker-checker half of the control: the machine refused, a named human
        takes responsibility. The override is a *new* audit entry, not an edit of
        the refusal — the original block remains in the chain forever.
        """
        if verdict.allowed:
            return verdict

        token = self._issue_token(verdict.proposal, approver=approver)
        approved = GateVerdict(
            proposal=verdict.proposal,
            decision=GateDecision.APPROVED_BY_HUMAN,
            tier=verdict.tier,
            category=verdict.category,
            reason=(
                f"Human override by {approver}: {justification} "
                f"(original refusal: {verdict.reason})"
            ),
            token=token,
            expected_paise=verdict.expected_paise,
            amount_at_risk_paise=verdict.amount_at_risk_paise,
            evidence=verdict.evidence + [f"Override justification: {justification}"],
        )
        self.verdicts.append(approved)
        self.audit.record(
            actor=approver,
            action="gate.human_override",
            subject_id=verdict.proposal.order_id,
            outcome=str(GateDecision.APPROVED_BY_HUMAN),
            payload={
                "proposal_id": verdict.proposal.proposal_id,
                "proposal_hash": verdict.proposal.content_hash(),
                "original_decision": str(verdict.decision),
                "original_reason": verdict.reason,
                "justification": justification,
                "approver": approver,
                "token_id": token.token_id,
            },
        )
        return approved

    def reject_by_human(
        self, verdict: GateVerdict, *, approver: str, justification: str
    ) -> GateVerdict:
        rejected = GateVerdict(
            proposal=verdict.proposal,
            decision=GateDecision.REJECTED_BY_HUMAN,
            tier=verdict.tier,
            category=verdict.category,
            reason=f"Rejected by {approver}: {justification}",
            expected_paise=verdict.expected_paise,
            amount_at_risk_paise=verdict.amount_at_risk_paise,
            evidence=verdict.evidence,
        )
        self.verdicts.append(rejected)
        self.audit.record(
            actor=approver,
            action="gate.human_reject",
            subject_id=verdict.proposal.order_id,
            outcome=str(GateDecision.REJECTED_BY_HUMAN),
            payload={
                "proposal_id": verdict.proposal.proposal_id,
                "justification": justification,
            },
        )
        return rejected

    # -- execution ---------------------------------------------------------

    def execute(self, verdict: GateVerdict) -> dict:
        """Execute an authorised proposal. Refuses anything without a token."""
        result = self.client.execute_transfer(verdict.proposal, verdict.token)
        self.audit.record(
            actor=self.actor,
            action="razorpay.transfer",
            subject_id=verdict.proposal.order_id,
            outcome="executed",
            payload={
                "proposal_id": verdict.proposal.proposal_id,
                "razorpay_id": result.get("id"),
                "amount_paise": verdict.proposal.amount_paise,
                "mode": result.get("mode"),
                "simulated": result.get("simulated", False),
                "authorised_by": verdict.token.approver if verdict.token else None,
            },
        )
        return result

    # -- metrics -----------------------------------------------------------

    def prevented_loss_paise(self) -> int:
        """Headline metric: money that would have moved incorrectly, but didn't.

        Counts each blocked proposal once. A proposal later approved by a human
        is excluded — the money did move, so it was not prevented.
        """
        overridden = {
            v.proposal.proposal_id
            for v in self.verdicts
            if v.decision == GateDecision.APPROVED_BY_HUMAN
        }
        return sum(
            v.amount_at_risk_paise
            for v in self.verdicts
            if v.decision in (GateDecision.BLOCKED, GateDecision.PENDING_APPROVAL)
            and v.proposal.proposal_id not in overridden
        )

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for v in self.verdicts:
            counts[str(v.decision)] = counts.get(str(v.decision), 0) + 1
        return {
            "proposals": len(self.verdicts),
            "by_decision": counts,
            "prevented_loss_paise": self.prevented_loss_paise(),
            "prevented_loss_inr": format_inr(self.prevented_loss_paise()),
            "unsafe_action_attempts": self.client.unsafe_action_attempts,
            "transfers_executed": len(self.client.executed),
        }


PAYABLE_ROLES = (PartyRole.SELLER, PartyRole.DELIVERY_PARTNER)
