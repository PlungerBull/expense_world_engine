from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class BootstrapRequest(BaseModel):
    display_name: str
    timezone: str


class UserResponse(BaseModel):
    id: str
    email: Optional[str]
    display_name: Optional[str]
    last_login_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class UserSettingsResponse(BaseModel):
    user_id: str
    theme: int
    start_of_week: int
    main_currency: str
    transaction_sort_preference: int
    display_timezone: str
    sidebar_show_bank_accounts: bool
    sidebar_show_people: bool
    sidebar_show_categories: bool
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime]


class BootstrapResponse(BaseModel):
    user: UserResponse
    settings: UserSettingsResponse


class SettingsUpdateRequest(BaseModel):
    theme: Optional[int] = None
    start_of_week: Optional[int] = None
    main_currency: Optional[str] = None
    transaction_sort_preference: Optional[int] = None
    display_timezone: Optional[str] = None
    sidebar_show_bank_accounts: Optional[bool] = None
    sidebar_show_people: Optional[bool] = None
    sidebar_show_categories: Optional[bool] = None


class ProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None


def user_from_row(row) -> dict:
    return UserResponse(
        id=str(row["id"]),
        email=row["email"],
        display_name=row["display_name"],
        last_login_at=row["last_login_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    ).model_dump(mode="json")


def settings_from_row(row) -> dict:
    return UserSettingsResponse(
        user_id=str(row["user_id"]),
        theme=row["theme"],
        start_of_week=row["start_of_week"],
        main_currency=row["main_currency"],
        transaction_sort_preference=row["transaction_sort_preference"],
        display_timezone=row["display_timezone"],
        sidebar_show_bank_accounts=row["sidebar_show_bank_accounts"],
        sidebar_show_people=row["sidebar_show_people"],
        sidebar_show_categories=row["sidebar_show_categories"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")
