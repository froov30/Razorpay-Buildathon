"""Shared domain types for EntitleGraph.

These are the *ledger-side* entities — what a marketplace's systems actually
record. The contract-side representation (what was *promised*) lives in
``src.contract_compiler.dsl``. Keeping the two apart is the whole point of the
system: entitlement checking is the comparison between them.

All monetary fields are integer paise (see ``src.common.money``).
All data produced against these types in this repository is SYNTHETIC.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class StrEnum(str, enum.Enum):
    """String enum that serialises to its value (py3.10-compatible)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


# ---------------------------------------------------------------------------
# Decision vocabulary
# ---------------------------------------------------------------------------


class ConfidenceTier(StrEnum):
    """How much the engine trusts its own entitlement conclusion.

    A bare true/false is banned by design. Every decision lands in one of these
    three tiers, and the tier — not the boolean — drives what the gate does.
    """

    AUTO_CLEAR = "auto_clear"
    """Computed entitlement matches the settlement exactly and every input the
    computation depended on was unambiguous. Safe to close without a human."""

    NEEDS_REVIEW = "needs_review"
    """Either a variance was found, or an input was ambiguous enough that the
    engine declines to assert a conclusion. Routed to the review queue."""

    BLOCKED = "blocked"
    """A proposed money movement would violate the contract. The gate refuses
    to let it fire. Requires explicit human approval to proceed."""


class ExceptionCategory(StrEnum):
    """Root-cause classification for a non-clean record."""

    NONE = "none"
    PREMATURE_PAYOUT = "premature_payout"
    """Seller paid before a contractually required condition (usually delivery
    confirmation or a hold window) was satisfied."""

    MISSING_REVERSAL = "missing_reversal"
    """Customer refund issued without the matching seller payout reversal."""

    DUPLICATE_TRANSFER = "duplicate_transfer"
    """The same entitlement was settled more than once."""

    TAX_LINE_MISMATCH = "tax_line_mismatch"
    """TDS/GST withheld differs from the contractually specified treatment."""

    RATE_MISMATCH = "rate_mismatch"
    """Commission applied at a rate that no active contract version authorises."""

    PROMOTION_FUNDING_MISMATCH = "promotion_funding_mismatch"
    """Discount funded against the wrong party's budget, or double-funded."""

    CONTRACT_VERSION_CONFLICT = "contract_version_conflict"
    """Two contract versions both plausibly govern this order and the engine
    cannot determine which applies. Deliberately unresolvable — see
    docs/CHANGELOG.md, "What broke"."""

    AMBIGUOUS_UNRESOLVABLE = "ambiguous_unresolvable"
    """The contract genuinely does not specify an answer for this situation."""


class GateDecision(StrEnum):
    """Outcome of the maker-checker entitlement gate."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    PENDING_APPROVAL = "pending_approval"
    APPROVED_BY_HUMAN = "approved_by_human"
    REJECTED_BY_HUMAN = "rejected_by_human"


class RazorpayMode(StrEnum):
    """Execution mode of the Razorpay client — surfaced loudly, never silent."""

    LIVE_TEST = "LIVE-TEST"
    """Real API calls against Razorpay's TEST environment. No real money."""

    MOCK = "MOCK"
    """No network calls at all. Local simulation."""


class PartyRole(StrEnum):
    """Who a share of an order's money belongs to."""

    SELLER = "seller"
    DELIVERY_PARTNER = "delivery_partner"
    PLATFORM = "platform"
    PROMOTION_BUDGET = "promotion_budget"


class LedgerEventType(StrEnum):
    """Event kinds in the entitlement graph's append-only log."""

    ORDER_PLACED = "order_placed"
    PAYMENT_CAPTURED = "payment_captured"
    PROMOTION_APPLIED = "promotion_applied"
    DELIVERY_CONFIRMED = "delivery_confirmed"
    DELIVERY_FAILED = "delivery_failed"
    TRANSFER_EXECUTED = "transfer_executed"
    REFUND_ISSUED = "refund_issued"
    REVERSAL_EXECUTED = "reversal_executed"


