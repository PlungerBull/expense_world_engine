from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query

from app import db
from app.deps import CurrentUser, Limit, Offset
from app.errors import ERROR_RESPONSES
from app.helpers.pagination import DEFAULT_LIMIT, list_page, paginated_response
from app.schemas.activity import ActivityLogResponse, activity_from_row
from app.schemas.pagination import Paginated

router = APIRouter(prefix="/activity", tags=["activity"], responses=ERROR_RESPONSES)


@router.get("", response_model=Paginated[ActivityLogResponse])
async def list_activity(
    auth_user: CurrentUser,
    resource_type: Optional[str] = Query(None),
    resource_id: Optional[UUID] = Query(None),
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
):

    conditions = ["user_id = $1"]
    params: list = [auth_user.id]

    if resource_type is not None:
        params.append(resource_type)
        conditions.append(f"resource_type = ${len(params)}")

    if resource_id is not None:
        params.append(resource_id)
        conditions.append(f"resource_id = ${len(params)}")

    async with db.pool.acquire() as conn:
        rows, total = await list_page(
            conn,
            from_sql="activity_log",
            conditions=conditions,
            params=params,
            order_by="created_at DESC",
            limit=limit,
            offset=offset,
            select=(
                "id, user_id, resource_type, resource_id, action, "
                "before_snapshot, after_snapshot, changed_by, created_at"
            ),
        )

        data = [activity_from_row(row) for row in rows]
        return paginated_response(data, total, limit, offset)
