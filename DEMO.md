# Demo script — 5 minutes

> **🎥 VIDEO RECORDING: PLACEHOLDER — to be recorded by the builder.**
> This file is the script. Timings are cumulative. Everything below has been
> verified to work end to end; nothing needs to be faked or edited around.

---

## Before you record

```bash
pip install -r requirements.txt
python -m pytest -q            # expect: 163 passed
python -m tests.eval --quiet   # expect: PASS, prevented loss ₹19,941.19
streamlit run dashboard/app.py
```

**Optional but recommended:** put a `rzp_test_...` key in `.env` so the banner
reads `LIVE-TEST` rather than `MOCK`. It costs nothing, moves no real money, and
shows a Razorpay judge you actually wired up Route.

Have two things open: the dashboard, and a terminal at the repo root.

---

## 0:00 – 0:15 · Hook

> "A payment record proves money was collected. It cannot prove the seller was
> *entitled* to it.
>
> This platform paid a seller three hours after delivery — under a contract
> requiring a forty-eight hour hold. The amount was right. The ledger balanced.
> Reconciliation passed. And it was still a breach of contract."

*Do not open anything yet. Say this over a black screen or the dashboard header.*

---

## 0:15 – 0:35 · The differentiation

*On screen: the dashboard header, which carries this line verbatim.*

> "Razorpay already ships production-scale reconciliation. So let me be precise
> about what this is and isn't:
>
> **Razorpay Recon proves the money that moved matches the settlement records.
> EntitleGraph proves the money that moved matches what the contract actually
> promised.**
>
> Recon asks whether the ledger agrees with itself. That's a data-matching
> question. I'm asking whether the party was contractually entitled to what they
> received — a question about the agreement, which no amount of ledger-matching
> can answer. These are complementary. You can't check entitlement against a
> ledger you don't trust."

*Say this clearly and slowly. It is the single most important twenty seconds of
the pitch — it is the difference between "he rebuilt our product" and "he found
a gap next to our product."*

---

## 0:35 – 1:05 · The headline

*On screen: top of the dashboard.*

> "Forty synthetic orders across ten merchant agreements. The system caught
> **₹19,941.19** of incorrect payouts before they fired.
>
> Note the banner — this says exactly which mode it's running in. It's never
> ambiguous whether you're watching real API traffic or a simulation.
>
> One hundred percent classification accuracy against ground truth the engine
> never sees. Exception precision and recall both a hundred percent. And
> zero unsafe actions — I'll come back to how that's proven rather than
> asserted."

---

## 1:05 – 1:50 · A clean auto-clear

*Blocked payouts tab → note the count → then Review queue → point at a clean
order, or open `ORD-1001` via the API/fund-flow tab.*

> "Half the batch clears automatically. Here's what that means: the compiler read
> this seller's agreement — the prose, not a config file — and extracted a 30%
> commission on net order value, a 48-hour hold after delivery confirmation,
> platform-funded promotions, and 1% TDS on commission.
>
> It then computed what each party was owed, to the paise, and compared it to
> what actually settled. Exact match across three parties. No human needed.
>
> Everything is integer paise. No floats anywhere — a one-paise drift across a
> batch produces phantom variances that look exactly like real breaches."

---

## 1:50 – 2:50 · The blocked payout · **the moment**

*Blocked payouts tab. Expand a `premature_payout` case — `ORD-1003` or
`ORD-1018`.*

> "Fourteen payouts were stopped at the gate. Here's one.
>
> The amount is *correct*. The ledger balances. Recon would pass this. But look
> at the reason — the seller was paid before the contractual condition was met.
> On this one, delivery was never confirmed at all.
>
> And here's the subtle part: each settlement is replayed through the gate **as
> of the moment it actually fired** — not as of today. A payout released early is
> perfectly payable now, so checking it today finds nothing. You have to ask the
> question the settlement job faced at the time.
>
> The reviewer gets the derivation, the clause that triggered it, a recommended
> action, and a named owner. And on the right — a human can override, with a
> justification. The refusal stays in the audit chain permanently. An override is
> a new entry, never an edit."

*Scroll to show the evidence list and the override form.*

---

## 2:50 – 3:50 · What broke · the version conflict

*Expand `ORD-1011`, `ORD-1012`, or `ORD-1013` under `contract_version_conflict`.*

