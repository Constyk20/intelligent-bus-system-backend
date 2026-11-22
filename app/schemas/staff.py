from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, EmailStr, Field


Role = Literal["super_admin", "terminal_admin", "booking_operator"]


class StaffCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    full_name: str = Field(..., min_length=2)
    role: Role
    terminal_id: Optional[str] = None   # required later for terminal_admin / operator


class StaffBootstrap(BaseModel):
    """Used only when no staff exist yet"""
    email: EmailStr
    password: str = Field(..., min_length=8)
    full_name: str = Field(..., min_length=2)


class StaffLogin(BaseModel):
    email: EmailStr
    password: str


class StaffResponse(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    role: Role
    terminal_id: Optional[str] = None
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime] = None

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    staff: StaffResponse