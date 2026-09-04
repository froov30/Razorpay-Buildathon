"""Shared fixtures.

Tests run hermetically: the compiler is pinned to the deterministic backend and
the Razorpay client to MOCK, so no test touches the network or depends on an API
key being present.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.audit.log import AuditLog
from src.common.types import (
    ContractSource,
    DeliveryEvent,
    Order,
    PartyRole,
    RazorpayMode,
)
from src.contract_compiler.compiler import ContractCompiler, DeterministicBackend
from src.razorpay_client.client import RazorpayRouteClient, TransferProposal
from src.settlement_engine.compute import OrderContext
from src.settlement_engine.gate import EntitlementGate


def pytest_addoption(parser):
    parser.addoption(
        "--run-live-llm",
        action="store_true",
        default=False,
        help="run the live LLM fidelity test (makes real, rate-limited API calls)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live_llm: makes real LLM API calls; opt in with --run-live-llm"
    )


def pytest_collection_modifyitems(config, items):
    """Keep metered API calls out of the default test run.

    The live fidelity test paces itself against a 5-requests-per-minute free
    tier, so it takes minutes and consumes a daily quota. Running it on every
    `pytest` invocation would make the suite slow, non-hermetic, and capable of
    exhausting a shared resource — so it is opt-in via `--run-live-llm`, and
    `python -m tests.eval.run_llm_fidelity` remains the normal way to score it.
    """
    if config.getoption("--run-live-llm"):
        return
    skip = pytest.mark.skip(reason="live LLM test: pass --run-live-llm to run it")
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip)


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


@pytest.fixture
def compiler(tmp_path: Path) -> ContractCompiler:
    """Compiler with the deterministic backend and a throwaway cache."""
    return ContractCompiler(
        backend=DeterministicBackend(), cache_dir=tmp_path / "policies"
    )


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    log = AuditLog(tmp_path / "audit.db")
    yield log
    log.close()


@pytest.fixture
def client() -> RazorpayRouteClient:
    """MOCK-mode client with a fixed token secret."""
    return RazorpayRouteClient(
        key_id="", key_secret="", mode=RazorpayMode.MOCK, token_secret=b"test-secret-32"
    )


@pytest.fixture
def gate(client: RazorpayRouteClient, audit: AuditLog) -> EntitlementGate:
    return EntitlementGate(client, audit)


SIMPLE_CONTRACT_BODY = """\
SYNTHETIC DOCUMENT — test fixture.

Contract: CTR-TEST
Version: 1
Seller: SLR-TEST
Effective from: 2026-01-01

1. COMMISSION. The Platform shall retain a commission of twenty percent (20%) of
   the Net Order Value.

2. SETTLEMENT. The Seller's share shall become payable only upon confirmation of
   delivery, and shall be settled no earlier than twenty-four (24) hours
   thereafter.

3. PROMOTIONS. Discounts shall be funded 100% by the Platform.

4. REFUNDS. Where a refund is issued the Seller payout shall first be reversed.
   Commission is refundable.

5. TAX. No tax shall be withheld at source under this agreement.

6. DELIVERY. No delivery partner fee applies to this agreement.
"""


@pytest.fixture
def simple_source() -> ContractSource:
    return ContractSource(
        contract_id="CTR-TEST",
        version=1,
        seller_id="SLR-TEST",
        effective_from=utc("2026-01-01"),
        effective_to=None,
        body=SIMPLE_CONTRACT_BODY,
    )


@pytest.fixture
def simple_ctx(compiler: ContractCompiler, simple_source: ContractSource) -> OrderContext:
    """One clean order: ₹1,000.00 net, delivered, hold elapsed."""
    policy = compiler.compile(simple_source)
    placed = utc("2026-01-10")
    order = Order(
        order_id="ORD-TEST",
        seller_id="SLR-TEST",
        placed_at=placed,
        gross_amount_paise=100_000,
        shipping_fee_paise=0,
        tax_collected_paise=0,
    )
    delivered = placed + timedelta(hours=12)
    return OrderContext(
        order=order,
        policy=policy,
        deliveries=[
            DeliveryEvent(
                delivery_id="DEL-TEST",
                order_id="ORD-TEST",
                occurred_at=delivered,
                confirmed=True,
            )
        ],
        as_of=delivered + timedelta(hours=25),  # 24h hold satisfied
    )


@pytest.fixture
def seller_proposal() -> TransferProposal:
    """The correct seller payout for `simple_ctx`: ₹1,000 less 20% = ₹800."""
    return TransferProposal(
        proposal_id="PRP-TEST",
        order_id="ORD-TEST",
        party_role=PartyRole.SELLER,
        party_account_id="acc_SLR-TEST",
        amount_paise=80_000,
    )
