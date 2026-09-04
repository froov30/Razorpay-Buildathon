"""Evaluation metrics, scored against ground truth the engine never sees.

Ground-truth labels live in ``data/synthetic/ground_truth.json``, written by the
generator and read only here. Nothing on the engine's code path opens that file.

A note on what these numbers do and do not prove
------------------------------------------------
The synthetic ledger is settled from :data:`INTENDED_TERMS` — the terms the
contract prose is meant to express — using an implementation of the settlement
formula written separately from the engine's. So a passing score means the
compiler recovered the right terms from prose and the engine applied them the
way the generator's independent implementation did.

It does not prove the money-flow *convention* is the one a given marketplace
uses; both sides share that convention by construction, and it is documented in
``src/settlement_engine/compute.py`` for exactly that reason. This limitation is
stated again in ``docs/test_plan.md``. Reporting a metric without its blind spot
would be the same failure mode this project exists to criticise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from src.common.money import format_inr
from src.common.types import ConfidenceTier, ExceptionCategory
from src.pipeline import RunResult

GROUND_TRUTH_PATH = Path("data/synthetic/ground_truth.json")


def load_ground_truth(path: Path | str = GROUND_TRUTH_PATH) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found — run `python -m data.generator` first."
        )
    return json.loads(p.read_text(encoding="utf-8"))["labels"]


@dataclass
class ConfusionEntry:
    order_id: str
    expected: str
    predicted: str

    @property
    def correct(self) -> bool:
        return self.expected == self.predicted


@dataclass
class Metrics:
    # -- classification ---------------------------------------------------
    total_orders: int = 0
    classification_correct: int = 0
    exception_tp: int = 0
    exception_fp: int = 0
    exception_fn: int = 0
    exception_tn: int = 0

    # -- entitlement ------------------------------------------------------
    exact_entitlement_matches: int = 0
    resolvable_orders: int = 0

    # -- money ------------------------------------------------------------
    amount_total_paise: int = 0
    amount_correct_paise: int = 0
    prevented_loss_paise: int = 0

    # -- operations -------------------------------------------------------
    auto_closed: int = 0
    unsafe_actions: int = 0
    audit_chain_ok: bool = True
    audit_chain_message: str = ""
    elapsed_s: float = 0.0
    records_processed: int = 0

    mismatches: list[ConfusionEntry] = field(default_factory=list)
    per_category: dict[str, dict[str, int]] = field(default_factory=dict)

    # -- derived ----------------------------------------------------------

    @property
    def classification_accuracy(self) -> float:
        return self.classification_correct / self.total_orders if self.total_orders else 0.0

    @property
    def exception_precision(self) -> float:
        denom = self.exception_tp + self.exception_fp
        return self.exception_tp / denom if denom else 1.0

    @property
    def exception_recall(self) -> float:
        denom = self.exception_tp + self.exception_fn
        return self.exception_tp / denom if denom else 1.0

    @property
    def exception_f1(self) -> float:
        p, r = self.exception_precision, self.exception_recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def exact_entitlement_match_rate(self) -> float:
        return (
            self.exact_entitlement_matches / self.resolvable_orders
            if self.resolvable_orders
            else 0.0
        )

    @property
    def amount_weighted_accuracy(self) -> float:
        return (
            self.amount_correct_paise / self.amount_total_paise
            if self.amount_total_paise
            else 0.0
        )

    @property
    def auto_close_rate(self) -> float:
        return self.auto_closed / self.total_orders if self.total_orders else 0.0

    @property
    def throughput_records_per_s(self) -> float:
        return self.records_processed / self.elapsed_s if self.elapsed_s else 0.0

    @property
    def throughput_orders_per_s(self) -> float:
        return self.total_orders / self.elapsed_s if self.elapsed_s else 0.0

    def to_dict(self) -> dict:
        return {
            "classification_accuracy": round(self.classification_accuracy, 4),
            "exact_entitlement_match_rate": round(self.exact_entitlement_match_rate, 4),
            "exception_precision": round(self.exception_precision, 4),
            "exception_recall": round(self.exception_recall, 4),
            "exception_f1": round(self.exception_f1, 4),
            "amount_weighted_accuracy": round(self.amount_weighted_accuracy, 4),
            "auto_close_rate": round(self.auto_close_rate, 4),
            "throughput_records_per_s": round(self.throughput_records_per_s, 1),
            "throughput_orders_per_s": round(self.throughput_orders_per_s, 1),
            "unsafe_action_count": self.unsafe_actions,
            "prevented_loss_paise": self.prevented_loss_paise,
            "prevented_loss_inr": format_inr(self.prevented_loss_paise),
            "audit_chain_ok": self.audit_chain_ok,
            "total_orders": self.total_orders,
            "records_processed": self.records_processed,
        }


def evaluate(
    result: RunResult,
    ground_truth: dict[str, str] | None = None,
    *,
    order_amounts: dict[str, int] | None = None,
) -> Metrics:
    """Score a pipeline run against ground truth."""
    gt = ground_truth if ground_truth is not None else load_ground_truth()
    m = Metrics()

    m.elapsed_s = result.elapsed_s
    m.records_processed = result.records_processed
    m.unsafe_actions = int(result.gate_summary.get("unsafe_action_attempts", 0))
    m.prevented_loss_paise = int(result.gate_summary.get("prevented_loss_paise", 0))
    m.audit_chain_ok = result.audit_ok
    m.audit_chain_message = result.audit_message

    amounts = order_amounts or {}

    for outcome in result.outcomes:
        d = outcome.decision
        expected = gt.get(d.order_id, str(ExceptionCategory.NONE))
        predicted = str(d.category)
        m.total_orders += 1

        weight = amounts.get(d.order_id, 1)
        m.amount_total_paise += weight

        correct = expected == predicted
        if correct:
            m.classification_correct += 1
            m.amount_correct_paise += weight
        else:
            m.mismatches.append(ConfusionEntry(d.order_id, expected, predicted))

        # Binary exception detection.
        expected_is_exc = expected != str(ExceptionCategory.NONE)
        predicted_is_exc = predicted != str(ExceptionCategory.NONE)
        if expected_is_exc and predicted_is_exc:
            m.exception_tp += 1
        elif not expected_is_exc and predicted_is_exc:
            m.exception_fp += 1
        elif expected_is_exc and not predicted_is_exc:
            m.exception_fn += 1
        else:
            m.exception_tn += 1

        # Per-category breakdown.
        bucket = m.per_category.setdefault(
            expected, {"support": 0, "correct": 0, "predicted_as_other": 0}
        )
        bucket["support"] += 1
        if correct:
            bucket["correct"] += 1
        else:
            bucket["predicted_as_other"] += 1

        # Entitlement exactness — only meaningful where a contract resolved.
        if d.contract_version is not None:
            m.resolvable_orders += 1
            if d.total_abs_variance_paise() == 0:
                m.exact_entitlement_matches += 1

        if d.tier == ConfidenceTier.AUTO_CLEAR:
            m.auto_closed += 1

    return m
