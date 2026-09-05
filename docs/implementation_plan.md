# Implementation plan and task checklist

The task checklist lives here rather than in a separate `tasks.md`. Updated to
reflect what was **actually** built.

---

## Sequencing principle

> Build one contract type end-to-end until it is airtight, then widen.

The original prompt specified seven parallel phases. That produces modules that
all exist and none of which survive a follow-up question. The vertical slice —
compile → resolve → compute → match → gate → audit — was made to work completely
before breadth was added anywhere.

Cut order when time is short (cut from the bottom, never the top):

1. Contract Compiler + Policy DSL — the differentiator
2. Settlement engine, integer paise
3. Gate + audit log + adversarial tests
4. Synthetic data including the version conflict
5. Evaluation harness
6. Dashboard
7. API
8. Breadth of contract types and exception categories

---

## Phase 1 — Foundations ✅

- [x] Repo structure, `.gitignore`, `.env.example`, pinned `requirements.txt`
- [x] `src/common/money.py` — integer paise, half-up, largest-remainder splits
- [x] `src/common/types.py` — ledger entities, tiers, categories, roles
- [x] `src/common/console.py` — UTF-8 forcing (₹ is unencodable in cp1252)
- [x] `src/razorpay_client/client.py` — LIVE-TEST/MOCK, live-key refusal,
      token-gated execution, lazy SDK import

## Phase 2 — Contract Compiler (centrepiece) ✅

- [x] `dsl.py` — Policy DSL where any field may refuse to have a value
- [x] `Ambiguity` with competing readings, severity, and source quote
- [x] Content-addressed policies; hash excludes provenance
- [x] Canonical JSON serialisation
- [x] `DeterministicBackend` — offline, corpus-tuned, refuses rather than guesses
- [x] `LLMBackend` — Anthropic, prompted to emit `null` + ambiguity over a guess
- [x] Compile cache; a hit never invokes a backend
- [x] Structural validation, ambiguity-aware; invalid policies never cached
- [x] Four hard clause situations planted and handled (BUILD_PROMPT §5.1)

## Phase 3 — Vertical slice ✅

- [x] `resolver.py` — supersession semantics, readings as complete worlds
- [x] `compute.py` — pure function, three-way identity asserted every call
- [x] `entitled_now` distinguishing *owed* from *owed yet*
- [x] `matcher.py` — 8 root causes, severity-ranked, tiered
- [x] `gate.py` — maker-checker, single-use content-bound tokens
- [x] `src/audit/log.py` — hash-chained, trigger-enforced append-only
- [x] `pipeline.py` — orchestration, gate replay at historical fire time

## Phase 4 — Synthetic data ✅

- [x] 10 contracts / 11 versions, prose, varied phrasing
- [x] `CTR-0003` version conflict (70% → 65%, ambiguous effective date)
- [x] `CTR-0007` unresolvable promotion funding (60% + 60%)
- [x] 40 orders, 245 records total
- [x] 20 injected exceptions across 8 categories
- [x] `INTENDED_TERMS` as compiler ground truth
- [x] Ledger settled independently of the engine
- [x] Ground truth quarantined from the engine's code path

## Phase 5 — Exceptions and gating ✅

- [x] `investigator.py` — playbook per category: mechanism, action, owner,
      severity, hold-funds
- [x] Severity-then-exposure triage ordering
- [x] Optional LLM narrative, presentational only
- [x] Adversarial gate suite, six attack classes
- [x] Version conflict verified to route to review, not resolution

## Phase 6 — Evaluation ✅

- [x] `tests/eval/metrics.py` — all seven metrics plus prevented loss
- [x] `python -m tests.eval` report with pass/fail verdict
- [x] 163 tests across unit, integration, and eval
- [x] Reproducibility asserted with all keys removed

## Phase 7 — Dashboard ✅

- [x] Prevented loss as the headline number
- [x] Loud LIVE-TEST/MOCK banner
- [x] Blocked-payouts tab with clause evidence and override form
- [x] Review queue, fund flow, contracts, metrics tabs
- [x] Verified rendering end to end in a browser

## Phase 8 — API and documentation ✅

- [x] FastAPI: health, metrics, orders, order detail, exceptions, contracts, review
- [x] Mode envelope on every response
- [x] `prd.md`, `TRD.md`, `architecture.md`, `database.md`, `test_plan.md`
- [x] `CHANGELOG.md` with the full failure narrative
- [x] Mermaid source + exported SVG in `diagrams/`
- [x] `README.md`, `DEMO.md`

---

## Deviations from the original prompt

| Change | Reason |
|---|---|
| `tasks.md` folded into this file | Judges read README → architecture → CHANGELOG → DEMO; a tenth doc adds surface area, not signal |
| `design.md` / `auth_security.md` / `deployment.md` folded into `architecture.md` | Same |
| 245 records rather than "90–120" | The linked lifecycle (40 orders × transfers per party + events) naturally produces more; order count matches the brief |
| `src/common/` added | Money primitives and shared types are used by every module; duplicating them would have been worse |
| `src/pipeline.py` added | Orchestration was implicit in the original structure; the API, dashboard, and eval harness all need one entry point |
| Gate replays proposals at historical fire time | Evaluating at today's clock finds no premature payouts at all — the check would have been decorative |

---

## Not built, and why

- **Bank-statement reconciliation** — that is Recon's job; building it would
  blur the exact distinction this submission argues for.
- **Contract lifecycle management** — consumes agreements, does not author them.
- **Authentication on the API** — a local demo surface; real auth would be
  security theatre at this scope, and pretending otherwise would be worse.
- **Concurrent gate execution** — the single-use token set is per-process. A
  production version needs it atomic and durable. Listed as a known limitation
  in `docs/test_plan.md` rather than quietly omitted.
