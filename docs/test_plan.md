# Test plan

**163 tests, ~5 seconds, no network, no API key.** Run with `python -m pytest -q`.

The suite is built around one question: *which of this system's claims would
survive someone actively trying to break them?* Tests that only confirm the happy
path are noted as such and are not counted as evidence for a safety claim.

---

## 1. Layers

| Layer | Location | Count | What it establishes |
|---|---|---|---|
| Unit | `tests/unit/` | 110 | Primitives behave under adversarial inputs |
| Integration | `tests/integration/` | 42 | Modules compose; the gate cannot be bypassed |
| Evaluation | `tests/eval/` | 11 | Metrics are correct *and reproducible* |

Everything runs hermetically: the compiler is pinned to the deterministic
backend, the Razorpay client to `MOCK`, and caches go to `tmp_path`. The optional
`razorpay` and `anthropic` packages are not installed in the dev environment,
which is itself a test — it proves the lazy-import boundaries hold.

---

## 2. Unit tests

### `test_money.py` — arithmetic (33 tests)

Floats are rejected rather than coerced, including `bool` (an `int` subclass —
silently treating `True` as one paise would be genuinely nasty to debug).
Half-up rounding is asserted at the exact `.5` boundary and for negatives.

`split_proportional` is checked across amounts chosen to force rounding residues
(1, 7, 99, 9999, 12345 paise) with the invariant `sum(parts) == amount`, plus a
determinism check: 50 identical calls must produce identical output, because a
non-deterministic split would make the eval metrics irreproducible.

### `test_compiler_fidelity.py` — extraction correctness (30 tests)

**The most important unit file.** Every clause of every unambiguous contract is
asserted against `INTENDED_TERMS` — what a careful human reader should extract.
It is not enough that the compiler produces *a* policy.

Specifically covered: seller-side phrasing inverted to commission ("seller
receives 75%" → 2500 bps); compound number words not truncated ("forty-eight (48)
hours" ≠ 8 hours — a real bug that would have made premature payouts look
compliant); digit and word forms both parsed; commission base (net vs gross)
distinguished.

Refusal cases: overlapping promotion clauses produce a blocking ambiguity; the
ambiguous effective date carries *both* candidate dates; a date ambiguity does
**not** block the terms themselves; every ambiguity carries a reason long enough
to act on.

### `test_resolver.py` — version resolution (20 tests)

The conflict window is asserted date by date: `2026-01-02/15/31` → v1;
`2026-02-01/05/11` → conflict; `2026-02-12/20`, `2026-06-01` → v2.

`test_never_silently_prefers_the_newest_version` exists because "take the highest
version" is a plausible heuristic that would quietly under-pay the seller across
the whole window.

### `test_compute.py` — settlement (21 tests)

The three-way identity is asserted across awkward amounts. Timing conditions are
tested at the boundary (23h vs 24h into a 24h hold) and for the case where the
triggering event never arrives — a year later, a failed delivery is still not
payable, which is different from being late.

Also asserts the engine **refuses** to compute against a policy with blocking
term ambiguities, rather than defaulting.

### `test_audit.py` — immutability (12 tests)

Trigger-level `UPDATE`/`DELETE` rejection, and the absence of any mutation method
on the class. Tamper detection drops the triggers and rewrites history directly,
then asserts `verify_chain` names the exact `seq` where content was altered or an
entry removed.

---

## 3. Integration tests

### `test_gate_adversarial.py` — the safety claim (10 tests)

`unsafe_action_count == 0` observed from a clean run proves nothing, because a
clean run never attempts anything unsafe. Six attack classes:

| # | Attack | Expected |
|---|---|---|
| 1 | Call the client with no token | `UnsafeActionError`, nothing executed |
| 2 | Replay a spent token | Refused; exactly one transfer exists |
| 3 | Approve ₹800, execute ₹80,000 | Refused — token is content-bound |
| 4 | Forge a token without the secret | Refused — HMAC mismatch |
| 5 | Reuse a valid token for a different payee | Refused |
| 6 | Execute a refusal | Refused |

Plus: prevented loss counts only the *excess* on an overpayment; a human override
issues a fresh token and writes a second audit entry without touching the first;
overridden proposals are excluded from prevented loss (the money did move); live
keys are refused unconditionally.

### `test_pipeline.py` — full batch (13 tests)

40 orders end to end. Asserts exact classification against ground truth; every
order carries a tier, an explanation, and evidence; **no transfer executes during
an analysis run**; at least six distinct exception categories appear; both
auto-clear and needs-review are non-empty (a batch that all auto-clears reads as
untested); the review queue is severity-ordered and every case has an owner and
an action; every resolvable decision records a replayable policy hash.

### `test_version_conflict_scenario.py` — the "what broke" case (19 tests)

The judge-facing trace. Asserts the three conflicted orders route to human review
with **no entitlement figure asserted**, the gate holds the money with no token
issued, and the evidence contains both candidate dates, both competing rates, and
the clause text.

Equally important, it asserts the window is *bounded*: `ORD-1010` (before) and
`ORD-1014` (after) settle normally, and exactly `{ORD-1011, ORD-1012, ORD-1013}`
are conflicted. An ambiguous amendment must not poison the relationship.

The final test asserts `docs/CHANGELOG.md` exists and names the demo orders —
the failure narrative is a submission deliverable, so its absence should fail CI.

---

## 4. Evaluation tests

### `test_reproducibility.py` — the determinism claim (6 tests)

If this file fails, the metrics in the README are untrustworthy and the claim
must be withdrawn.

- Two runs produce identical decision rows.
- Metrics are stable across runs (throughput excluded — it varies with load).
- The pipeline runs with `ANTHROPIC_API_KEY`, `RAZORPAY_KEY_ID`, and
  `RAZORPAY_KEY_SECRET` all removed, and still scores 100%.
