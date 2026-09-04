"""The determinism claim, asserted rather than promised.

BUILD_PROMPT.md §5.2 states that evaluation is reproducible on judging day with
no API key and no network. That is only true if:

* the compile cache is consulted before any model call, and
* the scored pipeline reads solely from cached artifacts, and
* repeated runs produce byte-identical decisions.

Each of those is tested here. If this file fails, the metrics reported in the
README are not trustworthy and the claim must be withdrawn.
"""

from __future__ import annotations

import json

import pytest

from src.common.types import RazorpayMode
from src.contract_compiler.compiler import ContractCompiler, DeterministicBackend
from src.pipeline import load_dataset, run
from src.razorpay_client.client import RazorpayRouteClient
from tests.eval.metrics import evaluate, load_ground_truth


def _run(tmp_path, cache_dir=None):
    return run(
        dataset=load_dataset(),
        db_path=tmp_path / "audit.db",
        compiler=ContractCompiler(
            backend=DeterministicBackend(),
            cache_dir=cache_dir or (tmp_path / "policies"),
        ),
        client=RazorpayRouteClient(key_id="", key_secret="", mode=RazorpayMode.MOCK),
    )


def test_two_runs_produce_identical_decisions(tmp_path):
    a = _run(tmp_path / "a")
    b = _run(tmp_path / "b")

    rows_a = [o.decision.to_row() for o in a.outcomes]
    rows_b = [o.decision.to_row() for o in b.outcomes]
    assert rows_a == rows_b


def test_metrics_are_stable_across_runs(tmp_path):
    gt = load_ground_truth()
    amounts = {o.order_id: o.gross_amount_paise for o in load_dataset().orders}

    first = evaluate(_run(tmp_path / "a"), gt, order_amounts=amounts).to_dict()
    second = evaluate(_run(tmp_path / "b"), gt, order_amounts=amounts).to_dict()

    # Throughput legitimately varies with machine load; everything else must not.
    for key in ("throughput_records_per_s", "throughput_orders_per_s"):
        first.pop(key), second.pop(key)
    assert first == second


def test_runs_with_no_api_key_present(tmp_path, monkeypatch):
    """The judging-day condition: no key, no network, same numbers."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)

    result = _run(tmp_path)
    metrics = evaluate(result, load_ground_truth())

    assert metrics.total_orders == 40
    assert metrics.classification_accuracy == 1.0
    assert metrics.unsafe_actions == 0


def test_cache_hit_never_invokes_a_backend(tmp_path):
    """A populated cache must not call the extractor at all."""

    class ExplodingBackend:
        name = "exploding"

        def extract(self, source):  # pragma: no cover - must never run
            raise AssertionError(
                "backend was invoked despite a warm compile cache — the "
                "determinism guarantee is broken"
            )

    cache = tmp_path / "policies"
    warm = ContractCompiler(backend=DeterministicBackend(), cache_dir=cache)
    dataset = load_dataset()
    for source in dataset.contracts:
        warm.compile(source)

    cold = ContractCompiler(backend=ExplodingBackend(), cache_dir=cache)
    policies = [cold.compile(s) for s in dataset.contracts]

    assert len(policies) == len(dataset.contracts)
    assert cold.stats["misses"] == 0
    assert cold.stats["hits"] == len(dataset.contracts)


def test_cached_policies_are_canonical_json(tmp_path):
    """Byte-identical serialisation is what makes the content hash stable."""
    cache = tmp_path / "policies"
    compiler = ContractCompiler(backend=DeterministicBackend(), cache_dir=cache)
    source = load_dataset().contracts[0]
    policy = compiler.compile(source)

    raw = (cache / compiler.cache_path(source).name).read_text(encoding="utf-8")
    parsed = json.loads(raw)

    assert raw == json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert parsed["contract_id"] == source.contract_id


def test_recompiling_identical_text_yields_the_same_hash(tmp_path):
    compiler = ContractCompiler(
        backend=DeterministicBackend(), cache_dir=tmp_path / "p"
    )
    source = load_dataset().contracts[0]
    before = compiler.compile(source).content_hash()
    after = compiler.compile(source, force=True).content_hash()
    assert before == after
