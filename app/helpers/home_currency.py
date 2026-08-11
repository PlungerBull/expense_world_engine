"""Native → home currency conversion for set-based reads, expressed in SQL.

The single place the conversion rule is written for reports, dashboards and list
endpoints. Callers splice these fragments into their existing queries rather than
restructuring around them; the point is that every read path converts *the same
way*, instead of each one growing its own copy (which is how the engine ended up
with four disagreeing home-value mechanisms — see docs/currency-model-decision.md).

Amounts are stored in each account's own currency. The home value is computed at
read time from ``exchange_rates`` using the rate on the transaction's date, with
the most recent rate on or before that date carried forward.


The caller's query contract
---------------------------

These fragments are not self-contained. A query using them must expose three
aliases, and they are part of the contract — the read paths and the parity test embed the
same scaffold rather than two::

    FROM expense_transactions t
    LEFT JOIN expense_bank_accounts a ON a.id = t.account_id
    <home_rate_join("$N")>          -- introduces r

``LEFT JOIN``, not ``JOIN``. Several aggregators (``routers/dashboard.py``'s
archived category and hashtag panels) reach transactions through a ``LEFT JOIN``
so that categories and hashtags with no transactions survive with zero totals. An
inner join to the accounts table would re-drop exactly those rows, one join later.
On such a row ``a.*`` is NULL, the lateral's ON is NULL, ``r.rate`` is NULL, and
UNCONVERTIBLE_FLAG_EXPR's ``t.id IS NOT NULL`` guard keeps the row at 0 rather
than reporting it as unconvertible.


NULL means unconvertible. Never COALESCE it.
--------------------------------------------

HOME_CENTS_EXPR yields NULL when a foreign-currency row has no resolvable rate.
That NULL is a signal, not a gap to be filled. The code this module replaces did::

    COALESCE(t.amount_home_cents, t.amount_cents)

which treats USD cents as PEN cents — a 3.58x understatement rendered without
complaint. Do not reintroduce it in any form.


The aggregation contract
------------------------

A per-row NULL does not survive aggregation, so the row-level policy above is only
half of it. Both SUM shapes this codebase uses swallow it silently:

  * ``SUM(signed_home_cents)`` — SUM skips NULLs, so the total quietly omits the
    unconvertible rows.
  * ``SUM(CASE WHEN signed_home_cents > 0 THEN signed_home_cents ELSE 0 END)``
    (the inflow/outflow totals) — ``NULL > 0`` is NULL, not true, so the row takes
    the ELSE arm and counts as **zero**. This shape cannot even fail loudly: a
    group where every row is unconvertible returns 0, not NULL.

So: **every SUM of a home expression is paired with SUM(UNCONVERTIBLE_FLAG_EXPR),
and a non-zero count makes the aggregate ``null`` rather than a partial total.**
This applies to the inflow/outflow totals just as much as to the category
breakdown — the totals are the shape that fails silently.

Scope note for the flag: the aliases ``a`` and ``r`` exist only inside the CTE
where the join lives, so interpolating UNCONVERTIBLE_FLAG_EXPR into an outer
``SELECT ... FROM signed_txns`` is a hard SQL error. Project it as a column inside
the CTE (``... AS is_unconvertible``) and sum it by name outside.

What the flag does *not* cover: it measures convertibility, not classifiability.
The ``ELSE 0`` arm of the sign matrix would drop a row whose ``transaction_type``
is out of range from both the native and home totals without raising the flag.
That was the one remaining silent drop until sql/020 made it unreachable —
``transaction_type`` is now ``NOT NULL`` with ``CHECK (… IN (1, 2))``, so the
only rows reaching ``ELSE`` are LEFT JOIN misses, which must score 0. See
``signed_expr``.


Rate dates resolve in the user's display_timezone
-------------------------------------------------

``expense_transactions.date`` is ``timestamptz``, so a bare ``::date`` would
resolve in the session TimeZone — which ``app/db.py`` never sets, making it
machine-dependent (``America/Lima`` on the current host, UTC elsewhere). The cast
is therefore done explicitly in the user's ``display_timezone``, the same zone
``monthly_report.compute_month_bounds`` uses to bucket months. That keeps a
transaction's rate date and its report month in agreement: a transaction at
``2026-03-31T23:00-05:00`` is counted in March *and* priced at the March 31 rate.

This does **not** match the write path, and that divergence is deliberate. Writes
resolve from ``body.date.date()``, at the *client's* UTC offset, which is not
recoverable from a stored ``timestamptz``. Its user-visible consequence is that a
near-midnight transaction may price on a different day than its stored value did.

``display_timezone`` is unvalidated user input (``text NOT NULL DEFAULT 'UTC'`` in
sql/002, settable via ``PUT /auth/settings`` with no IANA check), so it is a bind
parameter and never interpolated — which is why home_rate_join is a builder. Two
consequences worth knowing:

  * Python and SQL disagree on a bad value: ``compute_month_bounds`` catches the
    bad zone and falls back to UTC, while ``AT TIME ZONE`` raises and would 500.
    The root fix is validating ``display_timezone`` on write; that shipped with
    ``sql/024`` (helpers/validation.validate_timezone), so this covers only
    out-of-band DB writes.
  * Callers already have the value from ``settings.get_user_report_settings``
    (already called by ``routers/dashboard.py`` and ``routers/reports.py``). Reuse
    it; do not add a second settings loader.


Why the home currency is a literal
----------------------------------

``<home>`` is interpolated at import from ``app.constants.HOME_CURRENCY``, not
bound. Bind parameters carry positional ``$N`` indexes, and these fragments are
spliced into queries whose numbering differs, so an index baked into them cannot
work — the timezone escapes this only by having its placeholder passed in. Making
the home currency a builder parameter too would turn all five exports into
functions for a value that cannot vary.

Interpolation is safe *only* because sql/018 locks
``user_settings.main_currency`` to ``'PEN'``: it is a constant, not user input,
and never reaches these strings from a request. If that CHECK is ever lifted:

  * these constants become builder functions taking ``home_currency``, and
  * the ``a.currency_code = 'USD'`` guard in home_rate_join must be revisited at
    the same time (see its own note below).

Note the tension with sql/018's own comment, which argues the policy should live
at the ``main_currency`` chokepoint rather than as a ``'PEN'`` literal. Resolved
in favour of the constant for the reason above, with one obligation attached:
**callers must assert ``settings["main_currency"] == HOME_CURRENCY``.** They
already hold that value, it costs nothing, and it makes a lifted CHECK fail loudly
instead of silently pricing a non-PEN ledger in PEN.


The get_rate duplication
------------------------

``helpers/exchange_rate.get_rate`` expresses the same carry-forward rule in
Python, and it stays: account balances convert at *today's* rate, the account list
uses ``batch_get_rates`` to avoid an N+1, and ``GET /exchange-rates`` serves rates
directly. (Reconciliations used to be on that list; they report native only as of
``sql/021``, the read-time currency migration.) So the rule is
implemented twice, in two languages. **That is a real DRY violation, accepted
deliberately** — aggregates must run in SQL, and pulling every row into Python to
convert would be worse.

It is also exactly how audit finding WP1.2 happened: a rule implemented twice, the
copies drifted, and the surviving copy was the buggy one. The mitigation is
``tests/test_home_currency_parity.py``, which asserts the two resolve the same
rate row. If you change the resolution logic here, that test is what catches the
other half going stale.

Known, out of scope (audit WP1.7): ``_fetch_rate_from_db`` returns
``float(row["rate"])``, truncating the stored ``numeric`` to binary float, after
which Python's ``round()`` applies banker's rounding. These fragments keep full
``numeric`` precision and round half-away-from-zero. SQL and Python can therefore
differ by one cent on a *converted amount* even when they agree on the rate, which
is why the parity test compares rates rather than cents. The fix (``Decimal`` +
``ROUND_HALF_UP`` throughout) is deliberately out of scope for the rework and
remains unscheduled.
"""
from textwrap import indent

