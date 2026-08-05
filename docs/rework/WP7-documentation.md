# WP7 — Reconcile the documentation, then delete this directory

**Read [`README.md`](README.md) first. Run last — after WP1–WP6 have all landed.**

> **Do not start early.** This package rewrites `engine-spec.md` and `schema-reference.md`
> against the final shape. Every package that lands after you means doing it again. If any
> of WP1–WP6 is outstanding, stop and say so.

---

## What you are reconciling

Six packages changed the engine. Each was required to fix the `CLAUDE.md` convention it
personally invalidated, and to append its own entry to `client-breaking-changes.md`. **You
own everything else** — chiefly the two large reference documents that nobody updated
along the way, on purpose, because updating them six times would have been waste.

Assume nothing about what actually landed. Some packages had open questions with more than
one acceptable answer (`sort_order` in WP6, `users.email` in WP5, the dashboard archived
panels in WP2). **Read the code, not the plan.**

## The documents

### `docs/engine-spec.md` — the rulebook

Every endpoint, every business rule, every validation. The largest job here.

Known to be wrong before you start:

- `GET /sync` is fully specified and no longer exists (WP4).
- Reconciliation chaining has sections on the cascade, `beginning_balance_source`, and
  chained beginning balances (WP6).
- Transfers are specified in terms of `transaction_type = 3` and `transfer_direction`
  (WP1). This is the most invasive rewrite — the transfer model is described in several
  places and the encoding changed underneath all of them.
- Currency: stored `amount_home_cents` / `exchange_rate`, and any response field carrying a
  per-record home value (WP2).
- Four archive/unarchive routes, six settings fields, `parent_transaction_id` as "reserved,
  always null" (WP5).
- Balance atomicity, wherever it is stated as engine behaviour (WP3).

The route count should now be **56** if every package landed as scoped (61 − 1 sync − 4
archive). Verify against the decorators rather than trusting that arithmetic:

```bash
grep -rhoE '@router\.(get|post|put|patch|delete)\("[^"]*"' app/routers/ | wc -l
```

### `docs/schema-reference.md` — the schema

Rebuild it from the live database rather than from the migration files, so that anything
applied out of band is captured:

```sql
SELECT table_name, ordinal_position, column_name, data_type, is_nullable, column_default
FROM information_schema.columns WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position;

SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY tablename;

SELECT conname, conrelid::regclass, pg_get_constraintdef(oid)
FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY conrelid::regclass::text;
```

Expected end state, to check your work against — **treat as a hypothesis, not an answer**:

| | Before | After |
|---|---|---|
| Tables | 15 | 14 |
| Columns | 157 | ~127–131 |
| Routes | 61 | 56 |

The range on columns is real: it depends on whether the four free leftovers went with WP5
and how WP6 answered `sort_order`.

The **new indexes from WP3 must be documented**. They are load-bearing now — the computed
balance depends on one of them — and an undocumented index is one a future migration
drops.

### `CLAUDE.md` — the conventions

WP1 and WP3 should have rewritten the two ⏳ conventions already. **Verify, don't assume.**
Then check the rest of the file for rules the program invalidated in passing:

- The "Home currency" table — does it still match what the responses actually carry?
- "Collection ordering" — still accurate if WP6 removed reconciliation `sort_order`?
- The activity-log exception for balance writes — vacuous after WP3?
- The "single-user-shaped" table — `/sync`'s "returns the whole delta, no cursor" row is
  now describing an endpoint that doesn't exist.
- The `docs/` table at the top — remove the `docs/rework/` row when you delete this
  directory.

**Do not add new conventions.** You are recording what is true, not designing.

### `docs/currency-model-decision.md`

Survives the program and remains the authority. Check that it describes what WP2 actually
built, and that it no longer implies a work plan exists. The core statement should now be
plain: convert at read time, one rate lookup per row's date, `null` plus an unconverted
count when no rate resolves, home values only on figures summed across currencies.

### `docs/open-bugs.md`

It is **a work queue, not a changelog** — delete a row when it closes rather than
annotating it done.

Expected to have closed during the program: **3.1** (WP4), **1.4** and **1.5** (WP2),
**6.3** (WP1), and **1.3** (WP1, if the regression test proved it). Verify each is actually
gone from the code before deleting its row.

**4.1 survives** — expired idempotency keys duplicating financial writes. Nothing in the
program touched it. So does **2.4**, the PAT plaintext sitting in
`idempotency_keys.response_snapshot` for 24 hours. Both should be the top of the queue when
this is done.

### `docs/client-breaking-changes.md`

Each package appended its own entry. Your job is to check they are coherent read
end-to-end by someone updating the CLI, and that the largest one is unmissable: **the CLI
must stop reading `transfer_direction`, stop expecting `transaction_type = 3`, and detect
transfers via `transfer_transaction_id != null`.** Everything else is removing field reads
and six CLI options.

### `docs/audit-2026-08-03-data-model.md`

Carries a "superseded" header pointing here. Its Part 1 census describes the pre-cut
schema, which after this is history rather than reference. **Delete it** — `schema-reference.md`
is the living version and two competing inventories is exactly the drift this program
exists to remove.

## What you must work out

- **Whether the code and the spec disagree anywhere the program didn't touch.** You are
  reading both documents closely for the first time in a while; drift predating this
  program is in scope if you find it. Report it rather than silently fixing behaviour.
- **Whether any package left a stated invariant unproven.** Each definition-of-done named
  tests that should exist. If one is missing, that is a finding.
- **Whether the open product questions are still open.** `is_person` and
  `transaction_source` were explicitly out of scope for every package. If they are still
  undecided, they need a home outside this directory before it is deleted — otherwise the
  context dies with it. `TODO.md` or `open-bugs.md`, your call.

## Definition of done

- [ ] `engine-spec.md` matches the implementation. Spot-check by picking five routes at
      random and reading the code.
- [ ] `schema-reference.md` regenerated from the live catalog, including WP3's indexes and
      every CHECK constraint.
- [ ] `CLAUDE.md` has no ⏳ markers and no rule contradicted by the code.
- [ ] `docs/open-bugs.md` contains only bugs that still exist.
- [ ] `grep -rn "currency-rework\|docs/rework" . ` finds nothing outside this directory.
- [ ] The two open product questions have a home outside `docs/rework/`.
- [ ] `pytest` green.
- [ ] **`docs/rework/` deleted**, along with its row in `CLAUDE.md`'s documentation table
      and `docs/audit-2026-08-03-data-model.md`.
- [ ] A short closing summary: final table/column/route counts, which bugs closed, which
      survive, and anything a package left undone.

## Out of scope

- Changing behaviour. If the code is wrong, **report it** — do not fix it here and do not
  document the wrong thing as intended. A documentation package that quietly edits logic is
  how a spec becomes untrustworthy.
- `docs/design-philosophy.md` — UX vision, unaffected by any of this.
- Writing the next program. This one ends.
