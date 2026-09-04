# LLM Compiler Fidelity Scoring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure and report how accurately the LLM contract-compiler backend recovers contract terms from prose, and whether it correctly refuses to guess on the two deliberately unreadable contracts.

**Architecture:** A new pure-function scoring module compares any compiled `Policy` against the `INTENDED_TERMS` ground truth, producing a field-level and refusal-level report. A CLI runner compiles the corpus through `LLMBackend` into a **separate** cache directory, scores it, and prints a report. The committed deterministic cache is never touched, so the existing reproducibility guarantee is preserved.

**Tech Stack:** Python 3.12, Anthropic SDK (already a pinned dependency), pytest, existing `ContractCompiler` / `LLMBackend` / `INTENDED_TERMS` machinery.

**Spec:** No separate spec document. This plan implements Option A from the design critique in the conversation of 2026-09-05: closing the "LLM backend is not scored" gap documented as a known limitation in `docs/test_plan.md` §6.3.

## Global Constraints

- All money is integer paise; all rates integer basis points. Never floats.
- The committed deterministic cache at `data/synthetic/compiled_policies/` MUST NOT be overwritten. LLM compilation writes to `data/synthetic/compiled_policies_llm/`.
- The existing 163 tests must still pass unchanged at every commit.
- Any test that requires `ANTHROPIC_API_KEY` must `skipif` when it is absent, so the suite stays hermetic.
- No real merchant data. All contracts remain synthetic.
- Report measured numbers honestly, including bad ones. A poor LLM score is a finding to publish, not to hide.
- `src/common/console.py::setup_console` must be called by any new CLI entry point (Windows cp1252 cannot encode `₹`).

---

### Task 1: Policy-vs-terms scoring module

**Files:**
- Create: `tests/eval/llm_fidelity.py`
- Test: `tests/eval/test_llm_fidelity.py`

**Interfaces:**
- Consumes: `INTENDED_TERMS` and `DELIBERATELY_AMBIGUOUS` from `data.generator.contracts`; `Policy` from `src.contract_compiler.dsl`.
- Produces:
  - `POLICY_ACCESSORS: dict[str, Callable[[Policy], object]]`
  - `score_policy(policy: Policy, expected: dict) -> list[FieldResult]`
  - `score_refusal(policy: Policy) -> RefusalResult`
  - `FidelityReport` dataclass with `.field_accuracy`, `.refusal_accuracy`, `.to_dict()`
  - `build_report(policies: dict[tuple[str, int], Policy], model: str, elapsed_s: float) -> FidelityReport`

**Note on duplication:** `tests/unit/test_compiler_fidelity.py` already asserts the same field mapping inline. That test is passing and load-bearing; it is deliberately left untouched rather than refactored to share this accessor table, because refactoring a green safety-net test hours before a deadline trades real risk for cosmetic DRY.

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_llm_fidelity.py`:

```python
"""Unit tests for the LLM fidelity scorer. No API key required."""

from __future__ import annotations

import pytest

from data.generator.contracts import INTENDED_TERMS, build_contract_sources
from src.contract_compiler.compiler import ContractCompiler, DeterministicBackend
from tests.eval.llm_fidelity import (
    POLICY_ACCESSORS,
    FieldResult,
    build_report,
    score_policy,
    score_refusal,
)


@pytest.fixture(scope="module")
def det_policies(tmp_path_factory):
    """Deterministic-backend policies, used as a known-good reference."""
    compiler = ContractCompiler(
        backend=DeterministicBackend(), cache_dir=tmp_path_factory.mktemp("llmfid")
    )
    return {(s.contract_id, s.version): compiler.compile(s) for s in build_contract_sources()}


def test_accessors_cover_every_intended_field():
    """Every ground-truth field must have a way to read it off a Policy."""
    expected_fields = set(next(iter(INTENDED_TERMS.values())).keys())
    assert set(POLICY_ACCESSORS) == expected_fields


def test_score_policy_all_correct_for_deterministic_backend(det_policies):
    """The deterministic backend is known to recover CTR-0001 exactly."""
    key = ("CTR-0001", 1)
    results = score_policy(det_policies[key], INTENDED_TERMS[key])
    assert len(results) == 10
    assert all(r.correct for r in results), [r for r in results if not r.correct]


