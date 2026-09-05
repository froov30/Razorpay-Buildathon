"""Configuration loading: configs/ is authoritative, src/ never hardcodes.

The property under test is the one the configs/ vs src/ split exists to give:
changing a threshold or a model id must not require editing Python. If these
tests pass but a constant is still hardcoded somewhere, the split is decorative.
"""

from __future__ import annotations

import pytest

from src.common import config


@pytest.fixture(autouse=True)
def _clear_cache():
    config.reload()
    yield
    config.reload()


class TestLoading:
    def test_reads_committed_yaml(self):
        assert config.get("engine", "matcher", "rate_quantum_bps") == 25
        assert config.get("evaluation", "dataset", "seed") == 20260904

    def test_missing_key_returns_default_not_none(self):
        assert config.get("engine", "nope", "missing", default="fallback") == "fallback"

    def test_missing_file_falls_back_to_embedded_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "absent")
        config.reload()
        # Safety gates must survive a missing config file. A deleted YAML that
        # silently disabled the unsafe-action gate would be worse than no file.
        assert config.get("evaluation", "gates", "unsafe_action_count_max") == 0
        assert config.get("engine", "matcher", "rate_quantum_bps") == 25

    def test_malformed_file_falls_back_wholesale(self, monkeypatch, tmp_path):
        (tmp_path / "engine.yaml").write_text("{{{ not yaml", encoding="utf-8")
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
        config.reload()
        # Not half a file: a partially-applied malformed config is worse than
        # none, because the half that applied is invisible.
        assert config.get("engine", "matcher", "rate_quantum_bps") == 25

    def test_partial_file_merges_over_defaults(self, monkeypatch, tmp_path):
        (tmp_path / "engine.yaml").write_text(
            "matcher:\n  rate_quantum_bps: 50\n", encoding="utf-8"
        )
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
        config.reload()
        assert config.get("engine", "matcher", "rate_quantum_bps") == 50
        # untouched key still present from defaults
        assert config.get("engine", "matcher", "rate_tolerance_bps") == 1


class TestConfigIsAuthoritative:
    """The split is only real if code actually reads it."""

    def test_matcher_threshold_comes_from_config(self):
        from src.settlement_engine import matcher

        assert matcher._RATE_QUANTUM_BPS == config.get(
            "engine", "matcher", "rate_quantum_bps"
        )

    def test_generator_seed_comes_from_config(self):
        from data.generator import ledger

        assert ledger.SEED == config.get("evaluation", "dataset", "seed")

    def test_every_llm_backend_model_is_declared_in_config(self):
        backends = config.get("models", "backends", default={})
        for name in ("claude", "gemini", "nim"):
            assert name in backends, f"{name} missing from configs/models.yaml"
            assert backends[name].get("model"), f"{name} has no pinned model id"

    def test_model_ids_are_pinned_not_aliased(self):
        """An alias silently changes which model produced a cached policy."""
        backends = config.get("models", "backends", default={})
        for name, spec in backends.items():
            model = str(spec.get("model", ""))
            assert "latest" not in model, (
                f"{name} uses alias {model!r}; pin an exact version so the "
                f"compile cache stays reproducible"
            )

    def test_eval_gates_are_declared(self):
        gates = config.get("evaluation", "gates", default={})
        for key in (
            "unsafe_action_count_max",
            "audit_chain_must_verify",
            "classification_accuracy_min",
            "exception_recall_min",
        ):
            assert key in gates, f"{key} missing from configs/evaluation.yaml"
