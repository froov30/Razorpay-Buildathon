# Data model

Two stores, deliberately separated:

- **Ledger and contracts** — JSON fixtures under `data/synthetic/`, regenerable
  with `python -m data.generator`. This is what a real deployment would read
  from a marketplace's own systems.
- **Audit log** — SQLite at `data/synthetic/entitlegraph.db`, the only mutable
  artifact the system writes, and it is append-only.

Ground truth lives in a third file that nothing on the engine's code path opens.

**All amounts are integer paise. All rates are integer basis points. All
timestamps are ISO-8601 UTC. All data is synthetic.**

---

## 1. Entity relationships

```
ContractSource (contract_id, version)  ──compiles to──▶  Policy (content-addressed)
        │ 1:N versions                                        │
        │                                                     │ governs (resolved by order date)
        ▼                                                     ▼
      Seller ──1:N──▶ Order ──1:1──▶ PaymentEvent
                        │
                        ├──0:1──▶ Promotion
                        ├──0:N──▶ DeliveryEvent
                        ├──0:N──▶ Transfer ──0:N──▶ ReversalEvent
                        └──0:N──▶ RefundEvent

      Every decision and every gate verdict ──▶ AuditEntry (hash-chained)
```

An order joins to a contract **through its seller and its date** — not by a
foreign key. Which version applies is a computed question, and sometimes an
unanswerable one. That indirection is the reason the resolver exists.

---

## 2. Contract side

### `ContractSource` — `contracts.json`

The raw agreement, deliberately unstructured.

| Field | Type | Notes |
|---|---|---|
| `contract_id` | str | e.g. `CTR-0003` |
| `version` | int | Increments per amendment |
| `seller_id` | str | |
| `effective_from` | datetime\|null | May be contradicted by the body — that is the point |
| `effective_to` | datetime\|null | Usually null; supersession is implied |
| `body` | str | **The agreement prose.** Compiler input |
| `notes` | str | Provenance |

Storing prose rather than structured terms is what leaves an interpretation
problem to solve. Structured terms here would make the compiler trivial and the
differentiation from Recon vacuous.

### `Policy` — `compiled_policies/{contract}_v{n}_{fingerprint}.json`

Compiler output. Content-addressed; the hash excludes provenance so identical
terms compiled at different times by different backends collide correctly.

| Group | Fields |
|---|---|
| Identity | `contract_id`, `version`, `seller_id`, `dsl_version` |
| `effective` | `starts_at`, `ends_at`, `ambiguous` |
| `commission` | `rate_bps` (nullable), `applies_to` (`order_net`\|`order_gross`), `minimum_paise`, `source_quote` |
| `hold` | `requires_delivery_confirmation`, `hold_hours_after_delivery`, `source_quote` |
| `promotion_funding` | `platform_share_bps`, `seller_share_bps`, `source_quote` |
| `refund` | `commission_refundable`, `reversal_must_precede_refund`, `reversal_window_hours`, `source_quote` |
| `tax` | `tds_on_commission_bps`, `applies_to`, `source_quote` |
| `delivery_fee` | `flat_fee_paise`, `payable_on_confirmation_only`, `source_quote` |
| `ambiguities` | list of `{field_path, reason, candidates[], severity, source_quote}` |
| `provenance` | `backend`, `model`, `compiled_at`, `source_sha256`, `notes` |

**Nullable rate is load-bearing.** `commission.rate_bps = null` with a blocking
ambiguity is a valid, meaningful policy: "this contract exists, and its rate
cannot be determined." Validation rejects a null rate only when no ambiguity
explains it.

`source_quote` on every clause is the evidence chain — a reviewer sees the
sentence a number came from, not just the number.

---

## 3. Ledger side

### `Order` — `orders.json`

| Field | Type | Notes |
|---|---|---|
| `order_id` | str | |
| `seller_id` | str | Joins to contract via seller + date |
| `placed_at` | datetime | **Determines the governing contract version** |
| `gross_amount_paise` | int | Charged to the customer, discount already applied |
| `shipping_fee_paise` | int | |
| `tax_collected_paise` | int | |
| `delivery_partner_id` | str\|null | |
| `promotion_id` | str\|null | |

Derived bases: `net_order_value = gross − shipping − tax`;
`gross_merchandise_value = net + discount`. Which one commission applies to is a
contract term, not a system constant — `CTR-0005` uses the gross base.

### `PaymentEvent`, `DeliveryEvent`, `Promotion`

