from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends

from app.routers.auth import get_current_staff, require_roles
from app.schemas.passenger import (
    CardIssue,
    CardResponse,
    CardStatusUpdate,
    PassengerCreate,
    PassengerResponse,
    TransactionResponse,
    WalletTopUp,
)
from app.services.passenger_service import (
    ensure_passenger_indexes,
    get_passenger,
    identify_card,
    issue_card,
    list_passengers,
    list_transactions,
    register_passenger,
    top_up,
    update_card_status,
)

router = APIRouter(tags=["Passengers and Cards"])
OPERATORS = ("super_admin", "terminal_admin", "booking_operator")


def _passenger(doc: dict) -> PassengerResponse:
    return PassengerResponse(
        id=str(doc["_id"]),
        full_name=doc["full_name"],
        phone=doc["phone"],
        terminal_id=doc["terminal_id"],
        gender=doc.get("gender"),
        address=doc.get("address"),
        is_active=doc.get("is_active", True),
        created_at=doc["created_at"],
    )


def _card(doc: dict, passenger_name: str | None = None) -> CardResponse:
    return CardResponse(
        id=str(doc["_id"]),
        card_number=doc["card_number"],
        passenger_id=doc["passenger_id"],
        passenger_name=passenger_name,
        rfid_uid=doc.get("rfid_uid"),
        balance=doc["balance"],
        status=doc["status"],
        issued_at=doc["issued_at"],
    )


def _txn(doc: dict) -> TransactionResponse:
    return TransactionResponse(
        id=str(doc["_id"]),
        reference=doc["reference"],
        card_id=doc["card_id"],
        passenger_id=doc["passenger_id"],
        type=doc["type"],
        amount=doc["amount"],
        balance_before=doc["balance_before"],
        balance_after=doc["balance_after"],
        note=doc.get("note"),
        created_at=doc["created_at"],
        created_by=doc.get("created_by"),
    )


@router.post("/passengers", response_model=PassengerResponse, status_code=201)
def create_passenger(
    data: PassengerCreate,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    ensure_passenger_indexes()
    return _passenger(register_passenger(data, staff))


@router.get("/passengers", response_model=List[PassengerResponse])
def get_passengers(
    staff: Annotated[dict, Depends(get_current_staff)],
    terminal_id: Optional[str] = None,
    phone: Optional[str] = None,
):
    return [_passenger(doc) for doc in list_passengers(staff, terminal_id, phone)]


@router.post("/cards", response_model=CardResponse, status_code=201)
def create_card(
    data: CardIssue,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    ensure_passenger_indexes()
    card = issue_card(data, staff)
    passenger = get_passenger(card["passenger_id"])
    return _card(card, passenger["full_name"])


@router.get("/cards/{card_number}", response_model=CardResponse)
def get_card(card_number: str, staff: Annotated[dict, Depends(require_roles(*OPERATORS))]):
    card = identify_card(card_number)
    from app.services.core_service import assert_terminal_access
    assert_terminal_access(staff, card["terminal_id"])
    passenger = get_passenger(card["passenger_id"])
    return _card(card, passenger["full_name"])


@router.patch("/cards/{card_id}/status", response_model=CardResponse)
def set_card_status(
    card_id: str,
    data: CardStatusUpdate,
    staff: Annotated[dict, Depends(require_roles("super_admin", "terminal_admin"))],
):
    card = update_card_status(card_id, data.status, staff)
    passenger = get_passenger(card["passenger_id"])
    return _card(card, passenger["full_name"])


@router.post("/cards/{card_number}/top-up", response_model=TransactionResponse)
def wallet_top_up(
    card_number: str,
    data: WalletTopUp,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    result = top_up(card_number, data.amount, data.note, staff)
    return _txn(result["transaction"])


@router.get("/cards/{card_number}/transactions", response_model=List[TransactionResponse])
def transactions(
    card_number: str,
    staff: Annotated[dict, Depends(require_roles(*OPERATORS))],
):
    return [_txn(doc) for doc in list_transactions(card_number, staff)]