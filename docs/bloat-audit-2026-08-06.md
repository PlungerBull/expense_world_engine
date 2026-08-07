# Bloat Audit — expense_world_engine

**Date:** 2026-08-06
**Files reviewed:** all of `app/` (~7,000 non-test lines, 60 files) + `requirements.txt`
**Agents deployed:** 7 (routers · domain helpers · reference-data helpers · cross-cutting/core · schemas+jobs · cross-module duplication sweep · dependencies)

## Executive Summary

The rework achieved its stated goals: every sacred convention is genuinely single-sourced (sign matrix, sign reader, balances, activity log, idempotency, error format), and the deletion work was clean — one unused import in the whole app, no dead functions, no commented-out code. The bloat that remains is **repetition of mechanical CRUD plumbing**: the same five or six patterns (fetch-or-404, soft-delete/restore ceremony, active-reference checks, list scaffolds, response serializers) are hand-typed dozens of times each. Several copies have already drifted, including three queries missing the `user_id` tenant filter and eight request models that silently drop unknown fields — so consolidation here is a correctness program, not a style pass.

---

## Correctness-relevant drift (fix first)

These came out of the duplication analysis but are defects today, not just cleanups.

1. ~~**Three reconciliation-status fetches have no `user_id` filter** — `app/helpers/transactions.py:456`, `:750`, `:916` read `SELECT … FROM expense_reconciliations WHERE id = $1` with no tenant predicate; the fourth copy (`:469`) has it. CLAUDE.md classes a missing `user_id` filter as a security defect. One shared `fetch_recon_status(conn, user_id, recon_id)` closes all three.~~ ✅ **Done 2026-08-06** (`6ac4deb`) — `fetch_recon_status`, tenant-scoped, deliberately returning soft-deleted rows; pinned by `tests/test_tenant_scoping.py`.
2. ~~**Eight request models silently drop unknown fields** — `extra="forbid"` is copy-pasted onto 9 models and absent from 8: `AccountUpdateRequest`, `CategoryCreateRequest`/`CategoryUpdateRequest`, `HashtagCreateRequest`/`HashtagUpdateRequest`, `PatCreateRequest`, `InboxPromoteRequest`, `TransactionBatchRequest`. Violates fail-closed ("unknown input must 422"). Fix: shared `StrictModel` base in the empty `schemas/__init__.py`; every request model inherits it.~~ ✅ **Done 2026-08-06** (`3fef299`) — the gap was **10** models, not 8: the nested `TransferField`/`InboxTransferField` were also leaky (Pydantic config does not propagate into nested models). All 21 request models now inherit `schemas.StrictModel`; pinned by `tests/test_request_strictness.py`; logged in `client-breaking-changes.md`.
3. ~~**`monthly_report` runs an expression the parity test doesn't pin** — `monthly_report.py:74` re-derives `signed_expr(HOME_CENTS_EXPR)` locally while the exported `home_currency.SIGNED_HOME_CENTS_EXPR` is consumed only by `tests/test_home_currency_parity.py`. One-line import fix restores the coverage the test is assumed to provide.~~ ✅ **Done 2026-08-06** (`e2a15eb`).
4. ~~**`pat.py:27-31` comment claims JWT auth and RLS scoping** — both deleted/inert; it asserts a security control that does not exist. Rewrite to PAT-only + engine-side scoping. (Lesser stale wording: `idempotency.py:156`, `auth_token.py:13`, `routers/accounts.py:66` names `_get_home_balance` which no longer exists, and the dead `amount_home_cents` comment tail at `schemas/transactions.py:61-67`.)~~ ✅ **Done 2026-08-06** (`f0f6885`) — the file was `routers/pat.py`, not `helpers/`; the `amount_home_cents` tail was judged a live decision record and kept.
5. **The `sort_order` append convention is unimplemented** — no `MAX(sort_order)` exists anywhere in `app/` or `sql/`; all three creates use `sort_order or 0` (`accounts.py:120`, `categories.py:125`, `hashtags.py:61`), so every new row lands at 0 and CLAUDE.md's "new rows append max+1" is false. (`or 0` also collapses an explicit `sort_order: 0` into the default.) Behavior change if fixed — log in `docs/client-breaking-changes.md`. **Deferred (owner decision 2026-08-06)** → lands inside the Duplicates §3 `reference_data.py` refactor.
6. **Account names skipped the rules categories/hashtags enforce** — no `normalize_name`, case-**sensitive** uniqueness (`accounts.py:93`, `:253` use `name = $2`, not `LOWER()`), and no restore-collision check (`restore_account`, `accounts.py:369-403`, vs `categories.py:290` / `hashtags.py:239`). Product of the triplicated CRUD body (see Duplicates §3). **Deferred (owner decision 2026-08-06)** → lands inside the Duplicates §3 `reference_data.py` refactor; note the account uniqueness key is `(name, currency_code)`, and shipping `LOWER()` needs a data check for existing same-name-different-case rows.
7. ~~**"Today" for rate lookups is UTC in 4 places while reports use `display_timezone`** — `helpers/accounts.py:57`, `routers/accounts.py:82`, `routers/dashboard.py:71`, `routers/exchange_rates.py:23` all use `datetime.now(timezone.utc).date()`; balances and reports can disagree near midnight in `America/Lima`. Owner decision needed on which rule is right; either way, one `rate_lookup_date()` helper.~~ ✅ **Done 2026-08-06** (`040f1a7`) — owner chose `display_timezone`; `exchange_rate.rate_lookup_date` owns the definition. The fix also surfaced and closed a pre-existing 500: a junk stored `display_timezone` bound raw into `AT TIME ZONE` crashed every report/dashboard read — `validation.resolve_timezone` is now the single read-side fallback. Logged in `client-breaking-changes.md`.

