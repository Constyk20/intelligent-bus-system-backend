from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field


class TerminalCreate(BaseModel):
    name: str = Field(..., min_length=2)
    code: str = Field(..., min_length=2, max_length=20)
    city: str
    state: str
    address: Optional[str] = None


class TerminalUpdate(BaseModel):
    name: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    address: Optional[str] = None
    is_active: Optional[bool] = None


class TerminalResponse(BaseModel):
    id: str
    name: str
    code: str
    city: str
    state: str
    address: Optional[str] = None
    is_active: bool
    created_at: datetime


class RouteCreate(BaseModel):
    terminal_id: str
    origin: str
    destination: str
    fare: str = Field(..., description="Decimal string, e.g. 1500.00")
    estimated_duration_minutes: int = Field(..., gt=0)
    distance_km: Optional[float] = None


class RouteUpdate(BaseModel):
    origin: Optional[str] = None
    destination: Optional[str] = None
    fare: Optional[str] = None
    estimated_duration_minutes: Optional[int] = Field(None, gt=0)
    distance_km: Optional[float] = None
    is_active: Optional[bool] = None


class RouteResponse(BaseModel):
    id: str
    terminal_id: str
    origin: str
    destination: str
    fare: str
    estimated_duration_minutes: int
    distance_km: Optional[float] = None
    is_active: bool
    created_at: datetime


BusStatus = Literal["available", "on_trip", "maintenance", "inactive"]


class BusCreate(BaseModel):
    registration_number: str = Field(..., min_length=3)
    terminal_id: str
    capacity: int = Field(..., gt=0)
    model: Optional[str] = None
    status: BusStatus = "available"


class BusUpdate(BaseModel):
    terminal_id: Optional[str] = None
    capacity: Optional[int] = Field(None, gt=0)
    model: Optional[str] = None
    status: Optional[BusStatus] = None
    is_active: Optional[bool] = None


class BusResponse(BaseModel):
    id: str
    registration_number: str
    terminal_id: str
    capacity: int
    model: Optional[str] = None
    status: BusStatus
    is_active: bool
    created_at: datetime


class DriverCreate(BaseModel):
    full_name: str = Field(..., min_length=2)
    phone: str
    license_number: str
    terminal_id: str


class DriverUpdate(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    terminal_id: Optional[str] = None
    is_active: Optional[bool] = None


class DriverResponse(BaseModel):
    id: str
    full_name: str
    phone: str
    license_number: str
    terminal_id: str
    is_active: bool
    created_at: datetime