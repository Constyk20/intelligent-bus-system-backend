from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import HTTPException

from app.database import get_database
from app.intelligence.demand_model import predict_passengers
from app.schemas.trip import TripCreate
from app.services.core_service import assert_terminal_access
from app.services.events import publish
from app.services.trip_service import create_trip


def _now():
    return datetime.now(timezone.utc)


def ensure_scheduling_indexes():
    get_database()["dispatch_recommendations"].create_index(
        [("route_id", 1), ("status", 1)]
    )


def _minutes_waiting(created_at) -> int:
    if created_at is None:
        return 0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return max(0, int((_now() - created_at).total_seconds() // 60))


def _available_buses(db, terminal_id: str) -> list:
    open_bus_ids = {
        trip["bus_id"]
        for trip in db["trips"].find(
            {"terminal_id": terminal_id, "status": {"$in": ["scheduled", "in_transit"]}},
            {"bus_id": 1},
        )
    }
    buses = []
    for bus in db["buses"].find({"terminal_id": terminal_id, "is_active": True, "status": "available"}):
        if str(bus["_id"]) not in open_bus_ids:
            buses.append(bus)
    return buses


def _available_drivers(db, terminal_id: str) -> list:
    open_driver_ids = {
        trip["driver_id"]
        for trip in db["trips"].find(
            {"terminal_id": terminal_id, "status": {"$in": ["scheduled", "in_transit"]}},
            {"driver_id": 1},
        )
    }
    drivers = []
    for driver in db["drivers"].find({"terminal_id": terminal_id, "is_active": True}):
        if str(driver["_id"]) not in open_driver_ids:
            drivers.append(driver)
    return drivers


def _choose_bus(buses: list, passengers: int):
    if not buses or passengers <= 0:
        return None
    fitting = [bus for bus in buses if bus["capacity"] >= passengers]
    pool = fitting or buses
    return sorted(pool, key=lambda bus: (bus["capacity"] < passengers, abs(bus["capacity"] - passengers)))[0]


def _public(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc


def generate_recommendations(staff: dict) -> list:
    ensure_scheduling_indexes()
    db = get_database()
    query = {"is_active": True}
    if staff["role"] != "super_admin":
        if not staff.get("terminal_id"):
            return []
        query["_id"] = ObjectId(staff["terminal_id"])
    created = []
    now = _now()
    week_ago = now - timedelta(days=7)

    for terminal in db["terminals"].find(query):
        terminal_id = str(terminal["_id"])
        buses = _available_buses(db, terminal_id)
        drivers = _available_drivers(db, terminal_id)
        used_bus_ids = set()
        used_driver_ids = set()

        for route in db["routes"].find({"terminal_id": terminal_id, "is_active": True}):
            route_id = str(route["_id"])
            
            if db["dispatch_recommendations"].find_one({"route_id": route_id, "status": "pending"}):
                continue

            # Only count passengers who are NOT yet assigned to any trip
            waiting_rows = list(db["queues"].find(
                {
                    "route_id": route_id,
                    "status": {"$in": ["waiting", "called"]},
                    "trip_reference": {"$exists": False}
                }
            ))
            if not waiting_rows:
                continue

            oldest = min(_minutes_waiting(row.get("created_at")) for row in waiting_rows)
            recent = db["bookings"].count_documents({
                "route_id": route_id,
                "created_at": {"$gte": week_ago},
                "status": {"$ne": "cancelled"},
            })
            predicted = predict_passengers(route_id)
            score = (
                len(waiting_rows) * 100
                + min(oldest, 240)
                + min(recent, 30)
                + min(int(predicted), 40)
            )

            free_buses = [bus for bus in buses if str(bus["_id"]) not in used_bus_ids]
            free_drivers = [driver for driver in drivers if str(driver["_id"]) not in used_driver_ids]
            bus = _choose_bus(free_buses, len(waiting_rows))
            driver = free_drivers[0] if free_drivers else None
            if bus:
                used_bus_ids.add(str(bus["_id"]))
            if driver:
                used_driver_ids.add(str(driver["_id"]))

            if bus and bus["capacity"] >= len(waiting_rows):
                reason = (
                    f"{len(waiting_rows)} waiting. Forecast for this hour is {predicted}. "
                    f"Bus {bus['registration_number']} can carry the current queue."
                )
            elif bus:
                reason = (
                    f"{len(waiting_rows)} waiting, which is more than one bus. "
                    f"Forecast for this hour is {predicted}. "
                    f"Recommend {bus['registration_number']} for the first {bus['capacity']}."
                )
            else:
                reason = (
                    f"{len(waiting_rows)} waiting and the hourly forecast is {predicted}, "
                    "but no available bus is at this terminal."
                )

            doc = {
                "terminal_id": terminal_id,
                "route_id": route_id,
                "origin": route["origin"],
                "destination": route["destination"],
                "waiting_passengers": len(waiting_rows),
                "oldest_wait_minutes": oldest,
                "recent_bookings": recent,
                "predicted_passengers": predicted,
                "priority_score": score,
                "recommended_bus_id": str(bus["_id"]) if bus else None,
                "registration_number": bus["registration_number"] if bus else None,
                "bus_capacity": bus["capacity"] if bus else None,
                "recommended_driver_id": str(driver["_id"]) if driver else None,
                "driver_name": driver["full_name"] if driver else None,
                "reason": reason,
                "status": "pending",
                "trip_reference": None,
                "created_at": now,
                "created_by": str(staff["_id"]),
            }
            doc["_id"] = db["dispatch_recommendations"].insert_one(doc).inserted_id
            created.append(doc)

    created.sort(key=lambda item: item["priority_score"], reverse=True)
    publish(
        "recommendations_generated",
        f"{len(created)} scheduling recommendation(s)",
        {"count": len(created)},
        terminal_id=None,
    )
    return [_public(doc) for doc in created]


def list_recommendations(staff: dict, status: str | None = None) -> list:
    query = {}
    if staff["role"] != "super_admin":
        query["terminal_id"] = staff.get("terminal_id")
    if status:
        query["status"] = status
    rows = get_database()["dispatch_recommendations"].find(query).sort("priority_score", -1)
    return [_public(doc) for doc in rows]


def approve_recommendation(recommendation_id: str, staff: dict) -> dict:
    db = get_database()
    try:
        oid = ObjectId(recommendation_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid recommendation id")
    doc = db["dispatch_recommendations"].find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    assert_terminal_access(staff, doc["terminal_id"])
    if doc["status"] != "pending":
        raise HTTPException(status_code=400, detail="Recommendation is no longer pending")
    if not doc.get("recommended_bus_id") or not doc.get("recommended_driver_id"):
        raise HTTPException(status_code=400, detail="A bus and a driver are required before approval")

    # 1. Create the trip (this function ALSO auto-attaches unassigned passengers)
    trip = create_trip(
        TripCreate(
            route_id=doc["route_id"],
            bus_id=doc["recommended_bus_id"],
            driver_id=doc["recommended_driver_id"],
        ),
        staff,
    )
    
    # 2. Mark the recommendation as approved
    updated = db["dispatch_recommendations"].find_one_and_update(
        {"_id": oid, "status": "pending"},
        {"$set": {
            "status": "approved",
            "trip_reference": trip["trip_reference"],
            "approved_at": _now(),
            "approved_by": str(staff["_id"]),
        }},
        return_document=True,
    )

    # 3. Notify the frontend
    publish(
        "trip_created",
        f"Trip {trip['trip_reference']} scheduled for {doc['origin']} to {doc['destination']}",
        {"trip_reference": trip["trip_reference"]},
        terminal_id=doc["terminal_id"],
    )

    # 4. Auto-generate a follow-up recommendation for any remaining unassigned passengers
    remaining = db["queues"].count_documents({
        "route_id": doc["route_id"],
        "terminal_id": doc["terminal_id"],
        "trip_reference": {"$exists": False},
        "status": {"$in": ["waiting", "called", "boarded"]}
    })
    if remaining > 0:
        print(f"[approve_recommendation] {remaining} unassigned passengers remain - generating follow-up recommendation")
        generate_recommendations(staff)

    return _public(updated)


def reject_recommendation(recommendation_id: str, staff: dict) -> dict:
    db = get_database()
    try:
        oid = ObjectId(recommendation_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid recommendation id")
    doc = db["dispatch_recommendations"].find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    assert_terminal_access(staff, doc["terminal_id"])
    if doc["status"] != "pending":
        raise HTTPException(status_code=400, detail="Recommendation is no longer pending")
    updated = db["dispatch_recommendations"].find_one_and_update(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "rejected", "rejected_at": _now(), "rejected_by": str(staff["_id"])}},
        return_document=True,
    )
    return _public(updated)