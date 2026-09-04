"""Which contract version governed this order?

Everything downstream depends on answering this correctly, and the honest answer
is sometimes "more than one could, and the documents don't say which".

The resolver's contract with the rest of the system
---------------------------------------------------
It returns a :class:`Resolution` that is either:

* **resolved** — exactly one version governs under every defensible reading, or
* **conflicted** — two or more versions could govern, with the competing
  readings attached as evidence.

It never breaks a tie by picking the newest version, the highest version number,
or the one that happens to sort first. Those are all *plausible* heuristics and
each of them silently produces a wrong payout when it guesses wrong. A tie here
means a human decides.

This is the mechanism behind the required "what broke" narrative: an amendment
dropping commission 70% -> 65%, communicated as effective from the start of the
billing month but executed mid-month, leaves a boundary window in which orders
have two defensible commission rates. See ``docs/CHANGELOG.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import product

from src.contract_compiler.dsl import Ambiguity, Policy


@dataclass(frozen=True, slots=True)
class VersionCandidate:
    """One version that could govern an order, under one reading of its dates."""

    policy: Policy
    reading: str
    """Which interpretation of the effective date put this version in play."""

    starts_at: datetime | None
    ends_at: datetime | None


@dataclass
class Resolution:
    """Outcome of resolving an order timestamp against a contract's versions."""

    contract_id: str
    order_at: datetime
    policy: Policy | None
    candidates: list[VersionCandidate] = field(default_factory=list)
    conflict_reason: str = ""
    ambiguities: list[Ambiguity] = field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        return self.policy is not None and not self.conflict_reason

    @property
    def is_conflicted(self) -> bool:
        return bool(self.conflict_reason)

    def candidate_summary(self) -> str:
        parts = []
        for c in self.candidates:
            rate = c.policy.commission.rate_bps
            rate_txt = "unknown rate" if rate is None else f"{rate / 100:.2f}% commission"
            parts.append(f"v{c.policy.version} ({rate_txt}, {c.reading})")
        return "; ".join(parts)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _readings(policy: Policy) -> list[tuple[str, datetime | None]]:
    """Every defensible start date for a version.

    An unambiguous version has one reading. An ambiguous one has as many as the
    compiler recorded candidates for — this is precisely where a single version
    can put itself in play across a window it may not actually cover.
    """
    eff = policy.effective
    if not eff.ambiguous:
        return [("stated effective date", _as_utc(eff.starts_at))]

    readings: list[tuple[str, datetime | None]] = []
    for amb in policy.ambiguities:
        if amb.field_path != "effective.starts_at":
            continue
        for cand in amb.candidates:
            try:
                parsed = datetime.fromisoformat(cand).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            readings.append((f"reading: effective {cand}", parsed))
    if not readings:
        readings.append(("stated effective date", _as_utc(eff.starts_at)))
    return readings