---

## Dead Code — Delete

- ~~**`app/helpers/transactions.py:70`** — `TransactionUpdateRequest` imported, never used (the only unused import in the app; verified by ruff/pyflakes and by hand).~~ ✅ Deleted 2026-08-06 (`6ac4deb`).
- ~~**`InboxStatus.PROMOTED` (constants.py:73) has zero references** — the one place it is assigned writes literal `2` (`inbox.py:632`). Same read-side-enum/write-side-literal split for `ReconciliationStatus` (`reconciliations.py:150`, `:322`, `:392`) and `InboxStatus.PENDING` (`inbox.py:446`). Fix by *using* the enums at write sites, not deleting them.~~ ✅ **Done 2026-08-07** (`3bf4916`) — with corrections: the enum lives in `app/constants.py` (not `helpers/`); the audit's `transactions.py:459/:489/:753` literals were wrong (those already used the enum) but `transactions.py:520` (`== 2`) was a real one it missed, as were `reconciliations.py:149` (INSERT literal `1`) and `routers/inbox.py:46` (`i.status = 1`, enum not even imported there). All 7 sites now use the enums, bound as parameters.
- ~~**`bootstrap` redundant existence probe** — `auth.py:79-100` probes `SELECT user_id`, discards it, then re-fetches `SELECT *` on the common path. Widen the probe and reuse it; one round trip saved per login.~~ ✅ **Done 2026-08-07** (`3bf4916`) — the `users` probe at `:40` stays: its exists path is `UPDATE … RETURNING *`, nothing to collapse.
- ~~**Dead columns in projections** — `transfers.py:72`, `:83` select `currency_code` never read; `inbox.py:487` likewise. Residue of the deleted pre-`sql/021` dominant-side FX rule.~~ ✅ **Done 2026-08-07** (`3bf4916`) — `id` was equally dead at all three sites: transfers reads only `is_person` now; the inbox promote check is `SELECT id`, matching its own sibling-account check.
- ~~**`_STARLETTE_CODE_MAP` entries for 413/415/429** (`errors.py:15-17`) — codes the engine cannot emit (no body limit, no media-type guard, no rate limiting). Low priority; `HTTP_ERROR` fallback already handles them.~~ ✅ **Done 2026-08-07** (`3bf4916`).
- ~~**`fetch_hashtag_ids_map`** (`transactions.py:80`) — module-public with exactly one caller (inside `attach_hashtag_ids`); rename to private and drop the redundant seed-vs-`setdefault` double key handling.~~ ✅ **Done 2026-08-07** (`3bf4916`) — seed keys normalized to `str(tid)` in the same change; without that, dropping `setdefault` would have turned a non-string caller's silent miss into a `KeyError`.
- **Keep:** `clear_rate_cache` (test-only callers, but the module-global cache genuinely needs a reset hook); the prose tombstone blocks (`errors.py:95-102`, `exchange_rate.py:169-183`, `config.py:16-19`, `transactions.py:27-42`) — decision records, not dead code. The "balance write IS the balance change" point is made ~5× across domain modules — **owner decided 2026-08-07: keep all copies**, no reduction to pointers.

