"""fetch_recon_status is tenant-scoped.

Bloat audit 2026-08-06, Correctness §1: three reconciliation-status reads in
helpers/transactions.py ran with no user_id predicate. They are post-fetch
coherence reads on ids taken from already-scoped rows, so no over-the-wire
request can reach them with a foreign id — which is exactly why this pins the
helper directly: the predicate is a defense-in-depth invariant (CLAUDE.md:
a missing user_id filter is a security defect), not an observable behaviour.
"""
import uuid

import pytest

from app import db
from app.constants import ReconciliationStatus
from app.helpers.transactions import fetch_recon_status


@pytest.mark.asyncio
async def test_fetch_recon_status_is_tenant_scoped(test_data):
    user_b = str(uuid.uuid4())
    account_b = str(uuid.uuid4())
    recon_b = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        try:
            await conn.execute(
                """INSERT INTO users (id, display_name, created_at, updated_at)
                   VALUES ($1, 'tenant-scope-user-b', now(), now())""",
                user_b,
            )
            await conn.execute(
                """INSERT INTO expense_bank_accounts
                    (id, user_id, name, currency_code, is_person, color,
                     is_archived, sort_order, created_at, updated_at)
                   VALUES ($1, $2, 'B Account', 'PEN', false, '#000000',
                     false, 1, now(), now())""",
                account_b, user_b,
            )
            await conn.execute(
                """INSERT INTO expense_reconciliations
                    (id, user_id, account_id, name, status,
                     beginning_balance_cents, ending_balance_cents,
                     created_at, updated_at)
                   VALUES ($1, $2, $3, 'B recon', $4, 0, 0, now(), now())""",
                recon_b, user_b, account_b, int(ReconciliationStatus.DRAFT),
            )

            # Another tenant's reconciliation is invisible…
            assert await fetch_recon_status(conn, test_data.user_id, recon_b) is None
            # …while the owner sees it.
            owned = await fetch_recon_status(conn, user_b, recon_b)
            assert owned is not None
            assert owned["status"] == ReconciliationStatus.DRAFT

            # Soft-deleted rows are still returned — restore_transaction
            # distinguishes "recon deleted" from "recon completed", so the
            # helper must not filter on deleted_at.
            await conn.execute(
                "UPDATE expense_reconciliations SET deleted_at = now() WHERE id = $1",
                recon_b,
            )
            deleted = await fetch_recon_status(conn, user_b, recon_b)
            assert deleted is not None
            assert deleted["deleted_at"] is not None
        finally:
            await conn.execute(
                "DELETE FROM expense_reconciliations WHERE user_id = $1", user_b
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE user_id = $1", user_b
            )
            await conn.execute("DELETE FROM users WHERE id = $1", user_b)
