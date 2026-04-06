---
name: audit-coding-patterns
description: Audits the expense_world_engine codebase for consistency and adherence to the architectural patterns defined in api-design-principles.md. Use this skill whenever asked to audit coding patterns, check for inconsistencies, review API conventions, or verify that cross-cutting concerns (error format, null-over-omission, auth, idempotency, response shape, pagination, activity log) are applied uniformly across the codebase. Different from the business logic audit — this one looks for pattern consistency, not spec rule violations.
---

# Coding Patterns Audit

This skill checks whether the cross-cutting patterns defined in `docs/api-design-principles.md` are applied consistently across the entire codebase. Where the business logic audit asks "does the code do the right thing?", this audit asks "does the code do it the same way everywhere?"

Inconsistency is a debt that compounds. A response shape that's slightly different on one endpoint, a missing `null` here, a hardcoded name there — individually minor, collectively a reliability problem for clients.

## How it works

Same three-phase structure as the other audit skills:

**Phase 1 — Recon:** Map the codebase, measure file sizes, produce an agent assignment plan within the 9-agent budget ceiling.

**Phase 2 — Concern agents (parallel):** Each agent is assigned a cross-cutting concern (not a domain). It reads the entire codebase looking for how that concern is handled everywhere.

**Phase 3 — Assembly:** Consolidates all findings into a final report.

---

## Phase 1: Recon

Scan all non-test `.py` files outside of `.venv/`. Note their paths and line counts.

Unlike the business logic audit (which splits by domain), this audit splits by **concern** — each agent hunts for one specific pattern across all files. This means agents may read overlapping sets of files, and that's fine — they're looking for different things.

Budget ceiling: max 9 concern agents + 1 assembler = 10 total. The concern list below has 6 natural areas; group or split based on codebase size. For a small early-phase codebase, 3-4 agents may be sufficient.

Produce an assignment plan:

```
Assignment plan:
- Agent 1: Error format consistency — all files
- Agent 2: Response shape (null-over-omission + amount_home_cents) — all route/service files
- Agent 3: Auth + idempotency — all route files
- Agent 4: Soft delete + activity log — all service/DB files
- Agent 5: Pagination + sign conventions — all route/service files
- Agent 6: UUID discipline + IDs-only direction — all files
- Assembler: receives all findings
```

If the codebase is large enough that any concern area would involve reading too many files to stay thorough, split that concern into two agents (e.g., "response shape — read endpoints" and "response shape — write endpoints").

---

## Phase 2: Concern agents

Spawn all concern agents in parallel. Each agent reads all relevant files with a single focused lens.

### Concern 1 — Error format

