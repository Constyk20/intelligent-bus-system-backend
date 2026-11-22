from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class TripCreate(BaseModel):
    route_id: str
    bus_id: str
    driver_id: str


class TripAdvance(BaseModel):
    progress_percent: int = Field(..., ge=0, le=100)


class ArrivalSimulation(BaseModel):
    route_id: str
    full_name: str = Field(..., min_length=2)
    phone: str = Field(..., min_length=7)
    initial_balance: str = "5000.00"


class TripResponse(BaseModel):
    id: str
    trip_reference: str
    terminal_id: str
    route_id: str
    origin: str
    destination: str
    bus_id: str
    driver_id: str
    registration_number: Optional[str] = None
    driver_name: Optional[str] = None
    status: str
    capacity: int
    passenger_count: int
    boarded_booking_references: List[str]
    progress_percent: float  # <--- CHANGED FROM int TO float
    simulated: bool = True
    created_at: datetime
    departed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None