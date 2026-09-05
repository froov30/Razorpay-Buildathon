"""Synthetic ledger generation.

ALL DATA PRODUCED HERE IS FABRICATED. No real merchants, orders, or payments.

Independence from the engine
----------------------------
This module settles orders from :data:`INTENDED_TERMS` — the terms the contract
prose is *meant* to express — using its own implementation of the settlement
formula. It deliberately does not import
:func:`src.settlement_engine.compute.compute_entitlements`.

That matters for the honesty of the metrics. If the generator built the ledger
by calling the engine, the engine would be scored against its own output and
would trivially agree with itself, including where it is wrong. By settling from
the intended terms instead, the evaluation actually tests the thing under
question: *did the compiler recover the right terms from prose, and does the
engine apply them the way a careful human would?*

The two sides share only :mod:`src.common.money` — rounding primitives, not
business logic. The remaining shared assumption is that both implement the same
money-flow convention, which is documented in ``src/settlement_engine/compute.py``
and restated in ``docs/test_plan.md`` as a known limitation of this approach.

Scenarios are hand-placed rather than randomised so that every ground-truth label
is intentional and a reader can trace exactly why each order is what it is.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from data.generator.contracts import INTENDED_TERMS, build_contract_sources
from src.common import config
from src.common.money import apply_bps
from src.common.types import (
    ContractSource,
    DeliveryEvent,
    ExceptionCategory,
    Order,
    PartyRole,
    PaymentEvent,
    Promotion,
    RefundEvent,
    ReversalEvent,
    Transfer,
)

SEED = config.get("evaluation", "dataset", "seed", default=20260904)
BATCH_AS_OF = datetime(2026, 3, 15, tzinfo=timezone.utc)
DEFAULT_OUTPUT_DIR = Path("data/synthetic")


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Independent settlement arithmetic
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settlement:
    seller_paise: int
    platform_paise: int
    delivery_paise: int
    tds_paise: int
    commission_paise: int


def settle(
    terms: dict,
    *,
    gross: int,
    shipping: int,
    tax: int,
    discount: int,
    has_delivery_partner: bool,
    refunded: int = 0,
) -> Settlement:
    """Compute the correct split from the intended terms.

    Mirrors the convention documented in ``src/settlement_engine/compute.py``:
    net = gross - shipping - tax (discount already reflected in gross), the
    platform reimburses its funded share of the discount to the seller, and TDS
    is withheld from the seller rather than being a fourth party in the split.
    """
    net = gross - shipping - tax
    gmv = net + discount
    base = gmv if terms["applies_to"] == "order_gross" else net

    commission = apply_bps(base, terms["commission_bps"])
    platform_discount_share = apply_bps(discount, terms["promo_platform_bps"])
    delivery_fee = terms["delivery_fee_paise"] if has_delivery_partner else 0

    seller = net - commission + platform_discount_share
    platform = commission - platform_discount_share - delivery_fee

    ratio_bps = min(10_000, (refunded * 10_000) // gross) if refunded and gross else 0

    def prorate(x: int) -> int:
        return x - apply_bps(x, ratio_bps) if ratio_bps else x

    # The delivery fee is never prorated — the courier delivered. This mirrors
    # the same rule in src/settlement_engine/compute.py.
    delivery_final = delivery_fee
    if ratio_bps and not terms["commission_refundable"]:
        # Platform keeps commission; the refund is absorbed by the seller.
        seller_final = seller - (net - prorate(net)) + (commission - prorate(commission))
        platform_final = platform
    else:
        seller_final = prorate(seller)
        platform_final = prorate(platform)

    tds = apply_bps(prorate(commission), terms["tds_bps"])

    return Settlement(
        seller_paise=seller_final - tds,
        platform_paise=platform_final,
        delivery_paise=delivery_final,
        tds_paise=tds,
        commission_paise=commission,
    )


# ---------------------------------------------------------------------------
# Scenario specification
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Scenario:
    """One order's full lifecycle, plus how (if at all) it is corrupted."""

    order_id: str
    contract_id: str
    terms_version: int
    seller_id: str
    placed_at: str
    gross_rupees: str
    defect: str = "clean"
    expected: ExceptionCategory = ExceptionCategory.NONE
    discount_rupees: str = "0"
    declared_funder: PartyRole | None = None
    delivery_partner: str | None = None
    delivery_after_hours: int | None = 48
    delivery_confirmed: bool = True
    refund_rupees: str = "0"
    note: str = ""