# ---------------------------------------------------------------------------
# Ledger entities (synthetic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContractSource:
    """A raw, natural-language merchant agreement version — compiler input.

    This is deliberately *unstructured text*. The compiler's job is to turn it
    into a typed policy; if we stored structured terms here there would be no
    interpretation problem left to solve, and no differentiation from Recon.
    """

    contract_id: str
    version: int
    seller_id: str
    effective_from: datetime | None
    effective_to: datetime | None
    body: str
    """The agreement text, as a human would sign it."""

    notes: str = ""
    """Provenance note, e.g. how this version was communicated."""


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    seller_id: str
    placed_at: datetime
    gross_amount_paise: int
    """What the customer was charged, before any split."""

    shipping_fee_paise: int = 0
    tax_collected_paise: int = 0
    delivery_partner_id: str | None = None
    promotion_id: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentEvent:
    payment_id: str
    order_id: str
    captured_at: datetime
    amount_paise: int
    method: str = "upi"


@dataclass(frozen=True, slots=True)
class DeliveryEvent:
    delivery_id: str
    order_id: str
    occurred_at: datetime
    confirmed: bool
    delivery_partner_id: str | None = None


@dataclass(frozen=True, slots=True)
class Promotion:
    promotion_id: str
    order_id: str
    discount_paise: int
    campaign: str
    declared_funder: PartyRole | None = None
    """What the ledger *claims* funded it. The contract decides if that's right."""


@dataclass(frozen=True, slots=True)
class Transfer:
    """An actual money movement that already happened (or was proposed)."""

    transfer_id: str
    order_id: str
    party_role: PartyRole
    party_id: str
    amount_paise: int
    executed_at: datetime
    tds_withheld_paise: int = 0
    razorpay_transfer_id: str | None = None
    reversed_by: str | None = None


@dataclass(frozen=True, slots=True)
class RefundEvent:
    refund_id: str
    order_id: str
    issued_at: datetime
    amount_paise: int
    reason: str = "customer_return"


@dataclass(frozen=True, slots=True)
class ReversalEvent:
    reversal_id: str
    transfer_id: str
    order_id: str
    executed_at: datetime
    amount_paise: int


# ---------------------------------------------------------------------------
# Engine output
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartyEntitlement:
    """What one party was owed for one order, and why."""

    party_role: PartyRole
    party_id: str
    entitled_amount_paise: int
    entitled_now: bool
    """False when the amount is correct but a condition (delivery, hold window)
    is not yet satisfied — the distinction that catches premature payouts."""

    reasons: tuple[str, ...] = ()
    """Human-readable derivation steps, in order of application."""


@dataclass(slots=True)
class EntitlementDecision:
    """The engine's full conclusion for a single order."""

    order_id: str
    contract_id: str
    contract_version: int | None
    tier: ConfidenceTier
    category: ExceptionCategory
    expected: dict[PartyRole, PartyEntitlement] = field(default_factory=dict)
    actual_paise: dict[PartyRole, int] = field(default_factory=dict)
    variance_paise: dict[PartyRole, int] = field(default_factory=dict)
    explanation: str = ""
    evidence: list[str] = field(default_factory=list)
    policy_hash: str | None = None
    """Content hash of the compiled policy used — makes a decision replayable."""

    def total_abs_variance_paise(self) -> int:
        return sum(abs(v) for v in self.variance_paise.values())

    def to_row(self) -> dict[str, Any]:
        """Flatten for dashboard/reporting."""
        return {
            "order_id": self.order_id,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "tier": str(self.tier),
            "category": str(self.category),
            "abs_variance_paise": self.total_abs_variance_paise(),
            "explanation": self.explanation,
            "policy_hash": self.policy_hash,
        }
