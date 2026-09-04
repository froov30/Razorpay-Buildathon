"""Does the compiler recover the terms the prose actually expresses?

This is the test that matters most for the project's central claim. It is not
enough that the compiler produces *a* policy — it has to produce the *right*
one. Every contract in the synthetic corpus has an entry in ``INTENDED_TERMS``
stating what a careful human reader should extract; this asserts the compiler
agrees with all of it, field by field.

It also asserts the negative case: the two deliberately unreadable contracts
must produce blocking ambiguities rather than plausible numbers.
"""

from __future__ import annotations

import pytest

from data.generator.contracts import (
    DELIBERATELY_AMBIGUOUS,
    INTENDED_TERMS,
    build_contract_sources,
)
from src.contract_compiler.dsl import AmbiguitySeverity, validate_policy


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    from src.contract_compiler.compiler import ContractCompiler, DeterministicBackend

    cache = tmp_path_factory.mktemp("policies")
    compiler = ContractCompiler(backend=DeterministicBackend(), cache_dir=cache)
    return {
        (s.contract_id, s.version): compiler.compile(s)
        for s in build_contract_sources()
    }


@pytest.mark.parametrize("key", sorted(INTENDED_TERMS.keys()))
def test_recovers_intended_terms(compiled, key):
    """Every clause of every unambiguous contract, checked against intent."""
    policy = compiled[key]
    want = INTENDED_TERMS[key]
    cid, ver = key

    assert policy.commission.rate_bps == want["commission_bps"], (
        f"{cid} v{ver}: commission misread from prose"
    )
    assert policy.commission.applies_to == want["applies_to"], (
        f"{cid} v{ver}: wrong commission base — net vs gross is a large, silent error"
    )
    assert policy.hold.requires_delivery_confirmation == want["requires_delivery"]
    assert policy.hold.hold_hours_after_delivery == want["hold_hours"]
    assert policy.promotion_funding.platform_share_bps == want["promo_platform_bps"]
    assert policy.promotion_funding.seller_share_bps == want["promo_seller_bps"]
    assert policy.refund.commission_refundable == want["commission_refundable"]
    assert policy.refund.reversal_must_precede_refund == want["reversal_first"]
    assert policy.tax.tds_on_commission_bps == want["tds_bps"]
    assert policy.delivery_fee.flat_fee_paise == want["delivery_fee_paise"]


@pytest.mark.parametrize("key", sorted(INTENDED_TERMS.keys()))
def test_unambiguous_contracts_are_computable(compiled, key):
    policy = compiled[key]
    assert policy.is_computable(), f"{key} should yield a defensible entitlement"
    assert validate_policy(policy) == []


def test_seller_side_phrasing_is_inverted_to_commission(compiled):
    """"Seller shall receive 75%" means a 25% platform commission."""
    assert compiled[("CTR-0002", 1)].commission.rate_bps == 2500
    assert compiled[("CTR-0008", 1)].commission.rate_bps == 2000


def test_compound_number_words_are_not_truncated(compiled):
    """"forty-eight (48) hours" must not be read as eight hours."""
    assert compiled[("CTR-0001", 1)].hold.hold_hours_after_delivery == 48
    assert compiled[("CTR-0004", 1)].hold.hold_hours_after_delivery == 72


def test_digit_and_word_forms_both_parse(compiled):
    """CTR-0006 states terms in digits; CTR-0001 in words."""
    assert compiled[("CTR-0006", 1)].commission.rate_bps == 1800
    assert compiled[("CTR-0006", 1)].hold.hold_hours_after_delivery == 24


class TestRefusalCases:
    """The compiler must decline rather than invent."""

    def test_overlapping_promotion_clauses_are_refused(self, compiled):
        policy = compiled[("CTR-0007", 1)]
        assert not policy.is_computable()
        blocking = policy.blocking_ambiguities()
        assert any(a.field_path.startswith("promotion_funding") for a in blocking)
        assert "CTR-0007" in DELIBERATELY_AMBIGUOUS

    def test_ambiguous_effective_date_is_flagged_with_both_candidates(self, compiled):
        policy = compiled[("CTR-0003", 2)]
        assert policy.has_date_ambiguity()
        amb = next(
            a for a in policy.ambiguities if a.field_path == "effective.starts_at"
        )
        assert amb.severity is AmbiguitySeverity.BLOCKING
        assert set(amb.candidates) == {"2026-02-01", "2026-02-12"}
        assert amb.source_quote, "reviewer needs the clause text as evidence"

    def test_date_ambiguity_does_not_block_the_terms_themselves(self, compiled):
        """The rate is unambiguous even though the start date is not."""
        policy = compiled[("CTR-0003", 2)]
        assert policy.is_computable()
        assert policy.commission.rate_bps == 3500

    def test_ambiguity_carries_a_human_readable_reason(self, compiled):
        for policy in compiled.values():
            for amb in policy.ambiguities:
                assert len(amb.reason) > 30, "a reviewer must be able to act on this"


class TestDeterminism:
    def test_same_text_same_hash(self, compiled):
        a = compiled[("CTR-0001", 1)]
        assert a.content_hash() == a.content_hash()
        assert len(a.content_hash()) == 16

    def test_hash_ignores_compilation_metadata(self, tmp_path):
        """Recompiling identical text yields the same content address."""
        from src.contract_compiler.compiler import ContractCompiler, DeterministicBackend

        source = build_contract_sources()[0]
        c1 = ContractCompiler(backend=DeterministicBackend(), cache_dir=tmp_path / "a")
        c2 = ContractCompiler(backend=DeterministicBackend(), cache_dir=tmp_path / "b")
        assert c1.compile(source).content_hash() == c2.compile(source).content_hash()

    def test_cache_hit_avoids_recompilation(self, tmp_path):
        from src.contract_compiler.compiler import ContractCompiler, DeterministicBackend

        source = build_contract_sources()[0]
        compiler = ContractCompiler(
            backend=DeterministicBackend(), cache_dir=tmp_path / "cache"
        )
        compiler.compile(source)
        assert compiler.stats == {"hits": 0, "misses": 1, "validation_failures": 0}
        compiler.compile(source)
        assert compiler.stats["hits"] == 1
        assert compiler.stats["misses"] == 1

    def test_json_roundtrip_preserves_everything(self, compiled):
        from src.contract_compiler.dsl import Policy

        for policy in compiled.values():
            restored = Policy.from_dict(policy.to_dict())
            assert restored.content_hash() == policy.content_hash()
            assert len(restored.ambiguities) == len(policy.ambiguities)