SCENARIOS: list[Scenario] = [
    # -- CTR-0001 / SLR-0001 -------------------------------------------------
    Scenario("ORD-1001", "CTR-0001", 1, "SLR-0001", "2026-01-05", "2499.00",
             delivery_partner="DLP-01", note="Baseline clean settlement."),
    Scenario("ORD-1002", "CTR-0001", 1, "SLR-0001", "2026-01-08", "1799.50",
             discount_rupees="200.00", declared_funder=PartyRole.PLATFORM,
             delivery_partner="DLP-01", note="Clean, platform-funded discount."),
    Scenario("ORD-1003", "CTR-0001", 1, "SLR-0001", "2026-01-12", "3250.00",
             defect="premature", expected=ExceptionCategory.PREMATURE_PAYOUT,
             delivery_partner="DLP-02",
             note="Seller paid 6h after delivery despite a 48h contractual hold."),
    Scenario("ORD-1004", "CTR-0001", 1, "SLR-0001", "2026-01-18", "899.00",
             defect="refund_handled", refund_rupees="899.00",
             delivery_partner="DLP-01",
             note="Full refund, payout correctly reversed first."),
    Scenario("ORD-1005", "CTR-0001", 1, "SLR-0001", "2026-01-22", "5400.00",
             defect="missing_reversal", expected=ExceptionCategory.MISSING_REVERSAL,
             refund_rupees="5400.00", delivery_partner="DLP-02",
             note="Customer refunded but seller payout never clawed back."),

    # -- CTR-0002 / SLR-0002 (no delivery hold) ------------------------------
    Scenario("ORD-1006", "CTR-0002", 1, "SLR-0002", "2026-01-06", "1250.00",
             delivery_after_hours=None, delivery_confirmed=False,
             note="No-hold contract: payable on capture, no delivery needed."),
    Scenario("ORD-1007", "CTR-0002", 1, "SLR-0002", "2026-01-14", "2100.00",
             discount_rupees="300.00", declared_funder=PartyRole.PLATFORM,
             delivery_after_hours=None, delivery_confirmed=False,
             defect="promo_mismatch",
             expected=ExceptionCategory.PROMOTION_FUNDING_MISMATCH,
             note="Discount booked 100% to platform; contract splits it 50/50."),
    Scenario("ORD-1008", "CTR-0002", 1, "SLR-0002", "2026-01-25", "760.00",
             delivery_after_hours=None, delivery_confirmed=False,
             defect="duplicate", expected=ExceptionCategory.DUPLICATE_TRANSFER,
             note="Settlement job ran twice; seller paid twice."),
    Scenario("ORD-1009", "CTR-0002", 1, "SLR-0002", "2026-02-02", "3300.00",
             delivery_after_hours=None, delivery_confirmed=False,
             note="Clean."),

    # -- CTR-0003 / SLR-0003 — the version-conflict window -------------------
    Scenario("ORD-1010", "CTR-0003", 1, "SLR-0003", "2026-01-20", "4200.00",
             delivery_partner="DLP-03", delivery_after_hours=24,
             note="Before the amendment: v1 governs unambiguously."),
    Scenario("ORD-1011", "CTR-0003", 1, "SLR-0003", "2026-02-03", "3800.00",
             defect="version_conflict",
             expected=ExceptionCategory.CONTRACT_VERSION_CONFLICT,
             delivery_partner="DLP-03", delivery_after_hours=24,
             note="IN CONFLICT WINDOW: settled at v1's 70% seller share."),
    Scenario("ORD-1012", "CTR-0003", 1, "SLR-0003", "2026-02-06", "2950.00",
             defect="version_conflict",
             expected=ExceptionCategory.CONTRACT_VERSION_CONFLICT,
             delivery_partner="DLP-03", delivery_after_hours=24,
             note="IN CONFLICT WINDOW."),
    Scenario("ORD-1013", "CTR-0003", 2, "SLR-0003", "2026-02-09", "6100.00",
             defect="version_conflict",
             expected=ExceptionCategory.CONTRACT_VERSION_CONFLICT,
             delivery_partner="DLP-03", delivery_after_hours=24,
             note="IN CONFLICT WINDOW: settled at v2's 65%, the other reading."),
    Scenario("ORD-1014", "CTR-0003", 2, "SLR-0003", "2026-02-18", "1980.00",
             delivery_partner="DLP-03", delivery_after_hours=24,
             note="After both candidate dates: v2 governs unambiguously."),
    Scenario("ORD-1015", "CTR-0003", 2, "SLR-0003", "2026-02-25", "5250.00",
             delivery_partner="DLP-03", delivery_after_hours=24,
             defect="rate_mismatch", expected=ExceptionCategory.RATE_MISMATCH,
             note="Settled at the superseded 70% rate after v2 took effect."),

    # -- CTR-0004 / SLR-0004 — delivery-contingent entitlement ---------------
    Scenario("ORD-1016", "CTR-0004", 1, "SLR-0004", "2026-01-09", "1650.00",
             delivery_partner="DLP-04", delivery_after_hours=72,
             note="Clean; 72h hold satisfied."),
    Scenario("ORD-1017", "CTR-0004", 1, "SLR-0004", "2026-01-16", "2450.00",
             delivery_confirmed=False, delivery_after_hours=None,
             defect="pending", delivery_partner="DLP-04",
             note="Never delivered, never paid — correctly pending, not an exception."),
    Scenario("ORD-1018", "CTR-0004", 1, "SLR-0004", "2026-01-19", "3100.00",
             delivery_confirmed=False, delivery_after_hours=None,
             defect="premature_no_delivery",
             expected=ExceptionCategory.PREMATURE_PAYOUT,
             delivery_partner="DLP-04",
             note="Paid despite no delivery confirmation ever arriving."),
    Scenario("ORD-1019", "CTR-0004", 1, "SLR-0004", "2026-01-28", "980.00",
             delivery_partner="DLP-04", delivery_after_hours=72,
             defect="tax_mismatch", expected=ExceptionCategory.TAX_LINE_MISMATCH,
             note="TDS withheld at 2% against a contractual 1%."),
    Scenario("ORD-1020", "CTR-0004", 1, "SLR-0004", "2026-02-04", "4400.00",
             delivery_partner="DLP-04", delivery_after_hours=72, note="Clean."),

    # -- CTR-0005 / SLR-0005 — refund precedence -----------------------------
    Scenario("ORD-1021", "CTR-0005", 1, "SLR-0005", "2026-01-11", "2200.00",
             discount_rupees="200.00", declared_funder=PartyRole.SELLER,
             delivery_after_hours=24,
             note="Clean: commission on GROSS, seller funds 75% of discount."),
    Scenario("ORD-1022", "CTR-0005", 1, "SLR-0005", "2026-01-21", "1450.00",
             delivery_after_hours=24,
             defect="missing_reversal", expected=ExceptionCategory.MISSING_REVERSAL,
             refund_rupees="1450.00",
             note="Refund issued with no prior reversal; contract requires it first."),
    Scenario("ORD-1023", "CTR-0005", 1, "SLR-0005", "2026-02-08", "3600.00",
             delivery_after_hours=24, note="Clean."),
    Scenario("ORD-1024", "CTR-0005", 1, "SLR-0005", "2026-02-14", "1100.00",
             delivery_after_hours=24,
             defect="duplicate", expected=ExceptionCategory.DUPLICATE_TRANSFER,
             note="Retry after a timeout produced a second identical transfer."),

    # -- CTR-0006 / SLR-0006 -------------------------------------------------
    Scenario("ORD-1025", "CTR-0006", 1, "SLR-0006", "2026-01-07", "1875.00",
             delivery_partner="DLP-05", delivery_after_hours=24, note="Clean."),
    Scenario("ORD-1026", "CTR-0006", 1, "SLR-0006", "2026-01-17", "2640.00",
             delivery_partner="DLP-05", delivery_after_hours=24,
             defect="premature", expected=ExceptionCategory.PREMATURE_PAYOUT,
             note="Paid 3h after delivery against a 24h hold."),
    Scenario("ORD-1027", "CTR-0006", 1, "SLR-0006", "2026-02-01", "990.00",
             delivery_partner="DLP-05", delivery_after_hours=24, note="Clean."),
    Scenario("ORD-1028", "CTR-0006", 1, "SLR-0006", "2026-02-11", "4750.00",
             delivery_partner="DLP-05", delivery_after_hours=24,
             defect="rate_mismatch", expected=ExceptionCategory.RATE_MISMATCH,
             note="Settled at 22% commission; contract says 18%."),

    # -- CTR-0007 / SLR-0007 — unresolvable promotion funding ----------------
    Scenario("ORD-1029", "CTR-0007", 1, "SLR-0007", "2026-01-13", "2300.00",
             discount_rupees="300.00", declared_funder=PartyRole.PLATFORM,
             delivery_partner="DLP-06", delivery_after_hours=24,
             defect="unresolvable",
             expected=ExceptionCategory.AMBIGUOUS_UNRESOLVABLE,
             note="Contract over-allocates the discount (60% + 60%)."),
    Scenario("ORD-1030", "CTR-0007", 1, "SLR-0007", "2026-01-27", "1560.00",
             discount_rupees="160.00", declared_funder=PartyRole.SELLER,
             delivery_partner="DLP-06", delivery_after_hours=24,
             defect="unresolvable",
             expected=ExceptionCategory.AMBIGUOUS_UNRESOLVABLE,
             note="Same unresolvable funding clause."),
    Scenario("ORD-1031", "CTR-0007", 1, "SLR-0007", "2026-02-15", "3400.00",
             delivery_partner="DLP-06", delivery_after_hours=24,
             defect="unresolvable",
             expected=ExceptionCategory.AMBIGUOUS_UNRESOLVABLE,
             note="No discount on this order, but the clause is still unreadable."),

    # -- CTR-0008 / SLR-0008 — seller-funded promotions ----------------------
    Scenario("ORD-1032", "CTR-0008", 1, "SLR-0008", "2026-01-10", "1290.00",
             discount_rupees="90.00", declared_funder=PartyRole.SELLER,
             delivery_partner="DLP-07", delivery_after_hours=12, note="Clean."),
    Scenario("ORD-1033", "CTR-0008", 1, "SLR-0008", "2026-01-24", "2780.00",
             delivery_partner="DLP-07", delivery_after_hours=12, note="Clean."),
    Scenario("ORD-1034", "CTR-0008", 1, "SLR-0008", "2026-02-07", "1640.00",
             discount_rupees="140.00", declared_funder=PartyRole.PLATFORM,
             delivery_partner="DLP-07", delivery_after_hours=12,
             defect="promo_mismatch",
             expected=ExceptionCategory.PROMOTION_FUNDING_MISMATCH,
             note="Booked to platform budget; this contract is seller-funded."),

    # -- CTR-0009 / SLR-0009 -------------------------------------------------
    Scenario("ORD-1035", "CTR-0009", 1, "SLR-0009", "2026-01-15", "5900.00",
             discount_rupees="400.00", declared_funder=None,
             delivery_partner="DLP-08", delivery_after_hours=48,
             note="Clean: 50/50 split contract, ledger asserts no sole funder."),
    Scenario("ORD-1036", "CTR-0009", 1, "SLR-0009", "2026-02-05", "3150.00",
             delivery_partner="DLP-08", delivery_after_hours=48,
             defect="tax_mismatch", expected=ExceptionCategory.TAX_LINE_MISMATCH,
             note="No TDS withheld at all; contract requires 1%."),
    Scenario("ORD-1037", "CTR-0009", 1, "SLR-0009", "2026-02-20", "2470.00",
             delivery_partner="DLP-08", delivery_after_hours=48, note="Clean."),

    # -- CTR-0010 / SLR-0010 -------------------------------------------------
    Scenario("ORD-1038", "CTR-0010", 1, "SLR-0010", "2026-01-23", "1120.00",
             delivery_after_hours=None, delivery_confirmed=False, note="Clean."),
    Scenario("ORD-1039", "CTR-0010", 1, "SLR-0010", "2026-02-10", "3890.00",
             delivery_after_hours=None, delivery_confirmed=False, note="Clean."),
    Scenario("ORD-1040", "CTR-0010", 1, "SLR-0010", "2026-02-22", "2050.00",
             delivery_after_hours=None, delivery_confirmed=False,
             defect="unexplained_variance",
             expected=ExceptionCategory.AMBIGUOUS_UNRESOLVABLE,
             note="Seller short-paid Rs 137.00 with no pattern that explains it."),
]


