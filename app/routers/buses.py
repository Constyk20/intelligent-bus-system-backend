from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends

from app.routers.auth import get_current_staff, require_roles
from app.schemas.core import BusCreate, BusResponse, BusUpdate
from app.services.core_service import create_bus, list_buses, update_bus

router = APIRouter(prefix="/buses", tags=["Buses"])


def _to_bus(doc: dict) -> BusResponse:
    return BusResponse(
        id=str(doc["_id"]),
        registration_number=doc["registration_number"],
        terminal_id=doc["terminal_id"],
        capacity=doc["capacity"],
        model=doc.get("model"),
        status=doc.get("status", "available"),
        is_active=doc.get("is_active", True),
        created_at=doc["created_at"],
    )


@router.post("", response_model=BusResponse, status_code=201)
def create(
    data: BusCreate,
    staff: Annotated[dict, Depends(require_roles("super_admin", "terminal_admin"))],
):
    return _to_bus(create_bus(data, staff))


@router.get("", response_model=List[BusResponse])
def list_all(
    staff: Annotated[dict, Depends(get_current_staff)],
    terminal_id: Optional[str] = None,
    status: Optional[str] = None,
):
    return [_to_bus(doc) for doc in list_buses(staff, terminal_id, status)]


@router.patch("/{bus_id}", response_model=BusResponse)
def update(
    bus_id: str,
    data: BusUpdate,
    staff: Annotated[dict, Depends(require_roles("super_admin", "terminal_admin"))],
):
    return _to_bus(update_bus(bus_id, data, staff))