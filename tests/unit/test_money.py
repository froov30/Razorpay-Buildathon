"""Money arithmetic — the foundation every other number rests on."""

from __future__ import annotations

import pytest

from src.common.money import (
    MoneyError,
    apply_bps,
    format_inr,
    guard_no_float,
    paise_to_rupees_str,
    rupees_to_paise,
    split_proportional,
    total,
)


class TestFloatRejection:
    def test_guard_rejects_float(self):
        with pytest.raises(MoneyError, match="integer paise"):
            guard_no_float(1.5)

    def test_guard_rejects_bool(self):
        # bool is an int subclass; silently treating True as 1 paise would be a
        # genuinely nasty bug to track down.
        with pytest.raises(MoneyError):
            guard_no_float(True)

    def test_apply_bps_rejects_float_amount(self):
        with pytest.raises(MoneyError):
            apply_bps(100.0, 3000)  # type: ignore[arg-type]

    def test_rupees_to_paise_rejects_float(self):
        with pytest.raises(MoneyError, match="refusing to parse float"):
            rupees_to_paise(12.34)  # type: ignore[arg-type]


class TestParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [("1234.56", 123456), ("1,234.56", 123456), ("0.01", 1), ("100", 10000),
         (1234, 123400), ("-45.50", -4550)],
    )
    def test_parses(self, raw, expected):
        assert rupees_to_paise(raw) == expected

    def test_rejects_sub_paise(self):
        with pytest.raises(MoneyError, match="sub-paise"):
            rupees_to_paise("10.001")

    def test_roundtrip(self):
        assert paise_to_rupees_str(rupees_to_paise("9876.54")) == "9876.54"


class TestFormatting:
    @pytest.mark.parametrize(
        "paise,expected",
        [
            (12345678, "₹1,23,456.78"),   # lakh grouping
            (100000000, "₹10,00,000.00"),  # ten lakh
            (99999, "₹999.99"),
            (0, "₹0.00"),
            (-4550, "-₹45.50"),
        ],
    )
    def test_indian_grouping(self, paise, expected):
        assert format_inr(paise) == expected


class TestApplyBps:
    def test_simple_percentage(self):
        assert apply_bps(100_00, 3000) == 3000  # 30% of ₹100

    def test_rounds_half_up_not_toward_zero(self):
        # 333 * 3333 / 10000 = 110.98... -> 111
        assert apply_bps(333, 3333) == 111

    def test_exact_half_rounds_up(self):
        # 5 * 5000 / 10000 = 2.5 -> 3
        assert apply_bps(5, 5000) == 3

    def test_symmetric_for_negatives(self):
        assert apply_bps(-100_00, 3000) == -3000
        assert apply_bps(-5, 5000) == -3

    def test_full_and_zero_rates(self):
        assert apply_bps(12345, 10_000) == 12345
        assert apply_bps(12345, 0) == 0

    def test_rejects_negative_rate(self):
        with pytest.raises(MoneyError, match="negative bps"):
            apply_bps(100, -1)


class TestSplitProportional:
    def test_sums_exactly(self):
        assert split_proportional(100, [1, 1, 1]) == [34, 33, 33]

    @pytest.mark.parametrize(
        "amount,weights",
        [(9999, [7, 2, 1]), (1, [1, 1, 1]), (100_00, [3333, 3333, 3334]),
         (7, [1, 1, 1, 1, 1, 1, 1]), (12345, [50, 50])],
    )
    def test_never_creates_or_destroys_paise(self, amount, weights):
        parts = split_proportional(amount, weights)
        assert sum(parts) == amount

    def test_deterministic_tie_breaking(self):
        # Irreproducible splits would make the eval metrics irreproducible.
        first = split_proportional(100, [1, 1, 1])
        for _ in range(50):
            assert split_proportional(100, [1, 1, 1]) == first

    def test_negative_amount_mirrors(self):
        assert split_proportional(-100, [1, 1, 1]) == [-34, -33, -33]

    def test_rejects_zero_weights(self):
        with pytest.raises(MoneyError, match="positive"):
            split_proportional(100, [0, 0])

    def test_rejects_negative_weight(self):
        with pytest.raises(MoneyError, match="negative weight"):
            split_proportional(100, [1, -1])


def test_total_refuses_float_midstream():
    with pytest.raises(MoneyError):
        total([100, 200, 3.5])  # type: ignore[list-item]
