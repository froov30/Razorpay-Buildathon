"""The Policy DSL — the typed representation of what a contract *promised*.

This module is the load-bearing abstraction of the whole project. A merchant
agreement arrives as prose; the settlement engine needs something it can
evaluate deterministically. The Policy DSL is that intermediate form.

Design rules, and why each exists
---------------------------------
1. **Every rate is integer basis points.** No floats, no "0.7" that might be a
   share or a percentage depending on who wrote it.

2. **A policy can refuse to have a value.** Any field may instead carry an
   :class:`Ambiguity`. This is the single most important property in the DSL:
   an extraction step that is forced to always produce a number will invent one,
   and an invented commission rate silently produces a confident wrong answer.
   Making "I don't know, and here is exactly what was unclear" a first-class
   representable state is what lets the engine route to human review honestly.

3. **Policies are content-addressed.** :meth:`Policy.content_hash` is a stable
   hash of the canonical JSON. Every entitlement decision records the hash of
   the policy that produced it, so any decision can be replayed and audited
   against the exact terms used — and so the compile cache can be keyed safely.

4. **Canonical serialisation.** ``to_json`` sorts keys and fixes separators, so
   the same policy always produces byte-identical JSON and therefore the same
   hash on every machine and every run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from src.common.types import PartyRole, StrEnum

DSL_VERSION = "1.0"


class AmbiguitySeverity(StrEnum):
    BLOCKING = "blocking"
    """The engine cannot compute a defensible entitlement without resolving
    this. Any order touching it routes to human review."""

    ADVISORY = "advisory"
    """Worth surfacing to a reviewer, but a defensible default exists and the
    computation can proceed."""


@dataclass(frozen=True, slots=True)
class Ambiguity:
    """A thing the contract does not say clearly enough to act on.

    The presence of one of these is a *feature*. It is the difference between
    "the seller gets 65%" and "the seller gets 65% or 70% and this document
    does not tell me which, because the effective date is written two
    incompatible ways".
    """

    field_path: str
    """Dotted path to the affected field, e.g. ``commission.rate_bps``."""

    reason: str
    """Plain-language statement of what is unclear. Shown to the reviewer."""

    candidates: tuple[str, ...] = ()
    """The competing readings, verbatim where possible."""

    severity: AmbiguitySeverity = AmbiguitySeverity.BLOCKING

    source_quote: str = ""
    """The clause text this was read from — evidence for the reviewer."""


@dataclass(frozen=True, slots=True)
class CommissionClause:
    """What the platform keeps from an order."""

    rate_bps: int | None
    """Platform commission in basis points. 30% -> 3000. ``None`` when ambiguous."""

    applies_to: Literal["order_net", "order_gross"] = "order_net"
    """``order_net`` = gross minus discounts and shipping; ``order_gross`` = the
    full charged amount. Marketplaces genuinely differ on this and getting it
    wrong is a large, silent error."""

    minimum_paise: int = 0
    source_quote: str = ""


@dataclass(frozen=True, slots=True)
class SettlementHoldClause:
    """When the seller's share becomes payable — the premature-payout guard."""

    requires_delivery_confirmation: bool = True
    hold_hours_after_delivery: int = 0
    """Additional cooling-off window after delivery before payout is due."""

    source_quote: str = ""


@dataclass(frozen=True, slots=True)
class PromotionFundingClause:
    """Who absorbs a discount. Split in bps; must sum to 10_000.

    Either share may be ``None``. That is not a defect — it is the extractor
    correctly declining to assign a number when the contract does not support
    one, exactly as the DSL asks every field to behave. A model that answered
    "I cannot tell who funds this" and then had its answer crash the type it
    was answering into would be punished for being honest.
    """

    platform_share_bps: int | None = 10_000
    seller_share_bps: int | None = 0
    source_quote: str = ""

    def is_known(self) -> bool:
        return self.platform_share_bps is not None and self.seller_share_bps is not None

    def is_balanced(self) -> bool:
        """True only when both shares are known and sum to exactly 100%.

        An unknown split is not balanced. It is also not *unbalanced* in the
        arithmetic sense, but treating it as balanced would let an unreadable
        clause pass validation and reach the settlement engine.
        """
        if not self.is_known():
            return False
        return self.platform_share_bps + self.seller_share_bps == 10_000  # type: ignore[operator]