def test_score_policy_flags_a_wrong_field(det_policies):
    """A mismatch must be reported, not silently passed."""
    key = ("CTR-0001", 1)
    tampered = dict(INTENDED_TERMS[key])
    tampered["commission_bps"] = 9999
    results = score_policy(det_policies[key], tampered)
    wrong = [r for r in results if not r.correct]
    assert len(wrong) == 1
    assert wrong[0].field == "commission_bps"
    assert wrong[0].expected == 9999


def test_score_refusal_passes_when_unreadable_contract_refuses(det_policies):
    """CTR-0007 is deliberately unreadable and must refuse."""
    result = score_refusal(det_policies[("CTR-0007", 1)])
    assert result.expectation == "must_refuse"
    assert result.passed


def test_score_refusal_flags_date_ambiguity(det_policies):
    """CTR-0003 v2 must flag both candidate effective dates."""
    result = score_refusal(det_policies[("CTR-0003", 2)])
    assert result.expectation == "must_flag_date_ambiguity"
    assert result.passed


def test_score_refusal_penalises_over_refusal(det_policies):
    """A clean contract that refuses is a false positive."""
    result = score_refusal(det_policies[("CTR-0001", 1)])
    assert result.expectation == "must_not_refuse"
    assert result.passed


def test_build_report_computes_accuracies(det_policies):
    report = build_report(det_policies, model="deterministic", elapsed_s=1.0)
    assert report.total_fields == 100
    assert report.field_accuracy == 1.0
    assert report.refusal_accuracy == 1.0
    assert "field_accuracy" in report.to_dict()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/eval/test_llm_fidelity.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.eval.llm_fidelity'`

- [ ] **Step 3: Write the implementation**

Create `tests/eval/llm_fidelity.py`:

```python
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
unreadable one is worse than useless, because the failure is silent. Both
directions are scored: failing to refuse on CTR-0007, and over-refusing on a
contract that is perfectly clear.
"""

from __future__ import annotations

import time
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
        return RefusalResult(
            contract_id=policy.contract_id,
            version=policy.version,
            expectation="must_refuse",
            passed=refused,
            detail=(
                "correctly refused: "
                + "; ".join(a.reason for a in policy.term_blocking_ambiguities())
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
        both = set(candidates) == {"2026-02-01", "2026-02-12"}
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
        return sum(1 for r in self.refusal_results if r.passed) / len(self.refusal_results)

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
    policies: dict[tuple[str, int], Policy], model: str, elapsed_s: float
) -> FidelityReport:
    """Score every compiled policy against ground truth."""
    report = FidelityReport(model=model, elapsed_s=elapsed_s)
    for key, policy in sorted(policies.items()):
        if key in INTENDED_TERMS:
            report.field_results.extend(score_policy(policy, INTENDED_TERMS[key]))
        report.refusal_results.append(score_refusal(policy))
    return report
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/eval/test_llm_fidelity.py -q`
Expected: PASS, 7 tests

- [ ] **Step 5: Verify the existing suite is untouched**

Run: `python -m pytest -q`
Expected: PASS, 170 tests (163 existing + 7 new)

- [ ] **Step 6: Commit**

```bash
git add tests/eval/llm_fidelity.py tests/eval/test_llm_fidelity.py
git commit -m "Add policy-vs-ground-truth fidelity scorer

Scores any compiled Policy against INTENDED_TERMS on both field
extraction and refusal correctness. Refusal is scored in both
directions: failing to refuse on an unreadable contract, and
over-refusing on a clear one. Pure functions, no API key needed."
```

---

### Task 2: LLM runner CLI

**Files:**
- Create: `tests/eval/run_llm_fidelity.py`
- Test: append to `tests/eval/test_llm_fidelity.py`

**Interfaces:**
- Consumes: `build_report`, `FidelityReport` from Task 1; `ContractCompiler`, `LLMBackend` from `src.contract_compiler.compiler`; `build_contract_sources` from `data.generator.contracts`.
- Produces: `compile_with_llm(cache_dir, force, model) -> tuple[dict[tuple[str,int], Policy], float]` and `main() -> int`, runnable as `python -m tests.eval.run_llm_fidelity`.