# ---------------------------------------------------------------------------
# Dataset assembly
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SyntheticDataset:
    contracts: list[ContractSource] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    payments: list[PaymentEvent] = field(default_factory=list)
    deliveries: list[DeliveryEvent] = field(default_factory=list)
    promotions: list[Promotion] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)
    refunds: list[RefundEvent] = field(default_factory=list)
    reversals: list[ReversalEvent] = field(default_factory=list)
    ground_truth: dict[str, str] = field(default_factory=dict)
    scenario_notes: dict[str, str] = field(default_factory=dict)

    def record_count(self) -> int:
        return (
            len(self.contracts) + len(self.orders) + len(self.payments)
            + len(self.deliveries) + len(self.promotions) + len(self.transfers)
            + len(self.refunds) + len(self.reversals)
        )


def generate(seed: int = SEED) -> SyntheticDataset:
    """Build the full synthetic dataset deterministically."""
    rng = random.Random(seed)
    ds = SyntheticDataset(contracts=build_contract_sources())

    for idx, sc in enumerate(SCENARIOS):
        placed = _utc(sc.placed_at) + timedelta(hours=9 + (idx % 7))
        from src.common.money import rupees_to_paise

        gross = rupees_to_paise(sc.gross_rupees)
        discount = rupees_to_paise(sc.discount_rupees)
        shipping = 4900 if sc.delivery_partner else 0
        tax = apply_bps(gross - shipping, 500)  # 5% notional GST component

        order = Order(
            order_id=sc.order_id,
            seller_id=sc.seller_id,
            placed_at=placed,
            gross_amount_paise=gross,
            shipping_fee_paise=shipping,
            tax_collected_paise=tax,
            delivery_partner_id=sc.delivery_partner,
            promotion_id=f"PRM-{sc.order_id[-4:]}" if discount else None,
        )
        ds.orders.append(order)
        ds.ground_truth[sc.order_id] = str(sc.expected)
        ds.scenario_notes[sc.order_id] = sc.note

        ds.payments.append(
            PaymentEvent(
                payment_id=f"PAY-{sc.order_id[-4:]}",
                order_id=sc.order_id,
                captured_at=placed + timedelta(minutes=rng.randint(1, 4)),
                amount_paise=gross,
                method=rng.choice(["upi", "card", "netbanking"]),
            )
        )

        if discount:
            ds.promotions.append(
                Promotion(
                    promotion_id=f"PRM-{sc.order_id[-4:]}",
                    order_id=sc.order_id,
                    discount_paise=discount,
                    campaign="FESTIVE26" if idx % 2 == 0 else "SELLERWEEK26",
                    declared_funder=sc.declared_funder,
                )
            )

        delivered_at: datetime | None = None
        if sc.delivery_confirmed and sc.delivery_after_hours is not None:
            delivered_at = placed + timedelta(hours=24)
            ds.deliveries.append(
                DeliveryEvent(
                    delivery_id=f"DEL-{sc.order_id[-4:]}",
                    order_id=sc.order_id,
                    occurred_at=delivered_at,
                    confirmed=True,
                    delivery_partner_id=sc.delivery_partner,
                )
            )
        elif sc.defect in ("pending", "premature_no_delivery"):
            ds.deliveries.append(
                DeliveryEvent(
                    delivery_id=f"DEL-{sc.order_id[-4:]}",
                    order_id=sc.order_id,
                    occurred_at=placed + timedelta(hours=30),
                    confirmed=False,
                    delivery_partner_id=sc.delivery_partner,
                )
            )

        terms = INTENDED_TERMS.get((sc.contract_id, sc.terms_version))
        refunded = rupees_to_paise(sc.refund_rupees)

        if terms is None:
            # CTR-0007: no correct terms exist. Settle at a plausible-looking
            # rate so the ledger is populated; the engine must refuse to judge
            # it rather than agreeing or disagreeing with this number.
            _unresolvable_transfers(ds, sc, order, placed, delivered_at)
            continue

        # Release time under the intended terms.
        if terms["requires_delivery"]:
            release = (
                delivered_at + timedelta(hours=terms["hold_hours"])
                if delivered_at
                else None
            )
        else:
            release = placed + timedelta(hours=1)

        # Transfers are always emitted at the pre-refund amount. A refund is a
        # later event that gets clawed back by a *reversal* record; baking the
        # proration into the original transfer as well would double-count it and
        # leave the ledger showing money that was never paid being returned.
        settlement = settle(
            terms,
            gross=gross,
            shipping=shipping,
            tax=tax,
            discount=discount,
            has_delivery_partner=bool(sc.delivery_partner),
        )

        _emit_transfers(
            ds,
            sc=sc,
            order=order,
            terms=terms,
            settlement=settlement,
            placed=placed,
            delivered_at=delivered_at,
            release=release,
            gross=gross,
            shipping=shipping,
            tax=tax,
            discount=discount,
            refunded=refunded,
        )

    return ds


