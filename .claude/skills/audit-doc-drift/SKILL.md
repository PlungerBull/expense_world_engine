---
name: audit-doc-drift
description: Audits the expense_world_engine codebase for documentation drift — comparing docs/engine-spec.md against the actual implementation to find gaps and divergences in both directions. Use this skill whenever asked to check if the code matches the docs, find undocumented behavior, identify unimplemented spec items, or detect where code and documentation tell different stories. Flags planned gaps, undocumented code, and behavioral divergences.
---

# Documentation Drift Audit

This skill compares `docs/engine-spec.md` against the actual codebase, in both directions, and reports on every place they diverge. It is not concerned with *whether* the spec rule is correct — only with *whether the code and the docs agree*.

Three types of findings:

- **PLANNED GAP** — the spec describes it, the code doesn't have it yet. Expected during active development, but tracked so nothing gets forgotten.
- **UNDOCUMENTED** — the code does something the spec doesn't describe. Could be an intentional addition, or a deviation that crept in. Either way it needs a decision: document it or remove it.
- **DIVERGENCE** — both exist, but they disagree. The spec says one thing, the code does another. This is the most important finding type — it means clients built against the spec will encounter unexpected behavior.

## How it works

**Phase 1 — Recon:** Map the codebase and the spec. Identify every documented resource domain and every code module. Build the assignment plan.

**Phase 2 — Domain agents (parallel):** Each agent covers one resource domain. It reads the spec section for that domain and the corresponding code files simultaneously, looking for drift in both directions.

**Phase 3 — Assembly:** Consolidates all findings into the final report.

---

## Phase 1: Recon

Do two things in parallel:

1. **Map the spec:** Read `docs/engine-spec.md` and list every documented endpoint (`METHOD /path`) and every documented business rule. Group by resource domain (Auth, Accounts, Categories, Hashtags, Inbox, Transactions, Transfers, Reconciliations, Sync, Dashboard, Exchange Rates).

2. **Map the code:** List all non-test `.py` files outside `.venv/`. Note their paths and line counts. Identify which files belong to which resource domain (usually inferrable from filenames like `routes/accounts.py`, `services/inbox.py`).

Budget ceiling: max 9 domain agents + 1 assembler = 10 total. Group smaller domains (e.g., Categories + Hashtags, Sync + Dashboard + Exchange Rates) to stay within budget. Prioritize giving complex domains (Inbox, Transactions, Transfers) their own agent.

Produce the assignment plan before proceeding:

```
Assignment plan:
- Agent 1: Auth — spec §Auth & User Bootstrap + routes/auth.py + services/auth.py
- Agent 2: Accounts — spec §Bank Accounts + routes/accounts.py + services/accounts.py
- Agent 3: Categories + Hashtags — spec §Categories + §Hashtags + relevant code
- Agent 4: Inbox — spec §Inbox + routes/inbox.py + services/inbox.py
- Agent 5: Transactions — spec §Transactions (Ledger) + routes/transactions.py + services/transactions.py
- Agent 6: Transfers — spec §Transfers + relevant code
- Agent 7: Reconciliations — spec §Reconciliations + relevant code
- Agent 8: Sync + Dashboard + Exchange Rates — spec §Sync + §Dashboard & Reporting + §Exchange Rates + relevant code
- Assembler: receives all findings
```

If the codebase is very early (few or no Python files written yet), most findings will be PLANNED GAPs. That's expected and still worth reporting — it gives a clear picture of what remains to be built.

---

## Phase 2: Domain agents

Each agent receives its spec section(s) and its code file list. It performs a systematic two-pass comparison.

### Pass 1 — Spec → Code (find gaps and divergences)

For every endpoint and rule documented in the spec section:

1. **Does the endpoint exist in the code?**
   - No → **PLANNED GAP**: the spec defines it but no implementation exists yet
   - Yes → continue to step 2

2. **Does the route signature match?**
   - Check HTTP method, path, and path parameters
   - Mismatch → **DIVERGENCE**: route exists but signature differs from spec

