"""The entitlement graph — an order's lifecycle, folded from its events.

What this is for
----------------
The ledger arrives as flat lists: every order, every delivery, every transfer,
all in separate collections. Answering "what happened to order ORD-1011, in
what order, and what does that imply about entitlement?" means joining five of
those collections on ``order_id`` and sorting by time.

This module owns that join. Before it existed the indexing was inlined in
``src.pipeline``, which meant the component the architecture diagram calls the
"entitlement graph" had no code of its own — the concept was real but homeless.

Why a graph rather than a table
-------------------------------
An order is not a row. It is a small event-sourced object: placed, captured,
possibly discounted, possibly delivered, possibly settled, possibly refunded,
possibly reversed. Entitlement is a *fold* over that sequence, and the fold is
order-dependent — a payout before a delivery confirmation means something
different from the same payout after it. The timeline is the thing being
reasoned about, so it gets a first-class representation.

Two views come out of it:

:meth:`EntitlementGraph.context_for`
    The bundle the settlement engine needs: order, policy, promotion,
    deliveries, refunds, and an evaluation time.

:meth:`EntitlementGraph.timeline_for`
    The ordered event sequence, for a reviewer who needs to see *when* things
    happened rather than what they summed to. This is what makes an exception
    explainable rather than merely detected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from src.common.types import (
    DeliveryEvent,
    LedgerEventType,
    Order,
    PaymentEvent,
    Promotion,
    RefundEvent,
    ReversalEvent,
    Transfer,
)
from src.contract_compiler.dsl import Policy
from src.settlement_engine.compute import OrderContext


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """One thing that happened to an order, at a known time."""

    occurred_at: datetime
    event_type: LedgerEventType
    reference_id: str
    amount_paise: int | None = None
    detail: str = ""

    def describe(self) -> str:
        when = _as_utc(self.occurred_at).isoformat()
        amount = "" if self.amount_paise is None else f" {self.amount_paise} paise"
        return f"{when}  {self.event_type}{amount}  ({self.reference_id}) {self.detail}".strip()


class EntitlementGraph:
    """Per-order view over the flat ledger.

    Built once per batch. Indexing is done eagerly in the constructor because
    the pipeline touches every order exactly once, so a lazy index would pay
    the same cost with more bookkeeping.
    """

    def __init__(
        self,
        *,
        orders: Sequence[Order],
        payments: Sequence[PaymentEvent] = (),
        deliveries: Sequence[DeliveryEvent] = (),
        promotions: Sequence[Promotion] = (),
        transfers: Sequence[Transfer] = (),
        refunds: Sequence[RefundEvent] = (),
        reversals: Sequence[ReversalEvent] = (),
    ) -> None:
        self._orders = {o.order_id: o for o in orders}
        self._promotion: dict[str, Promotion] = {p.order_id: p for p in promotions}
        self._payments: dict[str, list[PaymentEvent]] = {}
        self._deliveries: dict[str, list[DeliveryEvent]] = {}
        self._transfers: dict[str, list[Transfer]] = {}
        self._refunds: dict[str, list[RefundEvent]] = {}
        self._reversals: dict[str, list[ReversalEvent]] = {}

        for p in payments:
            self._payments.setdefault(p.order_id, []).append(p)
        for d in deliveries:
            self._deliveries.setdefault(d.order_id, []).append(d)
        for t in transfers:
            self._transfers.setdefault(t.order_id, []).append(t)
        for r in refunds:
            self._refunds.setdefault(r.order_id, []).append(r)
        for r in reversals:
            self._reversals.setdefault(r.order_id, []).append(r)

    # -- accessors ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._orders)

    def orders(self) -> Iterable[Order]:
        return self._orders.values()

    def promotion_for(self, order_id: str) -> Promotion | None:
        return self._promotion.get(order_id)

    def deliveries_for(self, order_id: str) -> list[DeliveryEvent]:
        return self._deliveries.get(order_id, [])

    def transfers_for(self, order_id: str) -> list[Transfer]:
        return self._transfers.get(order_id, [])

    def refunds_for(self, order_id: str) -> list[RefundEvent]:
        return self._refunds.get(order_id, [])

    def reversals_for(self, order_id: str) -> list[ReversalEvent]:
        return self._reversals.get(order_id, [])

    def seller_transfer(self, order_id: str) -> Transfer | None:
        """The seller payout, which is the movement the gate replays."""
        from src.common.types import PartyRole

        return next(
            (t for t in self.transfers_for(order_id) if t.party_role == PartyRole.SELLER),
            None,
        )

    # -- views -------------------------------------------------------------

    def context_for(
        self,
        order: Order,
        policy: Policy,
        *,
        as_of: datetime | None = None,
        refunds_before: datetime | None = None,
    ) -> OrderContext:
        """Bundle everything the settlement engine needs for one order.

        ``refunds_before`` exists for the gate's replay: when re-asking whether
        a payout should have fired at time T, refunds issued after T had not
        happened yet and must not influence the answer. Passing the full refund
        list there would let hindsight leak into a historical decision.
        """
        refunds = self.refunds_for(order.order_id)
        if refunds_before is not None:
            cutoff = _as_utc(refunds_before)
            refunds = [r for r in refunds if _as_utc(r.issued_at) <= cutoff]

        return OrderContext(
            order=order,
            policy=policy,
            promotion=self.promotion_for(order.order_id),
            deliveries=self.deliveries_for(order.order_id),
            refunds=refunds,
            as_of=as_of,
        )

    def timeline_for(self, order_id: str) -> list[LedgerEvent]:
        """The order's events in the sequence they actually occurred.

        Ordering is the point. A transfer before a delivery confirmation and a
        transfer after it are the same row in a totals view and opposite
        findings in an entitlement view.
        """
        order = self._orders.get(order_id)
        events: list[LedgerEvent] = []

        if order is not None:
            events.append(
                LedgerEvent(
                    occurred_at=order.placed_at,
                    event_type=LedgerEventType.ORDER_PLACED,
                    reference_id=order.order_id,
                    amount_paise=order.gross_amount_paise,
                )
            )

        for p in self._payments.get(order_id, []):
            events.append(
                LedgerEvent(p.captured_at, LedgerEventType.PAYMENT_CAPTURED,
                            p.payment_id, p.amount_paise, p.method)
            )

        promo = self._promotion.get(order_id)
        if promo is not None and order is not None:
            events.append(
                LedgerEvent(order.placed_at, LedgerEventType.PROMOTION_APPLIED,
                            promo.promotion_id, promo.discount_paise, promo.campaign)
            )

        for d in self._deliveries.get(order_id, []):
            events.append(
                LedgerEvent(
                    d.occurred_at,
                    LedgerEventType.DELIVERY_CONFIRMED
                    if d.confirmed
                    else LedgerEventType.DELIVERY_FAILED,
                    d.delivery_id,
                )
            )

        for t in self._transfers.get(order_id, []):
            events.append(
                LedgerEvent(t.executed_at, LedgerEventType.TRANSFER_EXECUTED,
                            t.transfer_id, t.amount_paise, str(t.party_role))
            )

        for r in self._refunds.get(order_id, []):
            events.append(
                LedgerEvent(r.issued_at, LedgerEventType.REFUND_ISSUED,
                            r.refund_id, r.amount_paise, r.reason)
            )

        for r in self._reversals.get(order_id, []):
            events.append(
                LedgerEvent(r.executed_at, LedgerEventType.REVERSAL_EXECUTED,
                            r.reversal_id, r.amount_paise, f"reverses {r.transfer_id}")
            )

        # Stable sort: same-instant events keep insertion order, which follows
        # the causal sequence above rather than an arbitrary one.
        events.sort(key=lambda e: _as_utc(e.occurred_at))
        return events

    @classmethod
    def from_dataset(cls, dataset) -> "EntitlementGraph":
        """Build from a `src.pipeline.Dataset` without importing it.

        Duck-typed on purpose: `pipeline` imports this module, so importing
        `Dataset` back would be circular.
        """
        return cls(
            orders=dataset.orders,
            payments=dataset.payments,
            deliveries=dataset.deliveries,
            promotions=dataset.promotions,
            transfers=dataset.transfers,
            refunds=dataset.refunds,
            reversals=dataset.reversals,
        )