def _unresolvable_transfers(
    ds: SyntheticDataset,
    sc: Scenario,
    order: Order,
    placed: datetime,
    delivered_at: datetime | None,
) -> None:
    """Populate a ledger for a contract whose terms cannot be read."""
    net = order.gross_amount_paise - order.shipping_fee_paise - order.tax_collected_paise
    commission = apply_bps(net, 2200)
    executed = (delivered_at or placed) + timedelta(hours=26)
    ds.transfers.append(
        Transfer(
            transfer_id=f"TRF-{sc.order_id[-4:]}-S",
            order_id=sc.order_id,
            party_role=PartyRole.SELLER,
            party_id=sc.seller_id,
            amount_paise=net - commission - apply_bps(commission, 100),
            executed_at=executed,
            tds_withheld_paise=apply_bps(commission, 100),
        )
    )
    ds.transfers.append(
        Transfer(
            transfer_id=f"TRF-{sc.order_id[-4:]}-P",
            order_id=sc.order_id,
            party_role=PartyRole.PLATFORM,
            party_id="PLATFORM",
            amount_paise=commission - 4500,
            executed_at=executed,
        )
    )
    if sc.delivery_partner:
        ds.transfers.append(
            Transfer(
                transfer_id=f"TRF-{sc.order_id[-4:]}-D",
                order_id=sc.order_id,
                party_role=PartyRole.DELIVERY_PARTNER,
                party_id=sc.delivery_partner,
                amount_paise=4500,
                executed_at=executed,
            )
        )


