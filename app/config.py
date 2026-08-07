import os
from urllib.parse import urlparse

from pydantic_settings import BaseSettings

# Hosts that count as "this machine". An empty hostname covers socket-style
# URLs (postgresql:///expense_world), which are local by construction.
_LOCAL_DB_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})

_REMOTE_DB_OPT_IN = "EXPENSE_ALLOW_REMOTE_DB"


class Settings(BaseSettings):
    supabase_db_url: str

    # `supabase_url` and `supabase_jwt_secret` were removed 2026-08-03 with the
    # JWT auth branch (audit 2.1). They had no consumer outside it, and the
    # secret's committed placeholder was itself the vulnerability — a required
    # setting nobody sets meaningfully becomes a published constant.

    # Connection pool — defaults are sized for the ACTIVE (local) profile:
    # a direct connection to Homebrew Postgres, where every pool slot pins a
    # real backend connection and must stay well under `max_connections`.
    #
    # Cloud profile (Supabase pgBouncer transaction-mode pooler, port 6543):
    # raise max_size to ~50. The pooler multiplexes client conns onto a much
    # smaller real-connection set, so there max_size is a logical limit rather
    # than a real-Postgres one. Override via DB_POOL_MAX_SIZE — see
    # .env.example and deploy/cloud/README.md.
    db_pool_min_size: int = 5
    db_pool_max_size: int = 20
    # Upper bound (seconds) on any single statement issued through the pool.
    # Bounds request-serving queries only — the jobs in app/jobs/ build their
    # own pools without it, since a backfill may legitimately run long.
    db_command_timeout: float = 30.0

    model_config = {"env_file": ".env"}

    def model_post_init(self, __context) -> None:
        """Refuse to start against a non-local database unless told to.

        Under the local profile the ledger lives on this machine, but a stale
        or mistyped SUPABASE_DB_URL would otherwise connect somewhere else and
        work silently — the failure has no symptom until the books disagree.
        This matters most for the test suite: tests/conftest.py imports these
        settings and its fixtures insert and delete rows, so a wrong address
        there mutates the wrong database. Cloud deployments are legitimate and
        opt in via EXPENSE_ALLOW_REMOTE_DB=1 (see deploy/cloud/README.md).
        """
        host = urlparse(self.supabase_db_url).hostname or ""
        if host in _LOCAL_DB_HOSTS or os.environ.get(_REMOTE_DB_OPT_IN) == "1":
            return
        raise RuntimeError(
            f"Refusing to connect: SUPABASE_DB_URL points at the non-local host "
            f"{host!r}, but this is the local deployment profile. If that is "
            f"deliberate (a cloud deployment), set {_REMOTE_DB_OPT_IN}=1. "
            f"Otherwise fix SUPABASE_DB_URL — see deploy/local/README.md."
        )


settings = Settings()