## Unused Imports — Delete

- ~~`app/helpers/transactions.py:70` — `TransactionUpdateRequest`.~~ ✅ Deleted 2026-08-06. **That was the complete list** — every other file in `app/` is clean (ruff `F401,F811,F841` + pyflakes verified by two agents independently).

---

## Duplicates — Consolidate

### Tier 1 — shared query/CRUD machinery

**1. Fetch-owned-row-or-404, hand-rolled ~28×** — the tenant-isolation predicate `WHERE id = $1 AND user_id = $2 AND deleted_at IS [NOT] NULL [FOR UPDATE]` + `raise not_found(...)` exists as ~28 independent literals and no helper.
Active-row: `routers/accounts.py:141`, `routers/categories.py:75`, `routers/hashtags.py:75`, `routers/transactions.py:42`, `routers/inbox.py:148`, `helpers/accounts.py:240`, `:286`, `:330`, `:445`, `helpers/categories.py:157`, `:166`, `:218`, `helpers/hashtags.py:93`, `:102`, `:161`, `helpers/inbox.py:219`, `:248`, `:327`, `helpers/transactions.py:425`, `:441`, `:649`, `:694` (last three `FOR UPDATE`). Deleted-row (restore paths): `helpers/accounts.py:380`, `helpers/categories.py:283`, `helpers/hashtags.py:232`, `helpers/inbox.py:376`, `helpers/transactions.py:824`, `:844`. Already-drifted: `inbox.py:247` lacks the `FOR UPDATE` its `transactions.py:441` twin has.
→ **`fetch_owned_row(conn, table, id, user_id, *, deleted=False, for_update=False)`** + `_or_404` wrapper in `query_builder.py` (which already owns the write half of this predicate in `dynamic_update`/`soft_delete`/`restore`; `reconciliations.fetch_reconciliation` at `reconciliations.py:82-102` is the proven shape). This is the highest-value single change in the audit — a security consolidation, not tidiness.

**2. Soft-delete/restore ceremony ×10, plus `transactions.py` hand-rolling `query_builder` ×6** — the five-step fetch→before-snapshot→mutate→after-snapshot→activity-log body is repeated at: delete — `categories.py:205-264`, `hashtags.py:141-209`, `accounts.py:318-366`, `inbox.py:315-344`, `reconciliations.py:425-471`; restore — `categories.py:267-315`, `hashtags.py:212-264`, `accounts.py:369-403`, `inbox.py:351-403`, `reconciliations.py:474-508`. Separately, `transactions.py` re-implements `query_builder.soft_delete` verbatim at `:663-672`, `:702-711`, `restore` at `:958-967`, `:1005-1014`, `dynamic_update`-with-empty-fields at `:574-583` (the whole `else:` branch is redundant), and single-field `dynamic_update` at `:593-603`.
→ **`soft_delete_with_audit` / `restore_with_audit(conn, user_id, table, resource_type, id, serialize, *, guard=None, cascade=None)`** in `query_builder.py`; route `transactions.py` through the existing helpers (pure deletion, zero new abstraction).

