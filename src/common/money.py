"""Integer-paise money arithmetic.

Every monetary value in EntitleGraph is an ``int`` number of paise. Floats are
never used for money anywhere in this codebase, and :func:`guard_no_float` is
called at module boundaries to enforce that at runtime.

Why this matters for an entitlement engine specifically
-------------------------------------------------------
The system's core claim is "the money that moved matches what the contract
promised". That claim is only as good as the arithmetic behind it. A float
rounding drift of a single paise across 40 orders would produce phantom
variances that look exactly like real entitlement breaches — the engine would
report exceptions that are artifacts of its own arithmetic. So:

* All amounts are integer paise (``₹1.00`` -> ``100``).
* Percentages are basis points (``bps``), integers. 70% -> ``7000`` bps.
* Rounding is **half-up**, applied once, at the point a rate is applied.
* Proportional splits use the **largest-remainder method** so the parts always
  sum back to exactly the whole — a split can never create or destroy a paise.

Rounding convention
-------------------
Half-up (``0.5`` rounds away from zero) is used rather than banker's rounding
because it matches the convention stated in the synthetic merchant agreements
this project compiles, and because it is what Indian marketplace settlement
statements conventionally use. The important property for correctness here is
not *which* convention, but that exactly one convention is applied consistently
on both sides of every comparison.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Sequence

__all__ = [
    "BPS_DENOMINATOR",
    "PAISE_PER_RUPEE",
    "apply_bps",
    "format_inr",
    "guard_no_float",
    "paise_to_rupees_str",
    "rupees_to_paise",
    "split_proportional",
]

PAISE_PER_RUPEE = 100
BPS_DENOMINATOR = 10_000

# Half of the bps denominator, pre-computed for the half-up integer rounding
# trick used in `apply_bps`.
_BPS_HALF = BPS_DENOMINATOR // 2


class MoneyError(ValueError):
    """Raised when a monetary operation would be unsafe or ill-defined."""


def guard_no_float(value: object, *, field: str = "amount") -> int:
    """Return ``value`` as an int, refusing floats.

    This is deliberately strict. A float reaching the settlement engine is a
    correctness bug, not something to coerce silently — coercing would hide the
    exact class of drift this module exists to prevent.

    >>> guard_no_float(100)
    100
    >>> guard_no_float(1.5)                      # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
        ...
    MoneyError: amount must be integer paise, got float 1.5
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(
            f"{field} must be integer paise, got {type(value).__name__} {value!r}"
        )
    return value


def rupees_to_paise(value: str | int | Decimal) -> int:
    """Parse a rupee amount into integer paise.

    Accepts ``"1234.56"``, ``"1,234.56"``, ``1234``, or ``Decimal("1234.56")``.
    Rejects anything with sub-paise precision rather than silently truncating,
    because a contract that specifies sub-paise terms is a contract this engine
    cannot faithfully evaluate — better to fail loudly at ingestion.

    >>> rupees_to_paise("1,234.56")
    123456
    >>> rupees_to_paise(1234)
    123400
    """
    if isinstance(value, float):
        raise MoneyError("refusing to parse float rupee amount; pass str or Decimal")
    if isinstance(value, int):
        return value * PAISE_PER_RUPEE
    try:
        dec = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise MoneyError(f"could not parse rupee amount {value!r}") from exc

    paise = dec * PAISE_PER_RUPEE
    if paise != paise.to_integral_value():
        raise MoneyError(
            f"rupee amount {value!r} has sub-paise precision; refusing to truncate"
        )
    return int(paise)


def paise_to_rupees_str(paise: int) -> str:
    """Render paise as a plain decimal rupee string, no symbol or separators.

    >>> paise_to_rupees_str(123456)
    '1234.56'
    """
    guard_no_float(paise, field="paise")
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), PAISE_PER_RUPEE)
    return f"{sign}{whole}.{frac:02d}"


def format_inr(paise: int, *, symbol: bool = True) -> str:
    """Render paise in Indian digit grouping (lakh/crore), for UI and reports.

    >>> format_inr(12345678)
    '₹1,23,456.78'
    >>> format_inr(-50000, symbol=False)
    '-500.00'
    """
    guard_no_float(paise, field="paise")
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), PAISE_PER_RUPEE)

    digits = str(whole)
    if len(digits) <= 3:
        grouped = digits
    else:
        # Indian grouping: last three digits, then pairs.
        head, tail = digits[:-3], digits[-3:]
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        if head:
            pairs.insert(0, head)
        grouped = ",".join(pairs + [tail])

    prefix = "₹" if symbol else ""
    return f"{sign}{prefix}{grouped}.{frac:02d}"


def apply_bps(amount_paise: int, bps: int) -> int:
    """Apply a basis-point rate to an amount, rounding half-up to whole paise.

    Pure integer arithmetic — no Decimal, no float, no drift.

    >>> apply_bps(100_00, 3000)      # 30% of ₹100.00
    3000
    >>> apply_bps(333, 3333)         # rounds half-up, not toward zero
    111
    >>> apply_bps(-100_00, 3000)     # symmetric for negatives (reversals)
    -3000
    """
    guard_no_float(amount_paise, field="amount_paise")
    guard_no_float(bps, field="bps")
    if bps < 0:
        raise MoneyError(f"negative bps {bps} is not a meaningful rate")
    if amount_paise < 0:
        return -apply_bps(-amount_paise, bps)
    return (amount_paise * bps + _BPS_HALF) // BPS_DENOMINATOR


def split_proportional(amount_paise: int, weights: Sequence[int]) -> list[int]:
    """Split an amount across weights so the parts sum to exactly the whole.

    Uses the largest-remainder method. This guarantees
    ``sum(split_proportional(x, w)) == x`` for every input, which is what makes
    a three-way marketplace split (seller / delivery partner / platform)
    reconcile to the paise instead of leaving an orphan rounding residue that
    the matcher would later flag as a phantom entitlement variance.

    >>> split_proportional(100, [1, 1, 1])
    [34, 33, 33]
    >>> sum(split_proportional(9999, [7, 2, 1]))
    9999
    """
    guard_no_float(amount_paise, field="amount_paise")
    for i, w in enumerate(weights):
        guard_no_float(w, field=f"weights[{i}]")
        if w < 0:
            raise MoneyError(f"negative weight {w} at index {i}")

    total_weight = sum(weights)
    if total_weight <= 0:
        raise MoneyError("split weights must sum to a positive value")

    if amount_paise < 0:
        return [-part for part in split_proportional(-amount_paise, weights)]

    raw = [amount_paise * w for w in weights]
    parts = [r // total_weight for r in raw]
    remainder = amount_paise - sum(parts)

    # Hand out the leftover paise to the largest fractional remainders first.
    # Ties break on index so the result is deterministic across runs — a
    # non-deterministic split would make the eval metrics irreproducible.
    order = sorted(
        range(len(weights)),
        key=lambda i: (-(raw[i] % total_weight), i),
    )
    for i in range(remainder):
        parts[order[i]] += 1
    return parts


def total(amounts: Iterable[int]) -> int:
    """Sum integer-paise amounts, refusing floats mid-stream."""
    running = 0
    for i, amount in enumerate(amounts):
        running += guard_no_float(amount, field=f"amounts[{i}]")
    return running
