from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TicketVerifyRequest(BaseModel):
    ticket_number: str
    token: str


class TicketResponse(BaseModel):
    id: str
    ticket_number: str
    booking_reference: str
    passenger_name: str
    origin: str
    destination: str
    fare: str
    terminal_id: str
    status: str
    qr_payload: str
    qr_png_base64: str
    verification_token: str  # <--- Added this line
    issued_at: datetime
    cancelled_at: Optional[datetime] = None


class TicketVerifyResponse(BaseModel):
    valid: bool
    ticket_number: str
    origin: Optional[str] = None
    destination: Optional[str] = None
    status: Optional[str] = None
    message: str