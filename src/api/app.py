"""FastAPI surface over the entitlement engine.

Read-only by default. The only mutating endpoint is the human review decision,
which is the maker-checker half of the gate — and even that cannot move money
unless the run was started with execution explicitly enabled.

Every response carries the Razorpay execution mode in its envelope. A caller
should never have to guess whether they are looking at real test-mode API
traffic or a local simulation.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.common.money import format_inr
from src.common.types import ExceptionCategory
from src.exception_investigator.investigator import investigate, triage
from src.pipeline import RunResult, load_dataset, run
from tests.eval.metrics import evaluate, load_ground_truth

logger = logging.getLogger(__name__)

app = FastAPI(
    title="EntitleGraph Close Agent",
    version="0.1.0",
    description=(
        "Verifies that money which moved matches what the contract promised. "
        "All data is SYNTHETIC; all Razorpay calls are TEST MODE ONLY."
    ),
)


class _State:
    result: RunResult | None = None
    dataset: Any = None


STATE = _State()


def get_run(refresh: bool = False) -> RunResult:
    """Run the pipeline once and cache it for the process lifetime."""
    if STATE.result is None or refresh:
        STATE.dataset = load_dataset()
        STATE.result = run(dataset=STATE.dataset)
    return STATE.result


def _envelope(result: RunResult) -> dict[str, Any]:
    return {
        "razorpay_mode": result.razorpay_mode,
        "mode_banner": result.razorpay_banner,
        "data_is_synthetic": True,
        "production_integration": False,
    }


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ReviewDecision(BaseModel):
    action: Literal["approve", "reject"]
    approver: str = Field(min_length=3, description="Named human taking responsibility")
    justification: str = Field(min_length=10, description="Why, in the reviewer's words")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    result = get_run()
    return {
        **_envelope(result),
        "status": "ok",
        "orders": len(result.outcomes),
        "records": result.records_processed,
        "audit_chain_ok": result.audit_ok,
        "audit_chain": result.audit_message,
    }


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    """All seven scored metrics, plus the gate breakdown."""
    result = get_run()
    amounts = {o.order_id: o.gross_amount_paise for o in STATE.dataset.orders}
    scored = evaluate(result, load_ground_truth(), order_amounts=amounts)
    return {
        **_envelope(result),
        "headline": {
            "prevented_loss_paise": scored.prevented_loss_paise,
            "prevented_loss": format_inr(scored.prevented_loss_paise),
            "description": "Incorrect payouts the gate refused before they fired",
        },
        "metrics": scored.to_dict(),
        "gate": result.gate_summary,
        "tiers": result.tier_counts(),
        "categories": result.category_counts(),
    }


@app.get("/orders")
def list_orders(tier: str | None = None, category: str | None = None) -> dict[str, Any]:
    result = get_run()
    rows = []
    for outcome in result.outcomes:
        d = outcome.decision
        if tier and str(d.tier) != tier:
            continue
        if category and str(d.category) != category:
            continue
        rows.append(
            {
                **d.to_row(),
                "gate_decision": str(outcome.gate_verdict.decision)
                if outcome.gate_verdict
                else None,
                "amount_at_risk_paise": outcome.gate_verdict.amount_at_risk_paise
                if outcome.gate_verdict
                else 0,
            }
        )
    return {**_envelope(result), "count": len(rows), "orders": rows}


@app.get("/orders/{order_id}")
def order_detail(order_id: str) -> dict[str, Any]:
    """Full derivation and evidence for one order — the audit view."""
    result = get_run()
    outcome = next((o for o in result.outcomes if o.order_id == order_id), None)
    if outcome is None:
        raise HTTPException(status_code=404, detail=f"order {order_id} not found")

    d = outcome.decision
    verdict = outcome.gate_verdict
    case = investigate(d) if d.category != ExceptionCategory.NONE else None

    return {
        **_envelope(result),
        "order_id": d.order_id,
        "contract": {
            "contract_id": d.contract_id,
            "version": d.contract_version,
            "policy_hash": d.policy_hash,
        },
        "conclusion": {
            "tier": str(d.tier),
            "category": str(d.category),
            "explanation": d.explanation,
        },
        "expected": {
            str(role): {
                "party_id": e.party_id,
                "amount_paise": e.entitled_amount_paise,
                "amount": format_inr(e.entitled_amount_paise),
                "payable_now": e.entitled_now,
                "reasons": list(e.reasons),
            }
            for role, e in d.expected.items()
        },
        "actual_paise": {str(k): v for k, v in d.actual_paise.items()},
        "variance_paise": {str(k): v for k, v in d.variance_paise.items()},
        "evidence": d.evidence,
        "gate": None
        if verdict is None
        else {
            "decision": str(verdict.decision),
            "reason": verdict.reason,
            "expected_paise": verdict.expected_paise,
            "amount_at_risk_paise": verdict.amount_at_risk_paise,
            "amount_at_risk": format_inr(verdict.amount_at_risk_paise),
            "token_issued": verdict.token is not None,
        },
        "investigation": None if case is None else case.to_row(),
    }


@app.get("/exceptions")
def exception_queue(limit: int = 50) -> dict[str, Any]:
    """The review queue, most urgent first."""
    result = get_run()
    cases = triage(result.decisions)[:limit]
    return {
        **_envelope(result),
        "count": len(cases),
        "total_exposure": format_inr(sum(c.amount_at_stake_paise for c in cases)),
        "cases": [
            {
                **c.to_row(),
                "mechanism": c.mechanism,
                "evidence": c.evidence[:8],
            }
            for c in cases
        ],
    }


@app.get("/contracts")
def list_contracts() -> dict[str, Any]:
    """Compiled policies, including anything the compiler refused to read."""
    result = get_run()
    return {
        **_envelope(result),
        "contracts": [
            {
                "contract_id": p.contract_id,
                "version": p.version,
                "seller_id": p.seller_id,
                "policy_hash": p.content_hash(),
                "commission_bps": p.commission.rate_bps,
                "commission_base": p.commission.applies_to,
                "computable": p.is_computable(),
                "date_ambiguous": p.has_date_ambiguity(),
                "backend": p.provenance.backend if p.provenance else None,
                "ambiguities": [
                    {
                        "field": a.field_path,
                        "reason": a.reason,
                        "candidates": list(a.candidates),
                        "severity": str(a.severity),
                    }
                    for a in p.ambiguities
                ],
            }
            for p in result.policies
        ],
    }


@app.post("/review/{order_id}")
def review(order_id: str, decision: ReviewDecision) -> dict[str, Any]:
    """Record a human decision on a held order.

    Note this endpoint does not itself release funds: the analysis run holds no
    live gate. It records the reviewer's determination so the demo can show the
    maker-checker flow. Releasing money is the demo script's explicit path.
    """
    result = get_run()
    outcome = next((o for o in result.outcomes if o.order_id == order_id), None)
    if outcome is None:
        raise HTTPException(status_code=404, detail=f"order {order_id} not found")
    if outcome.gate_verdict is None:
        raise HTTPException(
            status_code=409, detail=f"order {order_id} has no proposal to review"
        )
    if outcome.gate_verdict.allowed:
        raise HTTPException(
            status_code=409,
            detail=f"order {order_id} was not held; nothing to review",
        )

    return {
        **_envelope(result),
        "order_id": order_id,
        "recorded": {
            "action": decision.action,
            "approver": decision.approver,
            "justification": decision.justification,
        },
        "original_refusal": outcome.gate_verdict.reason,
        "note": (
            "Recorded. In this reference implementation the analysis run holds "
            "no live gate, so no funds are released by this call."
        ),
    }


@app.post("/refresh")
def refresh() -> dict[str, Any]:
    """Re-run the pipeline (e.g. after regenerating synthetic data)."""
    result = get_run(refresh=True)
    return {**_envelope(result), "orders": len(result.outcomes)}


@lru_cache(maxsize=1)
def _startup_note() -> str:  # pragma: no cover - informational
    return "EntitleGraph API ready — SYNTHETIC DATA, TEST MODE ONLY"
