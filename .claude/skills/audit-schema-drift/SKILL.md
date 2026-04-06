---
name: audit-schema-drift
description: Audits the expense_world_engine database schema for drift between docs/schema-reference.md and the actual SQL migration files in sql/. Use this skill whenever asked to check schema consistency, verify the schema docs match migrations, find undocumented tables or columns, or check for type and constraint mismatches. As the codebase grows, this skill will also compare against ORM model definitions. Does not require Python code to be present — can run on SQL migrations alone.
---

# Schema Drift Audit

This skill compares two sources of truth about the database schema:

1. **`docs/schema-reference.md`** — the documented schema: what tables, columns, types, constraints, and indexes *should* exist
2. **`sql/*.sql` migration files** — what the database *actually* creates

When they disagree, a client or developer reading the docs will have wrong expectations about the database. This matters for query writing, index design, and understanding what the engine stores.

As Python code is added, this skill will also compare against ORM model definitions (SQLAlchemy models, Pydantic schemas, etc.) — but it does not require them to be present. SQL migrations alone are sufficient.

## How it works

This skill is simpler than the code audits — it's a doc-to-doc comparison, not a doc-to-code one. The codebase is small enough (one schema doc, a handful of SQL files) that a small number of focused agents is sufficient.

**Phase 1 — Recon:** Read both sources and build a structured picture of each.

**Phase 2 — Comparison agents (parallel):** Split by schema concern. Each agent compares one aspect of the schema across both sources.

**Phase 3 — Assembly:** Consolidates findings into the final report.

---

## Phase 1: Recon

Read both sources in full before assigning agents:

1. **Parse `docs/schema-reference.md`:** Extract every table name, column name, data type, nullability, default value, primary key, foreign key, unique constraint, check constraint, and index mentioned in the doc.

2. **Parse `sql/*.sql` migration files:** Read all migration files in order (by filename prefix — `001_`, `002_`, etc.). Extract the same information: every `CREATE TABLE`, `ALTER TABLE`, `CREATE INDEX`, column definitions, constraints.

3. Build a side-by-side map:
   - Tables in schema-reference but not in migrations
   - Tables in migrations but not in schema-reference
   - Tables present in both → compare column by column

Assign agents based on how many tables exist. A small schema (under 15 tables) can be covered by 3-4 agents. Never exceed 9 agents.

```
Assignment plan:
- Agent 1: Infrastructure tables (users, user_settings, idempotency_keys, activity_log, sync_checkpoints, global_currencies, exchange_rates)
- Agent 2: Core expense tables (accounts, categories, hashtags, expense_transactions, expense_transaction_hashtags)
- Agent 3: Supporting tables (inbox_items, reconciliations, reconciliation_transactions) + index and constraint review
- Assembler: receives all findings
```

Adjust groupings based on the actual tables present in both sources.

---

## Phase 2: Comparison agents

Each agent receives a list of tables to compare. For each table, it performs a systematic column-by-column and constraint-by-constraint comparison.

### What each agent checks

**Table existence**
- Table in schema-reference but not in any migration → **MISSING FROM MIGRATIONS**
- Table in migrations but not in schema-reference → **UNDOCUMENTED TABLE**

**Column existence** (for each table present in both sources)
- Column in schema-reference but not in migrations → **MISSING COLUMN**
- Column in migrations but not in schema-reference → **UNDOCUMENTED COLUMN**

**Column type**
- Type in schema-reference vs type in migration SQL (e.g., `TEXT` vs `VARCHAR(255)`, `TIMESTAMPTZ` vs `TIMESTAMP`, `SMALLINT` vs `INTEGER`)
- Mismatch → **TYPE MISMATCH**

**Nullability**
- Column documented as `NOT NULL` but migration allows null (or vice versa)
- Mismatch → **NULLABILITY MISMATCH**

**Default values**
- Default documented but not present in migration, or different default
- Mismatch → **DEFAULT MISMATCH**

**Primary keys**
- Wrong column designated as primary key, or composite PK differs
- Mismatch → **PK MISMATCH**