- **A warm cache never invokes a backend** — asserted by injecting a backend that
  raises `AssertionError` if called.
- Cached policies are byte-for-byte canonical JSON.
- Recompiling identical text yields an identical content hash.

---

## 5. Metrics

Computed by `tests/eval/metrics.py`, run via `python -m tests.eval`.

| Metric | Definition | Current |
|---|---|---|
| Classification accuracy | Predicted category == ground truth | **100%** (40/40) |
| Exception precision | TP / (TP+FP) on the binary exception question | **100%** |
| Exception recall | TP / (TP+FN) | **100%** |
| Amount-weighted accuracy | Correct classifications weighted by order value | **100%** |
| Exact entitlement-match rate | Resolvable orders with zero variance | 70.6% (24/34) |
| Auto-close rate | Closed with no human | 50% (20/40) |
| Throughput | Records per second | ~520 rec/s |
| Unsafe-action count | Money movements without a valid token | **0** |
| Prevented loss | Refused exposure, excluding human overrides | **₹19,941.19** |

The exact entitlement-match rate is deliberately not near 100%: half the batch
carries injected defects. Reporting 100% here would mean the test data contains
nothing to find.

The harness exits non-zero unless unsafe actions are 0, the audit chain verifies,
classification accuracy ≥ 95%, and exception recall ≥ 95%.

---

## 5a. LLM compiler fidelity

Run via `python -m tests.eval.run_llm_fidelity --backend {claude|gemini|nim}`.
Committed artifacts: `data/synthetic/llm_fidelity_report_*.json`.

Two things are scored, and the split matters more than either number:

**Term extraction** — of the 10 commercial terms per contract that a careful
reader should recover, how many were recovered exactly.

**Refusal correctness** — whether the model declined to answer where the
contract does not support an answer, and *only* there. Scored in both
directions, so over-refusing on a clear contract counts against it too.

| Model | Term extraction | Refusal correctness |
|---|---|---|
| `moonshotai/kimi-k3` (NVIDIA NIM) | **100%** (100/100 fields, 10/10 contracts) | 10/11 contracts |
| `gemini-3.1-flash-lite` | **100%** (100/100 fields, 10/10 contracts) | 9/11 contracts |

**Both models read every readable term perfectly. Neither handled the
deliberately unreadable contract correctly.**

- `CTR-0007` (promotion clauses over-allocating the same discount): **both
  models failed.** Kimi invented terms. flash-lite emitted an incoherent
  60% + 60% split with no ambiguity recorded — a reply so malformed that no
  valid policy could be built from it at all.
- `CTR-0003 v2` (amendment with two defensible effective dates): Kimi flagged
  **both** candidate readings correctly. flash-lite found only `2026-02-01` and
  missed the competing `2026-02-12` reading entirely — a half-detection, which
  is more dangerous than none because it looks resolved.

The reported `refusal_accuracy` in flash-lite's JSON reads 90% because its
`CTR-0007` reply produced no policy and so fell out of the denominator. Counting
it — as the scorer now does — gives 9 of 11. That omission is recorded here
rather than quietly left in the artifact.

**What this justifies.** The deterministic validation layer rejected
flash-lite's 120% split before it could reach the settlement engine. That is the
entire argument for the architecture: the model is the extractor, not the
authority, and a typed policy plus structural validation is what stands between
a plausible-looking model output and a wrong payout. Extraction is close to
solved; knowing when *not* to answer is not.

---

## 6. Known limitations

Stated because reporting a metric without its blind spot is the failure mode this
project exists to criticise.

1. **Shared money-flow convention.** The generator settles from `INTENDED_TERMS`
   with its own implementation, so the metrics genuinely test whether the
   compiler recovered the right terms and applied them consistently. But both
   sides share the *convention* (what "net order value" means, that the platform
   reimburses its funded discount share, that TDS is a deduction rather than a
   fourth party) by construction. The metrics do not prove that convention
   matches any particular marketplace's. It is documented in
   `src/settlement_engine/compute.py` for exactly this reason.

2. **The deterministic backend is corpus-tuned.** It recognises the phrasings in
   `data/generator/contracts.py`. It is a fallback for hermetic runs, not a claim
   that contract interpretation is a regex problem. Where phrasing falls outside
   its rules it emits a blocking ambiguity rather than a wrong number — the same
   failure mode the LLM backend is prompted to use — but its coverage on
   arbitrary real agreements is untested and would be poor.

3. **LLM fidelity is measured on eleven synthetic contracts, not a diverse
   corpus.** §5a reports real scored runs for two models, so the AI path is no
   longer unmeasured. But eleven documents written by this project is a small,
   friendly corpus: the prose is consistent, the clauses are well-formed, and
   the two hard cases were planted deliberately. Both models scoring 100% on
   term extraction says more about the corpus being clean than about the models
   being reliable on real agreements. A production version needs a held-out set
   of genuine merchant contracts with human-labelled terms, which this does not
   have. The refusal results are the more transferable finding, because failing
   to decline is a behaviour rather than a difficulty score.

4. **Synthetic scale.** 40 orders, 11 contract versions. Throughput at 245
   records says nothing about behaviour at 245 million; the scaling argument in
   `docs/architecture.md` is a design argument, not a measurement.

5. **No concurrency testing.** The gate's single-use token check is not tested
   under concurrent execution. A production deployment would need the consumed-
   token set to be atomic and durable rather than a per-process `set`.

6. **Human review is assumed to happen.** The system's value depends on someone
   working the queue. Nothing here tests reviewer behaviour, throughput, or
   fatigue, which are the real constraints on a control like this.
