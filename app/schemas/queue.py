from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class QueueResponse(BaseModel):
    id: str
    queue_number: int
    booking_reference: str
    ticket_number: str
    passenger_id: str
    passenger_name: Optional[str] = None
    terminal_id: str
    route_id: str
    origin: str
    destination: str
    status: str
    trip_reference: Optional[str] = None
    trip_status: Optional[str] = None
    created_at: datetime
    called_at: Optional[datetime] = None
    boarded_at: Optional[datetime] = None


class BoardRequest(BaseModel):
    ticket_number: str
    token: str