- [ ] **Step 1: Write the failing test**

Append to `tests/eval/test_llm_fidelity.py`:

```python
import os

NEEDS_KEY = pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="live LLM scoring requires ANTHROPIC_API_KEY",
)


def test_llm_cache_dir_is_separate_from_committed_cache():
    """The deterministic cache backs the reproducibility guarantee.

    Overwriting it with LLM output would silently change every headline
    metric in the README, so the runner must use its own directory.
    """
    from tests.eval.run_llm_fidelity import DEFAULT_LLM_CACHE_DIR

    assert "compiled_policies_llm" in str(DEFAULT_LLM_CACHE_DIR)
    assert str(DEFAULT_LLM_CACHE_DIR) != "data/synthetic/compiled_policies"


@NEEDS_KEY
def test_llm_backend_recovers_terms_and_refuses_correctly(tmp_path):
    """Live scored run. Skipped without a key so the suite stays hermetic."""
    from tests.eval.run_llm_fidelity import compile_with_llm

    policies, elapsed = compile_with_llm(cache_dir=tmp_path / "llm", force=False)
    report = build_report(policies, model="live", elapsed_s=elapsed)

    assert report.scored_contracts == 10
    assert report.total_fields == 100
    # Thresholds are deliberately below the observed score: this asserts the
    # backend is usable, not that a specific model version is pinned.
    assert report.field_accuracy >= 0.85, report.to_dict()["field_failures"]
    assert report.refusal_accuracy >= 0.90, report.to_dict()["refusals"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/eval/test_llm_fidelity.py::test_llm_cache_dir_is_separate_from_committed_cache -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.eval.run_llm_fidelity'`

- [ ] **Step 3: Write the implementation**

Create `tests/eval/run_llm_fidelity.py`:

```python
"""CLI: score the LLM contract compiler against the synthetic corpus.

    python -m tests.eval.run_llm_fidelity            # use cache if present
    python -m tests.eval.run_llm_fidelity --force    # recompile every contract
    python -m tests.eval.run_llm_fidelity --json     # machine-readable

Writes to `data/synthetic/compiled_policies_llm/`, never to the committed
deterministic cache — that cache is what makes the headline metrics
reproducible with no API key, and overwriting it would silently change every
number in the README.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from data.generator.contracts import build_contract_sources  # noqa: E402
from src.common.console import rule, setup_console  # noqa: E402
from src.contract_compiler.compiler import (  # noqa: E402
    ContractCompiler,
    LLMBackend,
)
from src.contract_compiler.dsl import Policy  # noqa: E402
from tests.eval.llm_fidelity import FidelityReport, build_report  # noqa: E402

DEFAULT_LLM_CACHE_DIR = Path("data/synthetic/compiled_policies_llm")
RESULTS_PATH = Path("data/synthetic/llm_fidelity_report.json")


def compile_with_llm(
    cache_dir: Path | str = DEFAULT_LLM_CACHE_DIR,
    *,
    force: bool = False,
    model: str | None = None,
) -> tuple[dict[tuple[str, int], Policy], float]:
    """Compile the whole corpus through the LLM backend. Returns policies + seconds."""
    backend = LLMBackend(model=model) if model else LLMBackend()
    compiler = ContractCompiler(backend=backend, cache_dir=cache_dir)
    started = time.perf_counter()
    policies: dict[tuple[str, int], Policy] = {}
    for source in build_contract_sources():
        policies[(source.contract_id, source.version)] = compiler.compile(
            source, force=force
        )
    return policies, time.perf_counter() - started


def print_report(report: FidelityReport) -> None:
    print()
    print(rule("LLM CONTRACT COMPILER — FIDELITY", char="="))
    print(f"  Model                    : {report.model}")
    print(f"  Contracts scored         : {report.scored_contracts}")
    print(f"  Wall time                : {report.elapsed_s:.1f}s")
    print()
    print(rule("TERM EXTRACTION"))
    print(f"  Field accuracy           {report.field_accuracy:>20.1%}"
          f"   ({report.correct_fields}/{report.total_fields} fields)")
    print(f"  Contracts fully correct  {report.perfect_contracts:>20}"
          f"   / {report.scored_contracts}")

    failures = report.failures()
    if failures:
        print()
        print(rule("MISREAD FIELDS"))
        for f in failures:
            print(f"  {f.contract_id} v{f.version}  {f.field}: "
                  f"expected {f.expected!r}, got {f.actual!r}")

    print()
    print(rule("REFUSAL CORRECTNESS"))
    print(f"  Refusal accuracy         {report.refusal_accuracy:>20.1%}")
    for r in report.refusal_results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"  [{mark}] {r.contract_id} v{r.version} ({r.expectation})")
        print(f"         {r.detail[:110]}")

    print()
    print(rule("VERDICT", char="="))
    ok = report.field_accuracy >= 0.85 and report.refusal_accuracy >= 0.90
    print(f"  {'PASS' if ok else 'REVIEW'} — field {report.field_accuracy:.1%}, "
          f"refusal {report.refusal_accuracy:.1%}")
    print(rule(char="="))
    print("Synthetic contracts only. No real merchant agreements.")
    print()


def main() -> int:
    setup_console()
    parser = argparse.ArgumentParser(description="Score the LLM contract compiler")
    parser.add_argument("--force", action="store_true", help="recompile, ignore cache")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--model", default=None, help="override the model id")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_LLM_CACHE_DIR)
    args = parser.parse_args()

    logging.disable(logging.WARNING)

    policies, elapsed = compile_with_llm(
        args.cache_dir, force=args.force, model=args.model
    )
    model_name = next(
        (p.provenance.model for p in policies.values() if p.provenance and p.provenance.model),
        "unknown",
    )
    report = build_report(policies, model=model_name, elapsed_s=elapsed)

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print_report(report)
        print(f"Report written to {RESULTS_PATH}")

    return 0 if report.field_accuracy >= 0.85 and report.refusal_accuracy >= 0.90 else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the offline test to verify it passes**

Run: `python -m pytest tests/eval/test_llm_fidelity.py -q -k "cache_dir"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/eval/run_llm_fidelity.py tests/eval/test_llm_fidelity.py
git commit -m "Add LLM fidelity runner CLI