**3. Reference-data CRUD triplicated (~250 lines)** — create/update/delete/restore written once per table: create `accounts.py:65-137` / `categories.py:86-136` / `hashtags.py:24-72`; update `accounts.py:213-315` / `categories.py:139-202` / `hashtags.py:75-138`; delete/restore as in §2. `update_category` and `update_hashtag` are the same 64 lines including identical comments. Name-uniqueness query alone appears 6× (`categories.py:102`, `:178`, `:290`; `hashtags.py:39`, `:114`, `:239`) plus 3 incompatible case-sensitive account renderings (`accounts.py:93`, `:253`, `:270`). `update_account` additionally fires 3 queries where 1 suffices and re-reads a row it just fetched (`accounts.py:252-289`).
→ **`helpers/reference_data.py` with a `ResourceSpec`** (table, resource_type, from_row, name-uniqueness mode, scope columns) and generic `create/update/delete/restore_resource`, with per-resource guards passed in (category system-row check, hashtag junction cascade, account active-transaction check). Includes one `assert_name_available(conn, spec, user_id, name, *, exclude_id, scope)` and one `next_sort_order` implementation. Turns drift items 5 and 6 into explicit flags.

**4. Active account/category validation ×13-14** — `validation.py` has only raising helpers and its docstring (`validation.py:9-13`) instructs collect-all-errors flows *not* to use them, mandating duplication. Hand-rolled at: `transactions.py:861-868`, `:872-879`, `:884-891`, `:895-902`, `:1113-1123`, `:1126-1136`; `inbox.py:485-492`, `:501-508`, `:517-524`; `transfers.py:70-77`, `:81-88`; plus a third *SQL* rendering in `routers/inbox.py:63-88` (`?ready=true`) that has already drifted once (WP7.2/7.3).
→ **Non-raising `active_account_row` / `active_category_row` + vectorised `active_*_ids`** in `validation.py`; the raising helpers become 3-line wrappers; export the error strings as constants; delete the "do NOT use these helpers" paragraph.

**5. `INSERT INTO expense_transactions` column list ×5** — `transactions.py:336-341` (create), `:1189-1194` (batch), `inbox.py:596-601` (promote), `transfers.py:165-170`, `:193-198` (both legs), each with its own `UniqueViolationError → conflict` translation. Highest-risk copy set: a new column means finding five INSERTs, and a missed one silently defaults — the same failure shape as the `create_batch` sign-matrix incident CLAUDE.md documents.
→ **`insert_transaction_row(conn, user_id, *, …, cleared=False, inbox_id=None, transfer_transaction_id=None, reconciliation_id=None)`** in `transactions.py`.

### Tier 2 — unrolled twins (~200 lines of deletion)

**6. Transfer-sibling blocks unrolled in delete/restore (~100 lines)** — `delete_transaction` primary `transactions.py:662-687` vs sibling `:698-731`; `restore_transaction` primary `:939-983` vs sibling `:986-1036`; inside restore a third level: unlink/no-unlink UPDATE pairs at `:946/:958` and `:993/:1005`.
→ **`_delete_leg` / `_restore_leg(conn, user_id, row, unlink)`** called once per id; collapse the four unlink variants via `reconciliation_id = CASE WHEN $3 THEN NULL ELSE reconciliation_id END`.

**7. Hashtag junction cascade ×5** — `transactions.py:679-687`, `:714-722`, `:973-983`, `:1017-1027`, plus the narrowed form in `_sync_hashtags` `:199-212`; also cascaded from the hashtag side at `hashtags.py:179-188`. The `now() == transaction_timestamp()` correctness argument is documented once (`:796-802`) but relied on twice.
→ **`_cascade_junctions_delete` / `_cascade_junctions_restore(…, deleted_at_marker)`**, docstring carrying the reasoning; lands together with §6.

**8. `complete_reconciliation` vs `revert_reconciliation` (~45 lines)** — `reconciliations.py:302-350` vs `:377-417`: identical lock, status flip, version bump, refetch/snapshot/log; only target status, guard, and the ≥1-assigned check differ. The docstring at `:374` asserts symmetry nothing enforces.
→ **`_transition_status(conn, user_id, id, *, target, require_assigned)`**.

**9. Opposite-sign transfer guard ×2, messages drifted** — `inbox.py:73-77` ("Must have opposite sign to amount_cents.") vs `transfers.py:54-59` ("…to primary amount_cents."). The only `> 0` sign reads outside `home_currency`; CLAUDE.md's single-sign-reader rule applies.
→ **`assert_opposite_signs(primary, sibling)`** in `schemas/transactions.py` beside `infer_transaction_type`.