def _emit_transfers(
    ds: SyntheticDataset,
    *,
    sc: Scenario,
    order: Order,
    terms: dict,
    settlement: Settlement,
    placed: datetime,
    delivered_at: datetime | None,
    release: datetime | None,
    gross: int,
    shipping: int,
    tax: int,
    discount: int,
    refunded: int,
) -> None:
    """Write the transfer/refund/reversal records, applying the defect."""
    suffix = sc.order_id[-4:]
    defect = sc.defect

    # When did the seller payout actually fire?
    if defect == "premature":
        executed = (delivered_at or placed) + timedelta(hours=3)
    elif defect == "premature_no_delivery":
        executed = placed + timedelta(hours=20)
    elif release is not None:
        executed = release + timedelta(hours=2)
    else:
        executed = placed + timedelta(hours=2)

    seller_amount = settlement.seller_paise
    tds_withheld = settlement.tds_paise

    if defect == "rate_mismatch":
        # Settled under a rate the governing version does not authorise.
        wrong_bps = 3000 if sc.contract_id == "CTR-0003" else 2200
        wrong = settle(
            {**terms, "commission_bps": wrong_bps},
            gross=gross, shipping=shipping, tax=tax, discount=discount,
            has_delivery_partner=bool(sc.delivery_partner),
        )
        seller_amount, tds_withheld = wrong.seller_paise, wrong.tds_paise
    elif defect == "tax_mismatch":
        wrong_tds_bps = 200 if sc.contract_id == "CTR-0004" else 0
        tds_withheld = apply_bps(settlement.commission_paise, wrong_tds_bps)
        seller_amount = settlement.seller_paise + settlement.tds_paise - tds_withheld
    elif defect == "unexplained_variance":
        seller_amount = settlement.seller_paise - 13700

    if defect != "pending":
        ds.transfers.append(
            Transfer(
                transfer_id=f"TRF-{suffix}-S",
                order_id=sc.order_id,
                party_role=PartyRole.SELLER,
                party_id=sc.seller_id,
                amount_paise=seller_amount,
                executed_at=executed,
                tds_withheld_paise=tds_withheld,
            )
        )
        ds.transfers.append(
            Transfer(
                transfer_id=f"TRF-{suffix}-P",
                order_id=sc.order_id,
                party_role=PartyRole.PLATFORM,
                party_id="PLATFORM",
                amount_paise=settlement.platform_paise,
                executed_at=executed,
            )
        )
        if sc.delivery_partner and settlement.delivery_paise:
            ds.transfers.append(
                Transfer(
                    transfer_id=f"TRF-{suffix}-D",
                    order_id=sc.order_id,
                    party_role=PartyRole.DELIVERY_PARTNER,
                    party_id=sc.delivery_partner,
                    amount_paise=settlement.delivery_paise,
                    executed_at=executed,
                )
            )

    if defect == "duplicate":
        ds.transfers.append(
            Transfer(
                transfer_id=f"TRF-{suffix}-S2",
                order_id=sc.order_id,
                party_role=PartyRole.SELLER,
                party_id=sc.seller_id,
                amount_paise=seller_amount,
                executed_at=executed + timedelta(minutes=11),
                tds_withheld_paise=0,
            )
        )

    # Refund / reversal lifecycle.
    if refunded:
        refund_at = executed + timedelta(days=3)
        if defect == "refund_handled":
            # Correct ordering: reverse the payout, then refund the customer.
            prorated = settle(
                terms, gross=gross, shipping=shipping, tax=tax, discount=discount,
                has_delivery_partner=bool(sc.delivery_partner), refunded=refunded,
            )
            ds.reversals.append(
                ReversalEvent(
                    reversal_id=f"REV-{suffix}",
                    transfer_id=f"TRF-{suffix}-S",
                    order_id=sc.order_id,
                    executed_at=refund_at - timedelta(hours=2),
                    amount_paise=settlement.seller_paise - prorated.seller_paise,
                )
            )
            # Commission is refundable under this contract, so the platform's
            # retained share is given back too. Only the delivery fee stays —
            # the courier already delivered.
            if terms["commission_refundable"]:
                ds.reversals.append(
                    ReversalEvent(
                        reversal_id=f"REV-{suffix}-P",
                        transfer_id=f"TRF-{suffix}-P",
                        order_id=sc.order_id,
                        executed_at=refund_at - timedelta(hours=2),
                        amount_paise=settlement.platform_paise - prorated.platform_paise,
                    )
                )
        ds.refunds.append(
            RefundEvent(
                refund_id=f"RFD-{suffix}",
                order_id=sc.order_id,
                issued_at=refund_at,
                amount_paise=refunded,
                reason="customer_return",
            )
        )


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _encode(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: _encode(v) for k, v in asdict(obj).items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [_encode(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    return obj


def write_dataset(ds: SyntheticDataset, out_dir: Path | str = DEFAULT_OUTPUT_DIR) -> Path:
    """Write the dataset as JSON. Ground truth is kept in its own file."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    files = {
        "contracts.json": ds.contracts,
        "orders.json": ds.orders,
        "payments.json": ds.payments,
        "deliveries.json": ds.deliveries,
        "promotions.json": ds.promotions,
        "transfers.json": ds.transfers,
        "refunds.json": ds.refunds,
        "reversals.json": ds.reversals,
    }
    for name, payload in files.items():
        (out / name).write_text(
            json.dumps(_encode(payload), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # Ground truth lives apart from the ledger the engine reads, so there is no
    # path by which the engine could consult the answers it is being scored on.
    (out / "ground_truth.json").write_text(
        json.dumps(
            {
                "_comment": (
                    "SYNTHETIC ground-truth labels. Consumed only by tests/eval. "
                    "The engine never reads this file."
                ),
                "labels": ds.ground_truth,
                "notes": ds.scenario_notes,
                "batch_as_of": BATCH_AS_OF.isoformat(),
                "seed": SEED,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return out