**Foreign keys**
- FK documented but not enforced in migration (or vice versa)
- FK references the wrong table or column
- Mismatch → **FK MISMATCH**

**Unique constraints**
- Unique constraint documented but not created in migration
- Different combination of columns in the unique constraint
- Mismatch → **CONSTRAINT MISMATCH**

**Check constraints**
- Any `CHECK` constraint in either source not present in the other
- Mismatch → **CONSTRAINT MISMATCH**

**Indexes**
- Index documented but not created in migration (performance concern, not a correctness issue)
- Index in migration not documented
- Mismatch → **INDEX DRIFT** (lower severity — label separately)

**RLS policies (if present)**
- If `docs/schema-reference.md` mentions Row-Level Security policies and they're defined in a migration (e.g., `005_rls_policies.sql`), check that every documented policy exists and matches
- Mismatch → **RLS DRIFT**

### ORM model check (if Python models exist)

If SQLAlchemy models, Pydantic schemas, or similar ORM definitions exist in the codebase, add a third comparison surface: do the model field names and types match the migration columns? This is optional — skip if no model files are found.

---

## Agent output format

```
## [Table Group] — Schema Drift Audit

### Sources reviewed
**Schema doc:** docs/schema-reference.md §[relevant sections]
**Migrations:** sql/001_*.sql, sql/003_*.sql (list which files were relevant)

### Findings

#### [MISSING FROM MIGRATIONS] Table `sync_checkpoints`
**Schema doc says:** Table exists with columns: id (UUID PK), user_id (UUID FK), device_id (TEXT), version (BIGINT), created_at (TIMESTAMPTZ)
**Migrations have:** Table not found in any migration file
**Risk:** This table is required for the sync token pattern — the sync endpoint will not function without it

#### [TYPE MISMATCH] `expense_transactions.amount_cents`
**Schema doc says:** INTEGER
**Migration has:** BIGINT (sql/003_expense_tables.sql line N)
**Risk:** Minor for most amounts, but may affect ORM type inference and client expectations

#### [UNDOCUMENTED COLUMN] `accounts.legacy_id`
**Migration has:** Column defined in sql/003_expense_tables.sql line N (type: TEXT, nullable)
**Schema doc says:** No mention of this column
**Decision needed:** Add to schema docs or remove from migration

#### [INDEX DRIFT] Missing index on `expense_transactions.account_id`
**Schema doc says:** Index recommended for account-based queries
**Migration has:** No index created for this column
**Note:** Performance concern, not a correctness issue

#### [PASS] Table `users` matches schema doc
All columns, types, constraints, and nullability verified as matching.

### Summary
X missing from migrations · X undocumented · X type mismatches · X nullability mismatches · X constraint mismatches · X index drift · X passing
```

---

## Phase 3: Assembly

```
# Schema Drift Audit — expense_world_engine
**Date:** [today]
**Schema doc:** docs/schema-reference.md
**Migrations reviewed:** sql/*.sql ([N files])
**Agents deployed:** [N]

## Executive Summary
[2-3 sentences: overall schema health, how aligned doc and migrations are, any systemic patterns]

## Critical Mismatches — Fix Before Running Migrations
[TYPE MISMATCH, NULLABILITY MISMATCH, PK MISMATCH, FK MISMATCH, CONSTRAINT MISMATCH — anything that will cause wrong behavior or query failures]

## Missing from Migrations — Tables/Columns Not Yet Created
[MISSING FROM MIGRATIONS and MISSING COLUMN findings — things the doc promises but migrations don't deliver]

## Undocumented — In Migrations But Not in Docs
[UNDOCUMENTED TABLE and UNDOCUMENTED COLUMN findings — decisions needed]

## Index Drift
[INDEX DRIFT findings — lower priority, performance-focused]

## RLS Drift (if applicable)
[RLS policy mismatches]

## Clean Tables
[Tables where migrations and schema doc fully agree]

## Table Breakdown
[One row per table: status summary]
```

The assembler does not re-read source files. It works from the findings blocks only.