**10. Small validation predicates re-typed** — amount-must-not-be-zero ×8 (`transactions.py:270`, `:519-524`, `:1145`; `inbox.py:110`, `:119`, `:230`, `:240`; `transfers.py:49`); future-date check with its own `SELECT now()` round trip ×4 (`transactions.py:281`, `:554-560`, `:1098+:1147`; `inbox.py:477-480`); `normalize_name` bypassed ×5 — `reconciliations.py:135-139+155`, `:225-230` (byte-identical to the helper), `transactions.py:545-551` (message already drifted: "Title validation failed."), `:267-268`, `:1143-1144` (collecting variant with no non-raising twin).
→ `reject_zero_amount(value, field)`, `db_now(conn)` + `reject_future_date(date, now, field)`, and a non-raising `clean_name` that `normalize_name` wraps — all in `validation.py`.

**11. Dynamic-UPDATE builder re-rolled in `auth.py`** — `auth.py:160-169` and `:204-212` reproduce `query_builder.dynamic_update:26-40`; the comment at `:157-159` explains why the helper's signature doesn't fit (`user_settings` keys on `user_id`), which is a signature limitation, not a semantic difference.
→ Generalize `dynamic_update` with `key_column=` / `scoped_by_user=` / `bump_version=` / `active_only=` flags, or minimally extract `_build_set_clause`.

### Tier 3 — routers and schemas (mechanical)

**12. List-endpoint scaffold ×8** — `accounts.py:36-101`, `activity.py:46-79`, `categories.py:26-52`, `hashtags.py:26-52`, `inbox.py:41-115`, `reconciliations.py:38-80`, `transactions.py:74-142`, `exchange_rates.py:62-98`. `categories`/`hashtags` list endpoints are line-for-line identical modulo table name. The `${len(params)+1}` placeholder arithmetic is retyped 8× in **two divergent idioms** (append-then vs then-append) — a latent off-by-one.
→ **`list_page(conn, *, table, conditions, params, order_by, limit, offset, mapper, select="*")`** in `helpers/pagination.py`, running count + page off one predicate and returning `paginated_response`.

**13. `X-Idempotency-Key` header declared ×36** — verbatim in every mutating route across 8 routers (accounts ×7, auth ×3, categories ×4, hashtags ×4, inbox ×5, pat ×2, reconciliations ×6, transactions ×5). `deps.py:67` already establishes the `Annotated` idiom with `CurrentUser`.
→ **`IdempotencyKey = Annotated[Optional[str], Header(alias="X-Idempotency-Key")]`** in `deps.py`.

**14. Pagination bounds hardcoded ×9 while the constants are dead** — `Query(50, ge=1, le=200)` at `accounts.py:33`, `activity.py:42`, `categories.py:23`, `exchange_rates.py:52`, `hashtags.py:23`, `inbox.py:38`, `reconciliations.py:35`, `:118`, `transactions.py:71`; `pagination.MAX_LIMIT`/`DEFAULT_LIMIT` have zero importers.
→ **`Limit = Annotated[int, Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)]` / `Offset`** shared annotations (reusable — reconciliations uses the bounds twice in one route).

**15. Balance→home conversion ×3** — `helpers/accounts.py:35-62` (`get_home_balance`, N+1 by its own admission), `routers/accounts.py:73-99`, `routers/dashboard.py:69-93` (both batch copies of the same settings-read → `batch_get_rates` → `round(balance * rate)` loop, with the same N+1-fix comment duplicated). None asserts `main_currency == HOME_CURRENCY` as `home_currency.py:130-132` requires.
→ **`fetch_home_balances` (batch) + thin `fetch_home_balance`** in `account_balance.py`, beside the balances they convert; delete `get_home_balance`; put the assertion in the one settings read.

