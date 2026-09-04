"""Match computed entitlement against what actually moved.

This is the comparison that gives the project its name. The computation says
what the contract promised; the ledger says what happened; the matcher reports
where those disagree and — importantly — *why*, with a confidence tier attached.

Tiering rules
-------------
``AUTO_CLEAR``
    Every party's variance is zero, no timing breach, no structural defect.

``NEEDS_REVIEW``
    Anything else that is *observable*: a variance, a premature payout, a
    duplicate, a missing reversal, a tax mismatch, or an unresolvable contract.

The matcher never emits ``BLOCKED``. Blocking is a statement about a *proposed*
action and belongs to the gate (``src.settlement_engine.gate``); the matcher
describes settlements that already happened.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from dataclasses import replace

from src.common.money import apply_bps, format_inr
from src.common.types import (
    ConfidenceTier,
    EntitlementDecision,
    ExceptionCategory,
    PartyRole,
    ReversalEvent,
    Transfer,
)
from src.contract_compiler.resolver import Resolution
from src.settlement_engine.compute import (
    Computation,
    OrderContext,
    compute_entitlements,
)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def unresolved_decision(
    order_id: str,
    resolution: Resolution,
    transfers: list[Transfer] | None = None,
) -> EntitlementDecision:
    """Build the decision for an order whose governing contract is unresolvable.

    This is the honest-refusal path. Nothing is computed, nothing is asserted,
    and the reviewer gets the competing readings rather than a number the engine
    cannot stand behind.
    """
    is_version_conflict = len({c.policy.version for c in resolution.candidates}) > 1
    category = (
        ExceptionCategory.CONTRACT_VERSION_CONFLICT
        if is_version_conflict
        else ExceptionCategory.AMBIGUOUS_UNRESOLVABLE
    )
    evidence = [resolution.conflict_reason]
    if resolution.candidates:
        evidence.append(f"Candidate versions: {resolution.candidate_summary()}")
    for amb in resolution.ambiguities:
        evidence.append(
            f"Ambiguity on {amb.field_path}: {amb.reason}"
            + (f" Candidates: {', '.join(amb.candidates)}." if amb.candidates else "")
        )
        if amb.source_quote:
            evidence.append(f'Clause text: "{amb.source_quote}"')

    # No entitlement can be computed, so there is no variance to report. What
    # *is* known — and what the reviewer needs — is how much money already moved
    # under terms the system cannot justify. Recording it keeps the exposure
    # visible instead of showing a misleading zero.
    actual: dict[PartyRole, int] = defaultdict(int)
    for t in transfers or []:
        actual[t.party_role] += t.amount_paise
    if actual:
        settled = sum(abs(v) for v in actual.values())
        evidence.append(
            f"{format_inr(settled)} has already been settled across "
            f"{len(actual)} parties under terms that cannot be justified."
        )

    return EntitlementDecision(
        order_id=order_id,
        contract_id=resolution.contract_id,
        contract_version=None,
        tier=ConfidenceTier.NEEDS_REVIEW,
        category=category,
        actual_paise=dict(actual),
        explanation=resolution.conflict_reason,
        evidence=evidence,
    )


def match(
    ctx: OrderContext,
    computation: Computation,
    transfers: list[Transfer],
    reversals: list[ReversalEvent] | None = None,
) -> EntitlementDecision:
    """Compare expected entitlement against executed transfers for one order."""
    reversals = reversals or []
    policy = ctx.policy
    order = ctx.order

    reversed_by_transfer: dict[str, int] = defaultdict(int)
    for rev in reversals:
        reversed_by_transfer[rev.transfer_id] += rev.amount_paise

    actual: dict[PartyRole, int] = defaultdict(int)
    by_role: dict[PartyRole, list[Transfer]] = defaultdict(list)
    tds_withheld = 0
    for t in transfers:
        reversed_amount = reversed_by_transfer.get(t.transfer_id, 0)
        net_amount = t.amount_paise - reversed_amount
        actual[t.party_role] += net_amount
        by_role[t.party_role].append(t)
        # Withholding follows the payment it was withheld from. If a payout is
        # clawed back, the TDS deducted from it is no longer withheld either —
        # comparing gross withholding against an entitlement that has been
        # prorated for the refund would flag every correctly-reversed order.
        if reversed_amount and t.amount_paise:
            ratio_bps = min(10_000, (reversed_amount * 10_000) // t.amount_paise)
            tds_withheld += t.tds_withheld_paise - apply_bps(
                t.tds_withheld_paise, ratio_bps
            )
        else:
            tds_withheld += t.tds_withheld_paise

    # Variance is measured against what is *payable now*, not against the full
    # entitlement. An order still inside its delivery hold with no payout yet is
    # correct — the seller is owed money but is not owed it today. Comparing
    # against the full entitlement would flag every in-flight order as a
    # shortfall and bury the real exceptions in noise.
    variance: dict[PartyRole, int] = {}
    payable_now: dict[PartyRole, int] = {}
    for role in set(computation.entitlements) | set(actual):
        ent = computation.entitlements.get(role)
        due = ent.entitled_amount_paise if (ent and ent.entitled_now) else 0
        payable_now[role] = due
        variance[role] = actual.get(role, 0) - due

    evidence: list[str] = list(computation.derivation)
    findings: list[tuple[ExceptionCategory, str]] = []

    # -- duplicate transfers ---------------------------------------------
    for role, group in by_role.items():
        if len(group) > 1:
            amounts = [t.amount_paise for t in group]
            if len(set(amounts)) < len(amounts):
                dupe = next(a for a in amounts if amounts.count(a) > 1)
                findings.append(
                    (
                        ExceptionCategory.DUPLICATE_TRANSFER,
                        f"{len(group)} transfers to {role} for this order include a "
                        f"repeated amount of {format_inr(dupe)} "
                        f"({', '.join(t.transfer_id for t in group)}).",
                    )
                )

    # -- premature payout -------------------------------------------------
    # Re-evaluate entitlement as of the moment each transfer fired. A payout
    # that is correct in amount but early is still a contract breach, and it is
    # invisible to any check that only compares totals.
    for role, group in by_role.items():
        if role not in (PartyRole.SELLER, PartyRole.DELIVERY_PARTNER):
            continue
        for t in group:
            at_execution = compute_entitlements(
                replace(ctx, as_of=_as_utc(t.executed_at))
            )
            ent = at_execution.entitlements.get(role)
            if ent and not ent.entitled_now:
                blocker = next(
                    (r for r in ent.reasons if r.startswith("NOT YET PAYABLE")),
                    "condition not satisfied at time of transfer",
                )
                findings.append(
                    (
                        ExceptionCategory.PREMATURE_PAYOUT,
                        f"Transfer {t.transfer_id} of {format_inr(t.amount_paise)} to "
                        f"{role} executed {_as_utc(t.executed_at).isoformat()} before "
                        f"entitlement arose. {blocker}",
                    )
                )

    # -- missing reversal --------------------------------------------------
    if ctx.refunds and policy.refund.reversal_must_precede_refund:
        earliest_refund = min(_as_utc(r.issued_at) for r in ctx.refunds)
        seller_transfers = by_role.get(PartyRole.SELLER, [])
        paid_before_refund = [
            t for t in seller_transfers if _as_utc(t.executed_at) <= earliest_refund
        ]
        reversed_amount = sum(
            reversed_by_transfer.get(t.transfer_id, 0) for t in paid_before_refund
        )
        if paid_before_refund and reversed_amount == 0:
            findings.append(
                (
                    ExceptionCategory.MISSING_REVERSAL,
                    f"Customer refunded {format_inr(ctx.refunded_paise())} on "
                    f"{earliest_refund.date().isoformat()} but the seller payout "
                    f"({', '.join(t.transfer_id for t in paid_before_refund)}) was "
                    f"never reversed. Contract requires reversal to precede refund.",
                )
            )

    # -- tax line ----------------------------------------------------------
    # Only meaningful once a seller payout has actually fired. An order still
    # awaiting delivery has withheld nothing because it has paid nothing, which
    # is correct, not a tax defect.
    seller_paid = bool(by_role.get(PartyRole.SELLER))
    if seller_paid and tds_withheld != computation.tds_paise:
        findings.append(
            (
                ExceptionCategory.TAX_LINE_MISMATCH,
                f"TDS withheld {format_inr(tds_withheld)} but contract specifies "
                f"{policy.tax.tds_on_commission_bps / 100:.2f}% of "
                f"{policy.tax.applies_to} = {format_inr(computation.tds_paise)}.",
            )
        )

    # -- rate mismatch -----------------------------------------------------
    # Deliberately not gated on "no other findings": a payout settled at the
    # wrong rate usually also withholds the wrong TDS, and suppressing the rate
    # finding because the tax finding fired first would report the symptom and
    # hide the cause. Both are collected; `_rank` decides which leads.
    seller_var = variance.get(PartyRole.SELLER, 0)
    if seller_var:
        implied = _implied_rate_bps(
            computation, actual.get(PartyRole.SELLER, 0), tds_withheld
        )
        if (
            implied is not None
            and implied != policy.commission.rate_bps
            and _is_quotable_rate(implied)
        ):
            findings.append(
                (
                    ExceptionCategory.RATE_MISMATCH,
                    f"Seller received {format_inr(actual.get(PartyRole.SELLER, 0))} "
                    f"against an entitlement of "
                    f"{format_inr(computation.expected_paise(PartyRole.SELLER))}. The "
                    f"settled amount implies a commission rate of {implied / 100:.2f}%, "
                    f"but contract v{policy.version} specifies "
                    f"{(policy.commission.rate_bps or 0) / 100:.2f}%.",
                )
            )

    # -- promotion funding -------------------------------------------------
    if ctx.promotion and ctx.promotion.declared_funder is not None:
        declared = ctx.promotion.declared_funder
        platform_bps = policy.promotion_funding.platform_share_bps
        seller_bps = policy.promotion_funding.seller_share_bps
        # The ledger names a single funder; the contract may not have one. Where
        # the split is even, no party solely funds the discount, so *any* sole
        # attribution contradicts the agreement.
        if platform_bps > seller_bps:
            expected_funder: PartyRole | None = PartyRole.PLATFORM
        elif seller_bps > platform_bps:
            expected_funder = PartyRole.SELLER
        else:
            expected_funder = None
        if declared != expected_funder and declared != PartyRole.PROMOTION_BUDGET:
            findings.append(
                (
                    ExceptionCategory.PROMOTION_FUNDING_MISMATCH,
                    f"Discount of {format_inr(ctx.promotion.discount_paise)} recorded "
                    f"as funded by {declared}, but the contract splits promotion "
                    f"funding {policy.promotion_funding.platform_share_bps / 100:.0f}% "
                    f"platform / {policy.promotion_funding.seller_share_bps / 100:.0f}% "
                    f"seller.",
                )
            )

    # -- residual variance with no identified cause -------------------------
    if not findings and any(v != 0 for v in variance.values()):
        detail = ", ".join(
            f"{role}: {format_inr(v)}" for role, v in variance.items() if v
        )
        findings.append(
            (
                ExceptionCategory.AMBIGUOUS_UNRESOLVABLE,
                f"Settlement differs from entitlement ({detail}) and no known "
                f"exception pattern explains it. Escalated rather than guessed.",
            )
        )

    if findings:
        category, explanation = _rank(findings)
        evidence.extend(msg for _, msg in findings)
        tier = ConfidenceTier.NEEDS_REVIEW
    else:
        pending = [
            str(role)
            for role, ent in computation.entitlements.items()
            if not ent.entitled_now
        ]
        if pending:
            explanation = (
                f"Consistent with contract v{policy.version}: nothing has been paid "
                f"to {', '.join(pending)} and nothing is due yet "
                f"(contractual condition not satisfied)."
            )
        else:
            explanation = (
                f"Settlement matches contract v{policy.version} exactly across "
                f"{len(computation.entitlements)} parties."
            )
        category = ExceptionCategory.NONE
        tier = ConfidenceTier.AUTO_CLEAR

    return EntitlementDecision(
        order_id=order.order_id,
        contract_id=policy.contract_id,
        contract_version=policy.version,
        tier=tier,
        category=category,
        expected=dict(computation.entitlements),
        actual_paise=dict(actual),
        variance_paise=dict(variance),
        explanation=explanation,
        evidence=evidence,
        policy_hash=policy.content_hash(),
    )


# Severity order — the most actionable root cause wins when several fire.
_SEVERITY = [
    ExceptionCategory.CONTRACT_VERSION_CONFLICT,
    ExceptionCategory.MISSING_REVERSAL,
    ExceptionCategory.PREMATURE_PAYOUT,
    ExceptionCategory.DUPLICATE_TRANSFER,
    ExceptionCategory.RATE_MISMATCH,
    ExceptionCategory.PROMOTION_FUNDING_MISMATCH,
    ExceptionCategory.TAX_LINE_MISMATCH,
    ExceptionCategory.AMBIGUOUS_UNRESOLVABLE,
]


def _rank(
    findings: list[tuple[ExceptionCategory, str]]
) -> tuple[ExceptionCategory, str]:
    for category in _SEVERITY:
        for found, msg in findings:
            if found == category:
                return category, msg
    return findings[0]


# Commercial commission rates are quoted in quarter-percent steps. A settled
# amount implying 35.00% points at a real (if wrong) contractual rate; one
# implying 35.03% points at something else entirely — a fee, an adjustment, a
# partial claw-back — and calling that a "rate mismatch" would hand the reviewer
# a confident, wrong root cause. Better to fall through to "unexplained".
_RATE_QUANTUM_BPS = 25
_RATE_TOLERANCE_BPS = 1


def _is_quotable_rate(implied_bps: int) -> bool:
    nearest = round(implied_bps / _RATE_QUANTUM_BPS) * _RATE_QUANTUM_BPS
    return abs(implied_bps - nearest) <= _RATE_TOLERANCE_BPS


def _implied_rate_bps(
    computation: Computation, actual_seller_paise: int, actual_tds_paise: int
) -> int | None:
    """Back out the commission rate implied by what the seller actually received.

    Inverts the seller-share formula:
        seller = net - commission + platform_discount_share - tds

    Uses the TDS **actually withheld**, not the contractual figure. Using the
    expected TDS would fold any tax discrepancy into the implied commission and
    shift it off a quotable rate — so an order settled at exactly the wrong rate
    *and* the wrong TDS would report only the tax symptom and lose the rate
    cause. Inverting against what really happened isolates the two.
    """
    if computation.refund_ratio_bps:
        return None
    net = computation.net_order_value_paise
    if net <= 0:
        return None
    implied_commission = (
        net + computation.platform_discount_share_paise
        - actual_tds_paise
        - actual_seller_paise
    )
    if implied_commission < 0:
        return None
    return round(implied_commission * 10_000 / net)
