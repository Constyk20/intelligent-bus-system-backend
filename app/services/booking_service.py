import secrets
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import PyMongoError

from app.database import get_client, get_database
from app.services.core_service import assert_terminal_access
from app.services.events import publish
from app.services.passenger_service import _fmt, _money, get_passenger, identify_card
from app.services.queue_service import build_queue_doc, ensure_queue_indexes, next_queue_number
from app.services.ticket_service import (
    build_ticket_doc,
    ensure_ticket_indexes,
    new_ticket_number,
)


def _now():
    return datetime.now(timezone.utc)


def _oid(value: str, label: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {label}")


def ensure_booking_indexes():
    db = get_database()
    db["bookings"].create_index("booking_reference", unique=True)
    try:
        db["bookings"].drop_index("idempotency_key_1")
    except Exception:
        pass
    db["bookings"].create_index(
        "idempotency_key", 
        unique=True, 
        partialFilterExpression={"idempotency_key": {"$type": "string"}}
    )
    db["bookings"].create_index([("terminal_id", 1), ("created_at", -1)])
    db["bookings"].create_index([("route_id", 1), ("status", 1)])
    db["bookings"].create_index([("passenger_id", 1), ("status", 1)])


_indexes_ready = False


def _ensure_indexes_once():
    """Create indexes once per process, not on every booking."""
    global _indexes_ready
    if _indexes_ready:
        return
    ensure_booking_indexes()
    ensure_ticket_indexes()
    ensure_queue_indexes()
    db = get_database()
    db["trips"].create_index([("route_id", 1), ("terminal_id", 1), ("status", 1)])
    _indexes_ready = True


def _reference() -> str:
    return f"BK{datetime.now(timezone.utc).strftime('%y%m%d')}{secrets.token_hex(3).upper()}"


def _txn_reference() -> str:
    return f"TXN{secrets.token_hex(6).upper()}"


def _to_public(booking: dict, passenger_name: str, card_number: str) -> dict:
    booking = dict(booking)
    booking["passenger_name"] = passenger_name
    booking["card_number"] = card_number
    ticket = get_database()["tickets"].find_one(
        {"booking_reference": booking["booking_reference"]}
    )
    booking["ticket_number"] = ticket["ticket_number"] if ticket else booking.get("ticket_number")
    return booking


def get_booking_by_reference(reference: str, staff: dict) -> dict:
    booking = get_database()["bookings"].find_one(
        {"booking_reference": reference.strip().upper()}
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    assert_terminal_access(staff, booking["terminal_id"])
    passenger = get_passenger(booking["passenger_id"])
    card = get_database()["transport_cards"].find_one({"_id": ObjectId(booking["card_id"])})
    return _to_public(booking, passenger["full_name"], card["card_number"] if card else "")


def list_bookings(staff: dict, terminal_id: str | None = None) -> list:
    query = {}
    if staff["role"] != "super_admin":
        query["terminal_id"] = staff.get("terminal_id")
    elif terminal_id:
        query["terminal_id"] = terminal_id
    return list(get_database()["bookings"].find(query).sort("created_at", -1).limit(100))


def _trip_capacity(db, trip: dict, session=None) -> int:
    """Seats on a trip. Uses the trip's own capacity, falling back to its bus."""
    capacity = trip.get("capacity")
    if capacity is not None:
        try:
            return int(capacity)
        except (TypeError, ValueError):
            pass
    try:
        bus = db["buses"].find_one({"_id": ObjectId(trip["bus_id"])}, session=session)
    except Exception:
        bus = None
    return int(bus["capacity"]) if bus and bus.get("capacity") is not None else 0


def _assign_to_scheduled_trip(db, booking_doc: dict, queue_doc: dict, session) -> str | None:
    """
    Attach the passenger to the oldest SCHEDULED trip on the same route and
    terminal that still has a free seat. Must run inside the booking
    transaction. Returns the trip_reference, or None if no trip has room
    (the passenger stays unassigned and is picked up later by create_trip or
    by an AI recommendation).
    """
    trips = db["trips"].find(
        {
            "route_id": booking_doc["route_id"],
            "terminal_id": booking_doc["terminal_id"],
            "status": "scheduled",
        },
        session=session,
    ).sort("created_at", 1)

    for trip in trips:
        capacity = _trip_capacity(db, trip, session=session)
        if capacity <= 0:
            continue

        taken = db["queues"].count_documents(
            {
                "trip_reference": trip["trip_reference"],
                "status": {"$in": ["waiting", "called", "boarded"]},
            },
            session=session,
        )
        if taken >= capacity:
            continue

        # Write to the trip document so two concurrent bookings for the same
        # trip conflict (one is retried) instead of both taking the last seat.
        touched = db["trips"].update_one(
            {"_id": trip["_id"], "status": "scheduled"},
            {"$set": {"last_assigned_at": _now()}},
            session=session,
        )
        if touched.matched_count == 0:
            continue  # trip was dispatched/cancelled meanwhile, try the next one

        trip_reference = trip["trip_reference"]
        queue_doc["trip_reference"] = trip_reference
        db["bookings"].update_one(
            {"_id": booking_doc["_id"]},
            {"$set": {"trip_reference": trip_reference}},
            session=session,
        )
        booking_doc["trip_reference"] = trip_reference
        return trip_reference

    return None


def create_booking(data, staff: dict) -> dict:
    _ensure_indexes_once()
    db = get_database()

    if data.idempotency_key:
        existing = db["bookings"].find_one({"idempotency_key": data.idempotency_key.strip()})
        if existing:
            passenger = get_passenger(existing["passenger_id"])
            card = db["transport_cards"].find_one({"_id": ObjectId(existing["card_id"])})
            return _to_public(
                existing,
                passenger["full_name"],
                card["card_number"] if card else data.card_number,
            )

    card = identify_card(data.card_number)
    if card.get("status") != "active":
        raise HTTPException(status_code=400, detail="Card is not active")

    route = db["routes"].find_one({"_id": _oid(data.route_id, "route_id")})
    if not route or not route.get("is_active", True):
        raise HTTPException(status_code=404, detail="Route not found or inactive")

    assert_terminal_access(staff, route["terminal_id"])
    passenger = get_passenger(card["passenger_id"])
    if not passenger.get("is_active", True):
        raise HTTPException(status_code=400, detail="Passenger is inactive")

    # A passenger who is on a bus (boarded or in transit) cannot book another
    # trip until that trip has completed and the booking is marked "arrived".
    active_journey = db["bookings"].find_one(
        {
            "passenger_id": card["passenger_id"],
            "status": {"$in": ["boarded", "in_transit"]},
        }
    )
    if active_journey:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{passenger['full_name']} is already on a trip to "
                f"{active_journey['destination']} (booking "
                f"{active_journey['booking_reference']}) and can book again "
                "only after arriving."
            ),
        )

    fare = _money(route["fare"])
    balance = _money(card["balance"])
    if balance < fare:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")

    booking_reference = _reference()
    while db["bookings"].find_one({"booking_reference": booking_reference}):
        booking_reference = _reference()

    txn_reference = _txn_reference()
    now = _now()
    after = balance - fare

    booking_doc = {
        "booking_reference": booking_reference,
        "passenger_id": card["passenger_id"],
        "card_id": str(card["_id"]),
        "route_id": str(route["_id"]),
        "terminal_id": route["terminal_id"],
        "origin": route["origin"],
        "destination": route["destination"],
        "fare": _fmt(fare),
        "balance_after": _fmt(after),
        "status": "confirmed",
        "payment_status": "paid",
        "transaction_reference": txn_reference,
        "created_at": now,
        "created_by": str(staff["_id"]),
    }
    
    if data.idempotency_key:
        booking_doc["idempotency_key"] = data.idempotency_key.strip()

    ticket_doc = build_ticket_doc({**booking_doc, "_id": None})
    while db["tickets"].find_one({"ticket_number": ticket_doc["ticket_number"]}):
        ticket_doc["ticket_number"] = new_ticket_number()

    txn_doc = {
        "reference": txn_reference,
        "card_id": str(card["_id"]),
        "passenger_id": card["passenger_id"],
        "type": "debit",
        "amount": _fmt(fare),
        "balance_before": _fmt(balance),
        "balance_after": _fmt(after),
        "note": f"Booking {booking_reference} {route['origin']} to {route['destination']}",
        "booking_reference": booking_reference,
        "created_at": now,
        "created_by": str(staff["_id"]),
    }

    client = get_client()
    try:
        with client.start_session() as session:
            with session.start_transaction():
                updated = db["transport_cards"].find_one_and_update(
                    {
                        "_id": card["_id"],
                        "status": "active",
                        "balance": card["balance"],
                    },
                    {"$set": {"balance": _fmt(after), "updated_at": now}},
                    session=session,
                )
                if not updated:
                    raise HTTPException(
                        status_code=409,
                        detail="Card balance changed or card is no longer active. Retry.",
                    )
                db["wallet_transactions"].insert_one(txn_doc, session=session)
                result = db["bookings"].insert_one(booking_doc, session=session)
                booking_doc["_id"] = result.inserted_id
                ticket_doc["booking_id"] = str(result.inserted_id)
                db["tickets"].insert_one(ticket_doc, session=session)
                queue_number = next_queue_number(
                    db,
                    booking_doc["terminal_id"],
                    booking_doc["route_id"],
                    session=session,
                )
                queue_doc = build_queue_doc(
                    booking_doc,
                    ticket_doc["ticket_number"],
                    queue_number,
                    passenger["full_name"],
                )

                # Assign to a scheduled trip on this route that still has a seat
                _assign_to_scheduled_trip(db, booking_doc, queue_doc, session)

                db["queues"].insert_one(queue_doc, session=session)
    except HTTPException:
        raise
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=f"Booking could not be completed: {exc}")

    booking_doc["ticket_number"] = ticket_doc["ticket_number"]
    publish(
        "booking_created",
        f"Booking {booking_doc['booking_reference']} confirmed",
        {
            "booking_reference": booking_doc["booking_reference"],
            "ticket_number": ticket_doc["ticket_number"],
            "destination": booking_doc["destination"],
            "trip_reference": booking_doc.get("trip_reference"),
        },
        terminal_id=booking_doc["terminal_id"],
    )
    return _to_public(booking_doc, passenger["full_name"], card["card_number"])