@dataclass(frozen=True, slots=True)
class RefundClause:
    """What must happen, and in what order, when a customer is refunded."""

    commission_refundable: bool = True
    """Whether the platform returns its commission on a refunded order."""

    reversal_must_precede_refund: bool = True
    """Whether the seller payout must be clawed back *before* the customer is
    refunded. When True and the ledger shows a refund with no prior reversal,
    that is a MISSING_REVERSAL exception."""

    reversal_window_hours: int = 168
    source_quote: str = ""


@dataclass(frozen=True, slots=True)
class TaxClause:
    """Withholding treatment on the seller's share."""

    tds_on_commission_bps: int = 0
    """TDS withheld, expressed against the commission base."""

    applies_to: Literal["commission", "seller_payout"] = "commission"
    source_quote: str = ""


@dataclass(frozen=True, slots=True)
class DeliveryFeeClause:
    """Fixed fee owed to the delivery partner, if the contract names one."""

    flat_fee_paise: int = 0
    payable_on_confirmation_only: bool = True
    source_quote: str = ""


@dataclass(frozen=True, slots=True)
class EffectivePeriod:
    """When this contract version governs.

    ``ambiguous`` is the mechanism behind the required "what broke" scenario:
    an amendment communicated as "effective this month" against a signature
    dated mid-month yields two defensible start dates, and the engine must not
    pick one.
    """

    starts_at: datetime | None = None
    ends_at: datetime | None = None
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class Provenance:
    """How this policy came to exist — auditability of the extraction itself."""

    backend: str
    """``llm`` or ``deterministic``."""

    model: str = ""
    compiled_at: str = ""
    source_sha256: str = ""
    """Hash of the contract text this was compiled from."""

    notes: str = ""


