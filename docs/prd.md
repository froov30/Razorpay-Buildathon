# Product Requirements — EntitleGraph Close Agent

**Razorpay Buildathon 2026 · Track 4 (AI Finance Controller)**
Reference implementation. Synthetic data only. Test mode only.

---

## 1. The problem

Marketplace settlement is not "payment amount minus platform fee." A single
order's money is split across a seller, a delivery partner, the platform, and
sometimes a promotion budget — and how it splits depends on the seller's
contract, whether delivery was confirmed, whether the order was returned, and
which version of the agreement was in force at the time.

A payment record proves money was collected. It cannot prove a party was
*entitled* to receive their share at the moment a transfer went out. Three
failures are invisible to any check that only compares totals:

- **Paid before entitlement arose.** The amount is right; the timing is a breach.
  A payout released three hours after delivery under a 48-hour hold is perfectly
  payable *today* — which is exactly why looking at it today finds nothing.
- **Paid under superseded terms.** The commission rate changed; the settlement
  service kept a cached copy of the old one.
- **Refunded without reversal.** The customer got their money back and the seller
  kept theirs, because the refund workflow and the reversal workflow are separate
  systems and the ordering rule lives in neither — it lives in the contract.

**The gap: nobody is verifying economic entitlement against the contract — only
verifying that the ledger is internally consistent.**

## 2. Who this is for

| Audience | What they get |
|---|---|
| Marketplace / platform CFOs and finance controllers | A defensible answer to "were these payouts contractually correct?" at close |
| Seller-operations and settlement teams | A prioritised exception queue with the clause that triggered each finding |
| Delivery and gig platforms with partner payout obligations | Verification that condition-contingent fees paid only once the condition held |
| Franchise and multi-vendor e-commerce operators | Version-aware settlement across amended agreements |

## 3. The solution

> It doesn't just check that the money that moved matches the books — it checks
> that the money that moved matches what the contract actually promised.

EntitleGraph compiles prose merchant agreements into a typed, versioned policy;
computes each party's entitlement per order deterministically; compares that
against what the ledger says moved; and gates every proposed money movement
behind that check.

### The differentiation, stated plainly

Razorpay already ships an AI-powered, production-scale reconciliation product
(Optimizer / Single View Recon) matching settlements against bank statements and
gateway records. **EntitleGraph is not a reconciliation clone.**

> **Razorpay Recon proves the money that moved matches the settlement records.
> EntitleGraph proves the money that moved matches what the contract actually
> promised.**

Recon asks *"does the ledger agree with itself?"* — a data-matching question.
EntitleGraph asks *"was this party contractually entitled to what they
received?"* — a semantic, contract-interpretation question. A ledger can be
perfectly self-consistent and still record a payout the agreement forbade.

The two are complementary, not competing. Recon is the prerequisite: you cannot
check entitlement against a ledger you do not trust.

## 4. What it does not do

Stated explicitly, because scope honesty is part of the argument.

| Out of scope | Why |
|---|---|
| Bank-statement and gateway reconciliation | Razorpay Recon already does this, at production scale |
| Payment processing or routing | Not a payments product |
| Contract drafting, negotiation, or lifecycle management | Consumes agreements; does not author them |
| Fraud or AML detection | Different problem, different signals |
| Tax filing | Flags withholding inconsistencies; does not compute or file returns |
| Resolving genuinely ambiguous contract language | **Deliberately refused.** Detecting ambiguity and routing it to a human is the product; guessing would be the anti-product |
| Production deployment | Reference implementation. No production credentials, no real merchant data |

## 5. Success criteria

### Functional

1. Compile prose agreements into a typed policy, correctly recovering commission
   rate and base, settlement conditions, promotion funding, refund precedence,
   withholding, and delivery fees.
2. Represent unresolvable clauses as ambiguities carrying the competing readings
   and clause text — never as a default value.
3. Determine the governing contract version per order, refusing where two
   versions are both defensible.
4. Compute per-party entitlement in integer paise, distinguishing *what is owed*
   from *what is owed yet*.
5. Classify every discrepancy into a root cause with a confidence tier.
6. Block any proposed transfer that contradicts the contract, with a named-human
   override path.
7. Record every decision in a tamper-evident audit trail.

### Measured

| Metric | Target | Achieved |
|---|---|---|
| Classification accuracy vs ground truth | ≥ 95% | **100%** (40/40) |
| Exception precision | ≥ 95% | **100%** |
| Exception recall | ≥ 95% | **100%** |
| Amount-weighted accuracy | ≥ 95% | **100%** |
| Exact entitlement-match rate | reported | 70.6% (24/34 resolvable) |
| Auto-close rate | reported | 50% (20/40) |
| Throughput | ≥ 100 rec/s | ~520 rec/s |
| **Unsafe-action count** | **exactly 0** | **0**, verified by 6 attack classes |
| Prevented loss | reported | **₹19,941.19** |

The exact entitlement-match rate is deliberately *not* near 100%: half the
synthetic batch carries injected defects. A submission reporting 100% here would
be reporting that its test data contains nothing to find.

### Qualitative

- A judge can understand the problem, solution, and how to run it from the README
  in under two minutes.
- The system can say "I don't know, and here is why" — and does, on 7 of 40 orders.
- Every finding carries the contract clause that produced it.
- Nothing about the demo depends on a network call or an API key.

## 6. Constraints

- **No real money movement.** Test mode only; live keys refused at construction
  with no override.
- **No real merchant data.** Everything synthetic and labelled as such in code,
  docs, and UI.
- **No production integrations.**
- **Confidence-tiered output.** No bare true/false anywhere.
- **Honest exception list.** Genuinely ambiguous cases stay ambiguous.
- **Reproducible offline.** Metrics identical with no API key present.

## 7. Risks

| Risk | Mitigation |
|---|---|
| LLM extraction is non-deterministic; metrics could drift | Compile once, cache canonical JSON, score only from cache. Asserted in `tests/eval/test_reproducibility.py` |
| Model unavailable during judging | Deterministic backend produces the same DSL shape; full pipeline runs with no key |
| Synthetic data could be built to flatter the engine | Ledger settled from intended terms by an independent implementation; ground truth never on the engine's code path |
| A confident wrong root cause is worse than none | Rate findings require a quotable implied rate; anything else escalates as unexplained |
| Judges cannot tell simulation from real API traffic | Execution mode surfaced in logs, API envelope, and dashboard banner |
