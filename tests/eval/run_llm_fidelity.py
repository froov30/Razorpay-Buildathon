"""CLI: score the LLM contract compiler against the synthetic corpus.

    python -m tests.eval.run_llm_fidelity            # use cache if present
    python -m tests.eval.run_llm_fidelity --force    # recompile every contract
    python -m tests.eval.run_llm_fidelity --json     # machine-readable

Writes to ``data/synthetic/compiled_policies_llm/``, never to the committed
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
from src.contract_compiler.compiler import ContractCompiler, LLMBackend  # noqa: E402
from src.contract_compiler.dsl import Policy  # noqa: E402
from tests.eval.llm_fidelity import FidelityReport, build_report  # noqa: E402

DEFAULT_LLM_CACHE_DIR = Path("data/synthetic/compiled_policies_llm")
RESULTS_PATH = Path("data/synthetic/llm_fidelity_report.json")

FIELD_ACCURACY_BAR = 0.85
REFUSAL_ACCURACY_BAR = 0.90


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
    print(
        f"  Field accuracy           {report.field_accuracy:>20.1%}"
        f"   ({report.correct_fields}/{report.total_fields} fields)"
    )
    print(
        f"  Contracts fully correct  {report.perfect_contracts:>20}"
        f"   / {report.scored_contracts}"
    )

    failures = report.failures()
    if failures:
        print()
        print(rule("MISREAD FIELDS"))
        for f in failures:
            print(
                f"  {f.contract_id} v{f.version}  {f.field}: "
                f"expected {f.expected!r}, got {f.actual!r}"
            )

    print()
    print(rule("REFUSAL CORRECTNESS"))
    print(f"  Refusal accuracy         {report.refusal_accuracy:>20.1%}")
    for r in report.refusal_results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"  [{mark}] {r.contract_id} v{r.version} ({r.expectation})")
        print(f"         {r.detail[:110]}")

    print()
    print(rule("VERDICT", char="="))
    ok = (
        report.field_accuracy >= FIELD_ACCURACY_BAR
        and report.refusal_accuracy >= REFUSAL_ACCURACY_BAR
    )
    print(
        f"  {'PASS' if ok else 'REVIEW'} — field {report.field_accuracy:.1%}, "
        f"refusal {report.refusal_accuracy:.1%}"
    )
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
        (
            p.provenance.model
            for p in policies.values()
            if p.provenance and p.provenance.model
        ),
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

    return (
        0
        if report.field_accuracy >= FIELD_ACCURACY_BAR
        and report.refusal_accuracy >= REFUSAL_ACCURACY_BAR
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
