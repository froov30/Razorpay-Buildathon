"""Settlement computation: the identity, the timing conditions, the edge cases."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from src.common.types import DeliveryEvent, Order, PartyRole, Promotion, RefundEvent
from src.settlement_engine.compute import compute_entitlements
from tests.conftest import utc


class TestSettlementIdentity:
    """Parties + withholding must reconcile to collected revenue, exactly."""

    def test_holds_for_a_simple_order(self, simple_ctx):
        c = compute_entitlements(simple_ctx)
        total = sum(e.entitled_amount_paise for e in c.entitlements.values())
        assert total + c.tds_paise == c.net_order_value_paise

    @pytest.mark.parametrize("gross", [1, 7, 99, 100_00, 123_45, 999_99, 12_34_567])
    def test_holds_across_awkward_amounts(self, simple_ctx, gross):
        """Amounts chosen to force rounding residues."""
        ctx = replace(
            simple_ctx, order=replace(simple_ctx.order, gross_amount_paise=gross)
        )
        c = compute_entitlements(ctx)
        total = sum(e.entitled_amount_paise for e in c.entitlements.values())
        assert total + c.tds_paise == c.net_order_value_paise

    def test_holds_with_a_discount(self, simple_ctx):
        ctx = replace(
            simple_ctx,
            promotion=Promotion(
                promotion_id="PRM-1", order_id="ORD-TEST",
                discount_paise=17_77, campaign="TEST",
            ),
        )
        c = compute_entitlements(ctx)
        total = sum(e.entitled_amount_paise for e in c.entitlements.values())
        assert total + c.tds_paise == c.net_order_value_paise


class TestTimingConditions:
    def test_not_payable_before_delivery_confirmation(self, simple_ctx):
        ctx = replace(simple_ctx, deliveries=[], as_of=utc("2026-02-01"))
        c = compute_entitlements(ctx)
        seller = c.entitlements[PartyRole.SELLER]
        assert not seller.entitled_now
        assert seller.entitled_amount_paise > 0, "owed, but not owed yet"
        assert any("NOT YET PAYABLE" in r for r in seller.reasons)

    def test_not_payable_inside_the_hold_window(self, simple_ctx):
        delivered = simple_ctx.deliveries[0].occurred_at
        ctx = replace(simple_ctx, as_of=delivered + timedelta(hours=23))
        assert not compute_entitlements(ctx).entitlements[PartyRole.SELLER].entitled_now

    def test_payable_once_the_hold_elapses(self, simple_ctx):
        delivered = simple_ctx.deliveries[0].occurred_at
        ctx = replace(simple_ctx, as_of=delivered + timedelta(hours=24))
        assert compute_entitlements(ctx).entitlements[PartyRole.SELLER].entitled_now

    def test_failed_delivery_never_becomes_payable(self, simple_ctx):
        ctx = replace(
            simple_ctx,
            deliveries=[
                DeliveryEvent(
                    delivery_id="D", order_id="ORD-TEST",
                    occurred_at=utc("2026-01-11"), confirmed=False,
                )
            ],
            as_of=utc("2027-01-01"),  # a year later
        )
        assert not compute_entitlements(ctx).entitlements[PartyRole.SELLER].entitled_now

    def test_platform_share_is_not_realised_before_settlement(self, simple_ctx):
        """An unsettled order is not a shortfall against the platform."""
        ctx = replace(simple_ctx, deliveries=[], as_of=utc("2026-02-01"))
        assert not compute_entitlements(ctx).entitlements[PartyRole.PLATFORM].entitled_now


class TestCommissionBase:
    def test_net_vs_gross_base_differ_when_discounted(self, compiler, simple_source):
        from dataclasses import replace as dc_replace

        from src.settlement_engine.compute import OrderContext

        policy = compiler.compile(simple_source)
        order = Order(
            order_id="O", seller_id="S", placed_at=utc("2026-01-10"),
            gross_amount_paise=90_000,
        )
        promo = Promotion(
            promotion_id="P", order_id="O", discount_paise=10_000, campaign="X"
        )
        delivered = [
            DeliveryEvent(
                delivery_id="D", order_id="O",
                occurred_at=utc("2026-01-11"), confirmed=True,
            )
        ]
        on_net = compute_entitlements(
            OrderContext(order, policy, promo, delivered, as_of=utc("2026-01-20"))
        )
        gross_policy = dc_replace(
            policy, commission=dc_replace(policy.commission, applies_to="order_gross")
        )
        on_gross = compute_entitlements(
            OrderContext(order, gross_policy, promo, delivered, as_of=utc("2026-01-20"))
        )
        # 20% of ₹900 vs 20% of ₹1000.
        assert on_net.commission_paise == 18_000
        assert on_gross.commission_paise == 20_000


class TestRefunds:
    def test_full_refund_zeroes_the_seller_share(self, simple_ctx):
        ctx = replace(
            simple_ctx,
            refunds=[
                RefundEvent(
                    refund_id="R", order_id="ORD-TEST",
                    issued_at=utc("2026-01-20"), amount_paise=100_000,
                )
            ],
        )
        c = compute_entitlements(ctx)
        assert c.refund_ratio_bps == 10_000
        assert c.entitlements[PartyRole.SELLER].entitled_amount_paise == 0

    def test_partial_refund_prorates(self, simple_ctx):
        ctx = replace(
            simple_ctx,
            refunds=[
                RefundEvent(
                    refund_id="R", order_id="ORD-TEST",
                    issued_at=utc("2026-01-20"), amount_paise=50_000,
                )
            ],
        )
        c = compute_entitlements(ctx)
        assert c.refund_ratio_bps == 5_000
        assert c.entitlements[PartyRole.SELLER].entitled_amount_paise == 40_000


def test_refuses_to_compute_against_an_ambiguous_policy(compiler, simple_ctx):
    """The silent-wrong-answer failure this system exists to prevent."""
    from src.contract_compiler.dsl import Ambiguity, AmbiguitySeverity

    broken = replace(
        simple_ctx.policy,
        ambiguities=[
            Ambiguity(
                field_path="commission.rate_bps",
                reason="two rates stated",
                severity=AmbiguitySeverity.BLOCKING,
            )
        ],
    )
    with pytest.raises(ValueError, match="not computable"):
        compute_entitlements(replace(simple_ctx, policy=broken))


def test_derivation_is_recorded_for_the_reviewer(simple_ctx):
    c = compute_entitlements(simple_ctx)
    assert c.derivation
    assert any("Commission" in step for step in c.derivation)
    assert any("Net order value" in step for step in c.derivation)
