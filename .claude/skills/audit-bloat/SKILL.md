---
name: audit-bloat
description: Audits the expense_world_engine codebase for bloat — dead code, unused imports, redundant logic, duplicate patterns, unnecessary abstractions, and unused dependencies. Use this skill whenever asked to audit for bloat, find dead code, clean up unused imports, check for over-engineering, or review dependencies for unused packages. Complements the business logic and coding patterns audits by focusing on what shouldn't be there at all.
---

# Bloat Audit

This skill finds code that has no business being in the codebase — unused imports, dead functions, duplicated logic, unnecessary abstractions, and dependencies that aren't actually used. Bloat is especially important to catch early in a project: patterns set at the beginning tend to be copied, so one redundant abstraction becomes five.

The question each agent asks is simple: *does this code earn its place?* If not, it's a finding.

## How it works

**Phase 1 — Recon:** Map the codebase, identify modules, decide on agent assignments within the 9-agent budget ceiling.

**Phase 2 — Module agents (parallel):** Each agent is assigned a set of files or a module directory. It reads its assigned files and looks for bloat.

**Phase 3 — Assembly:** Consolidates findings into the final report.

---

## Phase 1: Recon

Scan all non-test `.py` files outside of `.venv/`. Also note `requirements.txt` — this is always reviewed.

Split the work by module/directory. Group small directories together to stay within the budget:

```
Assignment plan:
- Agent 1: app/routers/ (all router files)
- Agent 2: app/services/ (all service files)
- Agent 3: app/models/ + app/schemas/ (data models and response shapes)
- Agent 4: app/utils/ + app/dependencies/ + app/config.py + app/db.py
- Agent 5: requirements.txt vs actual imports (cross-file dependency check)
- ...
- Assembler: receives all findings
```

The `requirements.txt` agent is always included regardless of codebase size — it doesn't read code, just cross-references declared dependencies against actual imports across all files.

If the codebase is very small (early phase), collapse module agents aggressively. A 3-agent setup (routes, services, everything-else + requirements) is fine for a small codebase.

---

## Phase 2: Module agents

Spawn all agents in parallel. Each agent reads its assigned files with a bloat-focused lens.

### What to look for

**Unused imports**
Imports that are never referenced in the file. This includes:
- Direct unused imports (`import os` where `os` is never used)
- Star imports (`from module import *`) that pull in more than needed
- Imports that are only used in commented-out code

**Dead code**
Functions, classes, or methods that are defined but never called from anywhere in the codebase. Check not just within the file but across the whole module. A helper that was useful once and then bypassed is common in fast-moving early builds.

**Duplicate logic**
The same transformation, validation, or query pattern written twice or more across different files. Common examples:
- The same date-range filter written manually in multiple service functions instead of a shared helper
- The same exchange rate lookup logic copied into multiple places
- Error-raising patterns repeated inline everywhere instead of a shared utility

**Unnecessary abstractions**
Wrapper functions or classes that do nothing beyond calling one other function. An abstraction layer earns its place when it adds behavior, enforces a contract, or hides complexity — not when it's just indirection for its own sake.

**Over-specified models**
Response or request models with fields that are never populated, always `None`, or that duplicate information already present elsewhere in the response. Early-phase over-specification often shows up as "we might need this later" fields that never get used.

**Magic values**
Hardcoded strings or numbers that should be constants or config values — things like hardcoded status codes (`status = 2`), hardcoded limits, or duplicated string literals that mean the same thing.

**Commented-out code**
Code that has been commented out rather than deleted. If it's commented out, it's dead. If it was important, it's in git history.

### Requirements agent (always included)

This agent's job is to cross-reference `requirements.txt` against actual imports in the codebase:

1. Parse every package declared in `requirements.txt`
2. Search all `.py` files outside `.venv/` for import statements
3. For each declared package, check whether it's actually imported anywhere
4. Flag packages that appear in `requirements.txt` but are never imported
5. Also flag packages that are imported but not in `requirements.txt` (implicit dependencies that aren't declared)

Note: some packages install CLI tools or are transitive dependencies — use judgment before flagging. A package like `uvicorn` is used to run the app, not imported directly. Mark these as "likely transitive" rather than "unused" if you're uncertain.

---

## Agent output format

```
## [Module/Directory Name] — Bloat Audit

### Files reviewed
- path/to/file.py (N lines)

### Findings

#### [DEAD CODE] Function `function_name` in file.py
**Location:** file.py line N
**Why it's dead:** Never called from anywhere in the codebase (or: only called from commented-out code)
**Recommendation:** Delete it (check git history if context is needed)

#### [UNUSED IMPORT] `import x` in file.py
**Location:** file.py line N
**Why:** Never referenced in this file

#### [DUPLICATE] Exchange rate lookup logic duplicated
**Locations:** services/inbox.py line N, services/transactions.py line N
**What's duplicated:** [brief description]
**Recommendation:** Extract to a shared utility function

#### [MAGIC VALUE] Hardcoded status code `2` in services/inbox.py
**Location:** file.py line N
**Recommendation:** Define as a named constant (e.g., `TransactionStatus.PROMOTED = 2`)

#### [PASS] Area that is clean
Brief note.

### Summary
X dead code · X unused imports · X duplicates · X magic values · X other · X passing
```

---

## Phase 3: Assembly

```
# Bloat Audit — expense_world_engine
**Date:** [today]
**Files reviewed:** [N files, N total lines]
**Agents deployed:** [N]

## Executive Summary
[2-3 sentences: overall bloat level, any patterns worth noting, is this early-phase normal or is there systematic bloat?]

## Dead Code — Delete
[All dead functions, commented-out code, and unreachable paths]

## Unused Imports — Delete
[All unused imports, by file]

## Duplicates — Consolidate
[All duplicated logic with both locations and a consolidation suggestion]

## Magic Values — Name Them
[All hardcoded values that should be named constants]

## Dependency Report
[Requirements.txt vs actual imports: unused packages, undeclared imports]

## Module Breakdown
[One section per module: files covered, finding counts]
```

The assembler does not re-read source files. It works from the findings blocks only.