from app.constants import BASE_CURRENCY, HOME_CURRENCY, TransactionType

# Table aliases the fragments below reference. Part of the contract — callers
# and tests build their scaffold from these rather than hardcoding letters.
TXN_ALIAS = "t"
ACCOUNT_ALIAS = "a"
RATE_ALIAS = "r"


def home_rate_join(tz_placeholder: str) -> str:
    """Return a LEFT JOIN LATERAL resolving one rate per transaction row.

    ``tz_placeholder`` is the caller's positional placeholder for the user's
    ``display_timezone`` (e.g. ``"$4"``). It differs per query — the monthly
    report binds ``$1..$3`` already so the timezone is ``$4``, while the
    dashboard's archived aggregators bind only ``$1`` so it is ``$2`` — which is
    why this is a builder and no index is hardcoded here. The value must be
    bound, never interpolated: it is unvalidated user input.

    Introduces the alias ``r`` with a single column, ``r.rate``, NULL when the
    row needs no conversion or when no rate resolves.
    """
    return f"""LEFT JOIN LATERAL (
    SELECT er.rate
    FROM exchange_rates er
    WHERE er.base_currency   = '{BASE_CURRENCY}'
      AND er.target_currency = '{HOME_CURRENCY}'
      AND er.rate_date <= ({TXN_ALIAS}.date AT TIME ZONE {tz_placeholder})::date
    ORDER BY er.rate_date DESC
    LIMIT 1
) {RATE_ALIAS} ON {ACCOUNT_ALIAS}.currency_code <> '{HOME_CURRENCY}'
   AND {ACCOUNT_ALIAS}.currency_code = 'USD'"""


