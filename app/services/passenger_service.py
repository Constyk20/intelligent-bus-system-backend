import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument

from app.database import get_database
from app.services.core_service import _require_terminal, assert_terminal_access


def _now():
    return datetime.now(timezone.utc)


def _oid(value: str, label: str = "id") -> ObjectId:
    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {label}")


def _money(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        raise HTTPException(status_code=400, detail="Amount must be a decimal string such as 2000.00")
    return amount.quantize(Decimal("0.01"))


def _fmt(amount: Decimal) -> str:
    return format(amount, "f")


def ensure_passenger_indexes():
    db = get_database()
    db["transport_cards"].create_index("card_number", unique=True)
    db["transport_cards"].create_index("passenger_id")
    
    # Drop the old broken index if it exists
    try:
        db["transport_cards"].drop_index("rfid_uid_1")
    except Exception:
        pass
        
    # Create a partial unique index that ONLY applies when the key is a string
    db["transport_cards"].create_index(
        "rfid_uid",
        unique=True,
        partialFilterExpression={"rfid_uid": {"$type": "string"}}
    )
    
    db["wallet_transactions"].create_index("reference", unique=True)
    db["wallet_transactions"].create_index([("card_id", 1), ("created_at", -1)])
    db["passengers"].create_index([("phone", 1), ("terminal_id", 1)])


def _card_number() -> str:
    return f"TC{datetime.now(timezone.utc).strftime('%y%m%d')}{secrets.randbelow(900000) + 100000}"


def _txn_reference() -> str:
    return f"TXN{secrets.token_hex(6).upper()}"


def register_passenger(data, staff: dict) -> dict:
    assert_terminal_access(staff, data.terminal_id)
    _require_terminal(data.terminal_id)
    doc = {
        "full_name": data.full_name.strip(),
        "phone": data.phone.strip(),
        "terminal_id": data.terminal_id,
        "gender": data.gender,
        "address": data.address,
        "is_active": True,
        "created_at": _now(),
        "updated_at": _now(),
        "created_by": str(staff["_id"]),
    }
    doc["_id"] = get_database()["passengers"].insert_one(doc).inserted_id
    return doc


def list_passengers(staff: dict, terminal_id: str | None = None, phone: str | None = None) -> list:
    query = {}
    if staff["role"] != "super_admin":
        query["terminal_id"] = staff.get("terminal_id")
    elif terminal_id:
        query["terminal_id"] = terminal_id
    if phone:
        query["phone"] = phone.strip()
    return list(get_database()["passengers"].find(query).sort("created_at", -1).limit(100))


def get_passenger(passenger_id: str) -> dict:
    doc = get_database()["passengers"].find_one({"_id": _oid(passenger_id, "passenger_id")})
    if not doc:
        raise HTTPException(status_code=404, detail="Passenger not found")
    return doc


def issue_card(data, staff: dict) -> dict:
    passenger = get_passenger(data.passenger_id)
    assert_terminal_access(staff, passenger["terminal_id"])
    cards = get_database()["transport_cards"]
    existing = cards.find_one({"passenger_id": data.passenger_id, "status": "active"})
    if existing:
        raise HTTPException(status_code=400, detail="Passenger already has an active card")

    opening = _money(data.initial_balance)
    if opening < 0:
        raise HTTPException(status_code=400, detail="Initial balance cannot be negative")

    card_number = _card_number()
    while cards.find_one({"card_number": card_number}):
        card_number = _card_number()

    now = _now()
    doc = {
        "card_number": card_number,
        "passenger_id": data.passenger_id,
        "balance": _fmt(opening),
        "status": "active",
        "issued_at": now,
        "updated_at": now,
        "issued_by": str(staff["_id"]),
        "terminal_id": passenger["terminal_id"],
    }
    
    # Only add rfid_uid if it was provided (avoids unique index conflict on null)
    if data.rfid_uid:
        doc["rfid_uid"] = data.rfid_uid.strip()

    doc["_id"] = cards.insert_one(doc).inserted_id

    if opening > 0:
        get_database()["wallet_transactions"].insert_one({
            "reference": _txn_reference(),
            "card_id": str(doc["_id"]),
            "passenger_id": data.passenger_id,
            "type": "top_up",
            "amount": _fmt(opening),
            "balance_before": "0.00",
            "balance_after": _fmt(opening),
            "note": "Initial card balance",
            "created_at": now,
            "created_by": str(staff["_id"]),
        })
    return doc


def identify_card(card_number: str) -> dict:
    card = get_database()["transport_cards"].find_one({"card_number": card_number.strip().upper()})
    if not card:
        card = get_database()["transport_cards"].find_one({"card_number": card_number.strip()})
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    return card


def update_card_status(card_id: str, new_status: str, staff: dict) -> dict:
    cards = get_database()["transport_cards"]
    card = cards.find_one({"_id": _oid(card_id, "card_id")})
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    assert_terminal_access(staff, card["terminal_id"])
    updated = cards.find_one_and_update(
        {"_id": card["_id"]},
        {"$set": {"status": new_status, "updated_at": _now()}},
        return_document=ReturnDocument.AFTER,
    )
    return updated


def top_up(card_number: str, amount_text: str, note: str | None, staff: dict) -> dict:
    amount = _money(amount_text)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Top-up amount must be greater than zero")

    cards = get_database()["transport_cards"]
    card = identify_card(card_number)
    assert_terminal_access(staff, card["terminal_id"])
    if card.get("status") != "active":
        raise HTTPException(status_code=400, detail="Card is not active")

    before = _money(card["balance"])
    after = before + amount
    updated = cards.find_one_and_update(
        {"_id": card["_id"], "balance": card["balance"], "status": "active"},
        {"$set": {"balance": _fmt(after), "updated_at": _now()}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Card balance changed. Retry the top-up.")

    txn = {
        "reference": _txn_reference(),
        "card_id": str(card["_id"]),
        "passenger_id": card["passenger_id"],
        "type": "top_up",
        "amount": _fmt(amount),
        "balance_before": _fmt(before),
        "balance_after": _fmt(after),
        "note": note or "Wallet top-up",
        "created_at": _now(),
        "created_by": str(staff["_id"]),
    }
    txn["_id"] = get_database()["wallet_transactions"].insert_one(txn).inserted_id
    return {"card": updated, "transaction": txn}


def list_transactions(card_number: str, staff: dict) -> list:
    card = identify_card(card_number)
    assert_terminal_access(staff, card["terminal_id"])
    return list(
        get_database()["wallet_transactions"]
        .find({"card_id": str(card["_id"])})
        .sort("created_at", -1)
        .limit(50)
    )