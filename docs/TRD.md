# Technical Requirements — EntitleGraph Close Agent

Derived from `docs/prd.md`. Stack: Python 3.11+, FastAPI, Streamlit, SQLite.
No servers to provision, no containers, no message bus.

---

## 1. Functional requirements by module

### 1.1 Contract Compiler — `src/contract_compiler/`

| ID | Requirement | Verified by |
|---|---|---|
| CC-1 | Compile prose agreements into the typed Policy DSL | `test_compiler_fidelity.py` |
| CC-2 | Recover commission rate **and base** (net vs gross) | `test_recovers_intended_terms` |
| CC-3 | Handle rates stated from either side ("seller receives 75%" → 2500 bps commission) | `test_seller_side_phrasing_is_inverted_to_commission` |
| CC-4 | Parse both word and digit forms, including compound number words | `test_compound_number_words_are_not_truncated` |
| CC-5 | Emit a blocking `Ambiguity` rather than a value where a clause is unreadable | `TestRefusalCases` |
| CC-6 | Every ambiguity carries competing readings and the source clause text | `test_ambiguity_carries_a_human_readable_reason` |
| CC-7 | Two backends: `llm` (Anthropic) and `deterministic` (offline fallback) | — |
| CC-8 | Structural validation before caching; invalid policies never persist | `PolicyValidationError` path |
| CC-9 | Content-addressed policies; hash excludes compilation metadata | `TestDeterminism` |

### 1.2 Version Resolver — `src/contract_compiler/resolver.py`

| ID | Requirement | Verified by |
|---|---|---|
| VR-1 | Later versions supersede earlier ones; predecessor end dates are implied | `test_resolver.py` |
| VR-2 | Each defensible reading of an ambiguous date is evaluated as a complete world | `TestVersionConflictWindow` |
| VR-3 | Conflict **only** where readings elect different versions | `test_after_every_candidate_date_resolves_to_v2` |
| VR-4 | Never break a tie by recency, version number, or sort order | `test_never_silently_prefers_the_newest_version` |
| VR-5 | Refuse a single-version contract whose terms are unreadable | `test_unreadable_terms_are_refused_even_with_one_version` |

### 1.3 Settlement Engine — `src/settlement_engine/`

| ID | Requirement | Verified by |
|---|---|---|
| SE-1 | Integer paise only; floats rejected at runtime, not coerced | `test_money.py::TestFloatRejection` |
| SE-2 | Three-way split reconciles exactly to collected revenue | `TestSettlementIdentity` |
| SE-3 | Distinguish *owed* from *owed yet* | `TestTimingConditions` |
| SE-4 | Refuse to compute against a policy with blocking term ambiguities | `test_refuses_to_compute_against_an_ambiguous_policy` |
| SE-5 | Record a human-readable derivation for every computation | `test_derivation_is_recorded_for_the_reviewer` |
| SE-6 | Variance measured against what is payable now, not full entitlement | `test_pipeline.py` |
| SE-7 | Classify into 8 root-cause categories with a confidence tier | `test_classification_matches_ground_truth_exactly` |
| SE-8 | Re-evaluate entitlement **as of transfer execution time** to catch early payouts | `matcher.match` premature check |

### 1.4 Entitlement Gate — `src/settlement_engine/gate.py`

| ID | Requirement | Verified by |
|---|---|---|
| GT-1 | Only path to money movement; client refuses without a token | `test_gate_adversarial.py` |
| GT-2 | Tokens are single-use | `test_replayed_token_is_refused` |
| GT-3 | Tokens are bound to proposal content | `test_mutated_proposal_invalidates_token` |
| GT-4 | Tokens are HMAC-signed with a secret the caller does not hold | `test_forged_token_is_refused` |
| GT-5 | Named-human override creates a new audit entry, never an edit | `test_human_override_issues_a_fresh_token_and_audits_both` |
| GT-6 | Overridden proposals excluded from prevented loss | `test_overridden_proposal_is_excluded_from_prevented_loss` |
| GT-7 | Prevented loss counts excess on overpayment, full amount on premature | `test_wrong_amount_is_blocked_with_only_excess_at_risk` |

### 1.5 Audit Log — `src/audit/log.py`

| ID | Requirement | Verified by |
|---|---|---|
| AU-1 | Append-only, enforced by SQLite triggers | `TestImmutability` |
| AU-2 | No mutation API exists on the class | `test_the_log_exposes_no_mutation_api` |
| AU-3 | Hash-chained; content tampering detected | `test_content_tampering_breaks_verification` |
| AU-4 | Deletion detected | `test_deleting_an_entry_breaks_the_chain` |
| AU-5 | Chain verified before any safety metric is reported | `pipeline.run` |

### 1.6 Razorpay Client — `src/razorpay_client/client.py`