Every error path in the codebase should return exactly this shape:
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Human-readable description.",
    "fields": { "field_name": "What went wrong." }
  }
}
```

Check for:
- Any error response that doesn't use this shape (e.g., FastAPI default validation errors that aren't caught and reshaped, bare string messages, different key names)
- `fields` omitted when it should be present (validation errors should always identify which field failed)
- Error codes that aren't screaming-snake-case strings
- HTTP status codes that don't match their semantic meaning (e.g., returning `400` for a conflict that should be `409`)
- Places where exceptions are raised but not caught by a global handler — these may leak stack traces or non-standard error shapes to clients

### Concern 2 — Response shape (null-over-omission + home currency)

**Null over omission:** Optional fields must always be present in responses, set to `null` when empty. They must never be conditionally omitted. Check for:
- `Optional` fields in response models that use `exclude_none=True` or `exclude_unset=True` — these cause omission
- Response serialization that uses `.dict(exclude_none=True)` or equivalent
- Any place where a field is only included in the response when it has a value

**Home currency everywhere:** Every response containing an amount must include both the native amount and a home-currency version. Check for:
- Any response model that has `amount_cents` but is missing `amount_home_cents`
- Category totals that include `spent_cents` but not `spent_home_cents`
- Account balance responses missing `current_balance_home_cents`
- Dashboard/reporting responses missing `_home_cents` equivalents
- Any aggregation endpoint that returns money without the home-currency version

### Concern 3 — Auth + idempotency

**Auth:** Every route must require JWT authentication. Check for:
- Any route that doesn't have the JWT dependency injected
- Any endpoint that is publicly accessible without a token
- Routes where `user_id` is not extracted from the verified JWT (e.g., taken from the request body instead)

**Idempotency:** Every write operation (POST, PUT, DELETE) should accept and check an `X-Idempotency-Key` header. Check for:
- Write endpoints missing the idempotency key header parameter
- The idempotency check happening after the write instead of before
- The idempotency key not being stored with the response so duplicates can return the cached result
- Read endpoints (GET) that incorrectly implement idempotency checks (they shouldn't need them)

### Concern 4 — Soft delete + activity log

**Soft delete:** All deletions should set `deleted_at = now()`. No financial record should be hard-deleted. Check for:
- Any `DELETE FROM` SQL or ORM equivalent that removes rows outright
- Delete operations that don't set `deleted_at`
- List queries that don't filter out soft-deleted records by default
- Missing `?include_deleted=true` support on list endpoints that should have it

**Activity log:** Every mutation (create, update, delete, restore) must write an `activity_log` row with full before/after JSON snapshots. Check for:
- Create operations with no activity log write
- Update operations that don't capture the before-state snapshot
- Delete operations with no activity log entry
- Activity log writes that happen outside the main DB transaction (risk of partial writes)
- The action code being wrong (1=created, 2=updated, 3=deleted, 4=restored)

### Concern 5 — Pagination + sign conventions

**Pagination:** All list endpoints should accept `?limit` and `?offset` and return `total`, `limit`, `offset` in the response. Check for:
- List endpoints missing pagination parameters
- Responses missing the `total`, `limit`, `offset` envelope fields
- Default limit not set to 50, max limit not capped at 200
- Pagination applied inconsistently (some endpoints paginate, similar ones don't)

**Sign conventions:** The `?debit_as_negative=true` flag is a caller-side preference that should be supported on relevant read endpoints. Check for:
- Read endpoints that return amounts but don't support the `debit_as_negative` flag
- The flag being applied to responses where it shouldn't (e.g., home-currency amounts should follow the same flag)
- Amount sign being baked into storage or response models rather than applied as a transformation at response time

### Concern 6 — UUID discipline + IDs-only direction

**UUID-first:** All resource IDs should be UUIDs. No resource should be identified by a name, slug, or any mutable attribute. Check for:
- Any primary key or foreign key that isn't a UUID
- Any endpoint that looks up a resource by name instead of ID
- Client-generated UUIDs being regenerated server-side (the client should generate the UUID before the request)

**IDs-only direction:** The spec notes that hydrated names alongside IDs (e.g., `category_name` next to `category_id`) are a Phase 1 convenience that should not be relied on. Check for:
- Any client-facing logic that reads hydrated name fields as authoritative
- Response models that only return the name without the ID (the ID must always be present)
- Any place where a name is used as a lookup key

---

## Agent output format

Each agent returns a findings block:

```
## [Concern Name] — Patterns Audit

### Files reviewed
- path/to/file.py (N lines)

### Findings

#### [INCONSISTENT] Title
**Principle:** api-design-principles.md §N
**Pattern expected:** What consistent code looks like
**Violation found in:** file.py line N (or "across N files")
**Example:** Brief code snippet or description of what was found

#### [MISSING] Title
**Principle:** ...
**What's missing:** ...
**Where:** ...

#### [PASS] Concern area that is applied consistently
Brief confirmation.

### Summary
X inconsistent · X missing · X passing
```

Use INCONSISTENT when the pattern exists in some places but not others. Use MISSING when it's absent entirely from a place it should be present. Use PASS to confirm areas where the pattern is applied uniformly — clean signal matters too.

---

## Phase 3: Assembly

The assembler produces the final report:

```
# Coding Patterns Audit — expense_world_engine
**Date:** [today]
**Files reviewed:** [N files, N total lines]
**Agents deployed:** [N]

## Executive Summary
[2-3 sentences: overall consistency health, which patterns are solid, which have drift]

## Inconsistencies — Fix for Client Reliability
[Each INCONSISTENT finding, with concern label, file/line, and the pattern that should be applied]

## Missing Patterns — Fix for Correctness
[Each MISSING finding, same format]

## Consistent Areas
[Patterns that are applied uniformly across the codebase]

## Concern Breakdown
[One section per concern: files covered, finding counts, brief narrative]
```

The assembler does not re-read source files. It synthesizes from the findings blocks only.
