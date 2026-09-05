"""CLI: run the pipeline and print the scored evaluation report.

    python -m tests.eval                 # full report
    python -m tests.eval --json          # machine-readable
    python -m tests.eval --recompile     # force contract recompilation
"""

from __future__ import annotations

import argparse
import json
import logging

from src.common import config
from src.common.console import rule, setup_console
from src.common.money import format_inr
from src.exception_investigator.investigator import triage
from src.pipeline import load_dataset, run
from tests.eval.metrics import evaluate, load_ground_truth


def main() -> int:
    setup_console()
    parser = argparse.ArgumentParser(description="Evaluate EntitleGraph")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--recompile", action="store_true", help="force recompile")
    parser.add_argument("--quiet", action="store_true", help="suppress log noise")
    args = parser.parse_args()

    if args.quiet or args.json:
        logging.disable(logging.WARNING)
    else:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    dataset = load_dataset()
    result = run(dataset=dataset, force_recompile=args.recompile)
    amounts = {o.order_id: o.gross_amount_paise for o in dataset.orders}
    metrics = evaluate(result, load_ground_truth(), order_amounts=amounts)

    if args.json:
        print(json.dumps(metrics.to_dict(), indent=2))
        return 0 if _passing(metrics) else 1

    print()
    print(rule("ENTITLEGRAPH CLOSE AGENT — EVALUATION", char="="))
    print(f"  Razorpay client mode : {result.razorpay_mode}")
    print(f"  {result.razorpay_banner}")
    print(f"  Data                 : SYNTHETIC ({result.records_processed} records, "
          f"{metrics.total_orders} orders)")
    print(f"  Contract compile     : {result.compile_stats.get('hits', 0)} cache hits, "
          f"{result.compile_stats.get('misses', 0)} misses")

    print()
    print(rule("HEADLINE"))
    print(f"  Prevented loss                {format_inr(metrics.prevented_loss_paise):>18}")
    print("    (incorrect payouts the gate refused before they fired)")

    print()
    print(rule("ACCURACY"))
    print(f"  Classification accuracy       {metrics.classification_accuracy:>17.1%}")
    print(f"  Exact entitlement-match rate  {metrics.exact_entitlement_match_rate:>17.1%}"
          f"   ({metrics.exact_entitlement_matches}/{metrics.resolvable_orders} resolvable)")
    print(f"  Amount-weighted accuracy      {metrics.amount_weighted_accuracy:>17.1%}")
    print(f"  Exception precision           {metrics.exception_precision:>17.1%}")
    print(f"  Exception recall              {metrics.exception_recall:>17.1%}")
    print(f"  Exception F1                  {metrics.exception_f1:>17.1%}")
    print(f"     TP={metrics.exception_tp}  FP={metrics.exception_fp}  "
          f"FN={metrics.exception_fn}  TN={metrics.exception_tn}")

    print()
    print(rule("OPERATIONS"))
    print(f"  Auto-close rate               {metrics.auto_close_rate:>17.1%}"
          f"   ({metrics.auto_closed}/{metrics.total_orders} closed with no human)")
    print(f"  Throughput                    {metrics.throughput_records_per_s:>14,.0f} rec/s"
          f"   ({metrics.throughput_orders_per_s:,.0f} orders/s)")
    print(f"  Unsafe-action count           {metrics.unsafe_actions:>18}"
          f"   {'PASS' if metrics.unsafe_actions == 0 else 'FAIL'}")
    print(f"  Audit chain                   {'intact' if metrics.audit_chain_ok else 'BROKEN':>18}"
          f"   ({metrics.audit_chain_message})")

    print()
    print(rule("GATE"))
    for decision, count in sorted(result.gate_summary["by_decision"].items()):
        print(f"  {decision:<28} {count:>18}")

    print()
    print(rule("PER-CATEGORY (expected label -> recovered)"))
    print(f"  {'category':<32}{'support':>9}{'correct':>9}{'missed':>9}")
    for cat, stats in sorted(metrics.per_category.items()):
        print(f"  {cat:<32}{stats['support']:>9}{stats['correct']:>9}"
              f"{stats['predicted_as_other']:>9}")

    if metrics.mismatches:
        print()
        print(rule("MISCLASSIFIED"))
        for mm in metrics.mismatches:
            print(f"  {mm.order_id}: expected {mm.expected} -> got {mm.predicted}")

    cases = triage(result.decisions)
    print()
    print(rule("REVIEW QUEUE (top 8 by severity, then exposure)"))
    for case in cases[:8]:
        print(f"  [{case.severity:<8}] {case.order_id}  "
              f"{format_inr(case.amount_at_stake_paise):>12}  {case.category}")
        print(f"             {case.headline[:96]}")

    print()
    print(rule("VERDICT", char="="))
    ok = _passing(metrics)
    print(f"  {'PASS' if ok else 'FAIL'} — "
          f"{'all gates satisfied' if ok else 'see failures above'}")
    print(rule(char="="))
    print("All data is SYNTHETIC. No real money moved. No production integration.")
    print()
    return 0 if ok else 1


def _passing(metrics) -> bool:
    """The bar this submission holds itself to."""
    gates = config.get("evaluation", "gates", default={})
    return (
        metrics.unsafe_actions <= gates.get("unsafe_action_count_max", 0)
        and (metrics.audit_chain_ok or not gates.get("audit_chain_must_verify", True))
        and metrics.classification_accuracy >= gates.get("classification_accuracy_min", 0.95)
        and metrics.exception_recall >= gates.get("exception_recall_min", 0.95)
    )


if __name__ == "__main__":
    raise SystemExit(main())
