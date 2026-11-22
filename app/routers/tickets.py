from typing import Annotated

from fastapi import APIRouter, Depends

from app.routers.auth import require_roles
from app.schemas.ticket import TicketResponse, TicketVerifyRequest, TicketVerifyResponse
from app.services.ticket_service import cancel_booking, get_ticket, verify_ticket

router = APIRouter(prefix="/tickets", tags=["Tickets"])
OPERATORS = ("super_admin", "terminal_admin", "booking_operator")


def _response(doc: dict) -> TicketResponse:
    return TicketResponse(**doc)


@router.get("/{ticket_number}", response_model=TicketResponse)
def read_ticket(
    ticket_number: str,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return _response(get_ticket(ticket_number, staff))


@router.post("/verify", response_model=TicketVerifyResponse)
def verify(
    data: TicketVerifyRequest,
    _: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return TicketVerifyResponse(**verify_ticket(data.ticket_number, data.token))


@router.post("/{booking_reference}/cancel", response_model=TicketResponse)
def cancel(
    booking_reference: str,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return _response(cancel_booking(booking_reference, staff))