"""JWKS fetching + in-process caching for Supabase's asymmetric JWTs.

Modern Supabase projects sign user JWTs with ES256 using keys published
at ``<supabase_url>/auth/v1/.well-known/jwks.json``. The engine must
fetch and cache these public keys to verify incoming tokens.

Cache strategy: keys rotate rarely, and every JWT carries the ``kid`` of
the key that signed it. We cache by ``kid`` and refetch the whole JWKS
only on a cache miss — a new kid always means "Supabase rotated, go look
again." No TTL, no background refresh.

Sync ``urllib`` is used intentionally: the fetch happens at most once per
kid per process lifetime (typically exactly once, at the first
authenticated request after deploy), so the blocking I/O cost is
amortised to zero across the process's lifetime. Every subsequent
request is a dict lookup with no I/O.
"""

import json
import urllib.request
from typing import Optional

from app.config import settings


_cache: dict[str, dict] = {}


def _jwks_url() -> str:
    return f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"


def _fetch_jwks() -> dict:
    with urllib.request.urlopen(_jwks_url(), timeout=5) as response:
        return json.loads(response.read())


def get_jwk(kid: str) -> Optional[dict]:
    """Return the JWK matching ``kid``, fetching on cache miss.

    Returns ``None`` when the kid is not present in the upstream JWKS —
    callers should treat that as an auth failure (unknown signing key).
    """
    if kid in _cache:
        return _cache[kid]

    # Miss — refresh the cache. Populate every key in the response so
    # subsequent lookups for other active kids also hit without an
    # extra network round-trip.
    jwks = _fetch_jwks()
    for key in jwks.get("keys", []):
        if "kid" in key:
            _cache[key["kid"]] = key
    return _cache.get(kid)


def _reset_cache_for_tests() -> None:
    """Test-only hook. Clears the in-process cache."""
    _cache.clear()
