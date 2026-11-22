from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends

from app.routers.auth import get_current_staff, require_roles
from app.schemas.queue import BoardRequest, QueueResponse
from app.services.queue_service import (
    board_passenger,
    call_next,
    call_passenger,
    list_queue,
    mark_no_show,
)

router = APIRouter(prefix="/queues", tags=["Queues"])
OPERATORS = ("super_admin", "terminal_admin", "booking_operator")


@router.get("", response_model=List[QueueResponse])
def get_queue(
    staff: Annotated[dict, Depends(get_current_staff)],
    route_id: Optional[str] = None,
    status: Optional[str] = None,
):
    return [QueueResponse(**doc) for doc in list_queue(staff, route_id, status)]


@router.post("/{booking_reference}/call", response_model=QueueResponse)
def call_one(
    booking_reference: str,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return QueueResponse(**call_passenger(booking_reference, staff))


@router.post("/routes/{route_id}/call-next", response_model=QueueResponse)
def call_route_next(
    route_id: str,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return QueueResponse(**call_next(route_id, staff))


@router.post("/{booking_reference}/board", response_model=QueueResponse)
def board(
    booking_reference: str,
    data: BoardRequest,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return QueueResponse(**board_passenger(booking_reference, data.ticket_number, data.token, staff))


@router.post("/{booking_reference}/no-show", response_model=QueueResponse)
def no_show(
    booking_reference: str,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return QueueResponse(**mark_no_show(booking_reference, staff))