# The second ON clause is NOT redundant — it is the fail-closed guard. The
# subquery hardcodes base_currency = 'USD' and never references a.currency_code,
# so without it any non-home currency would silently receive the USD→PEN rate.
# That is correct today only because sql/015 locks the currency set to
# {USD, PEN}: a single CHECK constraint. Admit a third currency and the guard is
# what makes it fall to HOME_CENTS_EXPR's ELSE NULL arm — missing and flagged —
# instead of producing a confidently wrong number. Do not simplify it away.


# Unsigned home value, or NULL when the row cannot be converted.
#
# The ::bigint cast keeps the expression one type: amount_cents is bigint but
# round(bigint * numeric) is numeric, so without it the CASE result type would
# depend on which arm fired and per-row projections would surface Decimal. It
# changes no value.
HOME_CENTS_EXPR = f"""CASE
    WHEN {ACCOUNT_ALIAS}.currency_code = '{HOME_CURRENCY}'
        THEN {TXN_ALIAS}.amount_cents
    WHEN {RATE_ALIAS}.rate IS NOT NULL
        THEN round({TXN_ALIAS}.amount_cents * {RATE_ALIAS}.rate)::bigint
    ELSE NULL
END"""


def signed_expr(magnitude: str) -> str:
    """Apply the sign matrix to an unsigned magnitude expression.

    Inflows are positive, outflows negative. That is the entire rule: after
    WP1 every row is an ordinary row and direction is the only branch,
    where this used to need four arms to express two outcomes.

    **This is the only rendering of the sign matrix in the engine.** It used to
    be one of five — two literal copies in helpers/monthly_report.py, two in
    routers/dashboard.py, and this builder, which nothing imported. That is
    audit finding WP9.1, whose stated risk was /dashboard and /reports/monthly
    disagreeing about the same month. Callers pass their own magnitude:
    ``HOME_CENTS_EXPR`` for the converted form (what ``helpers/monthly_report``
    uses), or ``t.amount_cents`` for the native one.

    Integers come from app.constants rather than being written as literals, so a
    renumbering cannot silently desync the SQL from the enum (audit WP9.9).

    On ``ELSE 0``: this is not a fail-open arm. ``transaction_type`` is
    ``NOT NULL`` with ``CHECK (transaction_type IN (1, 2))`` (sql/020), so no
    stored row can miss both branches. The only way to reach ``ELSE`` is a
    LEFT JOIN miss — a category or hashtag with no transactions at all, where
    ``t.*`` is entirely NULL — and those must report 0, not NULL, which is the
    invariant the caller contract's LEFT JOIN exists to preserve. Before
    sql/020 this arm also swallowed rows whose direction the engine could not
    read; that state is now unrepresentable.
    """
    outflow = int(TransactionType.OUTFLOW)
    inflow = int(TransactionType.INFLOW)
    ttype = f"{TXN_ALIAS}.transaction_type"
    # Nested one level so the composed expression stays readable in EXPLAIN
    # output and in the queries the read paths splice it into.
    body = indent(magnitude, " " * 8)

    return f"""CASE
    WHEN {ttype} = {inflow} THEN (
{body}
    )
    WHEN {ttype} = {outflow} THEN -(
{body}
    )
    ELSE 0
END"""


# Native signed amount — the account's own currency, no conversion.
SIGNED_CENTS_EXPR = signed_expr(f"{TXN_ALIAS}.amount_cents")

# Home signed amount. Wraps HOME_CENTS_EXPR by reference rather than re-deriving
# the multiplication with a sign inside it, so there is exactly one definition of
# the conversion to change. (Numerically either order works: Postgres
# round(numeric) is half-away-from-zero, an odd function, so round(-x * r) and
# -round(x * r) agree for every input. The rule is about single definition, and
# it matches the magnitude-then-round convention in helpers/transactions.py.)
SIGNED_HOME_CENTS_EXPR = signed_expr(HOME_CENTS_EXPR)


# 1 when a real transaction row has no home value, else 0. See "The aggregation
# contract" above: this is not optional, and it is not an outer-SELECT expression.
#
# The `t.id IS NOT NULL` guard is required, not defensive. Under the LEFT JOINs
# described in the caller contract, a category or hashtag with no transactions
# produces a row where t.* and a.* are all NULL, so HOME_CENTS_EXPR falls to its
# ELSE NULL arm — indistinguishable from a genuinely unconvertible row. Without
# the guard every empty archived category would report spent_home_cents: null
# instead of 0, contradicting the invariant its LEFT JOIN exists to preserve.
UNCONVERTIBLE_FLAG_EXPR = f"""CASE
    WHEN {TXN_ALIAS}.id IS NOT NULL AND (
{indent(HOME_CENTS_EXPR, " " * 8)}
    ) IS NULL THEN 1
    ELSE 0
END"""
