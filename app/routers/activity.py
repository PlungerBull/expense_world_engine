from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query

from app import db
from app.deps import CurrentUser
from app.errors import ERROR_RESPONSES
from app.helpers.pagination import paginated_response
from app.schemas.activity import ActivityLogResponse, activity_from_row
from app.schemas.pagination import Paginated

router = APIRouter(prefix="/activity", tags=["activity"], responses=ERROR_RESPONSES)


@router.get("", response_model=Paginated[ActivityLogResponse])
async def list_activity(
    auth_user: CurrentUser,
    resource_type: Optional[str] = Query(None),
    resource_id: Optional[UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):

    conditions = ["user_id = $1"]
    params: list = [auth_user.id]

    if resource_type is not None:
        conditions.append(f"resource_type = ${len(params) + 1}")
        params.append(resource_type)

    if resource_id is not None:
        conditions.append(f"resource_id = ${len(params) + 1}")
        params.append(resource_id)

    where = " AND ".join(conditions)

    async with db.pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM activity_log WHERE {where}", *params
        )

        rows = await conn.fetch(
            f"""
            SELECT id, user_id, resource_type, resource_id, action,
                   before_snapshot, after_snapshot, changed_by, created_at
            FROM activity_log
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

        data = [activity_from_row(row) for row in rows]
        return paginated_response(data, total, limit, offset)
