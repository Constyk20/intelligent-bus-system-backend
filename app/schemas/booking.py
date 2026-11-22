from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class BookingCreate(BaseModel):
    card_number: str = Field(..., min_length=4)
    route_id: str
    idempotency_key: Optional[str] = Field(
        None,
        description="Send the same key if the operator retries the same booking",
    )


class BookingResponse(BaseModel):
    id: str
    booking_reference: str
    ticket_number: Optional[str] = None
    passenger_id: str
    passenger_name: Optional[str] = None
    card_id: str
    card_number: str
    route_id: str
    terminal_id: str
    origin: str
    destination: str
    fare: str
    balance_after: str
    status: str
    payment_status: str
    transaction_reference: str
    created_at: datetime