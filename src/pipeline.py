"""End-to-end pipeline: synthetic ledger -> compiled contracts -> verdicts.

One run does four things for every order:

1. **Resolve** which contract version governed it (or refuse — see resolver).
2. **Compute** what each party was entitled to under that version.
3. **Match** the entitlement against what the ledger says actually moved.
4. **Replay through the gate** — re-propose the seller payout as though it had
   not fired yet, evaluated *as of the moment it actually fired*, and record
   whether the gate would have let it through.

Step 4 is what produces the prevented-loss figure, and the "as of the moment it
fired" detail is what makes it meaningful: a payout released three hours after
delivery under a 48-hour hold is perfectly payable *today*, so replaying it at
today's clock would wave it through and the breach would vanish. The gate has to
be asked the question the settlement job faced at the time.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Every entry point (dashboard, API, eval harness) imports this module, so
# loading .env here — once, at import time — is what actually makes a local
# .env file take effect. Without this, RazorpayRouteClient.resolve_mode() would
# never see RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET/ENTITLEGRAPH_RAZORPAY_MODE even
# with a correctly filled-in .env sitting in the repo root. Does not override
# variables already set in the real environment (e.g. by a test's monkeypatch).
load_dotenv()

from src.audit.log import AuditLog
from src.common.types import (
    ConfidenceTier,
    ContractSource,
    DeliveryEvent,
    EntitlementDecision,
    ExceptionCategory,
    Order,
    PartyRole,
    PaymentEvent,
    Promotion,
    RefundEvent,
    ReversalEvent,
    Transfer,
)
from src.contract_compiler.compiler import ContractCompiler
from src.contract_compiler.dsl import Policy
from src.contract_compiler.resolver import Resolution, group_by_contract, resolve
from src.entitlement_graph.graph import EntitlementGraph
from src.razorpay_client.client import RazorpayRouteClient, TransferProposal
from src.settlement_engine.compute import OrderContext, compute_entitlements
from src.settlement_engine.gate import EntitlementGate, GateVerdict
from src.settlement_engine.matcher import match, unresolved_decision

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path("data/synthetic")
DEFAULT_DB_PATH = Path("data/synthetic/entitlegraph.db")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(slots=True)
class Dataset:
    contracts: list[ContractSource] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    payments: list[PaymentEvent] = field(default_factory=list)
    deliveries: list[DeliveryEvent] = field(default_factory=list)
    promotions: list[Promotion] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)
    refunds: list[RefundEvent] = field(default_factory=list)
    reversals: list[ReversalEvent] = field(default_factory=list)

    def record_count(self) -> int:
        return sum(
            len(getattr(self, f))
            for f in (
                "contracts", "orders", "payments", "deliveries",
                "promotions", "transfers", "refunds", "reversals",
            )
        )


def load_dataset(data_dir: Path | str = DEFAULT_DATA_DIR) -> Dataset:
    """Read the synthetic ledger from disk. Never reads ground_truth.json."""
    d = Path(data_dir)

    def read(name: str) -> list[dict[str, Any]]:
        path = d / name
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found — run `python -m data.generator` first."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    return Dataset(
        contracts=[
            ContractSource(
                contract_id=c["contract_id"],
                version=c["version"],
                seller_id=c["seller_id"],
                effective_from=_dt(c["effective_from"]),
                effective_to=_dt(c["effective_to"]),
                body=c["body"],
                notes=c.get("notes", ""),
            )
            for c in read("contracts.json")
        ],
        orders=[
            Order(
                order_id=o["order_id"],
                seller_id=o["seller_id"],
                placed_at=_dt(o["placed_at"]),  # type: ignore[arg-type]
                gross_amount_paise=o["gross_amount_paise"],
                shipping_fee_paise=o["shipping_fee_paise"],
                tax_collected_paise=o["tax_collected_paise"],
                delivery_partner_id=o.get("delivery_partner_id"),
                promotion_id=o.get("promotion_id"),
            )
            for o in read("orders.json")
        ],
        payments=[
            PaymentEvent(
                payment_id=p["payment_id"],
                order_id=p["order_id"],
                captured_at=_dt(p["captured_at"]),  # type: ignore[arg-type]
                amount_paise=p["amount_paise"],
                method=p.get("method", "upi"),
            )
            for p in read("payments.json")
        ],
        deliveries=[
            DeliveryEvent(
                delivery_id=v["delivery_id"],
                order_id=v["order_id"],
                occurred_at=_dt(v["occurred_at"]),  # type: ignore[arg-type]
                confirmed=v["confirmed"],
                delivery_partner_id=v.get("delivery_partner_id"),
            )
            for v in read("deliveries.json")
        ],
        promotions=[
            Promotion(
                promotion_id=p["promotion_id"],
                order_id=p["order_id"],
                discount_paise=p["discount_paise"],
                campaign=p["campaign"],
                declared_funder=PartyRole(p["declared_funder"])
                if p.get("declared_funder")
                else None,
            )
            for p in read("promotions.json")
        ],
        transfers=[
            Transfer(
                transfer_id=t["transfer_id"],
                order_id=t["order_id"],
                party_role=PartyRole(t["party_role"]),
                party_id=t["party_id"],
                amount_paise=t["amount_paise"],
                executed_at=_dt(t["executed_at"]),  # type: ignore[arg-type]
                tds_withheld_paise=t.get("tds_withheld_paise", 0),
                razorpay_transfer_id=t.get("razorpay_transfer_id"),
                reversed_by=t.get("reversed_by"),
            )
            for t in read("transfers.json")
        ],
        refunds=[
            RefundEvent(
                refund_id=r["refund_id"],
                order_id=r["order_id"],
                issued_at=_dt(r["issued_at"]),  # type: ignore[arg-type]
                amount_paise=r["amount_paise"],
                reason=r.get("reason", "customer_return"),
            )
            for r in read("refunds.json")
        ],
        reversals=[
            ReversalEvent(
                reversal_id=r["reversal_id"],
                transfer_id=r["transfer_id"],
                order_id=r["order_id"],
                executed_at=_dt(r["executed_at"]),  # type: ignore[arg-type]
                amount_paise=r["amount_paise"],
            )
            for r in read("reversals.json")
        ],
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OrderOutcome:
    """Everything the system concluded about one order."""

    order_id: str
    decision: EntitlementDecision
    gate_verdict: GateVerdict | None = None
    resolution: Resolution | None = None


@dataclass(slots=True)
class RunResult:
    outcomes: list[OrderOutcome] = field(default_factory=list)
    policies: list[Policy] = field(default_factory=list)
    elapsed_s: float = 0.0
    records_processed: int = 0
    compile_stats: dict[str, int] = field(default_factory=dict)
    gate_summary: dict[str, Any] = field(default_factory=dict)
    audit_ok: bool = True
    audit_message: str = ""
    razorpay_mode: str = ""
    razorpay_banner: str = ""
    graph: EntitlementGraph | None = None
    """Per-order event views, retained so callers can show a timeline."""

    @property
    def decisions(self) -> list[EntitlementDecision]:
        return [o.decision for o in self.outcomes]

    def throughput_rps(self) -> float:
        return (self.records_processed / self.elapsed_s) if self.elapsed_s else 0.0

    def orders_per_second(self) -> float:
        return (len(self.outcomes) / self.elapsed_s) if self.elapsed_s else 0.0

    def tier_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.decisions:
            counts[str(d.tier)] = counts.get(str(d.tier), 0) + 1
        return counts

    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.decisions:
            counts[str(d.category)] = counts.get(str(d.category), 0) + 1
        return counts


def run(
    data_dir: Path | str = DEFAULT_DATA_DIR,
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    dataset: Dataset | None = None,
    compiler: ContractCompiler | None = None,
    client: RazorpayRouteClient | None = None,
    force_recompile: bool = False,
    execute_allowed: bool = False,
) -> RunResult:
    """Run the full pipeline over a dataset.

    ``execute_allowed`` controls whether gate-approved proposals are actually
    sent to the Razorpay client. It defaults to False so that analysis runs and
    the evaluation harness never move money, not even in test mode — executing
    is an explicit choice made by the demo path, not a side effect of scoring.
    """
    ds = dataset or load_dataset(data_dir)
    compiler = compiler or ContractCompiler()
    client = client or RazorpayRouteClient()

    db = Path(db_path)
    if db.exists():
        db.unlink()  # a run starts a fresh audit chain
    audit = AuditLog(db)
    gate = EntitlementGate(client, audit)

    result = RunResult(
        razorpay_mode=str(client.mode), razorpay_banner=client.mode_banner()
    )
    audit.record(
        actor="entitlegraph.pipeline",
        action="run.start",
        subject_id="batch",
        outcome="started",
        payload={
            "records": ds.record_count(),
            "orders": len(ds.orders),
            "razorpay_mode": str(client.mode),
            "data_is_synthetic": True,
        },
    )

    started = time.perf_counter()

    # -- compile every contract version (cached) --------------------------
    policies = [compiler.compile(src, force=force_recompile) for src in ds.contracts]
    result.policies = policies
    result.compile_stats = dict(compiler.stats)
    by_contract = group_by_contract(policies)
    contract_for_seller = {p.seller_id: p.contract_id for p in policies}

    # -- fold the flat ledger into per-order event views -------------------
    graph = EntitlementGraph.from_dataset(ds)
    result.graph = graph

    # -- per-order processing ---------------------------------------------
    for order in ds.orders:
        contract_id = contract_for_seller.get(order.seller_id, "UNKNOWN")
        versions = by_contract.get(contract_id, [])
        resolution = resolve(versions, order.placed_at, contract_id=contract_id)

        order_transfers = graph.transfers_for(order.order_id)
        seller_transfer = graph.seller_transfer(order.order_id)

        if not resolution.is_resolved:
            decision = unresolved_decision(order.order_id, resolution, order_transfers)
            verdict = None
            if seller_transfer is not None:
                verdict = gate.submit(
                    _proposal(order, seller_transfer), None, resolution
                )
            result.outcomes.append(
                OrderOutcome(order.order_id, decision, verdict, resolution)
            )
            _audit_decision(audit, decision, verdict)
            continue

        policy = resolution.policy
        assert policy is not None

        # as_of=None: the matcher evaluates "now"; the gate replays at fire time
        ctx = graph.context_for(order, policy)

        computation = compute_entitlements(ctx)
        decision = match(
            ctx, computation, order_transfers, graph.reversals_for(order.order_id)
        )

        # Replay the seller payout through the gate, as of when it fired.
        # `refunds_before` keeps hindsight out of the historical decision: a
        # refund issued after the payout had not happened when the gate would
        # have been asked.
        verdict = None
        if seller_transfer is not None:
            replay_ctx = graph.context_for(
                order,
                policy,
                as_of=seller_transfer.executed_at,
                refunds_before=seller_transfer.executed_at,
            )
            verdict = gate.submit(
                _proposal(order, seller_transfer), replay_ctx, resolution
            )
            if execute_allowed and verdict.allowed:
                gate.execute(verdict)

        result.outcomes.append(
            OrderOutcome(order.order_id, decision, verdict, resolution)
        )
        _audit_decision(audit, decision, verdict)

    result.elapsed_s = time.perf_counter() - started
    result.records_processed = ds.record_count()
    result.gate_summary = gate.summary()

    ok, message = audit.verify_chain()
    result.audit_ok, result.audit_message = ok, message

    audit.record(
        actor="entitlegraph.pipeline",
        action="run.finish",
        subject_id="batch",
        outcome="completed",
        payload={
            "orders": len(result.outcomes),
            "elapsed_s": round(result.elapsed_s, 4),
            "tiers": result.tier_counts(),
            "prevented_loss_paise": result.gate_summary.get("prevented_loss_paise"),
        },
    )
    audit.close()
    return result


def _proposal(order: Order, transfer: Transfer) -> TransferProposal:
    return TransferProposal(
        proposal_id=f"PRP-{transfer.transfer_id}",
        order_id=order.order_id,
        party_role=transfer.party_role,
        party_account_id=f"acc_{transfer.party_id}",
        amount_paise=transfer.amount_paise,
        notes={"seller_id": order.seller_id, "synthetic": "true"},
    )


def _audit_decision(
    audit: AuditLog, decision: EntitlementDecision, verdict: GateVerdict | None
) -> None:
    audit.record(
        actor="entitlegraph.engine",
        action="entitlement.decision",
        subject_id=decision.order_id,
        outcome=str(decision.tier),
        payload={
            "category": str(decision.category),
            "contract_id": decision.contract_id,
            "contract_version": decision.contract_version,
            "policy_hash": decision.policy_hash,
            "abs_variance_paise": decision.total_abs_variance_paise(),
            "explanation": decision.explanation,
            "gate_decision": str(verdict.decision) if verdict else None,
        },
    )


EXCEPTION_TIERS = (ConfidenceTier.NEEDS_REVIEW, ConfidenceTier.BLOCKED)
CLEAN_CATEGORIES = (ExceptionCategory.NONE,)
