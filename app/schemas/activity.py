import json
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel

from app.constants import ActivityAction


class ActivityLogResponse(BaseModel):
    id: str
    user_id: str
    resource_type: str
    resource_id: str
    # created/updated/deleted/restored; sql/029 CHECKs the column.
    action: ActivityAction
    before_snapshot: Optional[Any] = None
    after_snapshot: Optional[Any] = None
    changed_by: str
    created_at: datetime


def _parse_snapshot(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def activity_from_row(row) -> dict:
    return ActivityLogResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        resource_type=row["resource_type"],
        resource_id=str(row["resource_id"]),
        action=row["action"],
        before_snapshot=_parse_snapshot(row["before_snapshot"]),
        after_snapshot=_parse_snapshot(row["after_snapshot"]),
        changed_by=str(row["changed_by"]),
        created_at=row["created_at"],
    ).model_dump(mode="json")
