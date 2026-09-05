"""The entitlement graph: per-order event views folded from the flat ledger."""

from __future__ import annotations

import pytest

from src.common.types import LedgerEventType, PartyRole
from src.entitlement_graph.graph import EntitlementGraph
from src.pipeline import load_dataset


@pytest.fixture(scope="module")
def graph():
    return EntitlementGraph.from_dataset(load_dataset())


def test_covers_every_order(graph):
    assert len(graph) == 40


def test_joins_events_to_the_right_order(graph):
    """ORD-1005 is the missing-reversal case: paid, then refunded, never reversed."""
    assert graph.refunds_for("ORD-1005"), "refund should be attached"
    assert graph.transfers_for("ORD-1005"), "transfers should be attached"
    assert graph.reversals_for("ORD-1005") == [], "no reversal is the whole finding"


def test_seller_transfer_is_the_movement_the_gate_replays(graph):
    t = graph.seller_transfer("ORD-1003")
    assert t is not None
    assert t.party_role is PartyRole.SELLER


def test_unknown_order_yields_empty_views_not_errors(graph):
    assert graph.transfers_for("ORD-NOPE") == []
    assert graph.refunds_for("ORD-NOPE") == []
    assert graph.promotion_for("ORD-NOPE") is None


class TestTimeline:
    def test_events_are_ordered_by_time(self, graph):
        events = graph.timeline_for("ORD-1005")
        times = [e.occurred_at for e in events]
        assert times == sorted(times)

    def test_timeline_includes_the_full_lifecycle(self, graph):
        kinds = {e.event_type for e in graph.timeline_for("ORD-1005")}
        assert LedgerEventType.ORDER_PLACED in kinds
        assert LedgerEventType.PAYMENT_CAPTURED in kinds
        assert LedgerEventType.TRANSFER_EXECUTED in kinds
        assert LedgerEventType.REFUND_ISSUED in kinds

    def test_failed_delivery_is_distinguished_from_confirmed(self, graph):
        """ORD-1018 was paid although delivery was never confirmed."""
        kinds = {e.event_type for e in graph.timeline_for("ORD-1018")}
        assert LedgerEventType.DELIVERY_FAILED in kinds
        assert LedgerEventType.DELIVERY_CONFIRMED not in kinds

    def test_premature_payout_is_visible_as_ordering(self, graph):
        """The defect is a sequence, not a total — the transfer precedes release.

        ORD-1018's payout fired while no confirmed delivery existed at all, so
        no DELIVERY_CONFIRMED event precedes the transfer in the timeline. A
        totals view cannot express this; the ordering is the finding.
        """
        events = graph.timeline_for("ORD-1018")
        transfer_at = next(
            e.occurred_at for e in events
            if e.event_type is LedgerEventType.TRANSFER_EXECUTED
        )
        confirmed_before = [
            e for e in events
            if e.event_type is LedgerEventType.DELIVERY_CONFIRMED
            and e.occurred_at <= transfer_at
        ]
        assert confirmed_before == []

    def test_events_describe_themselves_for_a_reviewer(self, graph):
        for event in graph.timeline_for("ORD-1005"):
            assert event.reference_id in event.describe()


class TestContextFor:
    def test_bundles_what_the_engine_needs(self, graph, compiler):
        from data.generator.contracts import build_contract_sources

        source = next(
            s for s in build_contract_sources()
            if s.contract_id == "CTR-0001" and s.version == 1
        )
        policy = compiler.compile(source)
        order = next(o for o in graph.orders() if o.order_id == "ORD-1005")

        ctx = graph.context_for(order, policy)
        assert ctx.order.order_id == "ORD-1005"
        assert ctx.policy is policy
        assert ctx.refunds, "refund belongs to this order"

    def test_refunds_before_keeps_hindsight_out_of_a_replay(self, graph, compiler):
        """A refund issued after a payout must not influence whether that
        payout should have fired. Otherwise the gate is judged with
        information the settlement job did not have."""
        from data.generator.contracts import build_contract_sources

        source = next(
            s for s in build_contract_sources()
            if s.contract_id == "CTR-0001" and s.version == 1
        )
        policy = compiler.compile(source)
        order = next(o for o in graph.orders() if o.order_id == "ORD-1005")

        payout = graph.seller_transfer("ORD-1005")
        assert payout is not None

        full = graph.context_for(order, policy)
        replay = graph.context_for(
            order, policy,
            as_of=payout.executed_at,
            refunds_before=payout.executed_at,
        )
        assert len(full.refunds) == 1
        assert replay.refunds == [], "refund came after the payout, so it is excluded"
        assert replay.as_of == payout.executed_at
