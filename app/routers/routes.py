from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Query

from app.routers.auth import get_current_staff, require_roles
from app.schemas.core import RouteCreate, RouteResponse, RouteUpdate
from app.services.core_service import create_route, list_routes, update_route

router = APIRouter(prefix="/routes", tags=["Routes"])


def _to_route(doc: dict) -> RouteResponse:
    return RouteResponse(
        id=str(doc["_id"]),
        terminal_id=doc["terminal_id"],
        origin=doc["origin"],
        destination=doc["destination"],
        fare=doc["fare"],
        estimated_duration_minutes=doc["estimated_duration_minutes"],
        distance_km=doc.get("distance_km"),
        is_active=doc.get("is_active", True),
        created_at=doc["created_at"],
    )


@router.post("", response_model=RouteResponse, status_code=201)
def create(
    data: RouteCreate,
    staff: Annotated[dict, Depends(require_roles("super_admin", "terminal_admin"))],
):
    return _to_route(create_route(data, staff))


@router.get("", response_model=List[RouteResponse])
def list_all(
    staff: Annotated[dict, Depends(get_current_staff)],
    terminal_id: Optional[str] = None,
    active_only: bool = Query(False),
):
    return [_to_route(doc) for doc in list_routes(staff, terminal_id, active_only)]


@router.patch("/{route_id}", response_model=RouteResponse)
def update(
    route_id: str,
    data: RouteUpdate,
    staff: Annotated[dict, Depends(require_roles("super_admin", "terminal_admin"))],
):
    return _to_route(update_route(route_id, data, staff))