# EntitleGraph Close Agent

**Razorpay Buildathon 2026 · Track 4 (AI Finance Controller)**

![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-1.41-FF4B4B?style=flat-square&logo=streamlit&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-event%20store-003B57?style=flat-square&logo=sqlite&logoColor=white)
![Pydantic](https://img.shields.io/badge/Pydantic-2.10-E92063?style=flat-square&logo=pydantic&logoColor=white)
![pandas](https://img.shields.io/badge/pandas-2.2-150458?style=flat-square&logo=pandas&logoColor=white)
![pytest](https://img.shields.io/badge/pytest-192%20passing-0A9EDC?style=flat-square&logo=pytest&logoColor=white)
![Razorpay Route](https://img.shields.io/badge/Razorpay%20Route-test%20mode%20only-3395FF?style=flat-square&logo=razorpay&logoColor=white)
![Claude](https://img.shields.io/badge/Anthropic%20Claude-contract%20compiler-D97757?style=flat-square&logo=anthropic&logoColor=white)
![Gemini](https://img.shields.io/badge/Google%20Gemini-scored%20backend-4285F4?style=flat-square&logo=googlegemini&logoColor=white)
![Data](https://img.shields.io/badge/data-100%25%20synthetic-2DD4BF?style=flat-square)

> **Razorpay Recon proves the money that moved matches the settlement records.
> EntitleGraph proves the money that moved matches what the contract actually
> promised.**

A payment record proves money was collected. It cannot prove a seller was
*entitled* to receive their share at the moment a transfer went out — paid before
delivery was confirmed, paid under a commission rate that had been superseded, or
refunded to the customer without the seller's payout being clawed back first.

Reconciliation asks *"does the ledger agree with itself?"* EntitleGraph asks
*"was this party contractually entitled to what they received?"* — a semantic
question about the agreement, which no amount of ledger-matching can answer. It
compiles prose merchant agreements into typed, versioned policy, computes each
party's entitlement per order, and **refuses to let a transfer fire** when the
two disagree.

On the synthetic batch it caught **₹19,941.19** of incorrect payouts before they
executed, and correctly declined to decide on 7 of 40 orders where the contract
genuinely does not say.

---

## How a payout is decided

Every arrow that ends in a refusal is a payout that reconciliation would have
passed. The gate is the only path to money movement — the Razorpay client will
not execute without a signed token, so "no unsafe action can fire" is enforced
by construction rather than by policy.

```mermaid
flowchart LR
    A["Merchant agreement<br/><i>prose, not config</i>"] --> B{"Contract<br/>compiler"}
    B -->|term is clear| C["Typed Policy"]
    B -->|term has no answer| R["<b>null</b> + Ambiguity<br/><i>competing readings kept</i>"]

    C --> D{"Version<br/>resolver"}
    R --> D

    D -->|one version elected| E["Entitlement computed<br/><i>integer paise, three-way split</i>"]
    D -->|readings elect<br/>different versions| H1["HOLD<br/>version conflict"]

    E --> F{"Entitlement gate<br/><i>replayed as of the<br/>moment it actually fired</i>"}

    F -->|entitled| G["HMAC approval token<br/><i>single-use, content-bound</i>"]
    F -->|not entitled| H2["REFUSE<br/><i>amount at risk recorded</i>"]

    G --> T["Razorpay Route transfer<br/>LIVE-TEST · MOCK"]
    H1 --> Q["Review queue<br/><i>named owner + clause evidence</i>"]
    H2 --> Q

    T -.-> L[("Hash-chained<br/>append-only audit log")]
    H1 -.-> L
    H2 -.-> L

    style F fill:#2a1220,stroke:#fb7185,stroke-width:2px,color:#ffffff
    style H1 fill:#2a1220,stroke:#fb7185,color:#ffffff
    style H2 fill:#2a1220,stroke:#fb7185,color:#ffffff
    style R fill:#2a2410,stroke:#fbbf24,color:#ffffff
    style L fill:#10241e,stroke:#2dd4bf,color:#ffffff
    style G fill:#1a1638,stroke:#7c5cff,color:#ffffff
```

---

## Quickstart

```bash
pip install -r requirements.txt
python -m tests.eval          # scored evaluation report
```

No API keys needed — everything runs offline in `MOCK` mode and says so loudly.

```bash
streamlit run dashboard/app.py     # dashboard (start here for the demo)
uvicorn src.api.app:app --reload   # API docs at localhost:8000/docs
python -m pytest -q                # 192 tests, ~6s
python -m data.generator           # regenerate synthetic data
```

To exercise real Razorpay **test-mode** calls, copy `.env.example` to `.env` and
add a `rzp_test_...` key. The mode indicator switches to `LIVE-TEST` in logs, API
responses, and the dashboard banner. Live keys (`rzp_live_...`) are refused at
construction with no override.

---

## Results

| Metric | Result |
|---|---|
| **Prevented loss** | **₹19,941.19** |
| Classification accuracy | 100% (40/40 orders) |
| Exception precision / recall | 100% / 100% |
| Amount-weighted accuracy | 100% |
| Exact entitlement-match rate | 70.6% (24/34 resolvable) |
| Auto-close rate | 50% (20/40) |
| Throughput | ~520 records/sec |
| **Unsafe-action count** | **0** — verified by 6 attack classes, not by observation |

20 of 40 orders carry injected defects across 8 categories. The exact-match rate
is deliberately not near 100%: if it were, the test data would contain nothing to
find.

### The AI path, measured separately

The headline metrics above come from the cached deterministic compile, so they
reproduce with no API key. The LLM compiler is scored on its own, against two
model families:

| Model | Term extraction | Refusal correctness |
|---|---|---|
| `moonshotai/kimi-k3` (NVIDIA NIM) | 100% (100/100 fields) | 10/11 contracts |
| `gemini-3.1-flash-lite` | 100% (100/100 fields) | 9/11 contracts |

**Both models read every readable term perfectly. Neither handled the
deliberately unreadable contract correctly** — one invented a commission split,
the other emitted an incoherent 60% + 60% allocation with no ambiguity flagged.

That gap is the whole argument. Extraction is close to solved; knowing when
*not* to answer is not. The deterministic validation layer rejected the 120%
split before it could reach the settlement engine — the model is the extractor,
never the authority. Full breakdown in [`docs/test_plan.md`](docs/test_plan.md)
§5a, raw artifacts in `data/synthetic/llm_fidelity_report_*.json`.

Reproduce with `python -m tests.eval.run_llm_fidelity --backend {claude|gemini|nim}`.

---

## Architecture

![Architecture](diagrams/architecture.svg)

The system holds two representations of each order and compares them: **what was
promised** (compiled contract) and **what happened** (the ledger). Everything
else is machinery for producing those faithfully and reporting disagreements.

Three ideas carry the design:

**1. "I don't know" is a representable state.** Any field in the Policy DSL may
be `null` with an attached ambiguity carrying the competing readings and the
clause text. An extractor forced to always produce a number will produce one —
and an invented commission rate does not fail loudly, it settles money and
reports success.

**2. Ambiguity is scoped per order, not per contract.** Each defensible reading
of an ambiguous effective date is used to resolve the entire version stack, then
the elected versions are compared. If every reading elects the same version, the
ambiguity does not matter for that order. That is what keeps the conflict window
finite: 3 orders held, 37 unaffected, from a document that is equally ambiguous
throughout.

**3. Nothing moves money except through the gate.** The Razorpay client refuses
any transfer without a single-use, content-bound, HMAC-signed approval token that
only the gate can issue. Bypass, replay, and post-approval mutation all fail
closed.

---

## What broke

The buildathon asks what went wrong. The full account is in
[`docs/CHANGELOG.md`](docs/CHANGELOG.md); the short version:

A seller's commission changed from 70% to 65% mid-month, in an amendment stating
it was effective "from the commencement of the current billing month" but
executed on the 12th. Both dates are defensible.

The first resolver silently picked one — because contract versions carry no end
date, version 1's window was effectively infinite, so every February order
settled at the superseded rate and the engine reported no problem at all. The bug
was invisible precisely because the output looked clean.

The fix over-corrected: with a naive conflict check, orders in March and June
were flagged too, because version 1 still never ended. An exception queue that
flags everything forever is the same as no exception queue.

The real fix was two ideas — versions *supersede* rather than overlap, and each
reading is evaluated as a complete world. The conflict window is now exactly
1–11 February. Orders `ORD-1011`, `ORD-1012`, `ORD-1013` are held with ₹8,120.98
in seller payouts frozen and both competing rates shown to the reviewer.

That behaviour is the product. Detecting that an ambiguity exists *before* money
moves, and knowing exactly which orders it touches, is worth more than resolving
it automatically and being wrong half the time.

---

## Repository

| Path | Contents |
|---|---|
| `src/contract_compiler/` | Policy DSL, LLM + deterministic backends, compile cache, version resolver |
| `src/settlement_engine/` | Entitlement computation, matcher, maker-checker gate |
| `src/exception_investigator/` | Root-cause playbooks, triage, optional narratives |
| `src/razorpay_client/` | Route wrapper, LIVE-TEST/MOCK, token enforcement |
| `src/audit/` | Hash-chained append-only log |
| `data/generator/` | Synthetic contracts and ledger, ground truth |
| `dashboard/`, `src/api/` | Streamlit UI, FastAPI |
| `tests/` | 192 tests: unit, integration, adversarial, evaluation |

### Documentation

- [`docs/prd.md`](docs/prd.md) — problem, audience, scope boundaries
- [`docs/TRD.md`](docs/TRD.md) — requirements, mocked-vs-real inventory, determinism
- [`docs/architecture.md`](docs/architecture.md) — components, data flow, scaling, security
- [`docs/database.md`](docs/database.md) — schemas and the event model
- [`docs/test_plan.md`](docs/test_plan.md) — coverage **and known limitations**
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — the failure narrative and every design decision
- [`docs/implementation_plan.md`](docs/implementation_plan.md) — what was built, what was cut, and why

---

## Constraints

- **All data is synthetic**, generated by this repository. No real merchant data.
- **Test mode only.** Live Razorpay keys are refused; analysis runs never move
  money at all.
- **Reference implementation.** No production integration, no auth surface.
- **Reproducible offline.** Compiled contracts are cached as canonical JSON, so
  the scored pipeline produces identical numbers with every API key removed —
  asserted in `tests/eval/test_reproducibility.py`.
