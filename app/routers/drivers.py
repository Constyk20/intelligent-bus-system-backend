from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends

from app.routers.auth import get_current_staff, require_roles
from app.schemas.core import DriverCreate, DriverResponse, DriverUpdate
from app.services.core_service import create_driver, list_drivers, update_driver

router = APIRouter(prefix="/drivers", tags=["Drivers"])


def _to_driver(doc: dict) -> DriverResponse:
    return DriverResponse(
        id=str(doc["_id"]),
        full_name=doc["full_name"],
        phone=doc["phone"],
        license_number=doc["license_number"],
        terminal_id=doc["terminal_id"],
        is_active=doc.get("is_active", True),
        created_at=doc["created_at"],
    )


@router.post("", response_model=DriverResponse, status_code=201)
def create(
    data: DriverCreate,
    staff: Annotated[dict, Depends(require_roles("super_admin", "terminal_admin"))],
):
    return _to_driver(create_driver(data, staff))


@router.get("", response_model=List[DriverResponse])
def list_all(
    staff: Annotated[dict, Depends(get_current_staff)],
    terminal_id: Optional[str] = None,
):
    return [_to_driver(doc) for doc in list_drivers(staff, terminal_id)]


@router.patch("/{driver_id}", response_model=DriverResponse)
def update(
    driver_id: str,
    data: DriverUpdate,
    staff: Annotated[dict, Depends(require_roles("super_admin", "terminal_admin"))],
):
    return _to_driver(update_driver(driver_id, data, staff))