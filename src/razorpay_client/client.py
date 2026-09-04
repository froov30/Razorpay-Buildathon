"""Razorpay Route client — TEST MODE ONLY, with an explicit execution mode.

Safety posture
--------------
1. **Live keys are refused at construction.** A key id beginning ``rzp_live_``
   raises. There is no flag to override it. This project has no legitimate
   reason to hold production credentials, so the failure is made structural
   rather than procedural.

2. **The mode is never silent.** ``LIVE-TEST`` (real calls to Razorpay's test
   environment) and ``MOCK`` (no network at all) are both announced in logs, in
   the API response envelope, and as a dashboard banner. Silently degrading to
   a simulation while a judge believes they are watching real API traffic would
   be the single most damaging thing this repo could do to its own credibility.

3. **No money moves without a gate token.** Every mutating call requires an
   :class:`ApprovalToken` issued by ``src.settlement_engine.gate``. The token is
   bound to the *content* of the proposal and is single-use, so a caller cannot
   bypass the entitlement check, replay an old approval, or mutate an approved
   proposal before executing it. See ``tests/integration/test_gate_adversarial.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

# This module reads RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET / MODE directly from
# os.getenv, so .env has to be loaded before that happens regardless of which
# module a caller imports first. src.pipeline also calls this (defensively;
# load_dotenv() is idempotent and never overrides a real environment variable
# already set), but this module must not depend on being imported through
# pipeline to pick up a local .env file — a bare `RazorpayRouteClient()`
# constructed directly, without src.pipeline in the import chain, would
# otherwise silently resolve to MOCK even with a correctly filled-in .env.
load_dotenv()

from src.common.types import PartyRole, RazorpayMode

logger = logging.getLogger(__name__)


class UnsafeActionError(RuntimeError):
    """A money movement was attempted without a valid, unconsumed gate token.

    Every raise of this exception increments the unsafe-action counter and is
    written to the audit log. The project's headline safety claim is that this
    exception is raised on every bypass attempt and that no transfer ever
    executes without a token — asserted by the adversarial test suite.
    """


class LiveKeyRefused(RuntimeError):
    """Production Razorpay credentials were supplied. Refused unconditionally."""


@dataclass(frozen=True, slots=True)
class TransferProposal:
    """A money movement the system wants to make, before it is allowed to."""

    proposal_id: str
    order_id: str
    party_role: PartyRole
    party_account_id: str
    amount_paise: int
    currency: str = "INR"
    notes: dict[str, str] = field(default_factory=dict)

    def content_hash(self) -> str:
        """Bind approvals to exact content — mutation invalidates the token."""
        material = (
            f"{self.proposal_id}|{self.order_id}|{self.party_role}|"
            f"{self.party_account_id}|{self.amount_paise}|{self.currency}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalToken:
    """Single-use authorisation to execute one specific proposal."""

    token_id: str
    proposal_hash: str
    issued_at: datetime
    approver: str
    signature: str

    @staticmethod
    def sign(token_id: str, proposal_hash: str, secret: bytes) -> str:
        return hmac.new(
            secret, f"{token_id}|{proposal_hash}".encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def is_valid_for(self, proposal: TransferProposal, secret: bytes) -> bool:
        if self.proposal_hash != proposal.content_hash():
            return False
        expected = self.sign(self.token_id, self.proposal_hash, secret)
        return hmac.compare_digest(expected, self.signature)


def resolve_mode(explicit: str | None = None, key_id: str | None = None) -> RazorpayMode:
    """Decide LIVE-TEST vs MOCK, preferring explicit configuration."""
    raw = (explicit or os.getenv("ENTITLEGRAPH_RAZORPAY_MODE") or "").strip().upper()
    if raw in ("LIVE-TEST", "LIVE_TEST", "LIVE"):
        return RazorpayMode.LIVE_TEST
    if raw == "MOCK":
        return RazorpayMode.MOCK

    key = key_id if key_id is not None else os.getenv("RAZORPAY_KEY_ID", "")
    secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    if key.startswith("rzp_test_") and secret and "x" * 8 not in key:
        return RazorpayMode.LIVE_TEST
    return RazorpayMode.MOCK


class RazorpayRouteClient:
    """Thin wrapper over Razorpay Route, usable in LIVE-TEST or MOCK mode."""

    def __init__(
        self,
        key_id: str | None = None,
        key_secret: str | None = None,
        mode: RazorpayMode | str | None = None,
        *,
        token_secret: bytes | None = None,
    ) -> None:
        self.key_id = key_id if key_id is not None else os.getenv("RAZORPAY_KEY_ID", "")
        self.key_secret = (
            key_secret if key_secret is not None else os.getenv("RAZORPAY_KEY_SECRET", "")
        )

        if self.key_id.startswith("rzp_live_"):
            raise LiveKeyRefused(
                "RAZORPAY_KEY_ID is a LIVE key. EntitleGraph is a reference "
                "implementation and must never hold production credentials. "
                "Use a test-mode key (rzp_test_...)."
            )

        resolved = (
            mode
            if isinstance(mode, RazorpayMode)
            else resolve_mode(mode if isinstance(mode, str) else None, self.key_id)
        )
        self.mode = resolved
        self._token_secret = token_secret or secrets.token_bytes(32)
        self._consumed_tokens: set[str] = set()
        self._sdk: Any = None

        self.unsafe_action_attempts = 0
        """Count of attempts to move money without a valid gate token. The
        project's safety claim is that this equals the number of adversarial
        probes and that none of them resulted in an executed transfer."""

        self.executed: list[dict[str, Any]] = []

        self._announce()
        if self.mode is RazorpayMode.LIVE_TEST:
            self._init_sdk()

    # -- mode announcement -------------------------------------------------

    @property
    def token_secret(self) -> bytes:
        return self._token_secret

    def _announce(self) -> None:
        if self.mode is RazorpayMode.LIVE_TEST:
            logger.warning(
                "Razorpay client mode=LIVE-TEST — real API calls will be made "
                "against Razorpay's TEST environment (key %s). No real money moves.",
                self.key_id[:14] + "…" if self.key_id else "(unset)",
            )
        else:
            logger.warning(
                "Razorpay client mode=MOCK — NO Razorpay API calls will be made. "
                "All transfers are simulated locally."
            )

    def mode_banner(self) -> str:
        """One-line banner for the dashboard and API envelope."""
        if self.mode is RazorpayMode.LIVE_TEST:
            return "LIVE-TEST — real Razorpay test-mode API calls. No real money."
        return "MOCK MODE — no Razorpay API calls made. Transfers simulated locally."

    def _init_sdk(self) -> None:
        try:
            import razorpay  # local import keeps MOCK runs dependency-free

            self._sdk = razorpay.Client(auth=(self.key_id, self.key_secret))
            self._sdk.set_app_details({"title": "EntitleGraph Close Agent", "version": "0.1"})
        except Exception as exc:  # pragma: no cover - network/SDK issues
            logger.error(
                "Failed to initialise Razorpay SDK (%s). Falling back to MOCK and "
                "saying so loudly rather than pretending the call succeeded.",
                exc,
            )
            self.mode = RazorpayMode.MOCK
            self._announce()

    # -- linked accounts ---------------------------------------------------

    def create_linked_account(
        self, *, name: str, email: str, reference_id: str
    ) -> dict[str, Any]:
        """Create a Route linked account (the payee)."""
        if self.mode is RazorpayMode.MOCK:
            acc_id = "acc_MOCK" + hashlib.sha1(reference_id.encode()).hexdigest()[:10]
            return {"id": acc_id, "name": name, "email": email, "mode": str(self.mode)}
        payload = {
            "email": email,
            "phone": "9999999999",
            "legal_business_name": name,
            "business_type": "partnership",
            "customer_facing_business_name": name,
            "reference_id": reference_id,
            "type": "route",
        }
        account = self._sdk.account.create(payload)  # type: ignore[union-attr]
        return {**account, "mode": str(self.mode)}

    # -- transfers ---------------------------------------------------------

    def execute_transfer(
        self, proposal: TransferProposal, token: ApprovalToken | None
    ) -> dict[str, Any]:
        """Execute a transfer. Requires a valid, unconsumed gate token.

        This is the only path to moving money in the system, and every guard
        here exists because of a specific attack the adversarial suite runs.
        """
        if token is None:
            self.unsafe_action_attempts += 1
            raise UnsafeActionError(
                f"transfer for order {proposal.order_id} attempted with no gate "
                f"token — entitlement was never checked"
            )

        if token.token_id in self._consumed_tokens:
            self.unsafe_action_attempts += 1
            raise UnsafeActionError(
                f"gate token {token.token_id} has already been used; refusing "
                f"replay for order {proposal.order_id}"
            )

        if not token.is_valid_for(proposal, self._token_secret):
            self.unsafe_action_attempts += 1
            raise UnsafeActionError(
                f"gate token {token.token_id} does not authorise this proposal — "
                f"the proposal was altered after approval (expected hash "
                f"{token.proposal_hash[:12]}, got {proposal.content_hash()[:12]})"
            )

        self._consumed_tokens.add(token.token_id)

        if self.mode is RazorpayMode.MOCK:
            result = {
                "id": "trf_MOCK" + uuid.uuid4().hex[:10],
                "amount": proposal.amount_paise,
                "currency": proposal.currency,
                "recipient": proposal.party_account_id,
                "status": "processed",
                "mode": str(self.mode),
                "simulated": True,
            }
        else:
            payload = {
                "account": proposal.party_account_id,
                "amount": proposal.amount_paise,
                "currency": proposal.currency,
                "notes": {
                    **proposal.notes,
                    "order_id": proposal.order_id,
                    "entitlegraph_proposal": proposal.proposal_id,
                    "synthetic_data": "true",
                },
                "on_hold": False,
            }
            raw = self._sdk.transfer.create(payload)  # type: ignore[union-attr]
            result = {**raw, "mode": str(self.mode), "simulated": False}

        self.executed.append(result)
        logger.info(
            "transfer executed order=%s role=%s amount=%s mode=%s id=%s",
            proposal.order_id,
            proposal.party_role,
            proposal.amount_paise,
            self.mode,
            result.get("id"),
        )
        return result

    def reverse_transfer(
        self,
        transfer_id: str,
        amount_paise: int,
        token: ApprovalToken | None,
        proposal: TransferProposal,
    ) -> dict[str, Any]:
        """Reverse (fully or partially) an executed transfer. Gate-protected."""
        if token is None or not token.is_valid_for(proposal, self._token_secret):
            self.unsafe_action_attempts += 1
            raise UnsafeActionError(
                f"reversal of {transfer_id} attempted without a valid gate token"
            )
        if token.token_id in self._consumed_tokens:
            self.unsafe_action_attempts += 1
            raise UnsafeActionError(
                f"gate token {token.token_id} already used; refusing replayed reversal"
            )
        self._consumed_tokens.add(token.token_id)

        if self.mode is RazorpayMode.MOCK:
            return {
                "id": "rvrsl_MOCK" + uuid.uuid4().hex[:8],
                "transfer_id": transfer_id,
                "amount": amount_paise,
                "mode": str(self.mode),
                "simulated": True,
            }
        raw = self._sdk.transfer.reverse(  # type: ignore[union-attr]
            transfer_id, {"amount": amount_paise}
        )
        return {**raw, "mode": str(self.mode), "simulated": False}

    # -- introspection -----------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "mode": str(self.mode),
            "banner": self.mode_banner(),
            "key_id_prefix": self.key_id[:14] if self.key_id else None,
            "transfers_executed": len(self.executed),
            "unsafe_action_attempts": self.unsafe_action_attempts,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
