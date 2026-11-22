import secrets
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import HTTPException

from app.database import get_database
from app.services.core_service import assert_terminal_access
from app.services.events import publish


def _now():
    return datetime.now(timezone.utc)


def _oid(value: str, label: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {label}")


def ensure_trip_indexes():
    db = get_database()
    db["trips"].create_index("trip_reference", unique=True)
    db["trips"].create_index([("terminal_id", 1), ("status", 1)])
    db["trips"].create_index([("bus_id", 1), ("status", 1)])


def _reference() -> str:
    return f"TR{datetime.now(timezone.utc).strftime('%y%m%d')}{secrets.token_hex(3).upper()}"


def _public(doc: dict) -> dict:
    db = get_database()
    registration = doc.get("registration_number")
    driver_name = doc.get("driver_name")
    # Names are stored on the trip when it is created; older trips fall back to a lookup
    if registration is None and ObjectId.is_valid(doc["bus_id"]):
        bus = db["buses"].find_one({"_id": ObjectId(doc["bus_id"])}, {"registration_number": 1})
        registration = bus["registration_number"] if bus else None
    if driver_name is None and ObjectId.is_valid(doc["driver_id"]):
        driver = db["drivers"].find_one({"_id": ObjectId(doc["driver_id"])}, {"full_name": 1})
        driver_name = driver["full_name"] if driver else None
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    doc["registration_number"] = registration
    doc["driver_name"] = driver_name
    doc["simulated"] = True
    return doc


def create_trip(data, staff: dict) -> dict:
    ensure_trip_indexes()
    db = get_database()
    route = db["routes"].find_one({"_id": _oid(data.route_id, "route_id")})
    if not route or not route.get("is_active", True):
        raise HTTPException(status_code=404, detail="Route not found or inactive")
    assert_terminal_access(staff, route["terminal_id"])

    bus = db["buses"].find_one({"_id": _oid(data.bus_id, "bus_id")})
    if not bus or not bus.get("is_active", True):
        raise HTTPException(status_code=404, detail="Bus not found or inactive")
    if bus["terminal_id"] != route["terminal_id"]:
        raise HTTPException(status_code=400, detail="Bus is not at this route's terminal")
    if bus.get("status") != "available":
        raise HTTPException(status_code=400, detail="Bus is not available")

    driver = db["drivers"].find_one({"_id": _oid(data.driver_id, "driver_id")})
    if not driver or not driver.get("is_active", True):
        raise HTTPException(status_code=404, detail="Driver not found or inactive")
    if driver["terminal_id"] != route["terminal_id"]:
        raise HTTPException(status_code=400, detail="Driver is not assigned to this terminal")

    active_bus = db["trips"].find_one({"bus_id": data.bus_id, "status": {"$in": ["scheduled", "in_transit"]}})
    if active_bus:
        raise HTTPException(status_code=400, detail="Bus already has an open trip")
    active_driver = db["trips"].find_one({"driver_id": data.driver_id, "status": {"$in": ["scheduled", "in_transit"]}})
    if active_driver:
        raise HTTPException(status_code=400, detail="Driver already has an open trip")

    reference = _reference()
    while db["trips"].find_one({"trip_reference": reference}):
        reference = _reference()

    doc = {
        "trip_reference": reference,
        "terminal_id": route["terminal_id"],
        "route_id": str(route["_id"]),
        "origin": route["origin"],
        "destination": route["destination"],
        "bus_id": str(bus["_id"]),
        "driver_id": str(driver["_id"]),
        "registration_number": bus["registration_number"],
        "driver_name": driver["full_name"],
        "status": "scheduled",
        "capacity": bus["capacity"],
        "passenger_count": 0,
        "boarded_booking_references": [],
        "progress_percent": 0,
        "simulated": True,
        "created_at": _now(),
        "created_by": str(staff["_id"]),
        "departed_at": None,
        "completed_at": None,
    }
    doc["_id"] = db["trips"].insert_one(doc).inserted_id

    # === AUTO-ASSIGN EXISTING UNASSIGNED QUEUE ENTRIES TO THIS NEW TRIP ===
    unassigned = list(
        db["queues"]
        .find({
            "route_id": str(route["_id"]),
            "terminal_id": route["terminal_id"],
            "trip_reference": {"$exists": False},
            "status": {"$in": ["waiting", "called", "boarded"]}
        })
        .sort("queue_number", 1)
        .limit(bus["capacity"])
    )
    assigned_refs = [q["booking_reference"] for q in unassigned]
    if assigned_refs:
        db["queues"].update_many(
            {"booking_reference": {"$in": assigned_refs}},
            {"$set": {"trip_reference": doc["trip_reference"]}}
        )
        db["bookings"].update_many(
            {"booking_reference": {"$in": assigned_refs}},
            {"$set": {"trip_reference": doc["trip_reference"]}}
        )
        print(f"[create_trip] Assigned {len(assigned_refs)} unassigned queue entries to {doc['trip_reference']}")

    return _public(doc)


def list_trips(staff: dict, status: str | None = None) -> list:
    query = {}
    if staff["role"] != "super_admin":
        query["terminal_id"] = staff.get("terminal_id")
    if status:
        query["status"] = status
    return [_public(doc) for doc in get_database()["trips"].find(query).sort("created_at", -1).limit(100)]


def get_trip(trip_reference: str, staff: dict) -> dict:
    doc = get_database()["trips"].find_one({"trip_reference": trip_reference.strip().upper()})
    if not doc:
        raise HTTPException(status_code=404, detail="Trip not found")
    assert_terminal_access(staff, doc["terminal_id"])
    return _public(doc)


def dispatch_trip(trip_reference: str, staff: dict, allow_partial: bool = False) -> dict:
    """
    Dispatch a scheduled trip. By default the bus must be full. With
    allow_partial=True (used by the autopilot) it leaves with whoever has boarded
    and the passengers who did not board go back to the unassigned queue.
    """
    db = get_database()
    trip = db["trips"].find_one({"trip_reference": trip_reference.strip().upper()})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    assert_terminal_access(staff, trip["terminal_id"])
    if trip["status"] != "scheduled":
        raise HTTPException(status_code=400, detail="Only a scheduled trip can be dispatched")

    boarded = list(
        db["queues"].find({
            "trip_reference": trip["trip_reference"],
            "status": "boarded"
        }).sort("queue_number", 1)
    )
    
    is_full = len(boarded) >= trip["capacity"]
    if not is_full:
        if not allow_partial:
            raise HTTPException(
                status_code=400,
                detail=f"Bus must be full before dispatch. Currently {len(boarded)}/{trip['capacity']} passengers boarded."
            )
        if not boarded:
            raise HTTPException(status_code=400, detail="No passengers have boarded this trip yet")

    references = [row["booking_reference"] for row in boarded]
    now = _now()

    # Partial departure: release passengers who are assigned but did not board
    released = 0
    if not is_full:
        left_behind = [
            row["booking_reference"]
            for row in db["queues"].find(
                {"trip_reference": trip["trip_reference"], "status": {"$in": ["waiting", "called"]}},
                {"booking_reference": 1},
            )
        ]
        if left_behind:
            db["queues"].update_many(
                {"booking_reference": {"$in": left_behind}},
                {"$set": {"status": "waiting", "called_at": None}, "$unset": {"trip_reference": ""}},
            )
            db["bookings"].update_many(
                {"booking_reference": {"$in": left_behind}},
                {"$unset": {"trip_reference": ""}},
            )
            released = len(left_behind)

    db["buses"].update_one(
        {"_id": ObjectId(trip["bus_id"]), "status": "available"},
        {"$set": {"status": "on_trip", "updated_at": now}},
    )
    db["trips"].update_one(
        {"_id": trip["_id"]},
        {"$set": {
            "status": "in_transit",
            "passenger_count": len(references),
            "boarded_booking_references": references,
            "departed_at": now,
            "progress_percent": 0,
        }},
    )
    if references:
        db["queues"].update_many(
            {"booking_reference": {"$in": references}},
            {"$set": {"trip_status": "in_transit"}},
        )
        db["bookings"].update_many(
            {"booking_reference": {"$in": references}},
            {"$set": {"status": "in_transit"}},
        )
    publish(
        "trip_dispatched",
        f"{trip['trip_reference']} dispatched with {len(references)} passengers",
        {"trip_reference": trip["trip_reference"], "passengers": len(references), "released": released},
        terminal_id=trip["terminal_id"],
    )
    updated = db["trips"].find_one({"_id": trip["_id"]})
    return _public(updated)


def advance_trip(trip_reference: str, progress_percent: int, staff: dict) -> dict:
    db = get_database()
    trip = db["trips"].find_one({"trip_reference": trip_reference.strip().upper()})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    assert_terminal_access(staff, trip["terminal_id"])
    if trip["status"] != "in_transit":
        raise HTTPException(status_code=400, detail="Only a trip in transit can move")
    if progress_percent < trip.get("progress_percent", 0):
        raise HTTPException(status_code=400, detail="Progress cannot move backwards")
    if progress_percent >= 100:
        return complete_trip(trip["trip_reference"], staff)

    db["trips"].update_one(
        {"_id": trip["_id"]},
        {"$set": {"progress_percent": progress_percent}},
    )
    return _public(db["trips"].find_one({"_id": trip["_id"]}))


def complete_trip(trip_reference: str, staff: dict) -> dict:
    db = get_database()
    trip = db["trips"].find_one({"trip_reference": trip_reference.strip().upper()})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    assert_terminal_access(staff, trip["terminal_id"])
    if trip["status"] != "in_transit":
        raise HTTPException(status_code=400, detail="Only a trip in transit can be completed")

    now = _now()
    db["trips"].update_one(
        {"_id": trip["_id"]},
        {"$set": {"status": "completed", "progress_percent": 100, "completed_at": now}},
    )
    db["buses"].update_one(
        {"_id": ObjectId(trip["bus_id"])},
        {"$set": {"status": "available", "updated_at": now}},
    )
    references = trip.get("boarded_booking_references", [])
    if references:
        db["bookings"].update_many(
            {"booking_reference": {"$in": references}},
            {"$set": {"status": "arrived", "arrived_at": now}},
        )
        db["queues"].update_many(
            {"booking_reference": {"$in": references}},
            {"$set": {"status": "arrived", "arrived_at": now, "trip_status": "completed"}},
        )
    publish(
        "trip_completed",
        f"{trip['trip_reference']} completed - passengers arrived",
        {"trip_reference": trip["trip_reference"]},
        terminal_id=trip["terminal_id"],
    )
    return _public(db["trips"].find_one({"_id": trip["_id"]}))


def cancel_trip(trip_reference: str, staff: dict) -> dict:
    db = get_database()
    trip = db["trips"].find_one({"trip_reference": trip_reference.strip().upper()})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    assert_terminal_access(staff, trip["terminal_id"])
    if trip["status"] != "scheduled":
        raise HTTPException(status_code=400, detail="Only a scheduled trip can be cancelled")

    result = db["trips"].update_one(
        {"_id": trip["_id"], "status": "scheduled"},
        {"$set": {"status": "cancelled", "cancelled_at": _now()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="Trip changed. Refresh and retry.")

    # Release the passengers: without this they keep pointing at a cancelled
    # trip, never count as "unassigned", and so are never re-attached to a new
    # trip or included in an AI recommendation.
    db["queues"].update_many(
        {"trip_reference": trip["trip_reference"]},
        {"$unset": {"trip_reference": ""}},
    )
    db["bookings"].update_many(
        {"trip_reference": trip["trip_reference"]},
        {"$unset": {"trip_reference": ""}},
    )

    publish(
        "trip_cancelled",
        f"Trip {trip['trip_reference']} cancelled",
        {"trip_reference": trip["trip_reference"]},
        terminal_id=trip["terminal_id"],
    )
    return _public(db["trips"].find_one({"_id": trip["_id"]}))