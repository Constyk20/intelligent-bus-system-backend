from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends

from app.routers.auth import get_current_staff, require_roles
from app.schemas.booking import BookingCreate, BookingResponse
from app.services.booking_service import create_booking, get_booking_by_reference, list_bookings
from app.services.passenger_service import get_passenger

router = APIRouter(prefix="/bookings", tags=["Bookings"])
OPERATORS = ("super_admin", "terminal_admin", "booking_operator")


def _response(doc: dict) -> BookingResponse:
    name = doc.get("passenger_name")
    card_number = doc.get("card_number") or ""
    if not name:
        name = get_passenger(doc["passenger_id"])["full_name"]
    return BookingResponse(
        id=str(doc["_id"]),
        booking_reference=doc["booking_reference"],
        ticket_number=doc.get("ticket_number"),
        passenger_id=doc["passenger_id"],
        passenger_name=name,
        card_id=doc["card_id"],
        card_number=card_number,
        route_id=doc["route_id"],
        terminal_id=doc["terminal_id"],
        origin=doc["origin"],
        destination=doc["destination"],
        fare=doc["fare"],
        balance_after=doc["balance_after"],
        status=doc["status"],
        payment_status=doc["payment_status"],
        transaction_reference=doc["transaction_reference"],
        created_at=doc["created_at"],
    )


@router.post("", response_model=BookingResponse, status_code=201)
def book(
    data: BookingCreate,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return _response(create_booking(data, staff))


@router.get("", response_model=List[BookingResponse])
def all_bookings(
    staff: Annotated[dict, Depends(get_current_staff)],
    terminal_id: Optional[str] = None,
):
    return [_response(doc) for doc in list_bookings(staff, terminal_id)]


@router.get("/{booking_reference}", response_model=BookingResponse)
def one_booking(
    booking_reference: str,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return _response(get_booking_by_reference(booking_reference, staff))