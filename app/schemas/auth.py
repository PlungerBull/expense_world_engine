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
    created_at: datetime
    updated_at: datetime


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