> "The buildathon asks what broke. This did — and it's the case I'm proudest of.
>
> This seller's commission changed from 70% to 65% mid-month. The amendment says
> it's effective 'from the commencement of the current billing month.' It was
> executed on the twelfth. Both dates are defensible and nothing in the document
> ranks them.
>
> My first resolver silently picked one. Because contract versions don't carry
> end dates, version one's window was effectively infinite — so every February
> order settled at the old rate and the engine reported no problem at all. The
> bug was invisible because the output looked clean.
>
> Then I over-corrected. A naive conflict check flagged March and June too, since
> version one still never ended. An exception queue that flags everything forever
> is the same as no exception queue.
>
> The real fix was two ideas: versions *supersede* rather than overlap, and each
> defensible reading gets evaluated as a complete world. If every reading picks
> the same version, the ambiguity doesn't matter for that order.
>
> So the conflict window is exactly the first to the eleventh of February. Three
> orders held — ₹8,120.98 frozen — and thirty-seven unaffected, from a document
> that is equally ambiguous throughout.
>
> The system does not resolve this. It shows the reviewer both readings, both
> rates, and the clause text, and tells them to get a written clarification from
> contracting. That refusal is the product."

---

## 3:50 – 4:25 · Safety, proven not asserted

*Terminal:*

```bash
python -m pytest tests/integration/test_gate_adversarial.py -q
```

> "Zero unsafe actions, observed from a clean run, proves nothing — a clean run
> never tries anything unsafe.
>
> So I attack it. Six classes: call the client with no token; replay a spent
> token; get approval for ₹800 and try to send ₹80,000; forge a signature; reuse
> a token for a different payee; execute a refusal. All refused, nothing
> executed.
>
> The audit log is hash-chained and append-only at the storage layer — SQLite
> triggers reject UPDATE and DELETE. The tamper tests drop the triggers and
> rewrite history directly, and the chain still detects it. That matters: 'zero
> unsafe actions' means nothing if the log could have been edited afterwards."

---

## 4:25 – 5:00 · Metrics and close

*Metrics tab, then terminal:*

```bash
python -m tests.eval --quiet
```

> "A hundred percent classification accuracy, precision, and recall against
> ground truth the engine has no code path to read. Amount-weighted, so a ₹50
> miss and a ₹50,000 miss aren't treated the same.
>
> And I scored the AI part separately, across two model families. Both read a
> hundred percent of the contract terms correctly. **Neither** could decline to
> answer on the contract that has no answer — one invented a commission split,
> the other emitted a sixty-plus-sixty percent allocation that doesn't even sum
> to a hundred. My validator caught it before it reached the settlement engine.
>
> That's the point: extraction is close to solved, knowing when *not* to answer
> isn't. The model is the extractor, never the authority.
>
> Exact entitlement match is 70.6% — deliberately not a hundred. Half this batch
> has injected defects. A submission reporting a hundred percent here would be
> reporting that its test data contains nothing to find.
>
> The compiler is LLM-backed, but compiled contracts are cached as canonical
> JSON, so these numbers reproduce with every API key removed. There's a test
> that asserts exactly that.
>
> Recon proves the ledger agrees with itself. EntitleGraph proves the ledger
> agrees with the contract. Everything here is synthetic, test mode only, and it
> runs on a laptop in one command."

---

## Recording notes

- **Do not skip 0:15–0:35.** A Razorpay judge's first instinct will be "we
  already have this." Answer it before they think it.
- Spend the most time on 2:50–3:50. Admitting a real bug, showing the
  over-correction, and explaining the fix reads as engineering maturity — a
  submission with no failures reads as untested.
- Run the tests live. It takes five seconds and is far more convincing than a
  screenshot of a passing badge.
- If you record in `MOCK`, say so out loud rather than hoping nobody reads the
  banner. If in `LIVE-TEST`, point at it — it shows you wired up Route.
- Say the exact rupee figures. Specific numbers land; "significant savings"
  does not.

## Quick reference

| Thing | Where |
|---|---|
| Version-conflict demo | `ORD-1011`, `ORD-1012`, `ORD-1013` |
| Premature payout demo | `ORD-1003` (early), `ORD-1018` (never delivered) |
| Missing reversal demo | `ORD-1005`, `ORD-1022` |
| Duplicate transfer demo | `ORD-1008`, `ORD-1024` |
| Unreadable contract | `CTR-0007` — Contracts tab |
| Failure narrative | `docs/CHANGELOG.md` |
| Adversarial suite | `tests/integration/test_gate_adversarial.py` |
| Scenario trace | `tests/integration/test_version_conflict_scenario.py` |