| Entity | Key fields |
|---|---|
| `PaymentEvent` | `payment_id`, `order_id`, `captured_at`, `amount_paise`, `method` |
| `DeliveryEvent` | `delivery_id`, `order_id`, `occurred_at`, `confirmed` (bool), `delivery_partner_id` |
| `Promotion` | `promotion_id`, `order_id`, `discount_paise`, `campaign`, `declared_funder` |

`DeliveryEvent.confirmed = false` is not the same as no event: it records a
delivery attempt that failed, so entitlement never arises rather than arriving
late. `Promotion.declared_funder` is what the ledger *claims*; the contract
decides whether that claim is admissible.

### `Transfer`, `RefundEvent`, `ReversalEvent`

| Entity | Key fields |
|---|---|
| `Transfer` | `transfer_id`, `order_id`, `party_role`, `party_id`, `amount_paise`, `executed_at`, `tds_withheld_paise`, `razorpay_transfer_id` |
| `RefundEvent` | `refund_id`, `order_id`, `issued_at`, `amount_paise`, `reason` |
| `ReversalEvent` | `reversal_id`, `transfer_id`, `order_id`, `executed_at`, `amount_paise` |

`Transfer.executed_at` is the single most important timestamp in the system. The
gate replays each proposal *as of* it, because a payout released early is
perfectly payable later — evaluating at today's clock finds nothing.

Reversals attach to a `transfer_id`, not an order, so partial claw-backs net
correctly and withholding can follow the payment it was withheld from.

---

## 4. Event model

The entitlement graph is a fold over an append-only event sequence per order:

| Event | Effect on entitlement |
|---|---|
| `order_placed` | Establishes the date → selects the contract version |
| `payment_captured` | Establishes collected revenue |
| `promotion_applied` | Introduces a discount and a funding question |
| `delivery_confirmed` | **Starts the hold clock**; may make a share payable |
| `delivery_failed` | Entitlement never arises |
| `transfer_executed` | Records actual movement; checked against payable-now |
| `refund_issued` | Prorates entitlement; may require a prior reversal |
| `reversal_executed` | Reduces actual movement and its withholding |

Every decision stores the `policy_hash` it used, so `(events, policy_hash)` fully
reproduces it. That is what makes historical re-audit after a rule change a
replay rather than an archaeology exercise.

---

## 5. Audit log (SQLite)

```sql
CREATE TABLE audit_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at  TEXT    NOT NULL,
    actor        TEXT    NOT NULL,   -- 'entitlegraph.gate' or a named human
    action       TEXT    NOT NULL,   -- gate.decision | gate.human_override | ...
    subject_id   TEXT    NOT NULL,   -- order_id, or 'batch'
    outcome      TEXT    NOT NULL,
    payload_json TEXT    NOT NULL,
    prev_hash    TEXT    NOT NULL,
    entry_hash   TEXT    NOT NULL UNIQUE
);

CREATE INDEX idx_audit_subject ON audit_log(subject_id);
CREATE INDEX idx_audit_action  ON audit_log(action);

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is forbidden'); END;
```

`entry_hash = SHA256(recorded_at | actor | action | subject_id | outcome | payload_json | prev_hash)`,
with `prev_hash` seeded at 64 zeros. Altering or removing any entry breaks every
subsequent hash, and `verify_chain()` reports the exact `seq` where it broke.

Recorded actions: `run.start`, `run.finish`, `entitlement.decision`,
`gate.decision`, `gate.human_override`, `gate.human_reject`, `razorpay.transfer`.

A human override is a **new entry**. The original refusal stays in the chain
permanently — there is no code path that edits it, and the storage layer would
reject one.

---

## 6. Ground truth — quarantined

`data/synthetic/ground_truth.json`

```json
{
  "_comment": "SYNTHETIC ground-truth labels. Consumed only by tests/eval.",
  "labels": { "ORD-1011": "contract_version_conflict", ... },
  "notes":  { "ORD-1011": "IN CONFLICT WINDOW: settled at v1's 70% seller share." },
  "batch_as_of": "2026-03-15T00:00:00+00:00",
  "seed": 20260904
}
```

Written by the generator, read only by `tests/eval/metrics.py`. `src/pipeline.py`
loads eight fixture files and this is not one of them — there is no code path by
which the engine can consult the answers it is scored on.

---

## 7. Current dataset

| Entity | Count |
|---|---|
| Contract versions | 11 (10 contracts; `CTR-0003` has two) |
| Orders | 40 |
| Payments | 40 |
| Deliveries | 33 |
| Promotions | 8 |
| Transfers | 107 |
| Refunds | 3 |
| Reversals | 2 |
| **Total records** | **245** |

20 of 40 orders carry an injected defect, spread across 8 categories. Two
contracts are deliberately unreadable. Regenerate deterministically with
`python -m data.generator` (seed `20260904`).
