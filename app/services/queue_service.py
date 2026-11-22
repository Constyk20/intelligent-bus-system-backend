from datetime import datetime, timezone

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument

from app.database import get_database
from app.services.core_service import assert_terminal_access
from app.services.events import publish
from app.services.ticket_service import verify_ticket


def _now():
    return datetime.now(timezone.utc)


def ensure_queue_indexes():
    db = get_database()
    db["queues"].create_index("booking_reference", unique=True)
    db["queues"].create_index([("terminal_id", 1), ("route_id", 1), ("status", 1), ("queue_number", 1)])
    db["queues"].create_index([("trip_reference", 1), ("status", 1)])
    db["queues"].create_index("ticket_number")


def next_queue_number(db, terminal_id: str, route_id: str, session=None) -> int:
    """
    Atomic per-route counter. Reading the highest number and adding one lets two
    simultaneous bookings receive the same number; $inc cannot.
    """
    key = f"queue:{terminal_id}:{route_id}"
    counter = db["counters"].find_one_and_update(
        {"_id": key},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    if counter["seq"] == 1:
        # Counter was just created: continue after any numbers issued before it existed
        latest = db["queues"].find_one(
            {"terminal_id": terminal_id, "route_id": route_id},
            sort=[("queue_number", -1)],
            session=session,
        )
        if latest and latest["queue_number"] >= 1:
            seq = latest["queue_number"] + 1
            db["counters"].update_one({"_id": key}, {"$set": {"seq": seq}}, session=session)
            return seq
    return counter["seq"]


def build_queue_doc(booking: dict, ticket_number: str, queue_number: int, passenger_name: str | None = None) -> dict:
    return {
        "queue_number": queue_number,
        "booking_reference": booking["booking_reference"],
        "ticket_number": ticket_number,
        "passenger_id": booking["passenger_id"],
        "passenger_name": passenger_name,
        "card_id": booking["card_id"],
        "terminal_id": booking["terminal_id"],
        "route_id": booking["route_id"],
        "origin": booking["origin"],
        "destination": booking["destination"],
        "status": "waiting",
        "created_at": booking["created_at"],
        "called_at": None,
        "boarded_at": None,
        "created_by": booking["created_by"],
    }


def _attach_names(rows: list) -> list:
    """Public shape for queue rows. Uses the stored name, and fetches any missing names in ONE query."""
    missing = {row["passenger_id"] for row in rows if not row.get("passenger_name")}
    names = {}
    if missing:
        ids = [ObjectId(i) for i in missing if ObjectId.is_valid(i)]
        for passenger in get_database()["passengers"].find({"_id": {"$in": ids}}, {"full_name": 1}):
            names[str(passenger["_id"])] = passenger["full_name"]

    result = []
    for row in rows:
        doc = dict(row)
        doc["id"] = str(doc.pop("_id"))
        doc["passenger_name"] = row.get("passenger_name") or names.get(row["passenger_id"])
        result.append(doc)
    return result


def _public(doc: dict) -> dict:
    return _attach_names([doc])[0]


def list_queue(staff: dict, route_id: str | None = None, status: str | None = None) -> list:
    query = {}
    if staff["role"] != "super_admin":
        query["terminal_id"] = staff.get("terminal_id")
    if route_id:
        query["route_id"] = route_id
    if status:
        query["status"] = status
    rows = list(get_database()["queues"].find(query).sort([("queue_number", 1)]))
    return _attach_names(rows)


def _get_entry(booking_reference: str, staff: dict) -> dict:
    entry = get_database()["queues"].find_one(
        {"booking_reference": booking_reference.strip().upper()}
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Queue entry not found")
    assert_terminal_access(staff, entry["terminal_id"])
    return entry


def call_passenger(booking_reference: str, staff: dict) -> dict:
    entry = _get_entry(booking_reference, staff)
    if entry["status"] != "waiting":
        raise HTTPException(status_code=400, detail="Only a waiting passenger can be called")
    updated = get_database()["queues"].find_one_and_update(
        {"_id": entry["_id"], "status": "waiting"},
        {"$set": {"status": "called", "called_at": _now()}},
        return_document=True,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Queue entry changed. Refresh and retry.")
    publish(
        "passenger_called",
        f"Queue {updated['queue_number']} called",
        {"booking_reference": updated["booking_reference"]},
        terminal_id=updated["terminal_id"],
    )
    return _public(updated)


def call_next(route_id: str, staff: dict) -> dict:
    query = {"route_id": route_id, "status": "waiting"}
    if staff["role"] != "super_admin":
        query["terminal_id"] = staff.get("terminal_id")
    entry = get_database()["queues"].find_one(query, sort=[("queue_number", 1)])
    if not entry:
        raise HTTPException(status_code=404, detail="No passenger is waiting on this route")
    return call_passenger(entry["booking_reference"], staff)


def board_passenger(booking_reference: str, ticket_number: str, token: str, staff: dict) -> dict:
    entry = _get_entry(booking_reference, staff)
    if entry["status"] != "called":
        raise HTTPException(status_code=400, detail="Passenger must be called before boarding")
    if entry["ticket_number"] != ticket_number.strip().upper():
        raise HTTPException(status_code=400, detail="Ticket does not match this queue entry")

    check = verify_ticket(ticket_number, token)
    if not check["valid"]:
        raise HTTPException(status_code=400, detail=check["message"])

    now = _now()
    db = get_database()
    
    # 1. Mark queue entry as boarded (this keeps the trip_reference intact for counting)
    updated = db["queues"].find_one_and_update(
        {"_id": entry["_id"], "status": "called"},
        {"$set": {"status": "boarded", "boarded_at": now}},
        return_document=True,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Queue entry changed. Refresh and retry.")
    
    # 2. Mark ticket as used
    db["tickets"].update_one(
        {"ticket_number": entry["ticket_number"], "status": "issued"},
        {"$set": {"status": "used", "used_at": now}},
    )
    
    # 3. Mark booking as boarded
    db["bookings"].update_one(
        {"booking_reference": entry["booking_reference"], "status": "confirmed"},
        {"$set": {"status": "boarded", "boarded_at": now}},
    )
    
    publish(
        "passenger_boarded",
        f"{updated['ticket_number']} boarded",
        {
            "booking_reference": updated["booking_reference"],
            "trip_reference": updated.get("trip_reference"),
        },
        terminal_id=updated["terminal_id"],
    )
    return _public(updated)


def quick_board(booking_reference: str, staff: dict) -> dict:
    """
    One-step boarding for an operator who has the passenger in front of them:
    calls the passenger if needed, verifies the ticket server-side and boards.
    The ticket's secret token never has to travel to the browser.
    """
    entry = _get_entry(booking_reference, staff)
    if entry["status"] == "waiting":
        call_passenger(entry["booking_reference"], staff)
    elif entry["status"] != "called":
        raise HTTPException(status_code=400, detail=f"Passenger is {entry['status']} and cannot board")

    ticket = get_database()["tickets"].find_one({"ticket_number": entry["ticket_number"]})
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return board_passenger(
        entry["booking_reference"], entry["ticket_number"], ticket["verification_token"], staff
    )


def board_by_scan(payload: str, staff: dict) -> dict:
    """Board straight from a scanned ticket QR code (payload is 'TICKETNUMBER.TOKEN')."""
    ticket_number, _, token = (payload or "").strip().partition(".")
    if not ticket_number or not token:
        raise HTTPException(status_code=400, detail="That does not look like a ticket QR code")

    entry = get_database()["queues"].find_one({"ticket_number": ticket_number.upper()})
    if not entry:
        raise HTTPException(status_code=404, detail="No queue entry for this ticket")
    assert_terminal_access(staff, entry["terminal_id"])

    check = verify_ticket(ticket_number, token)
    if not check["valid"]:
        raise HTTPException(status_code=400, detail=check["message"])

    if entry["status"] == "waiting":
        call_passenger(entry["booking_reference"], staff)
    elif entry["status"] != "called":
        raise HTTPException(status_code=400, detail=f"Passenger is {entry['status']} and cannot board")

    return board_passenger(entry["booking_reference"], ticket_number, token, staff)


def mark_no_show(booking_reference: str, staff: dict) -> dict:
    entry = _get_entry(booking_reference, staff)
    if entry["status"] != "called":
        raise HTTPException(status_code=400, detail="Only a called passenger can be marked as a no-show")
    updated = get_database()["queues"].find_one_and_update(
        {"_id": entry["_id"], "status": "called"},
        {"$set": {"status": "no_show", "no_show_at": _now()}},
        return_document=True,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Queue entry changed. Refresh and retry.")
    publish(
        "passenger_no_show",
        f"Queue {updated['queue_number']} marked no-show",
        {"booking_reference": updated["booking_reference"]},
        terminal_id=updated["terminal_id"],
    )
    return _public(updated)