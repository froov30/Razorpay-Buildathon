"""Load configuration from ``configs/``.

The rule this enforces
----------------------
**``src/`` is never edited to run a different experiment.** Thresholds, model
ids, dataset paths and pass/fail gates live in YAML; code reads them.

That rule was learned the hard way on this project. Swapping contract-compiler
models during development meant editing ``src/contract_compiler/compiler.py``
four separate times — once per model — and each edit was a commit that mixed a
configuration choice with source history. The values were also scattered: a
rounding quantum in the matcher, a rate limit in the eval runner, a seed in the
generator. Nobody reading the repo could see what the system's parameters
actually were without grepping for constants.

Deliberately not Hydra
----------------------
The obvious reference implementation for this pattern is Hydra, and it is
overkill here. Hydra brings composition, CLI overrides and an output-directory
convention; this project needs three flat YAML files read once. Plain
``yaml.safe_load`` with a small accessor keeps the benefit — configuration
separated from code — without adding a dependency that would also have to be
explained, pinned and justified.

Defaults are embedded so the system still runs if ``configs/`` is missing or a
key is absent. A config file that can silently disable a safety gate by being
deleted would be worse than no config file at all.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs"

# Fallbacks, used when configs/ is absent or a key is missing. These mirror the
# committed YAML; the YAML is authoritative when present.
_DEFAULTS: dict[str, dict[str, Any]] = {
    "engine": {
        "matcher": {"rate_quantum_bps": 25, "rate_tolerance_bps": 1},
        "settlement": {
            "rounding": "half_up",
            "prorate_delivery_fee_on_refund": False,
        },
    },
    "models": {
        "default_backend": "deterministic",
        "backends": {
            "deterministic": {"kind": "deterministic"},
            "claude": {"kind": "anthropic", "model": "claude-sonnet-5",
                       "max_tokens": 2048, "api_key_env": "ANTHROPIC_API_KEY"},
            "gemini": {"kind": "google", "model": "gemini-3.5-flash",
                       "max_output_tokens": 16384, "thinking_budget": 0,
                       "api_key_env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
                       "min_request_interval_s": 13.0},
            "nim": {"kind": "openai_compatible", "model": "moonshotai/kimi-k3",
                    "base_url": "https://integrate.api.nvidia.com/v1",
                    "max_tokens": 4096,
                    "api_key_env": ["NVIDIA_NIM_API_KEY", "NVIDIA_API_KEY"],
                    "min_request_interval_s": 0.0},
        },
        "auto_select_order": ["claude", "gemini", "nim", "deterministic"],
    },
    "evaluation": {
        "dataset": {"seed": 20260904, "batch_as_of": "2026-03-15T00:00:00+00:00",
                    "output_dir": "data/synthetic"},
        "paths": {
            "ledger_dir": "data/synthetic",
            "ground_truth": "data/synthetic/ground_truth.json",
            "deterministic_cache": "data/synthetic/compiled_policies",
            "llm_cache_root": "data/synthetic/compiled_policies_llm",
            "audit_db": "data/synthetic/entitlegraph.db",
        },
        "gates": {
            "unsafe_action_count_max": 0,
            "audit_chain_must_verify": True,
            "classification_accuracy_min": 0.95,
            "exception_recall_min": 0.95,
        },
        "llm_fidelity_gates": {
            "field_accuracy_min": 0.85,
            "refusal_accuracy_min": 0.90,
        },
    },
}


@functools.lru_cache(maxsize=None)
def load(name: str) -> dict[str, Any]:
    """Load ``configs/<name>.yaml``, falling back to embedded defaults.

    Cached: config is read once per process. Call :func:`reload` in a test that
    needs to change it.
    """
    path = CONFIG_DIR / f"{name}.yaml"
    defaults = _DEFAULTS.get(name, {})
    if not path.exists():
        return defaults
    try:
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        # A malformed config must not take the system down, and must not
        # silently apply half a file either. Fall back wholesale.
        return defaults
    return _merge(defaults, loaded)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge override onto base so a partial config file still works."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def get(name: str, *path: str, default: Any = None) -> Any:
    """Read a dotted path out of a config file.

    >>> get("engine", "matcher", "rate_quantum_bps")
    25
    """
    node: Any = load(name)
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def reload() -> None:
    """Drop the cache. For tests that write a config and expect it read."""
    load.cache_clear()