Compiles the corpus through LLMBackend into a separate cache dir and
prints field-extraction and refusal-correctness scores. The committed
deterministic cache is untouched, so headline metrics stay reproducible
without an API key. Live test skips when no key is present."
```

---

### Task 3: Run it live and capture real results

**Files:**
- Create: `data/synthetic/llm_fidelity_report.json` (generated)
- Modify: `.gitignore` only if the report should be ignored (it should NOT be — it is evidence)

**Interfaces:**
- Consumes: Task 2's CLI.
- Produces: a committed JSON artifact of measured results, cited by the docs in Task 4.

- [ ] **Step 1: Run the live scored evaluation**

Run: `python -m tests.eval.run_llm_fidelity --force`
Expected: a printed report and `data/synthetic/llm_fidelity_report.json` written. Takes roughly 30-90 seconds for 11 contracts.

- [ ] **Step 2: Read the actual numbers**

Do not guess or reuse the numbers from this plan. Read the printed report and the JSON. Record the real `field_accuracy`, `perfect_contracts`, and `refusal_accuracy` — these are the values Task 4 must cite.

**If field accuracy is below 85% or a refusal check fails:** that is a legitimate finding, not a blocker. Report it honestly in the docs. Investigate whether the cause is a genuine model limitation or a prompt defect in `_LLM_SYSTEM_PROMPT`; a prompt fix is in scope, inventing a better number is not.

- [ ] **Step 3: Run the live test**

Run: `python -m pytest tests/eval/test_llm_fidelity.py -q`
Expected: PASS, 9 tests (the live one no longer skips)

- [ ] **Step 4: Verify the committed deterministic cache was not modified**

Run: `git status --short data/synthetic/compiled_policies/`
Expected: no output. If anything is listed, the runner wrote to the wrong directory — stop and fix before continuing.

- [ ] **Step 5: Confirm headline metrics are unchanged**

Run: `python -m tests.eval --quiet`
Expected: still PASS, prevented loss still ₹19,941.19, classification accuracy still 100%.

- [ ] **Step 6: Commit**

```bash
git add data/synthetic/llm_fidelity_report.json data/synthetic/compiled_policies_llm/
git commit -m "Add measured LLM compiler fidelity results

