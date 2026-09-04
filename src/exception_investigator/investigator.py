"""Turn an entitlement finding into something a finance controller can act on.

The matcher answers "what is wrong". This module answers the three questions a
reviewer actually has: *why did this happen*, *what should I do about it*, and
*how urgent is it*.

Two layers, in this order
-------------------------
1. **Rule-based** (always runs, deterministic, offline). Each exception category
   has a written playbook entry: the mechanism, the recommended action, who
   owns it, and whether money should be held. This is the layer the metrics are
   computed from, so it never depends on a model being available.

2. **LLM narrative** (optional, additive). When an API key is present, the
   finding's evidence is turned into a short plain-language paragraph for the
   reviewer. It is strictly presentational — it can change the *wording* a human
   reads, never the category, the recommended action, or any number. Anything
   that affects a decision stays in deterministic code, so a model outage
   degrades the reading experience and nothing else.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from src.common.money import format_inr
from src.common.types import (
    ConfidenceTier,
    EntitlementDecision,
    ExceptionCategory,
    PartyRole,
)

logger = logging.getLogger(__name__)


class Severity:
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Playbook:
    mechanism: str
    """What actually goes wrong upstream to cause this."""

    recommended_action: str
    owner: str
    severity: str
    hold_funds: bool
    """Whether further settlement on this order should be frozen pending review."""


PLAYBOOK: dict[ExceptionCategory, Playbook] = {
    ExceptionCategory.PREMATURE_PAYOUT: Playbook(
        mechanism=(
            "The settlement job released the seller's share before the "
            "contractual precondition was met — typically because it keys off "
            "payment capture rather than the delivery-confirmation event, or "
            "because the hold window is configured in the payout scheduler "
            "instead of being derived from the contract."
        ),
        recommended_action=(
            "Hold further payouts for this seller pending review. Confirm whether "
            "delivery was subsequently confirmed; if it was not, raise a recovery "
            "against the seller. Fix the payout trigger to read the hold from the "
            "compiled contract rather than a static scheduler config."
        ),
        owner="Settlement Operations",
        severity=Severity.HIGH,
        hold_funds=True,
    ),
    ExceptionCategory.MISSING_REVERSAL: Playbook(
        mechanism=(
            "A customer refund was processed without the corresponding seller "
            "payout being clawed back first. The refund and reversal paths are "
            "usually separate workflows, and the ordering constraint lives in the "
            "contract rather than in either system."
        ),
        recommended_action=(
            "Raise the reversal immediately — the platform is currently carrying "
            "the refunded amount. Verify the reversal window in the contract has "
            "not expired; if it has, this becomes a recovery claim rather than a "
            "reversal. Add the ordering constraint to the refund workflow."
        ),
        owner="Finance Controller",
        severity=Severity.CRITICAL,
        hold_funds=True,
    ),
    ExceptionCategory.DUPLICATE_TRANSFER: Playbook(
        mechanism=(
            "The same entitlement settled more than once — almost always a retry "
            "after a timeout where the original call had in fact succeeded, and "
            "the transfer request carried no idempotency key."
        ),
        recommended_action=(
            "Reverse the duplicate transfer. Add an idempotency key derived from "
            "(order_id, party_role, settlement_cycle) to the transfer call so a "
            "retry cannot double-pay."
        ),
        owner="Settlement Operations",
        severity=Severity.CRITICAL,
        hold_funds=True,
    ),
    ExceptionCategory.RATE_MISMATCH: Playbook(
        mechanism=(
            "The settled amount implies a commission rate that no active contract "
            "version authorises — usually a rate cached in the settlement service "
            "that was not invalidated when the contract was amended."
        ),
        recommended_action=(
            "Recompute the correct entitlement, raise an adjustment for the "
            "difference, and re-scan the seller's other orders in the same period "
            "for the identical defect. Point the settlement service at the "
            "compiled policy so it cannot hold a stale rate."
        ),
        owner="Settlement Operations",
        severity=Severity.HIGH,
        hold_funds=False,
    ),
    ExceptionCategory.TAX_LINE_MISMATCH: Playbook(
        mechanism=(
            "Withholding differs from the contractual treatment — either the rate "
            "is wrong or it was applied to the wrong base (commission vs. gross "
            "payout). Both are common when the tax config is maintained separately "
            "from the commercial terms."
        ),
        recommended_action=(
            "Correct the withholding before the filing period closes; an "
            "under-withholding becomes a compliance exposure once filed. Reconcile "
            "the tax configuration against the compiled contract terms."
        ),
        owner="Tax and Compliance",
        severity=Severity.MEDIUM,
        hold_funds=False,
    ),
    ExceptionCategory.PROMOTION_FUNDING_MISMATCH: Playbook(
        mechanism=(
            "A discount was booked against a funding source the contract does not "
            "support — often because the campaign tool records a single funder "
            "while the agreement splits the cost, so one side silently absorbs the "
            "whole discount."
        ),
        recommended_action=(
            "Rebook the discount to the contractual split and adjust the affected "
            "party's settlement. Check whether the whole campaign is mis-booked "
            "rather than this order alone."
        ),
        owner="Category / Campaign Finance",
        severity=Severity.MEDIUM,
        hold_funds=False,
    ),
    ExceptionCategory.CONTRACT_VERSION_CONFLICT: Playbook(
        mechanism=(
            "Two contract versions both defensibly govern this order because the "
            "amendment's effective date can be read more than one way. This is not "
            "a system defect — the source document is genuinely ambiguous, and any "
            "automatic choice would be a guess with money attached."
        ),
        recommended_action=(
            "Do not auto-resolve. Obtain a written clarification of the amendment's "
            "effective date from the seller-contracting team, apply it to every "
            "order in the boundary window at once, and record the decision so the "
            "same window is never re-litigated. Until then, hold settlement."
        ),
        owner="Seller Contracting (with Finance Controller sign-off)",
        severity=Severity.CRITICAL,
        hold_funds=True,
    ),
    ExceptionCategory.AMBIGUOUS_UNRESOLVABLE: Playbook(
        mechanism=(
            "Either the contract does not specify an answer for this situation, or "
            "the settlement differs from entitlement in a way that matches no known "
            "defect pattern. The engine declines to invent a root cause."
        ),
        recommended_action=(
            "Manual investigation. Pull the order's full event timeline and the "
            "governing clause text, both linked from this record. If a new "
            "recurring pattern emerges, encode it as its own exception category "
            "rather than leaving it in this bucket."
        ),
        owner="Finance Controller",
        severity=Severity.MEDIUM,
        hold_funds=True,
    ),
    ExceptionCategory.NONE: Playbook(
        mechanism="No discrepancy found.",
        recommended_action="No action required.",
        owner="—",
        severity=Severity.INFO,
        hold_funds=False,
    ),
}


@dataclass(slots=True)
class Investigation:
    """A reviewer-facing case file for one order."""

    order_id: str
    category: ExceptionCategory
    tier: ConfidenceTier
    severity: str
    headline: str
    mechanism: str
    recommended_action: str
    owner: str
    hold_funds: bool
    amount_at_stake_paise: int
    evidence: list[str] = field(default_factory=list)
    narrative: str = ""
    """Optional LLM-written paragraph. Presentational only."""

    def to_row(self) -> dict:
        return {
            "order_id": self.order_id,
            "category": str(self.category),
            "tier": str(self.tier),
            "severity": self.severity,
            "headline": self.headline,
            "owner": self.owner,
            "hold_funds": self.hold_funds,
            "amount_at_stake_paise": self.amount_at_stake_paise,
            "amount_at_stake": format_inr(self.amount_at_stake_paise),
            "recommended_action": self.recommended_action,
        }


_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


def investigate(decision: EntitlementDecision) -> Investigation:
    """Build the case file for one decision. Deterministic and offline."""
    play = PLAYBOOK.get(decision.category, PLAYBOOK[ExceptionCategory.AMBIGUOUS_UNRESOLVABLE])

    at_stake = decision.total_abs_variance_paise()
    if not at_stake and decision.category != ExceptionCategory.NONE:
        # Version conflicts and unreadable contracts produce no variance figure
        # because no entitlement was computed at all. The exposure is then the
        # whole seller payout, since none of it can be justified yet.
        at_stake = abs(decision.actual_paise.get(PartyRole.SELLER, 0))

    return Investigation(
        order_id=decision.order_id,
        category=decision.category,
        tier=decision.tier,
        severity=play.severity,
        headline=decision.explanation,
        mechanism=play.mechanism,
        recommended_action=play.recommended_action,
        owner=play.owner,
        hold_funds=play.hold_funds,
        amount_at_stake_paise=at_stake,
        evidence=list(decision.evidence),
    )


def triage(decisions: list[EntitlementDecision]) -> list[Investigation]:
    """Build the review queue, most urgent first.

    Ordered by severity, then by money at stake — a critical ₹50 issue still
    outranks a medium ₹50,000 one, because criticals are ordering/duplication
    defects that recur across every order until the upstream cause is fixed.
    """
    cases = [investigate(d) for d in decisions if d.category != ExceptionCategory.NONE]
    cases.sort(
        key=lambda c: (_SEVERITY_ORDER.get(c.severity, 9), -c.amount_at_stake_paise)
    )
    return cases


# ---------------------------------------------------------------------------
# Optional LLM narrative
# ---------------------------------------------------------------------------

_NARRATIVE_SYSTEM = """\
You write one short paragraph for a finance controller reviewing a settlement \
exception. Constraints:
- 2-3 sentences, plain language, no jargon, no bullet points.
- State what happened and why it matters commercially.
- Use ONLY the facts given. Never invent amounts, dates, rates, or causes.
- Do not recommend an action; the reviewer already has one.
- If the finding is that the contract is ambiguous, say plainly that the system \
declined to decide and why that is the correct outcome.
"""


def add_narrative(
    investigation: Investigation, *, model: str | None = None
) -> Investigation:
    """Attach an LLM-written paragraph. Silently a no-op without a key.

    Failure here is deliberately non-fatal: the case file is already complete
    and actionable without it.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return investigation
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        facts = "\n".join(
            [
                f"Order: {investigation.order_id}",
                f"Finding: {investigation.category}",
                f"Summary: {investigation.headline}",
                f"Amount at stake: {format_inr(investigation.amount_at_stake_paise)}",
                "Evidence:",
                *(f"  - {e}" for e in investigation.evidence[:12]),
            ]
        )
        message = client.messages.create(
            model=model or os.getenv("ENTITLEGRAPH_LLM_MODEL", "claude-sonnet-5"),
            max_tokens=300,
            system=_NARRATIVE_SYSTEM,
            messages=[{"role": "user", "content": facts}],
        )
        investigation.narrative = "".join(
            b.text for b in message.content if getattr(b, "type", "") == "text"
        ).strip()
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning("narrative generation failed for %s: %s", investigation.order_id, exc)
    return investigation
