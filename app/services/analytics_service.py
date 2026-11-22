from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.database import get_database


def _now():
    return datetime.now(timezone.utc)


def _start_of_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _money(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0.00")


def _fmt(amount: Decimal) -> str:
    return format(amount.quantize(Decimal("0.01")), "f")


def _aware(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def dashboard(staff: dict) -> dict:
    db = get_database()
    terminal_filter = {}
    if staff["role"] != "super_admin":
        terminal_filter = {"terminal_id": staff.get("terminal_id")}

    today = _start_of_day(_now())
    booking_query = {**terminal_filter, "created_at": {"$gte": today}, "status": {"$ne": "cancelled"}}
    todays = list(db["bookings"].find(booking_query))
    revenue = sum((_money(row.get("fare")) for row in todays), Decimal("0.00"))

    bus_query = {"is_active": True}
    if terminal_filter:
        bus_query.update(terminal_filter)
    buses = list(db["buses"].find(bus_query))

    return {
        "total_buses": len(buses),
        "available_buses": sum(1 for bus in buses if bus.get("status") == "available"),
        "active_trips": db["trips"].count_documents({**terminal_filter, "status": "in_transit"}),
        "passengers_waiting": db["queues"].count_documents({**terminal_filter, "status": {"$in": ["waiting", "called"]}}),
        "todays_bookings": len(todays),
        "todays_revenue": _fmt(revenue),
        "active_terminals": db["terminals"].count_documents({"is_active": True}) if not terminal_filter else (1 if staff.get("terminal_id") else 0),
        "pending_recommendations": db["dispatch_recommendations"].count_documents({**terminal_filter, "status": "pending"}),
        "currency": "NGN",
        "generated_at": _now().isoformat(),
    }


def _scoped(staff: dict) -> dict:
    if staff["role"] == "super_admin":
        return {}
    return {"terminal_id": staff.get("terminal_id")}


def reports(staff: dict) -> dict:
    db = get_database()
    scope = _scoped(staff)
    now = _now()
    today = _start_of_day(now)
    week = today - timedelta(days=6)

    bookings = list(db["bookings"].find({**scope, "created_at": {"$gte": week}}))
    queues = list(db["queues"].find(scope))
    trips = list(db["trips"].find(scope))
    recommendations = list(db["dispatch_recommendations"].find(scope))

    daily = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        next_day = day + timedelta(days=1)
        rows = [
            row for row in bookings
            if day <= _aware(row.get("created_at")) < next_day and row.get("status") != "cancelled"
        ]
        daily.append({
            "date": day.date().isoformat(),
            "bookings": len(rows),
            "revenue": _fmt(sum((_money(row.get("fare")) for row in rows), Decimal("0.00"))),
        })

    demand = {}
    for row in bookings:
        if row.get("status") == "cancelled":
            continue
        destination = row.get("destination", "Unknown")
        demand[destination] = demand.get(destination, 0) + 1
    demand_rows = [{"destination": key, "passengers": value} for key, value in sorted(demand.items(), key=lambda item: item[1], reverse=True)]

    waits = []
    for row in queues:
        end = row.get("boarded_at") or row.get("called_at") or row.get("no_show_at")
        start = _aware(row.get("created_at"))
        end = _aware(end)
        if start and end and end >= start:
            waits.append((end - start).total_seconds() / 60)
    average_wait = round(sum(waits) / len(waits), 1) if waits else 0

    completed = [trip for trip in trips if trip.get("status") == "completed"]
    utilizations = []
    for trip in completed:
        capacity = trip.get("capacity") or 0
        if capacity:
            utilizations.append(trip.get("passenger_count", 0) / capacity * 100)
    utilization = round(sum(utilizations) / len(utilizations), 1) if utilizations else 0

    route_performance = {}
    for row in bookings:
        key = f"{row.get('origin')} to {row.get('destination')}"
        item = route_performance.setdefault(key, {"route": key, "bookings": 0, "cancelled": 0, "revenue": Decimal("0.00")})
        if row.get("status") == "cancelled":
            item["cancelled"] += 1
        else:
            item["bookings"] += 1
            item["revenue"] += _money(row.get("fare"))
    routes = [
        {**item, "revenue": _fmt(item["revenue"])}
        for item in route_performance.values()
    ]

    approved = sum(1 for row in recommendations if row.get("status") == "approved")
    rejected = sum(1 for row in recommendations if row.get("status") == "rejected")
    pending = sum(1 for row in recommendations if row.get("status") == "pending")

    bus_rows = {}
    for bus in db["buses"].find({**scope, "is_active": True} if scope else {"is_active": True}):
        bus_rows[bus.get("status", "unknown")] = bus_rows.get(bus.get("status", "unknown"), 0) + 1

    return {
        "daily": daily,
        "demand_by_destination": demand_rows,
        "average_waiting_minutes": average_wait,
        "waiting_samples": len(waits),
        "bus_utilization_percent": utilization,
        "completed_trips": len(completed),
        "cancelled_bookings": sum(1 for row in bookings if row.get("status") == "cancelled"),
        "bus_availability": [{"status": key, "count": value} for key, value in bus_rows.items()],
        "route_performance": routes,
        "scheduling": {"pending": pending, "approved": approved, "rejected": rejected},
        "note": "Trip progress is simulated demonstration data, not GPS.",
    }