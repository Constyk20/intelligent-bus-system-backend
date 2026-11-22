"""
Phase 11: Snapshot for newly-connected dashboard clients.

Reuses existing services so the numbers match GET /analytics/dashboard.
"""
from app.database import get_database
from app.services.analytics_service import dashboard


def build_snapshot(staff: dict) -> dict:
    db = get_database()
    terminal_id = None if staff["role"] == "super_admin" else staff.get("terminal_id")

    queue_query = {"status": {"$in": ["waiting", "called"]}}
    bus_query = {"is_active": True}
    rec_query = {"status": "pending"}
    if terminal_id:
        queue_query["terminal_id"] = terminal_id
        bus_query["terminal_id"] = terminal_id
        rec_query["terminal_id"] = terminal_id

    queues = [
        {
            "booking_reference": row["booking_reference"],
            "queue_number": row["queue_number"],
            "route_id": row["route_id"],
            "origin": row["origin"],
            "destination": row["destination"],
            "status": row["status"],
        }
        for row in db["queues"].find(queue_query).sort("queue_number", 1).limit(50)
    ]

    buses = [
        {
            "id": str(row["_id"]),
            "registration_number": row["registration_number"],
            "status": row.get("status", "unknown"),
            "capacity": row["capacity"],
        }
        for row in db["buses"].find(bus_query).limit(100)
    ]

    recommendations = [
        {
            "id": str(row["_id"]),
            "route_id": row["route_id"],
            "destination": row["destination"],
            "waiting_passengers": row["waiting_passengers"],
            "priority_score": row["priority_score"],
        }
        for row in db["dispatch_recommendations"].find(rec_query)
        .sort("priority_score", -1).limit(20)
    ]

    return {
        "dashboard": dashboard(staff),
        "queues": queues,
        "buses": buses,
        "recommendations": recommendations,
    }