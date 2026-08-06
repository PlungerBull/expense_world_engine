from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class ActivityLogResponse(BaseModel):
    id: str
    user_id: str
    resource_type: str
    resource_id: str
    action: int
    before_snapshot: Optional[Any] = None
    after_snapshot: Optional[Any] = None
    changed_by: str
    created_at: datetime
