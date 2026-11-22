from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class RecommendationResponse(BaseModel):
    id: str
    terminal_id: str
    route_id: str
    origin: str
    destination: str
    waiting_passengers: int
    oldest_wait_minutes: int
    recent_bookings: int
    priority_score: int
    predicted_passengers: float = 0
    recommended_bus_id: Optional[str] = None
    registration_number: Optional[str] = None
    bus_capacity: Optional[int] = None
    recommended_driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    reason: str
    status: str
    trip_reference: Optional[str] = None
    created_at: datetime