def resolve(
    policies: list[Policy], order_at: datetime, *, contract_id: str | None = None
) -> Resolution:
    """Resolve which policy version governs an order placed at ``order_at``.

    Supersession, not overlap
    -------------------------
    Contract versions are not independent date ranges — a later version
    supersedes its predecessor. An amendment rarely states an end date for the
    version it replaces; that end date is *implied* by its own start. So the
    governing version under any given reading is simply the highest-numbered
    version whose start date has arrived.

    That framing is what makes the conflict window finite. Each defensible
    reading of an ambiguous effective date is evaluated as a complete world:
    resolve the whole version stack under that reading and see which version
    wins. If every reading elects the same version, the ambiguity is immaterial
    to *this* order and it resolves cleanly. Only when readings elect different
    versions is the order genuinely conflicted.

    For the planted CTR-0003 case that yields exactly the right behaviour: an
    order on 5 Feb is v1 under one reading and v2 under the other (conflict),
    while an order on 20 Feb is v2 under both (resolves), even though the
    underlying document is just as ambiguous in both cases.
    """
    order_at = _as_utc(order_at)  # type: ignore[assignment]
    cid = contract_id or (policies[0].contract_id if policies else "UNKNOWN")

    if not policies:
        return Resolution(
            contract_id=cid,
            order_at=order_at,
            policy=None,
            conflict_reason="No contract version exists for this seller.",
        )

    ordered = sorted(policies, key=lambda p: p.version)

    # Enumerate every defensible reading of every version's start date, then
    # evaluate each complete combination as its own world.
    per_version_readings = [_readings(p) for p in ordered]
    candidates: list[VersionCandidate] = []
    elected: list[tuple[str, Policy]] = []

    for combo in product(*per_version_readings):
        label_parts: list[str] = []
        winner: Policy | None = None
        winner_start: datetime | None = None
        for policy, (reading, starts) in zip(ordered, combo):
            ends = _as_utc(policy.effective.ends_at)
            started = starts is None or order_at >= starts
            not_ended = ends is None or order_at < ends
            if started and not_ended:
                # Later versions supersede earlier ones.
                winner, winner_start = policy, starts
            if policy.effective.ambiguous:
                label_parts.append(f"v{policy.version} {reading}")
        if winner is None:
            continue
        label = "; ".join(label_parts) or "stated effective dates"
        elected.append((label, winner))
        if not any(
            c.policy.version == winner.version and c.reading == label
            for c in candidates
        ):
            candidates.append(
                VersionCandidate(
                    policy=winner,
                    reading=label,
                    starts_at=winner_start,
                    ends_at=_as_utc(winner.effective.ends_at),
                )
            )

    distinct_versions = {p.version for _, p in elected}

    if not elected:
        return Resolution(
            contract_id=cid,
            order_at=order_at,
            policy=None,
            conflict_reason=(
                "No contract version's effective period covers this order date."
            ),
        )

    if len(distinct_versions) > 1:
        # Two versions in play. Do not pick — this is the honest failure.
        rates = {
            c.policy.commission.rate_bps
            for c in candidates
            if c.policy.commission.rate_bps is not None
        }
        detail = (
            f" The competing readings imply different commission rates "
            f"({', '.join(f'{r/100:.2f}%' for r in sorted(rates))}), so the "
            f"entitlement cannot be computed without a human decision."
            if len(rates) > 1
            else ""
        )
        return Resolution(
            contract_id=cid,
            order_at=order_at,
            policy=None,
            candidates=candidates,
            conflict_reason=(
                f"Order dated {order_at.date().isoformat()} falls in a window where "
                f"contract versions {sorted(distinct_versions)} are both defensibly "
                f"in force.{detail}"
            ),
            ambiguities=[
                a
                for p in ordered
                for a in p.ambiguities
                if a.field_path == "effective.starts_at"
            ],
        )

    winner = elected[0][1]

    # A single version can still be uncomputable — e.g. its commission clause
    # itself was ambiguous. Surface that rather than settling against a null.
    if not winner.is_computable():
        return Resolution(
            contract_id=cid,
            order_at=order_at,
            policy=winner,
            candidates=candidates,
            conflict_reason=(
                f"Contract version {winner.version} governs, but its terms could not "
                f"be read unambiguously: "
                + "; ".join(a.reason for a in winner.blocking_ambiguities())
            ),
            ambiguities=winner.blocking_ambiguities(),
        )

    return Resolution(
        contract_id=cid, order_at=order_at, policy=winner, candidates=candidates
    )


def group_by_contract(policies: list[Policy]) -> dict[str, list[Policy]]:
    grouped: dict[str, list[Policy]] = {}
    for p in policies:
        grouped.setdefault(p.contract_id, []).append(p)
    for versions in grouped.values():
        versions.sort(key=lambda p: p.version)
    return grouped


def group_by_seller(policies: list[Policy]) -> dict[str, list[Policy]]:
    grouped: dict[str, list[Policy]] = {}
    for p in policies:
        grouped.setdefault(p.seller_id, []).append(p)
    for versions in grouped.values():
        versions.sort(key=lambda p: p.version)
    return grouped
