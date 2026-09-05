"""Score a compiled Policy against the terms the prose was meant to express.

The project's central claim is that it interprets contracts with an LLM. Every
other metric in this repository is computed from the deterministic fallback
parser, which means the AI component — the differentiating one — carries no
numbers. This module supplies them.

Two things are measured:

**Field extraction accuracy** — of the 10 commercial terms per contract that a
careful human reader should recover, how many did the backend get exactly right?
Exact match only. A commission rate that is close is wrong; it settles money.

**Refusal correctness** — does the backend refuse where it should, and only
where it should? This matters more than raw extraction accuracy. A backend that
scores 100% on readable contracts but confidently invents terms for the
unreadable one is worse than useless, because that failure is silent. Both
directions are scored: failing to refuse on CTR-0007, and over-refusing on a
contract that is perfectly clear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from data.generator.contracts import DELIBERATELY_AMBIGUOUS, INTENDED_TERMS
from src.contract_compiler.dsl import Policy

# How to read each ground-truth field off a compiled Policy.
POLICY_ACCESSORS: dict[str, Callable[[Policy], Any]] = {
    "commission_bps": lambda p: p.commission.rate_bps,
    "applies_to": lambda p: p.commission.applies_to,
    "requires_delivery": lambda p: p.hold.requires_delivery_confirmation,
    "hold_hours": lambda p: p.hold.hold_hours_after_delivery,
    "promo_platform_bps": lambda p: p.promotion_funding.platform_share_bps,
    "promo_seller_bps": lambda p: p.promotion_funding.seller_share_bps,
    "commission_refundable": lambda p: p.refund.commission_refundable,
    "reversal_first": lambda p: p.refund.reversal_must_precede_refund,
    "tds_bps": lambda p: p.tax.tds_on_commission_bps,
    "delivery_fee_paise": lambda p: p.delivery_fee.flat_fee_paise,
}

# Contracts whose effective date is deliberately ambiguous.
DATE_AMBIGUOUS: set[tuple[str, int]] = {("CTR-0003", 2)}

# The two defensible readings planted in CTR-0003 v2's amendment.
EXPECTED_DATE_CANDIDATES = {"2026-02-01", "2026-02-12"}


@dataclass(frozen=True, slots=True)
class FieldResult:
    contract_id: str
    version: int
    field: str
    expected: Any
    actual: Any

    @property
    def correct(self) -> bool:
        return self.expected == self.actual


@dataclass(frozen=True, slots=True)
class RefusalResult:
    contract_id: str
    version: int
    expectation: str
    """One of: must_refuse | must_flag_date_ambiguity | must_not_refuse."""

    passed: bool
    detail: str


def score_policy(policy: Policy, expected: dict[str, Any]) -> list[FieldResult]:
    """Compare every ground-truth field against what the backend extracted."""
    return [
        FieldResult(
            contract_id=policy.contract_id,
            version=policy.version,
            field=name,
            expected=expected[name],
            actual=accessor(policy),
        )
        for name, accessor in POLICY_ACCESSORS.items()
        if name in expected
    ]


def score_refusal(policy: Policy) -> RefusalResult:
    """Check the backend refused where it should, and nowhere else."""
    key = (policy.contract_id, policy.version)

    if policy.contract_id in DELIBERATELY_AMBIGUOUS:
        refused = not policy.is_computable()
        reasons = "; ".join(a.reason for a in policy.term_blocking_ambiguities())
        return RefusalResult(
            contract_id=policy.contract_id,
            version=policy.version,
            expectation="must_refuse",
            passed=refused,
            detail=(
                f"correctly refused: {reasons}"
                if refused
                else "FAILED TO REFUSE — invented terms for an unreadable contract"
            ),
        )

    if key in DATE_AMBIGUOUS:
        flagged = policy.has_date_ambiguity()
        candidates: tuple[str, ...] = ()
        for amb in policy.ambiguities:
            if amb.field_path == "effective.starts_at":
                candidates = amb.candidates
        both = set(candidates) == EXPECTED_DATE_CANDIDATES
        return RefusalResult(
            contract_id=policy.contract_id,
            version=policy.version,
            expectation="must_flag_date_ambiguity",
            passed=flagged and both,
            detail=(
                f"flagged with candidates {sorted(candidates)}"
                if flagged
                else "FAILED TO FLAG the ambiguous effective date"
            ),
        )

    over_refused = not policy.is_computable()
    return RefusalResult(
        contract_id=policy.contract_id,
        version=policy.version,
        expectation="must_not_refuse",
        passed=not over_refused,
        detail=(
            "OVER-REFUSED — a readable contract was reported unreadable"
            if over_refused
            else "correctly readable"
        ),
    )


@dataclass
class FidelityReport:
    model: str
    elapsed_s: float
    field_results: list[FieldResult] = field(default_factory=list)
    refusal_results: list[RefusalResult] = field(default_factory=list)

    @property
    def total_fields(self) -> int:
        return len(self.field_results)

    @property
    def correct_fields(self) -> int:
        return sum(1 for r in self.field_results if r.correct)

    @property
    def field_accuracy(self) -> float:
        return self.correct_fields / self.total_fields if self.total_fields else 0.0

    @property
    def refusal_accuracy(self) -> float:
        if not self.refusal_results:
            return 0.0
        return sum(1 for r in self.refusal_results if r.passed) / len(
            self.refusal_results
        )

    @property
    def perfect_contracts(self) -> int:
        by_contract: dict[tuple[str, int], list[FieldResult]] = {}
        for r in self.field_results:
            by_contract.setdefault((r.contract_id, r.version), []).append(r)
        return sum(1 for rs in by_contract.values() if all(r.correct for r in rs))

    @property
    def scored_contracts(self) -> int:
        return len({(r.contract_id, r.version) for r in self.field_results})

    def failures(self) -> list[FieldResult]:
        return [r for r in self.field_results if not r.correct]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "elapsed_s": round(self.elapsed_s, 2),
            "field_accuracy": round(self.field_accuracy, 4),
            "correct_fields": self.correct_fields,
            "total_fields": self.total_fields,
            "perfect_contracts": self.perfect_contracts,
            "scored_contracts": self.scored_contracts,
            "refusal_accuracy": round(self.refusal_accuracy, 4),
            "refusals": [
                {
                    "contract": f"{r.contract_id} v{r.version}",
                    "expectation": r.expectation,
                    "passed": r.passed,
                    "detail": r.detail,
                }
                for r in self.refusal_results
            ],
            "field_failures": [
                {
                    "contract": f"{r.contract_id} v{r.version}",
                    "field": r.field,
                    "expected": r.expected,
                    "actual": r.actual,
                }
                for r in self.failures()
            ],
        }


def build_report(
    policies: dict[tuple[str, int], Policy],
    model: str,
    elapsed_s: float,
    compile_failures: dict[tuple[str, int], str] | None = None,
) -> FidelityReport:
    """Score every compiled policy against ground truth.

    ``compile_failures`` must be passed for the refusal score to be honest. A
    reply that could not be validated into a policy produces no entry in
    ``policies``, so without this it silently vanishes from the denominator —
    and a model that emitted an incoherent split for the one contract it was
    supposed to refuse would be scored only on the contracts it found easy.
    Each failure is recorded as a failed refusal result instead.
    """
    report = FidelityReport(model=model, elapsed_s=elapsed_s)
    for key, policy in sorted(policies.items()):
        if key in INTENDED_TERMS:
            report.field_results.extend(score_policy(policy, INTENDED_TERMS[key]))
        report.refusal_results.append(score_refusal(policy))

    for (contract_id, version), detail in sorted((compile_failures or {}).items()):
        expectation = (
            "must_refuse" if contract_id in DELIBERATELY_AMBIGUOUS else "must_not_refuse"
        )
        report.refusal_results.append(
            RefusalResult(
                contract_id=contract_id,
                version=version,
                expectation=expectation,
                passed=False,
                detail=f"UNUSABLE REPLY — no valid policy could be built: {detail[:180]}",
            )
        )
    return report