**16. `debit_as_negative` residue** — the conditional wrapper repeated ×5 (`routers/transactions.py:50`, `:140`; `routers/inbox.py:113`, `:155`; `routers/reconciliations.py:153`) → `maybe_debit_as_negative(data, enabled, *, inbox=False)` in `formatting.py`. The no-op parameter + 6-line description duplicated in `dashboard.py:122-133` / `reports.py:66-77` → shared annotation or delete the parameter (an accepted-then-`del`'d flag deserves a deliberate decision under fail-closed).

**17. Schema-layer boilerplate** —
- `extra="forbid"` ×9 with 8 omissions → `StrictModel` (see Correctness §2).
- Ten `*_from_row` serializers re-typing the same head (`id`/`user_id` str-ification ×9 models) and audit tail (`created_at`/`updated_at`/`version`/`deleted_at` ×6 models); `hashtag_from_row` is 100% boilerplate → **`OwnedResource` / `AuditedResource` bases + `_audit_fields(row)`** in `schemas/__init__.py` (wire shape unchanged).
- `str(row[x]) if row[x] else None` ×6 (`inbox.py:95-98`, `transactions.py:122-124`), using falsy where the engine uses `is not None` → `_opt_id(value)`.
- `PatCreateResponse` restates `PatResponse` + both constructor branches (`pat.py:11-27`, `:35-50`) → inherit + conditional `token`.
- `ExchangeRateResponse` restates `ExchangeRateHistoryItem` (`exchange_rates.py:6-18`) → inherit.
- Enum-valued fields typed bare `int` with prose comments ×5 (`transactions.py:70`, `inbox.py:71`, `:75`, `reconciliations.py:44`, `activity.py:12`) → type with the `IntEnum`s from `constants.py` (wire-identical, OpenAPI-documented).
- Request FK fields typed `str` beside `id: UUID` in the same models → shared `Annotated[UUID, …]` so malformed FKs 422 instead of 500 (behavior change; log in `client-breaking-changes.md`).
- `activity.py` router carries its row mapper inline (`routers/activity.py:15-34`) unlike all nine other domains → move to `schemas/activity.py`.

**18. Jobs residue** — fetch/backfill already share four primitives; remaining duplication is the per-target upsert loop (`fetch_exchange_rates.py:173-186` vs `backfill_exchange_rates.py:206-212`), empty-targets early-return, and pool lifecycle → `_apply_rates(conn, targets, rates, rate_date)` + optional `_job_pool()` context manager. (Also: `fetch` calls blocking `_fetch_currency_api` without `asyncio.to_thread` where `backfill` wraps it — harmless single-shot, worth aligning.)

**19. Empty-update short-circuit ×7** — `categories.py:155`, `hashtags.py:91`, `accounts.py:238`, `inbox.py:217`, `reconciliations.py:217`, `transactions.py:423`, `auth.py:127` — falls out for free once §1/§2 land; not worth its own extraction.

---

## Magic Values — Name Them

| Value | Sites | Fix |
|---|---|---|
| `'USD'` rate base | `helpers/exchange_rate.py:64,68,80,84` · `helpers/home_currency.py:189,195` · `jobs/fetch_exchange_rates.py:87,92,124` · `jobs/backfill_exchange_rates.py:92` · `routers/exchange_rates.py:20` | `BASE_CURRENCY = "USD"` in `constants.py` beside `HOME_CURRENCY`, with the sql/015 note; the currency-lock docs say the two must move together and only one is greppable today |
| `'opening_balance'` | `monthly_report.py:174`, `:211`, `:278` | bind/interpolate `SystemCategoryKey.OPENING_BALANCE` (`accounts.py:186` already does it right) |
| ~~Status literals in SQL~~ | ~~`inbox.py:446`, `:632` · `reconciliations.py:150`, `:322`, `:392` · `transactions.py:459`, `:489`, `:753` (`== 2` with the enum imported in scope)~~ | ✅ Done 2026-08-07 (`3bf4916`) with the Dead Code enum item — see the corrected site list there |
| `transaction_source = 1` | `transactions.py:100,144,204,222,683,718,977,1021` | `TransactionSource(IntEnum): LEDGER = 1; INBOX = 2` in `constants.py` (confirm inbox value against schema) |
| ~~Idempotency TTL~~ | ~~`idempotency.py:103` (`interval '24 hours'` inside an SQL string)~~ | ✅ Moot 2026-08-07 — `sql/026` deleted the TTL outright (keys are permanent; open-bugs 4.1); there is no interval left to name |
| Hex color defaults | `accounts.py:119` (`#3b82f6`), `categories.py:60` (`#6b7280`) — restating `sql/003` column DEFAULTs | omit the column when caller passed nothing; let the DB default own it |
| ~~"Today" for rate lookups~~ | ~~`helpers/accounts.py:57` · `routers/accounts.py:82` · `routers/dashboard.py:71` · `routers/exchange_rates.py:23`~~ | ✅ Done 2026-08-06 (`040f1a7`) — `rate_lookup_date()` in `exchange_rate.py`, resolving in `display_timezone` (Correctness §7) |

## Dependency Report

- **Delete now:** `cryptography==46.0.6`, `cffi==2.0.0`, `pycparser==2.23` — fully orphaned (zero imports, `Required-by:` empty; PAT hashing is stdlib `hashlib`/`secrets` in `auth_token.py`). Residue of the 2026-08-03 JWT deletion: the code went, the crypto stack stayed.
- **Move to `requirements-dev.txt`:** `python-jose==3.5.0` + `ecdsa`, `rsa`, `pyasn1` — imported only by `tests/test_pat.py:19` and `tests/test_auth_over_the_wire.py:29`, which forge HS256 tokens to pin the vulnerability *closed* (must stay installed for tests; must stop shipping to production). Note `six` (ecdsa's dep) was never pinned — evidence this block is unmanaged residue.
- **Keep as pins:** `anyio`, `h11`, `idna`, `click`, `annotated-types`, `pydantic_core`, `exceptiongroup`, `async-timeout`, `typing_extensions`, `python-dotenv` (load-bearing for `env_file` via pydantic-settings) — genuine transitives. `uvicorn` is the run command. `starlette` is imported directly (`errors.py:7`, `main.py:5`).
- **Undeclared imports:** none — `httpx`/`pytest` are in `requirements-dev.txt`.
- Net effect: requirements.txt 23 → 15 lines; production stops installing a crypto stack no code calls.

## Module Breakdown

| Module | Files | Dead | Unused imports | Duplicates | Magic | Other | Clean areas |
|---|---|---|---|---|---|---|---|
| `app/routers/` | 14 | 0 | 0 | 6 | 2 | 2 | mounting, delegation to helpers, no inline error/envelope building |
| `app/helpers/` (transactions, inbox, transfers, reconciliations) | 4 | 0 | 1 | 11 | 3 | 3 | sign conventions, error format, no dead functions |
| `app/helpers/` (accounts, categories, hashtags, balance, report, home_currency) | 6 | 0 | 0 | 7 | 3 | 1 | aggregate SQL single-sourced, `fetch_balance` wrapper earns its place |
| cross-cutting + core (`main`, `errors`, `deps`, `config`, `constants`, `db`, 10 helpers) | 20 | 5 | 0 | 2 | 3 | 1 | error factories 100%, auth/pat/deps separation clean |
| `app/schemas/` + `app/jobs/` | 16 | 0 | 0 | 6 | 1 | 3 | no orphaned models after the rework, single sign reader confirmed |
| Cross-module sweep | all | — | — | 14 (overlapping above) | — | — | 11 patterns verified centralized: activity log 40/40, idempotency 36/36, sign matrix, balances, month bounds, hashtag attach/replace, `debit_as_negative` transform, envelope, reconciliation SELECT projection |

## Suggested execution order

1. ~~**Correctness first (small, self-contained)**~~ — ✅ done 2026-08-06, including the `rate_lookup_date` timezone decision (Correctness items 5–6 ride with step 3).
2. **`query_builder` layer:** `fetch_owned_row(_or_404)`; `soft_delete_with_audit`/`restore_with_audit`; route `transactions.py` through the existing helpers (pure deletion).
3. **`reference_data.py` ResourceSpec extraction** — deciding account-name normalization and `sort_order` max+1 on the way (both behavior changes → `client-breaking-changes.md`).
4. **Tier 2 twins:** `_delete_leg`/`_restore_leg` + junction cascade, `_transition_status`, `assert_opposite_signs`, validation predicates.
5. **Tier 3 sweeps:** list scaffold, `IdempotencyKey`, `Limit`/`Offset`, schema bases, home-balance batch helper, magic-value constants, requirements split.

Steps 1–2 alone remove the security-shaped duplication. The full program deletes roughly 700–900 lines and leaves each convention with exactly one implementation.
