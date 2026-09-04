"""Deterministic entitlement computation.

Given an order, its lifecycle events, and the governing compiled policy, work
out what each party was owed and whether they were owed it *yet*. Pure function,
integer paise only, no I/O — so it is trivially testable and trivially portable
to a distributed runtime later (see docs/architecture.md, "Scaling").

The settlement identity
-----------------------
For every order, the three party shares must sum exactly to the merchandise
revenue collected::

    seller + delivery_partner + platform == net_order_value

This identity is asserted at the end of every computation. It is the reason
:func:`split_proportional` uses largest-remainder allocation: a rounding residue
of one paise would break the identity and manifest downstream as a phantom
entitlement variance indistinguishable from a real breach.

Money-flow convention (stated explicitly because marketplaces genuinely differ)
------------------------------------------------------------------------------
``net_order_value``
    Merchandise revenue actually collected: ``gross - shipping - tax``. The
    promotional discount is already reflected here, because the customer paid
    the discounted price.

``gross_merchandise_value``
    ``net_order_value + discount`` — what the goods would have fetched at list
    price. Contracts that compute commission on "gross order value" mean this.

Discount funding
    The discount has already reduced collected revenue. The funding clause
    decides who *absorbs* it, so the platform reimburses its share to the seller.
    A contract where the platform funds 100% therefore leaves the seller whole.

TDS
    Withholding is a deduction from the seller's payable amount, not a fourth
    party in the split. The three-way identity holds pre-withholding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.common.money import apply_bps
from src.common.types import (
    DeliveryEvent,
    Order,
    PartyEntitlement,
    PartyRole,
    Promotion,
    RefundEvent,
)
from src.contract_compiler.dsl import Policy


class SettlementIdentityError(AssertionError):
    """The three-way split failed to reconcile — an engine bug, not a data issue."""


@dataclass(slots=True)
class OrderContext:
    """Everything the engine needs about one order to compute entitlement."""

    order: Order
    policy: Policy
    promotion: Promotion | None = None
    deliveries: list[DeliveryEvent] = field(default_factory=list)
    refunds: list[RefundEvent] = field(default_factory=list)
    as_of: datetime | None = None

    def evaluation_time(self) -> datetime:
        return self.as_of or datetime.now(timezone.utc)

    def confirmed_delivery(self) -> DeliveryEvent | None:
        confirmed = [d for d in self.deliveries if d.confirmed]
        return min(confirmed, key=lambda d: d.occurred_at) if confirmed else None

    def refunded_paise(self) -> int:
        return sum(r.amount_paise for r in self.refunds)


@dataclass(slots=True)
class Computation:
    """Result of computing entitlements for one order."""

    entitlements: dict[PartyRole, PartyEntitlement]
    net_order_value_paise: int
    gross_merchandise_value_paise: int
    commission_paise: int
    tds_paise: int
    discount_paise: int
    platform_discount_share_paise: int
    seller_discount_share_paise: int
    refund_ratio_bps: int
    derivation: list[str] = field(default_factory=list)

    def expected_paise(self, role: PartyRole) -> int:
        ent = self.entitlements.get(role)
        return ent.entitled_amount_paise if ent else 0


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_entitlements(ctx: OrderContext) -> Computation:
    """Compute what each party was owed for this order, and whether owed yet.

    Raises :class:`ValueError` if the policy is not computable — callers must
    check ``policy.is_computable()`` (or use the resolver) first. Computing
    against an ambiguous policy is exactly the silent-wrong-answer failure this
    system exists to prevent, so it is refused rather than defaulted.
    """
    policy = ctx.policy
    if not policy.is_computable():
        raise ValueError(
            f"policy {policy.contract_id} v{policy.version} is not computable: "
            + "; ".join(a.reason for a in policy.blocking_ambiguities())
        )

    order = ctx.order
    steps: list[str] = []

    # -- 1. Bases ---------------------------------------------------------
    net = order.gross_amount_paise - order.shipping_fee_paise - order.tax_collected_paise
    discount = ctx.promotion.discount_paise if ctx.promotion else 0
    gmv = net + discount
    steps.append(
        f"Net order value = gross {order.gross_amount_paise} - shipping "
        f"{order.shipping_fee_paise} - tax {order.tax_collected_paise} = {net} paise"
    )
    if discount:
        steps.append(f"Gross merchandise value = net {net} + discount {discount} = {gmv}")

    # -- 2. Commission ----------------------------------------------------
    rate_bps = policy.commission.rate_bps
    assert rate_bps is not None  # guaranteed by is_computable()
    base = gmv if policy.commission.applies_to == "order_gross" else net
    commission = apply_bps(base, rate_bps)
    if commission < policy.commission.minimum_paise:
        commission = policy.commission.minimum_paise
        steps.append(f"Commission floored at contractual minimum {commission} paise")
    steps.append(
        f"Commission = {rate_bps / 100:.2f}% of {policy.commission.applies_to} "
        f"({base}) = {commission} paise"
    )

    # -- 3. Discount funding ---------------------------------------------
    platform_discount_share = apply_bps(
        discount, policy.promotion_funding.platform_share_bps
    )
    seller_discount_share = discount - platform_discount_share
    if discount:
        steps.append(
            f"Discount {discount} funded "
            f"{policy.promotion_funding.platform_share_bps / 100:.0f}% platform "
            f"({platform_discount_share}) / "
            f"{policy.promotion_funding.seller_share_bps / 100:.0f}% seller "
            f"({seller_discount_share}); platform reimburses its share to seller"
        )

    # -- 4. Delivery fee --------------------------------------------------
    delivery_fee = policy.delivery_fee.flat_fee_paise if order.delivery_partner_id else 0
    if delivery_fee:
        steps.append(f"Delivery fee = {delivery_fee} paise to {order.delivery_partner_id}")

    # -- 5. Refund proration ---------------------------------------------
    refunded = ctx.refunded_paise()
    refund_ratio_bps = 0
    if refunded and order.gross_amount_paise:
        refund_ratio_bps = min(
            10_000, (refunded * 10_000) // order.gross_amount_paise
        )
        steps.append(
            f"Refund of {refunded} paise = {refund_ratio_bps / 100:.2f}% of order; "
            f"entitlements prorated accordingly"
        )

    def prorate(amount: int) -> int:
        if not refund_ratio_bps:
            return amount
        return amount - apply_bps(amount, refund_ratio_bps)

    # -- 6. Party shares --------------------------------------------------
    seller_share = net - commission + platform_discount_share
    platform_share = commission - platform_discount_share - delivery_fee

    seller_final = prorate(seller_share)
    # The delivery fee is NOT prorated on refund. The courier performed the
    # delivery; a customer returning the goods afterwards does not retroactively
    # un-perform that service, and no contract in the corpus claws it back. This
    # is a deliberate asymmetry — prorating it would understate what the delivery
    # partner is owed and generate a standing false exception on every return.
    delivery_final = delivery_fee
    if policy.refund.commission_refundable:
        platform_final = prorate(platform_share)
    else:
        # Platform keeps its commission on refunded orders; the refund is
        # absorbed entirely by the seller's share.
        platform_final = platform_share
        seller_final = seller_share - (net - prorate(net)) + (
            commission - prorate(commission)
        )
        steps.append(
            "Commission is non-refundable under this contract; refund absorbed "
            "by the seller's share"
        )

    # -- 7. TDS -----------------------------------------------------------
    tds_base = commission if policy.tax.applies_to == "commission" else seller_final
    tds = apply_bps(prorate(tds_base) if policy.tax.applies_to == "commission" else tds_base,
                    policy.tax.tds_on_commission_bps)
    if tds:
        steps.append(
            f"TDS = {policy.tax.tds_on_commission_bps / 100:.2f}% of "
            f"{policy.tax.applies_to} = {tds} paise (withheld from seller payout)"
        )

    # -- 8. Timing conditions --------------------------------------------
    now = ctx.evaluation_time()
    confirmed = ctx.confirmed_delivery()
    seller_reasons = list(steps)
    seller_due = True

    if policy.hold.requires_delivery_confirmation:
        if confirmed is None:
            seller_due = False
            seller_reasons.append(
                "NOT YET PAYABLE: contract requires delivery confirmation and no "
                "confirmed delivery event exists for this order"
            )
        else:
            release_at = _as_utc(confirmed.occurred_at) + timedelta(
                hours=policy.hold.hold_hours_after_delivery
            )
            if now < release_at:
                seller_due = False
                seller_reasons.append(
                    f"NOT YET PAYABLE: {policy.hold.hold_hours_after_delivery}h hold "
                    f"after delivery expires {release_at.isoformat()}"
                )
            else:
                seller_reasons.append(
                    f"Payable: delivery confirmed {_as_utc(confirmed.occurred_at).isoformat()}"
                    + (
                        f" + {policy.hold.hold_hours_after_delivery}h hold elapsed"
                        if policy.hold.hold_hours_after_delivery
                        else ""
                    )
                )

    delivery_due = True
    if policy.delivery_fee.payable_on_confirmation_only and confirmed is None:
        delivery_due = False

    entitlements = {
        PartyRole.SELLER: PartyEntitlement(
            party_role=PartyRole.SELLER,
            party_id=order.seller_id,
            entitled_amount_paise=seller_final - tds,
            entitled_now=seller_due,
            reasons=tuple(seller_reasons),
        ),
        PartyRole.PLATFORM: PartyEntitlement(
            party_role=PartyRole.PLATFORM,
            party_id="PLATFORM",
            entitled_amount_paise=platform_final,
            # The platform's commission is realised at settlement, not at
            # capture: on Route the platform retains its share out of the
            # transfer it releases. While the seller's payout is still held,
            # nothing has been split and the platform has retained nothing —
            # so an order sitting in its delivery hold is not a shortfall
            # against the platform, it is simply unsettled.
            entitled_now=seller_due,
            reasons=(
                f"Commission {commission} less funded discount and delivery fee",
            )
            + (
                ()
                if seller_due
                else ("NOT YET REALISED: order has not settled to the seller",)
            ),
        ),
    }
    if order.delivery_partner_id:
        entitlements[PartyRole.DELIVERY_PARTNER] = PartyEntitlement(
            party_role=PartyRole.DELIVERY_PARTNER,
            party_id=order.delivery_partner_id,
            entitled_amount_paise=delivery_final,
            entitled_now=delivery_due,
            reasons=(
                "Payable on confirmed delivery"
                if policy.delivery_fee.payable_on_confirmation_only
                else "Payable on dispatch",
            ),
        )

    _assert_identity(
        entitlements=entitlements,
        net=net,
        tds=tds,
        refund_ratio_bps=refund_ratio_bps,
        policy=policy,
    )

    return Computation(
        entitlements=entitlements,
        net_order_value_paise=net,
        gross_merchandise_value_paise=gmv,
        commission_paise=commission,
        tds_paise=tds,
        discount_paise=discount,
        platform_discount_share_paise=platform_discount_share,
        seller_discount_share_paise=seller_discount_share,
        refund_ratio_bps=refund_ratio_bps,
        derivation=steps,
    )


def _assert_identity(
    *,
    entitlements: dict[PartyRole, PartyEntitlement],
    net: int,
    tds: int,
    refund_ratio_bps: int,
    policy: Policy,
) -> None:
    """The three shares plus withholding must reconcile to collected revenue.

    Skipped for partially-refunded non-refundable-commission orders, where the
    contract deliberately breaks the clean identity by design (the platform
    keeps money the customer got back, funded from the seller's share).
    """
    if refund_ratio_bps and not policy.refund.commission_refundable:
        return

    total = sum(e.entitled_amount_paise for e in entitlements.values()) + tds

    if not refund_ratio_bps:
        if total != net:
            raise SettlementIdentityError(
                f"settlement identity broken: parties+tds={total} but expected {net}"
            )
        return

    # Refunded orders: the delivery fee is retained in full while every other
    # share is prorated, so the whole reconciles to `prorate(net - fee) + fee`.
    # Proration is applied per party, so each prorated share can carry up to one
    # paise of rounding residue; the tolerance is bounded by the party count
    # rather than waived, so a genuine arithmetic bug still trips this.
    delivery = entitlements.get(PartyRole.DELIVERY_PARTNER)
    fee = delivery.entitled_amount_paise if delivery else 0
    prorated_base = (net - fee) - apply_bps(net - fee, refund_ratio_bps)
    expected = prorated_base + fee
    tolerance = len(entitlements)
    if abs(total - expected) > tolerance:
        raise SettlementIdentityError(
            f"settlement identity broken: parties+tds={total} but expected "
            f"{expected} +/-{tolerance} (net={net}, fee={fee}, "
            f"refund_ratio_bps={refund_ratio_bps})"
        )
