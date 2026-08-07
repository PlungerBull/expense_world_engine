from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.schemas import StrictModel


class BootstrapRequest(StrictModel):
    display_name: str
    timezone: str


class UserResponse(BaseModel):
    id: str
    display_name: Optional[str]
    last_login_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class UserSettingsResponse(BaseModel):
    user_id: str
    main_currency: str
    display_timezone: str
    version: int
    created_at: datetime
    updated_at: datetime


class BootstrapResponse(BaseModel):
    user: UserResponse
    settings: UserSettingsResponse


class SettingsUpdateRequest(StrictModel):
    # Not updatable — the home currency is locked to PEN (sql/018). Declared
    # here only so update_settings can reject it with a 422; dropping the
    # field would make Pydantic reject it as unknown with a generic message,
    # and this one names the real rule.
    main_currency: Optional[str] = None
    display_timezone: Optional[str] = None


class ProfileUpdateRequest(StrictModel):
    display_name: Optional[str] = None


def user_from_row(row) -> dict:
    return UserResponse(
        id=str(row["id"]),
        display_name=row["display_name"],
        last_login_at=row["last_login_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    ).model_dump(mode="json")


def settings_from_row(row) -> dict:
    return UserSettingsResponse(
        user_id=str(row["user_id"]),
        main_currency=row["main_currency"],
        display_timezone=row["display_timezone"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    ).model_dump(mode="json")
