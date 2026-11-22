import base64
import io
import secrets
from datetime import datetime, timezone

import qrcode
from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import PyMongoError

from app.database import get_client, get_database
from app.services.core_service import assert_terminal_access
from app.services.passenger_service import _fmt, _money, get_passenger


def _now():
    return datetime.now(timezone.utc)


def ensure_ticket_indexes():
    db = get_database()
    db["tickets"].create_index("ticket_number", unique=True)
    db["tickets"].create_index("booking_reference", unique=True)
    db["tickets"].create_index("verification_token")


def new_ticket_number() -> str:
    return f"TK{datetime.now(timezone.utc).strftime('%y%m%d')}{secrets.token_hex(3).upper()}"


def new_verification_token() -> str:
    return secrets.token_urlsafe(24)


def qr_payload(ticket_number: str, token: str) -> str:
    return f"{ticket_number}.{token}"


def qr_png_base64(payload: str) -> str:
    image = qrcode.make(payload)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def build_ticket_doc(booking: dict) -> dict:
    ticket_number = new_ticket_number()
    token = new_verification_token()
    return {
        "ticket_number": ticket_number,
        "verification_token": token,
        "booking_id": str(booking["_id"]) if booking.get("_id") else None,
        "booking_reference": booking["booking_reference"],
        "passenger_id": booking["passenger_id"],
        "card_id": booking["card_id"],
        "route_id": booking["route_id"],
        "terminal_id": booking["terminal_id"],
        "origin": booking["origin"],
        "destination": booking["destination"],
        "fare": booking["fare"],
        "status": "issued",
        "issued_at": booking["created_at"],
        "cancelled_at": None,
        "created_by": booking["created_by"],
    }


def _public(ticket: dict) -> dict:
    passenger = get_passenger(ticket["passenger_id"])
    payload = qr_payload(ticket["ticket_number"], ticket["verification_token"])
    return {
        "id": str(ticket["_id"]),
        "ticket_number": ticket["ticket_number"],
        "booking_reference": ticket["booking_reference"],
        "passenger_name": passenger["full_name"],
        "origin": ticket["origin"],
        "destination": ticket["destination"],
        "fare": ticket["fare"],
        "terminal_id": ticket["terminal_id"],
        "status": ticket["status"],
        "qr_payload": payload,
        "qr_png_base64": qr_png_base64(payload),
        "verification_token": ticket["verification_token"],
        "issued_at": ticket["issued_at"],
        "cancelled_at": ticket.get("cancelled_at"),
    }


def get_ticket(ticket_number: str, staff: dict) -> dict:
    ticket = get_database()["tickets"].find_one({"ticket_number": ticket_number.strip().upper()})
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    assert_terminal_access(staff, ticket["terminal_id"])
    return _public(ticket)


def get_ticket_by_booking(booking_reference: str) -> dict | None:
    return get_database()["tickets"].find_one({"booking_reference": booking_reference})


def verify_ticket(ticket_number: str, token: str) -> dict:
    ticket = get_database()["tickets"].find_one({"ticket_number": ticket_number.strip().upper()})
    if not ticket or not secrets.compare_digest(ticket.get("verification_token", ""), token):
        return {
            "valid": False,
            "ticket_number": ticket_number,
            "message": "Ticket is not valid",
        }
    valid = ticket["status"] == "issued"
    return {
        "valid": valid,
        "ticket_number": ticket["ticket_number"],
        "origin": ticket["origin"],
        "destination": ticket["destination"],
        "status": ticket["status"],
        "message": "Ticket is valid for boarding" if valid else f"Ticket is {ticket['status']}",
    }


def cancel_booking(booking_reference: str, staff: dict) -> dict:
    db = get_database()
    booking = db["bookings"].find_one({"booking_reference": booking_reference.strip().upper()})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    assert_terminal_access(staff, booking["terminal_id"])
    if booking["status"] == "cancelled":
        ticket = db["tickets"].find_one({"booking_reference": booking["booking_reference"]})
        return _public(ticket)

    if booking["status"] != "confirmed":
        raise HTTPException(status_code=400, detail="Only a confirmed booking can be cancelled")

    ticket = db["tickets"].find_one({"booking_reference": booking["booking_reference"]})
    if not ticket or ticket["status"] != "issued":
        raise HTTPException(status_code=400, detail="Ticket cannot be cancelled")

    card = db["transport_cards"].find_one({"_id": ObjectId(booking["card_id"])})
    if not card or card.get("status") != "active":
        raise HTTPException(status_code=400, detail="Active card is required for a refund")

    fare = _money(booking["fare"])
    before = _money(card["balance"])
    after = before + fare
    now = _now()
    txn = {
        "reference": f"TXN{secrets.token_hex(6).upper()}",
        "card_id": str(card["_id"]),
        "passenger_id": booking["passenger_id"],
        "type": "refund",
        "amount": _fmt(fare),
        "balance_before": _fmt(before),
        "balance_after": _fmt(after),
        "note": f"Refund for cancelled booking {booking['booking_reference']}",
        "booking_reference": booking["booking_reference"],
        "created_at": now,
        "created_by": str(staff["_id"]),
    }

    client = get_client()
    try:
        with client.start_session() as session:
            with session.start_transaction():
                updated_card = db["transport_cards"].find_one_and_update(
                    {"_id": card["_id"], "status": "active", "balance": card["balance"]},
                    {"$set": {"balance": _fmt(after), "updated_at": now}},
                    session=session,
                )
                if not updated_card:
                    raise HTTPException(status_code=409, detail="Card changed. Retry the cancellation.")
                db["wallet_transactions"].insert_one(txn, session=session)
                db["bookings"].update_one(
                    {"_id": booking["_id"], "status": "confirmed"},
                    {"$set": {"status": "cancelled", "payment_status": "refunded", "cancelled_at": now}},
                    session=session,
                )
                db["tickets"].update_one(
                    {"_id": ticket["_id"], "status": "issued"},
                    {"$set": {"status": "cancelled", "cancelled_at": now}},
                    session=session,
                )
                # Free the seat: a cancelled passenger must not keep occupying
                # a place in the queue or on a scheduled trip.
                db["queues"].update_one(
                    {
                        "booking_reference": booking["booking_reference"],
                        "status": {"$in": ["waiting", "called"]},
                    },
                    {"$set": {"status": "cancelled", "cancelled_at": now}},
                    session=session,
                )
    except HTTPException:
        raise
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=f"Cancellation failed: {exc}")

    ticket = db["tickets"].find_one({"_id": ticket["_id"]})
    return _public(ticket)