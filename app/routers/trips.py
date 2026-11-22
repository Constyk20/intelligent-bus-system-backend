from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends

from app.routers.auth import get_current_staff, require_roles
from app.schemas.trip import ArrivalSimulation, TripAdvance, TripCreate, TripResponse
from app.services.arrival_service import simulate_arrival
from app.services.trip_service import (
    advance_trip,
    cancel_trip,
    complete_trip,
    create_trip,
    dispatch_trip,
    get_trip,
    list_trips,
)

router = APIRouter(tags=["Trips and Simulation"])
MANAGERS = ("super_admin", "terminal_admin")
OPERATORS = ("super_admin", "terminal_admin", "booking_operator")


@router.post("/trips", response_model=TripResponse, status_code=201)
def create(
    data: TripCreate,
    staff: Annotated[dict, Depends(require_roles(*MANAGERS))],
):
    return TripResponse(**create_trip(data, staff))


@router.get("/trips", response_model=List[TripResponse])
def all_trips(
    staff: Annotated[dict, Depends(get_current_staff)],
    status: Optional[str] = None,
):
    return [TripResponse(**doc) for doc in list_trips(staff, status)]


@router.get("/trips/{trip_reference}", response_model=TripResponse)
def one_trip(
    trip_reference: str,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return TripResponse(**get_trip(trip_reference, staff))


@router.post("/trips/{trip_reference}/dispatch", response_model=TripResponse)
def dispatch(
    trip_reference: str,
    staff: Annotated[dict, Depends(require_roles(*MANAGERS))],
):
    return TripResponse(**dispatch_trip(trip_reference, staff))


@router.post("/trips/{trip_reference}/advance", response_model=TripResponse)
def advance(
    trip_reference: str,
    data: TripAdvance,
    staff: Annotated[dict, Depends(require_roles(*MANAGERS))],
):
    return TripResponse(**advance_trip(trip_reference, data.progress_percent, staff))


@router.post("/trips/{trip_reference}/complete", response_model=TripResponse)
def complete(
    trip_reference: str,
    staff: Annotated[dict, Depends(require_roles(*MANAGERS))],
):
    return TripResponse(**complete_trip(trip_reference, staff))


@router.post("/trips/{trip_reference}/cancel", response_model=TripResponse)
def cancel(
    trip_reference: str,
    staff: Annotated[dict, Depends(require_roles(*MANAGERS))],
):
    return TripResponse(**cancel_trip(trip_reference, staff))


@router.post("/simulation/arrivals", status_code=201)
def arrival(
    data: ArrivalSimulation,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return simulate_arrival(data, staff)