from typing import Annotated, List

from fastapi import APIRouter, Depends, Query

from app.routers.auth import get_current_staff, require_roles
from app.schemas.core import TerminalCreate, TerminalResponse, TerminalUpdate
from app.services.core_service import (
    create_terminal,
    ensure_core_indexes,
    get_terminal,
    list_terminals,
    update_terminal,
)

router = APIRouter(prefix="/terminals", tags=["Terminals"])


def _to_terminal(doc: dict) -> TerminalResponse:
    return TerminalResponse(
        id=str(doc["_id"]),
        name=doc["name"],
        code=doc["code"],
        city=doc["city"],
        state=doc["state"],
        address=doc.get("address"),
        is_active=doc.get("is_active", True),
        created_at=doc["created_at"],
    )


@router.post("", response_model=TerminalResponse, status_code=201)
def create(
    data: TerminalCreate,
    _: Annotated[dict, Depends(require_roles("super_admin"))],
):
    ensure_core_indexes()
    return _to_terminal(create_terminal(data))


@router.get("", response_model=List[TerminalResponse])
def list_all(
    _: Annotated[dict, Depends(get_current_staff)],
    active_only: bool = Query(False),
):
    return [_to_terminal(doc) for doc in list_terminals(active_only)]


@router.get("/{terminal_id}", response_model=TerminalResponse)
def get_one(terminal_id: str, _: Annotated[dict, Depends(get_current_staff)]):
    return _to_terminal(get_terminal(terminal_id))


@router.patch("/{terminal_id}", response_model=TerminalResponse)
def update(
    terminal_id: str,
    data: TerminalUpdate,
    _: Annotated[dict, Depends(require_roles("super_admin"))],
):
    return _to_terminal(update_terminal(terminal_id, data))