| ID | Requirement | Verified by |
|---|---|---|
| RZ-1 | Live keys refused at construction, unconditionally | `test_live_keys_are_refused_unconditionally` |
| RZ-2 | Mode is `LIVE-TEST` or `MOCK`, never implicit | `resolve_mode` |
| RZ-3 | Mode surfaced in logs, API envelope, and dashboard banner | `/health`, dashboard |
| RZ-4 | SDK imported lazily so MOCK runs need no dependency | verified: suite passes with `razorpay` uninstalled |
| RZ-5 | SDK init failure downgrades to MOCK **loudly**, never silently | `_init_sdk` |

---

## 2. Non-functional requirements

### 2.1 Safety — the load-bearing guarantee

**`unsafe_action_count` must be exactly zero, and that must be provable.**

Enforced structurally rather than procedurally:

1. `RazorpayRouteClient.execute_transfer` requires an `ApprovalToken`.
2. Tokens are issued only by `EntitlementGate`, only after a full re-derivation.
3. Tokens are HMAC-signed, content-bound, and single-use.
4. Every issuance, refusal, and execution is written to the hash-chained log.
5. Six attack classes are run against it on every test run.

A clean run proves nothing here — a clean run never attempts anything unsafe.
The adversarial suite is the evidence.

### 2.2 Determinism — deliberate design decision

Evaluation must be reproducible on judging day, on a laptop with no API key and
no network. The compiler is LLM-backed, which is inherently non-deterministic
across model versions. Resolution:

- Cache key: `SHA-256(contract_id | version | body)`.
- A cache hit returns stored canonical JSON and **never invokes a backend** —
  asserted by injecting a backend that raises if called.
- Canonical serialisation (sorted keys, fixed separators) makes the content hash
  stable across machines.
- The scored pipeline reads only cached artifacts.
- `--recompile` forces refresh when contract text genuinely changes.

Consequence: `python -m tests.eval` produces identical numbers with
`ANTHROPIC_API_KEY` unset.

### 2.3 Performance

| Requirement | Target | Measured |
|---|---|---|
| Batch throughput | ≥ 100 rec/s | ~520 rec/s (245 records, ~0.5s) |
| Full test suite | < 30s | ~5s (163 tests) |
| Dashboard cold load | < 10s | ~3s |
| Memory | fits a laptop | < 200 MB |

Computation is a pure function of `(order, events, policy)` with no I/O, so
per-order work is trivially parallelisable. Single-threaded is used because at
this scale it is faster than the coordination overhead — see
`docs/architecture.md` §Scaling.

### 2.4 Portability

- Windows, macOS, Linux. `src/common/console.py` forces UTF-8 because the ₹ sign
  cannot be encoded by the default Windows console codepage.
- Optional dependencies (`razorpay`, `anthropic`) imported lazily; the full
  pipeline, test suite, and dashboard run without either installed.

---

## 3. Mocked vs. real — explicit inventory

| Capability | Status | Notes |
|---|---|---|
| Route linked-account creation | **Real test-mode call** in LIVE-TEST; simulated in MOCK | `account.create` |
| Route transfer creation | **Real test-mode call** in LIVE-TEST; simulated in MOCK | `transfer.create`, gate-protected |
| Route transfer reversal | **Real test-mode call** in LIVE-TEST; simulated in MOCK | `transfer.reverse`, gate-protected |
| Contract clause extraction | **Real LLM call** on cache miss with a key present | Cached; deterministic fallback otherwise |
| Reviewer narrative | **Real LLM call** when a key is present | Presentational only; never affects a decision |
| Merchant agreements | **Synthetic** | `data/generator/contracts.py` |
| Orders, payments, deliveries, transfers, refunds | **Synthetic** | `data/generator/ledger.py` |
| Bank statements / gateway files | **Not implemented** | Out of scope — that is Recon's job |
| Production deployment | **Not implemented** | Reference implementation |

Default mode with no keys present is `MOCK`, announced at WARNING level and shown
as a dashboard banner. It is never silent.

---

## 4. Safety constraints

1. `RAZORPAY_KEY_ID` beginning `rzp_live_` raises `LiveKeyRefused`. No flag,
   environment variable, or argument overrides it.
2. Analysis and evaluation runs default to `execute_allowed=False` — scoring
   never moves money, not even in test mode.
3. Secrets come from the environment only. `.env` is gitignored; `.env.example`
   contains placeholders.
4. All synthetic records are labelled as synthetic in code, JSON, API responses,
   and UI.
5. Transfers carry `notes.synthetic_data = "true"` so that even a LIVE-TEST call
   is self-identifying in the Razorpay dashboard.

---

## 5. Data contract

Amounts are integer paise everywhere — API responses, JSON fixtures, database.
Formatted strings appear alongside raw values (`amount_paise` + `amount`) but are
never the source of truth. Timestamps are ISO-8601 UTC. Rates are integer basis
points. See `docs/database.md` for schemas.