3. **Do the request parameters match?**
   - Check required fields, optional fields, and any forbidden fields (e.g., `is_person` must be rejected on `POST /accounts`)
   - Mismatch → **DIVERGENCE**

4. **Do the validation rules match?**
   - The spec often defines explicit validation (e.g., `currency_code` must exist in `global_currencies`, `name` must be unique per `(user_id, currency_code)`)
   - Missing validation in code → **PLANNED GAP** or **DIVERGENCE** depending on whether there's any validation at all

5. **Do the response codes match?**
   - The spec specifies exact status codes for specific conditions (e.g., `409` if account has non-deleted transactions, `422` for validation failures, `403` for system category modification)
   - Wrong code in code → **DIVERGENCE**

6. **Do the business logic steps match?**
   - For multi-step operations (especially `POST /inbox/{id}/promote`), count the steps and compare to spec
   - Missing or reordered steps → **DIVERGENCE**

### Pass 2 — Code → Spec (find undocumented behavior)

For every route handler, validation, and business rule in the code:

1. **Is there a matching entry in the spec?**
   - No → **UNDOCUMENTED**: the code does something the spec doesn't describe
   - Yes → already covered in Pass 1

2. **Does the code add extra behavior beyond what the spec describes?**
   - Extra validation, extra response fields, extra side effects
   - → **UNDOCUMENTED**: the spec should describe it or the code should not do it

### Special case: endpoints not yet written

If a code file doesn't exist at all for a domain, mark every spec endpoint in that domain as PLANNED GAP. Don't fabricate code-side findings for code that doesn't exist.

---

## Agent output format

```
## [Domain Name] — Documentation Drift Audit

### Sources reviewed
**Spec:** engine-spec.md §[Section name]
**Code:** path/to/routes.py (N lines), path/to/services.py (N lines)
  — or — "No code files found for this domain"

### Findings

#### [PLANNED GAP] POST /inbox/{id}/promote not implemented
**Spec ref:** engine-spec.md §Inbox — POST /inbox/{id}/promote
**Spec says:** 6-step atomic promotion with specific validation conditions
**Code has:** No implementation found
**Note:** Expected during active development — tracked for completeness

#### [DIVERGENCE] DELETE /accounts/{id} returns wrong status code
**Spec ref:** engine-spec.md §Bank Accounts — DELETE /accounts/{id}
**Spec says:** Return 409 if account has non-deleted transactions
**Code does:** Returns 400 for all validation failures regardless of type
**Risk:** Clients that branch on status code will behave incorrectly

#### [UNDOCUMENTED] PUT /accounts/{id} validates sort_order range
**Location:** services/accounts.py line N
**What code does:** Rejects sort_order values above 999
**Spec says:** No mention of sort_order validation
**Decision needed:** Add to spec or remove from code

#### [PASS] GET /accounts implemented and matches spec
Brief confirmation of what was checked and found clean.

### Summary
X planned gaps · X divergences · X undocumented · X passing
```

Label every finding clearly. PLANNED GAP is not a bug — it's a tracking item. DIVERGENCE and UNDOCUMENTED require an actual decision.

---

## Phase 3: Assembly

```
# Documentation Drift Audit — expense_world_engine
**Date:** [today]
**Spec reviewed:** docs/engine-spec.md
**Files reviewed:** [N code files, N total lines]
**Agents deployed:** [N]

## Executive Summary
[2-3 sentences: overall drift level, how much is planned gaps vs actual divergence, any patterns]

## Divergences — Fix Immediately
[Every DIVERGENCE finding: both sides of the disagreement, which one is correct]

## Undocumented Behavior — Needs a Decision
[Every UNDOCUMENTED finding: what the code does that the spec doesn't describe]

## Planned Gaps — Not Yet Built
[Every PLANNED GAP: grouped by domain, with the spec section reference]

## Clean Areas
[Domains where code and spec agree]

## Domain Breakdown
[One section per domain: finding counts and brief narrative]
```

The assembler does not re-read source files or the spec. It synthesizes from the findings blocks only.