Scored run against the synthetic corpus, committed as evidence so the
reported numbers can be checked rather than taken on trust."
```

---

### Task 4: Documentation — replace the known limitation with measured numbers

**Files:**
- Modify: `docs/test_plan.md` (§6 limitation 3)
- Modify: `README.md` (Results table)
- Modify: `docs/CHANGELOG.md` (new entry)
- Modify: `DEMO.md` (metrics beat, ~4:25-5:00)

**Interfaces:**
- Consumes: the real numbers recorded in Task 3 Step 2.

- [ ] **Step 1: Update the test-plan limitation**

In `docs/test_plan.md` §6, limitation 3 currently reads *"The LLM backend is not scored."* Replace it with the measured result and the limitation that genuinely remains — that scoring covers this 11-contract synthetic corpus, not arbitrary real-world agreements. Use the real numbers from Task 3.

- [ ] **Step 2: Add the metrics to the README results table**

Add two rows to the Results table in `README.md`, using the real measured values:

```markdown
| LLM term-extraction accuracy | <measured>% (<n>/100 fields) |
| LLM refusal correctness | <measured>% (<n>/11 contracts) |
```

Add one sentence under the table explaining that the headline metrics come from the cached deterministic compile for reproducibility, and these two rows measure the LLM path separately.

- [ ] **Step 3: Add a CHANGELOG entry**

Add a section to `docs/CHANGELOG.md` under "Defects found during the build" or a new "Post-submission hardening" heading, describing what was measured, the result, and anything the LLM got wrong. If the model misread a field, name the field and the contract — a documented miss is more credible than a clean sweep.

- [ ] **Step 4: Update the demo script**

In `DEMO.md`, the 4:25-5:00 metrics beat currently says the compiler is LLM-backed but that numbers come from cache. Add one line giving the measured LLM accuracy, so the pitch can claim the AI component is scored rather than asserted.

- [ ] **Step 5: Verify every cited number matches the artifact**

Run: `python -c "import json; print(json.load(open('data/synthetic/llm_fidelity_report.json'))['field_accuracy'])"`
Cross-check every number written in Steps 1-4 against the JSON. A number in the docs that does not appear in the artifact is a fabrication and must be corrected.

- [ ] **Step 6: Full verification**

Run: `python -m pytest -q`
Expected: PASS, 172 tests

Run: `python -m tests.eval --quiet`
Expected: PASS, headline metrics unchanged

- [ ] **Step 7: Commit**

```bash
git add docs/test_plan.md README.md docs/CHANGELOG.md DEMO.md
git commit -m "Document measured LLM compiler fidelity

Replaces the 'LLM backend is not scored' known limitation with real
numbers, and narrows the remaining limitation to what is actually still
true: scoring covers this synthetic corpus, not arbitrary agreements."
```

---

## Self-Review

**Spec coverage:** The design critique's Option A required (a) per-field extraction accuracy against `INTENDED_TERMS` — Task 1 + 3; (b) refusal correctness on the deliberately unreadable contracts — Task 1 `score_refusal` + Task 3; (c) closing the documented `test_plan.md` gap — Task 4. All covered.

**Placeholder scan:** No TBDs. Every code step contains complete runnable code. Task 3 Step 2 and Task 4 deliberately defer *numeric values* to measurement rather than inventing them — that is the point of the task, not a placeholder.

**Type consistency:** `FieldResult`, `RefusalResult`, `FidelityReport`, `POLICY_ACCESSORS`, `score_policy`, `score_refusal`, `build_report` are defined in Task 1 and used with identical names and signatures in Tasks 2 and 3. `compile_with_llm` and `DEFAULT_LLM_CACHE_DIR` are defined in Task 2 and referenced by the Task 2 tests. `RefusalResult.expectation` uses the same three string values in the implementation and the tests.

**Risk check:** The one destructive risk is overwriting the committed deterministic cache, which would silently change every headline metric. Guarded three ways: a separate default directory, a test asserting the paths differ, and an explicit `git status` check in Task 3 Step 4.
