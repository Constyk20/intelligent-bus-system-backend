"""
API for the autopilot, the demand forecast, and one-step boarding.

Registered in main.py with:
    app.include_router(autopilot.router)
"""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.routers.auth import get_current_staff, require_roles
from app.schemas.queue import QueueResponse
from app.services import autopilot_service as autopilot
from app.services.queue_service import board_by_scan, quick_board

router = APIRouter(tags=["Autopilot"])

OPERATORS = ("super_admin", "terminal_admin", "booking_operator")
MANAGERS = ("super_admin", "terminal_admin")

AnyStaff = Annotated[dict, Depends(get_current_staff)]
Operator = Annotated[dict, Depends(require_roles(*OPERATORS))]
Manager = Annotated[dict, Depends(require_roles(*MANAGERS))]


class SettingsUpdate(BaseModel):
    mode: Optional[str] = None
    min_passengers_to_schedule: Optional[int] = None
    max_wait_minutes: Optional[int] = None
    departure_wait_minutes: Optional[int] = None
    min_boarded_percent: Optional[int] = None
    auto_call: Optional[bool] = None
    simulate_boarding: Optional[bool] = None
    demo_time_scale: Optional[int] = None
    max_open_trips_per_terminal: Optional[int] = None


class RushRequest(BaseModel):
    route_id: str
    count: int = Field(18, ge=1, le=40)


class ScanRequest(BaseModel):
    payload: str


@router.get("/autopilot/status")
def get_status(staff: AnyStaff):
    return autopilot.autopilot_status(staff)


@router.put("/autopilot/settings")
def update_settings(body: SettingsUpdate, staff: Manager):
    return autopilot.update_autopilot_settings(body.model_dump(exclude_none=True), staff)


@router.get("/autopilot/decisions")
def get_decisions(staff: AnyStaff, limit: int = Query(30, ge=1, le=100)):
    return autopilot.list_decisions(staff, limit)


@router.post("/autopilot/tick")
def run_now(staff: Manager):
    """Run one autopilot pass immediately instead of waiting for the next one."""
    return {"decisions": autopilot.tick()}


@router.get("/autopilot/forecast")
def get_forecast(staff: AnyStaff, hours: int = Query(6, ge=1, le=12)):
    return autopilot.forecast_overview(staff, hours)


@router.post("/autopilot/simulate-rush")
def simulate_rush(body: RushRequest, staff: Manager):
    return autopilot.simulate_rush(body.route_id, body.count, staff)


@router.post("/boarding/scan", response_model=QueueResponse)
def scan_to_board(body: ScanRequest, staff: Operator):
    """Scan a ticket QR code: verifies it and boards the passenger in one step."""
    return QueueResponse(**board_by_scan(body.payload, staff))


@router.post("/queues/{booking_reference}/quick-board", response_model=QueueResponse)
def board_in_one_step(booking_reference: str, staff: Operator):
    """Call (if needed), verify and board a passenger with a single request."""
    return QueueResponse(**quick_board(booking_reference, staff))