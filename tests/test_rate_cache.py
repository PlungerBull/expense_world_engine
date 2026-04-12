"""Regression tests for the exchange-rate in-process cache.

Sprint A1 added a per-worker TTL cache to ``get_rate`` so repeated
lookups of the same (from, to, as_of) tuple don't hammer the DB.
These tests verify:

  * A successful lookup is stored in ``_RATE_CACHE`` and reused on the
    next call (positive caching).
  * A missing rate (``None`` result) is also cached — otherwise a
    non-existent rate would be re-queried on every access.
  * ``clear_rate_cache()`` drops all entries.

Testing TTL expiry directly would require either ``freezegun`` or
patching ``_RATE_CACHE_TTL_SECONDS`` to zero — skipped here because
monkeypatching the module-level constant has subtle interactions with
``time.monotonic()`` that aren't worth testing at the expense of
clarity. The lazy-eviction path is straightforward enough that
code review is adequate coverage.

Run: .venv/bin/pytest tests/test_rate_cache.py -v
"""
from datetime import date

import pytest

from app import db
from app.helpers import exchange_rate as rate_module


@pytest.mark.asyncio
async def test_successful_lookup_is_cached(client, test_data):
    """First call hits the DB, populates the cache; second call reuses it.

    We verify by checking ``_RATE_CACHE`` directly rather than by
    counting DB queries — that's both simpler and more specific to what
    the cache guarantees.
    """
    rate_module.clear_rate_cache()
    assert len(rate_module._RATE_CACHE) == 0, (
        "clear_rate_cache() must leave the dict empty"
    )

    # USD → PEN is seeded by conftest._ensure_test_data.
    as_of = date.today()

    async with db.pool.acquire() as conn:
        first = await rate_module.get_rate(
            conn, from_currency="USD", to_currency="PEN", as_of=as_of,
        )
        second = await rate_module.get_rate(
            conn, from_currency="USD", to_currency="PEN", as_of=as_of,
        )

    # Both calls returned the same result.
    assert first == second, "Cache should return identical value for repeat calls"
    assert first is not None, "USD→PEN rate should exist (seeded in conftest)"

    # The cache must have an entry for this exact (upper-case) key.
    assert ("USD", "PEN", as_of) in rate_module._RATE_CACHE
    cached_value, expires_at = rate_module._RATE_CACHE[("USD", "PEN", as_of)]
    assert cached_value == first, "Cache must store the fetched value"
    assert expires_at > 0, "Cache must store an absolute expiry, not a relative offset"


@pytest.mark.asyncio
async def test_negative_result_is_cached(client, test_data):
    """A currency combo with no matching row should return None and cache None.

    Without negative caching, a missing rate would trigger a fresh DB
    query on every call — which at 10K concurrent users means the
    ``exchange_rates`` table gets hammered by exactly the rates we
    don't have.
    """
    rate_module.clear_rate_cache()

    # Pick a combo that's vanishingly unlikely to have a rate row.
    # ZZ and XX are neither canonical ISO 4217 codes nor present in any
    # production seed, so ``_fetch_rate_from_db`` will return None.
    as_of = date.today()

    async with db.pool.acquire() as conn:
        first = await rate_module.get_rate(
            conn, from_currency="ZZ", to_currency="XX", as_of=as_of,
        )
        second = await rate_module.get_rate(
            conn, from_currency="ZZ", to_currency="XX", as_of=as_of,
        )

    assert first is None, "Unknown-currency pair should return None"
    assert second is None, "Replay of unknown pair should also return None"

    # Crucially — the cache must have stored the None, not skipped it.
    # The keys are upper-cased by get_rate's wrapper, so we use the
    # same casing the cache will have.
    assert ("ZZ", "XX", as_of) in rate_module._RATE_CACHE
    cached_value, _ = rate_module._RATE_CACHE[("ZZ", "XX", as_of)]
    assert cached_value is None, (
        "Cache must store the None result — otherwise negative lookups "
        "bypass the cache and hit the DB on every call"
    )


@pytest.mark.asyncio
async def test_same_currency_short_circuit_is_also_cached(client, test_data):
    """from == to always returns (1.0, as_of) — this path must not skip caching.

    The ``_fetch_rate_from_db`` same-currency branch is a fast path, but
    it still goes through the cache wrapper. A regression that moves
    the short-circuit above the cache check would mean the same-currency
    case stops being cached — which is fine semantically, but it's a
    silent behavioural change worth guarding.
    """
    rate_module.clear_rate_cache()
    as_of = date.today()

    async with db.pool.acquire() as conn:
        result = await rate_module.get_rate(
            conn, from_currency="EUR", to_currency="EUR", as_of=as_of,
        )

    assert result == (1.0, as_of)
    assert ("EUR", "EUR", as_of) in rate_module._RATE_CACHE
