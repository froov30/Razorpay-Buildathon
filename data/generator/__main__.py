"""CLI: regenerate the synthetic dataset.

    python -m data.generator            # write to data/synthetic/
    python -m data.generator --out tmp/ # write elsewhere
"""

from __future__ import annotations

import argparse
from pathlib import Path

from data.generator.ledger import DEFAULT_OUTPUT_DIR, SEED, generate, write_dataset
from src.common.console import rule, setup_console


def main() -> int:
    setup_console()
    parser = argparse.ArgumentParser(description="Generate EntitleGraph synthetic data")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    ds = generate(seed=args.seed)
    out = write_dataset(ds, args.out)

    print(rule("SYNTHETIC DATA GENERATED"))
    print(f"  output dir      : {out}")
    print(f"  seed            : {args.seed}")
    print(f"  contracts       : {len(ds.contracts)}")
    print(f"  orders          : {len(ds.orders)}")
    print(f"  payments        : {len(ds.payments)}")
    print(f"  deliveries      : {len(ds.deliveries)}")
    print(f"  promotions      : {len(ds.promotions)}")
    print(f"  transfers       : {len(ds.transfers)}")
    print(f"  refunds         : {len(ds.refunds)}")
    print(f"  reversals       : {len(ds.reversals)}")
    print(f"  TOTAL RECORDS   : {ds.record_count()}")

    labelled = {k: v for k, v in ds.ground_truth.items() if v != "none"}
    print(f"\n  orders tagged as exceptions: {len(labelled)} / {len(ds.orders)}")
    by_cat: dict[str, int] = {}
    for cat in labelled.values():
        by_cat[cat] = by_cat.get(cat, 0) + 1
    for cat, n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        print(f"    {cat:<32} {n}")
    print(rule())
    print("All records are SYNTHETIC. No real merchant data is present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