@dataclass
class Policy:
    """A single compiled contract version."""

    contract_id: str
    version: int
    seller_id: str
    effective: EffectivePeriod
    commission: CommissionClause
    hold: SettlementHoldClause
    promotion_funding: PromotionFundingClause
    refund: RefundClause
    tax: TaxClause
    delivery_fee: DeliveryFeeClause
    ambiguities: list[Ambiguity] = field(default_factory=list)
    provenance: Provenance | None = None
    dsl_version: str = DSL_VERSION

    # -- ambiguity helpers -------------------------------------------------

    def blocking_ambiguities(self) -> list[Ambiguity]:
        return [
            a for a in self.ambiguities if a.severity == AmbiguitySeverity.BLOCKING
        ]

    def term_blocking_ambiguities(self) -> list[Ambiguity]:
        """Blocking ambiguities that prevent evaluating the terms themselves.

        Deliberately excludes ``effective.starts_at``. Whether an ambiguous
        effective date actually matters is a *per-order* question — an order
        placed well after every candidate start date is governed by this version
        under all readings, so the date ambiguity is immaterial to it even
        though the document remains just as unclear. That determination belongs
        to :mod:`src.contract_compiler.resolver`, which evaluates each reading
        as a complete world; by the time a policy reaches the computation it has
        already been elected, so treating a date ambiguity as un-computable here
        would refuse orders the system can in fact answer confidently.
        """
        return [
            a for a in self.blocking_ambiguities() if a.field_path != "effective.starts_at"
        ]

    def has_date_ambiguity(self) -> bool:
        return any(
            a.field_path == "effective.starts_at" for a in self.blocking_ambiguities()
        )

    def is_computable(self) -> bool:
        """True when this version's *terms* yield a defensible entitlement.

        Every value the settlement engine will dereference must be present. An
        unknown promotion split reaches ``apply_bps`` as ``None`` and fails
        there, deep inside a money calculation, rather than here where the
        reason can still be explained to a reviewer.
        """
        return (
            not self.term_blocking_ambiguities()
            and self.commission.rate_bps is not None
            and self.promotion_funding.is_known()
        )

    def seller_share_bps(self) -> int | None:
        """Complement of the commission rate — what the seller keeps."""
        if self.commission.rate_bps is None:
            return None
        return 10_000 - self.commission.rate_bps

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        def encode(obj: Any) -> Any:
            if isinstance(obj, datetime):
                return obj.isoformat()
            if isinstance(obj, Enum):
                return obj.value
            return obj

        raw = asdict(self)

        def walk(node: Any) -> Any:
            if isinstance(node, dict):
                return {k: walk(v) for k, v in node.items()}
            if isinstance(node, (list, tuple)):
                return [walk(v) for v in node]
            return encode(node)

        return walk(raw)

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, fixed separators, stable across runs."""
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def content_hash(self) -> str:
        """Stable content address, excluding provenance timestamps.

        Provenance is stripped before hashing so that recompiling identical
        contract text produces an identical hash even though it was compiled at
        a different time by a different backend. The hash identifies *the terms*,
        not the compilation run.
        """
        payload = self.to_dict()
        payload.pop("provenance", None)
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        def parse_dt(value: Any) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        eff = data["effective"]
        prov = data.get("provenance")
        return cls(
            contract_id=data["contract_id"],
            version=int(data["version"]),
            seller_id=data["seller_id"],
            effective=EffectivePeriod(
                starts_at=parse_dt(eff.get("starts_at")),
                ends_at=parse_dt(eff.get("ends_at")),
                ambiguous=bool(eff.get("ambiguous", False)),
            ),
            commission=CommissionClause(**data["commission"]),
            hold=SettlementHoldClause(**data["hold"]),
            promotion_funding=PromotionFundingClause(**data["promotion_funding"]),
            refund=RefundClause(**data["refund"]),
            tax=TaxClause(**data["tax"]),
            delivery_fee=DeliveryFeeClause(**data["delivery_fee"]),
            ambiguities=[
                Ambiguity(
                    field_path=a["field_path"],
                    reason=a["reason"],
                    candidates=tuple(a.get("candidates", ())),
                    severity=AmbiguitySeverity(a.get("severity", "blocking")),
                    source_quote=a.get("source_quote", ""),
                )
                for a in data.get("ambiguities", [])
            ],
            provenance=Provenance(**prov) if prov else None,
            dsl_version=data.get("dsl_version", DSL_VERSION),
        )


def validate_policy(policy: Policy) -> list[str]:
    """Structural validation. Returns a list of problems; empty means valid.

    Deliberately separate from compilation: an LLM backend can produce a
    well-formed-looking policy that is internally contradictory, and we want
    that caught by code rather than trusted because a model emitted it.
    """
    problems: list[str] = []

    if policy.commission.rate_bps is not None:
        if not 0 <= policy.commission.rate_bps <= 10_000:
            problems.append(
                f"commission.rate_bps {policy.commission.rate_bps} outside 0..10000"
            )
    elif not policy.blocking_ambiguities():
        problems.append(
            "commission.rate_bps is None but no blocking ambiguity explains why"
        )

    # An unbalanced funding split is only a *bug* when nothing explains it. When
    # the compiler has already recorded a blocking ambiguity on the clause, the
    # unbalanced state is the faithful representation of a contract that really
    # does over-allocate the same discount — rejecting it here would throw away
    # the very finding the compiler is supposed to surface.
    explained_fields = {a.field_path for a in policy.blocking_ambiguities()}
    funding_explained = any(
        f == "promotion_funding" or f.startswith("promotion_funding.")
        for f in explained_fields
    )
    if not policy.promotion_funding.is_balanced() and not funding_explained:
        problems.append(
            "promotion_funding shares must sum to 10000 bps, got "
            f"{policy.promotion_funding.platform_share_bps}"
            f"+{policy.promotion_funding.seller_share_bps}"
            " (and no blocking ambiguity explains the imbalance)"
        )

    if policy.tax.tds_on_commission_bps < 0:
        problems.append("tax.tds_on_commission_bps must be non-negative")

    if policy.hold.hold_hours_after_delivery < 0:
        problems.append("hold.hold_hours_after_delivery must be non-negative")

    if (
        policy.effective.starts_at
        and policy.effective.ends_at
        and policy.effective.starts_at > policy.effective.ends_at
    ):
        problems.append("effective period ends before it starts")

    return problems


PARTY_ROLES_IN_SPLIT = (
    PartyRole.SELLER,
    PartyRole.DELIVERY_PARTNER,
    PartyRole.PLATFORM,
)
