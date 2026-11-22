"""
Autopilot: runs the terminal automatically.

Every TICK_SECONDS a background loop (one per deployment, guarded by a database
lease) looks at the queues and trips and, depending on the mode:

  off        does nothing (fully manual, the old behaviour)
  assisted   prepares dispatch recommendations automatically; a human approves
  autopilot  also schedules trips, calls passengers, dispatches buses and moves
             trips along to arrival, writing every decision to a log

Every decision is stored in `autopilot_decisions` and published as an
`autopilot_action` event so the dashboard can show it live.
"""
import logging
import os
import random
import threading
import uuid
from datetime import datetime, timedelta, timezone
from math import ceil

from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.database import get_database
from app.intelligence.demand_model import model_status, predict_passengers, train_demand_model
from app.schemas.booking import BookingCreate
from app.schemas.passenger import CardIssue, PassengerCreate
from app.schemas.trip import TripCreate
from app.services.booking_service import create_booking
from app.services.events import publish
from app.services.passenger_service import issue_card, register_passenger
from app.services.queue_service import call_passenger, quick_board
from app.services.scheduling_service import (
    _available_buses,
    _available_drivers,
    _choose_bus,
    _minutes_waiting,
    generate_recommendations,
)
from app.services.trip_service import advance_trip, create_trip, dispatch_trip

log = logging.getLogger("autopilot")

TICK_SECONDS = 15
LEASE_SECONDS = 45
RETRAIN_AFTER_BOOKINGS = 30
RETRAIN_CHECK_SECONDS = 600
WARN_REPEAT_SECONDS = 600

SYSTEM_STAFF = {
    "_id": "autopilot",
    "role": "super_admin",
    "full_name": "Autopilot",
    "email": "autopilot@system",
}

MODES = ("off", "assisted", "autopilot")

DEFAULT_SETTINGS = {
    "mode": "off",
    # Schedule a new bus when this many passengers are waiting without a trip...
    "min_passengers_to_schedule": 6,
    # ...or when the longest-waiting one has waited this long (minutes)
    "max_wait_minutes": 10,
    # Depart partly full this many minutes after the trip was scheduled...
    "departure_wait_minutes": 8,
    # ...as long as at least this share of the seats are boarded
    "min_boarded_percent": 60,
    # Call passengers to board as soon as they are assigned to a trip
    "auto_call": True,
    # Demo only: automatically board passengers created by the rush simulator
    "simulate_boarding": False,
    # Demo only: run waits and trip progress N times faster than real time
    "demo_time_scale": 1,
    "max_open_trips_per_terminal": 3,
}

_INT_RANGES = {
    "min_passengers_to_schedule": (1, 60),
    "max_wait_minutes": (1, 240),
    "departure_wait_minutes": (1, 120),
    "min_boarded_percent": (10, 100),
    "demo_time_scale": (1, 120),
    "max_open_trips_per_terminal": (1, 20),
}
_BOOL_KEYS = ("auto_call", "simulate_boarding")

HOLDER = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
_stop = threading.Event()
_thread = None
_last_tick = {"at": None}
_last_retrain_try = datetime.min.replace(tzinfo=timezone.utc)
_warned: dict = {}


def _now():
    return datetime.now(timezone.utc)


def _aware(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# ---------------------------------------------------------------- settings

def get_autopilot_settings() -> dict:
    doc = get_database()["system_settings"].find_one({"_id": "autopilot"}) or {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in doc.items() if k in DEFAULT_SETTINGS}}


def _validate(key: str, value):
    if key == "mode":
        if value not in MODES:
            raise HTTPException(status_code=400, detail=f"mode must be one of {', '.join(MODES)}")
        return value
    if key in _BOOL_KEYS:
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"{key} must be true or false")
        return value
    low, high = _INT_RANGES[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise HTTPException(status_code=400, detail=f"{key} must be a number from {low} to {high}")
    return int(value)


def update_autopilot_settings(changes: dict, staff: dict) -> dict:
    clean = {key: _validate(key, value) for key, value in changes.items() if key in DEFAULT_SETTINGS}
    if not clean:
        raise HTTPException(status_code=400, detail="No valid settings supplied")
    before = get_autopilot_settings()
    get_database()["system_settings"].update_one(
        {"_id": "autopilot"},
        {"$set": {**clean, "updated_at": _now(), "updated_by": str(staff["_id"])}},
        upsert=True,
    )
    if "mode" in clean and clean["mode"] != before["mode"]:
        labels = {"off": "Manual", "assisted": "Assisted", "autopilot": "Autopilot"}
        _log("mode_changed", f"{staff.get('full_name', 'A staff member')} switched the terminal to {labels[clean['mode']]} mode.")
    return get_autopilot_settings()


# ---------------------------------------------------------------- decision log

def _log(action: str, message: str, terminal_id=None, details=None):
    get_database()["autopilot_decisions"].insert_one({
        "action": action,
        "message": message,
        "terminal_id": terminal_id,
        "details": details or {},
        "at": _now(),
    })
    publish("autopilot_action", message, {"action": action, "details": details or {}}, terminal_id=terminal_id)


def _warn_once(key: str, message: str, terminal_id=None):
    """Log a problem (for example 'no free bus') at most once every few minutes."""
    now = _now()
    last = _warned.get(key)
    if last and (now - last).total_seconds() < WARN_REPEAT_SECONDS:
        return
    _warned[key] = now
    _log("warning", message, terminal_id)


def list_decisions(staff: dict, limit: int = 30) -> list:
    query = {}
    if staff["role"] != "super_admin":
        query["$or"] = [{"terminal_id": staff.get("terminal_id")}, {"terminal_id": None}]
    rows = get_database()["autopilot_decisions"].find(query).sort("at", -1).limit(max(1, min(limit, 100)))
    return [
        {
            "id": str(row["_id"]),
            "action": row["action"],
            "message": row["message"],
            "at": _aware(row["at"]).isoformat(),
            "details": row.get("details", {}),
        }
        for row in rows
    ]


# ---------------------------------------------------------------- autopilot steps

def _scale(settings: dict) -> int:
    return max(1, int(settings["demo_time_scale"]))


def _progress_trips(settings: dict) -> int:
    """Move in-transit trips along using departure time and the route's estimated duration."""
    db = get_database()
    now = _now()
    scale = _scale(settings)
    durations: dict = {}
    count = 0

    for trip in db["trips"].find({"status": "in_transit"}):
        departed = _aware(trip.get("departed_at"))
        if not departed:
            continue
        rid = trip["route_id"]
        if rid not in durations:
            route = db["routes"].find_one({"_id": ObjectId(rid)}, {"estimated_duration_minutes": 1}) if ObjectId.is_valid(rid) else None
            durations[rid] = float((route or {}).get("estimated_duration_minutes") or 60) or 60.0

        elapsed = (now - departed).total_seconds() / 60 * scale
        percent = min(100, int(elapsed / durations[rid] * 100))
        if percent <= trip.get("progress_percent", 0):
            continue

        advance_trip(trip["trip_reference"], percent, SYSTEM_STAFF)
        if percent >= 100:
            _log(
                "trip_completed",
                f"{trip['trip_reference']} arrived at {trip['destination']}. "
                f"{trip.get('passenger_count', 0)} passengers arrived and bus {trip.get('registration_number', '')} is free again.",
                trip["terminal_id"],
                {"trip_reference": trip["trip_reference"]},
            )
            count += 1
        else:
            publish(
                "trip_progress",
                f"{trip['trip_reference']} is {percent}% of the way to {trip['destination']}",
                {"trip_reference": trip["trip_reference"], "progress_percent": percent},
                terminal_id=trip["terminal_id"],
            )
    return count


def _dispatch_trips(settings: dict) -> int:
    db = get_database()
    now = _now()
    scale = _scale(settings)
    count = 0

    for trip in db["trips"].find({"status": "scheduled"}):
        ref = trip["trip_reference"]
        capacity = int(trip["capacity"])
        boarded = db["queues"].count_documents({"trip_reference": ref, "status": "boarded"})
        if boarded == 0:
            continue

        created = _aware(trip.get("created_at")) or now
        age = (now - created).total_seconds() / 60 * scale
        threshold = ceil(capacity * settings["min_boarded_percent"] / 100)
        still_expected = db["queues"].count_documents({"trip_reference": ref, "status": {"$in": ["waiting", "called"]}})

        is_full = boarded >= capacity
        timed_out = age >= settings["departure_wait_minutes"] and boarded >= threshold
        nobody_left = still_expected == 0 and boarded >= threshold and age >= settings["departure_wait_minutes"] / 2
        if not (is_full or timed_out or nobody_left):
            continue

        try:
            dispatch_trip(ref, SYSTEM_STAFF, allow_partial=not is_full)
        except HTTPException as exc:
            log.warning("Could not dispatch %s: %s", ref, exc.detail)
            continue

        why = "the bus is full" if is_full else f"{boarded}/{capacity} boarded after {int(age)} min"
        _log(
            "dispatch",
            f"Dispatched {ref} to {trip['destination']} with {boarded}/{capacity} passengers ({why}). "
            f"Bus {trip.get('registration_number', '')}, driver {trip.get('driver_name', '')}.",
            trip["terminal_id"],
            {"trip_reference": ref, "passengers": boarded, "capacity": capacity},
        )
        count += 1
    return count


def _simulate_boarding(settings: dict) -> int:
    """Demo only: board a few simulated passengers per tick so the flow is visible."""
    if not settings["simulate_boarding"]:
        return 0
    db = get_database()
    count = 0
    for trip in db["trips"].find({"status": "scheduled"}):
        entries = db["queues"].find(
            {"trip_reference": trip["trip_reference"], "status": {"$in": ["waiting", "called"]}}
        ).sort("queue_number", 1).limit(3)
        for entry in entries:
            passenger = db["passengers"].find_one({"_id": ObjectId(entry["passenger_id"])}, {"simulated": 1})
            if not passenger or not passenger.get("simulated"):
                continue
            try:
                quick_board(entry["booking_reference"], SYSTEM_STAFF)
                count += 1
            except HTTPException:
                pass
    return count


def _call_passengers(settings: dict) -> int:
    """Summon passengers to the bus as soon as they have been assigned to it."""
    if not settings["auto_call"]:
        return 0
    db = get_database()
    count = 0
    for trip in db["trips"].find({"status": "scheduled"}):
        waiting = list(
            db["queues"].find({"trip_reference": trip["trip_reference"], "status": "waiting"}).sort("queue_number", 1)
        )
        called = 0
        for entry in waiting:
            try:
                call_passenger(entry["booking_reference"], SYSTEM_STAFF)
                called += 1
            except HTTPException:
                pass
        if called:
            _log(
                "call_passengers",
                f"Called {called} passenger{'s' if called != 1 else ''} to board {trip['trip_reference']} to {trip['destination']}.",
                trip["terminal_id"],
                {"trip_reference": trip["trip_reference"], "called": called},
            )
            count += 1
    return count


def _create_trips(settings: dict) -> int:
    """Open a new trip when enough passengers are waiting without one."""
    db = get_database()
    scale = _scale(settings)
    count = 0

    for terminal in db["terminals"].find({"is_active": True}):
        tid = str(terminal["_id"])
        open_trips = db["trips"].count_documents({"terminal_id": tid, "status": {"$in": ["scheduled", "in_transit"]}})
        if open_trips >= settings["max_open_trips_per_terminal"]:
            continue

        candidates = []
        for route in db["routes"].find({"terminal_id": tid, "is_active": True}):
            rid = str(route["_id"])
            rows = list(db["queues"].find({
                "route_id": rid,
                "terminal_id": tid,
                "status": {"$in": ["waiting", "called", "boarded"]},
                "trip_reference": {"$exists": False},
            }))
            if not rows:
                continue
            oldest = max(_minutes_waiting(row.get("created_at")) for row in rows)
            if len(rows) >= settings["min_passengers_to_schedule"] or oldest * scale >= settings["max_wait_minutes"]:
                candidates.append((len(rows) * 100 + oldest, route, rows, oldest))
        if not candidates:
            continue

        candidates.sort(key=lambda item: item[0], reverse=True)
        buses = _available_buses(db, tid)
        drivers = _available_drivers(db, tid)
        used_buses, used_drivers = set(), set()

        for _, route, rows, oldest in candidates:
            if open_trips >= settings["max_open_trips_per_terminal"]:
                break
            rid = str(route["_id"])
            free_buses = [b for b in buses if str(b["_id"]) not in used_buses]
            free_drivers = [d for d in drivers if str(d["_id"]) not in used_drivers]
            bus = _choose_bus(free_buses, len(rows))
            driver = free_drivers[0] if free_drivers else None
            if not bus or not driver:
                missing = "bus" if not bus else "driver"
                _warn_once(
                    f"resource:{rid}",
                    f"{len(rows)} passengers are waiting for {route['destination']} but there is no free {missing} at the terminal.",
                    tid,
                )
                continue

            try:
                trip = create_trip(
                    TripCreate(route_id=rid, bus_id=str(bus["_id"]), driver_id=str(driver["_id"])),
                    SYSTEM_STAFF,
                )
            except HTTPException as exc:
                _warn_once(f"create:{rid}", f"Could not schedule a trip to {route['destination']}: {exc.detail}", tid)
                continue

            used_buses.add(str(bus["_id"]))
            used_drivers.add(str(driver["_id"]))
            open_trips += 1
            get_database()["dispatch_recommendations"].update_many(
                {"route_id": rid, "status": "pending"},
                {"$set": {
                    "status": "approved",
                    "trip_reference": trip["trip_reference"],
                    "approved_at": _now(),
                    "approved_by": "autopilot",
                }},
            )
            forecast = predict_passengers(rid)
            assigned = min(len(rows), int(bus["capacity"]))
            _log(
                "create_trip",
                f"Scheduled {trip['trip_reference']} to {route['destination']}: {assigned} passengers assigned to bus "
                f"{bus['registration_number']} ({len(rows)} waiting, longest wait {oldest} min, "
                f"forecast {forecast} this hour).",
                tid,
                {"trip_reference": trip["trip_reference"], "waiting": len(rows), "forecast": forecast},
            )
            publish(
                "trip_created",
                f"Trip {trip['trip_reference']} scheduled for {route['origin']} to {route['destination']}",
                {"trip_reference": trip["trip_reference"]},
                terminal_id=tid,
            )
            count += 1
    return count


def _prepare_recommendations() -> int:
    """Assisted mode: keep a recommendation ready for every route that has unassigned passengers."""
    db = get_database()
    route_ids = db["queues"].distinct(
        "route_id", {"status": {"$in": ["waiting", "called"]}, "trip_reference": {"$exists": False}}
    )
    if not route_ids:
        return 0
    pending = set(db["dispatch_recommendations"].distinct("route_id", {"status": "pending"}))
    if all(rid in pending for rid in route_ids):
        return 0
    created = generate_recommendations(SYSTEM_STAFF)
    if created:
        _log("recommendations", f"Prepared {len(created)} dispatch recommendation{'s' if len(created) != 1 else ''} for approval.")
        return 1
    return 0


def _last_training_time():
    """
    When the demand model was last trained, as an aware datetime (or None).
    Reads the run record directly: it stores `trained_at` as an ISO string and
    `trained_at_dt` as a real datetime, and older rows may have only the string.
    """
    run = get_database()["model_training_runs"].find_one({}, sort=[("trained_at_dt", -1), ("trained_at", -1)])
    if not run:
        return None
    value = run.get("trained_at_dt") or run.get("trained_at")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return _aware(value) if isinstance(value, datetime) else None


def _maybe_retrain() -> int:
    """Retrain the demand model once enough new bookings have accumulated."""
    global _last_retrain_try
    now = _now()
    if (now - _last_retrain_try).total_seconds() < RETRAIN_CHECK_SECONDS:
        return 0
    _last_retrain_try = now

    last = _last_training_time()
    query = {"created_at": {"$gt": last}} if last else {}
    if get_database()["bookings"].count_documents(query) < RETRAIN_AFTER_BOOKINGS:
        return 0
    try:
        metrics = train_demand_model(force=True)
    except HTTPException:
        return 0
    except Exception:
        log.exception("Automatic retraining failed")
        return 0
    _log(
        "retrain",
        f"Demand model retrained on {metrics['rows']} records: R² {metrics['rf_r2']}, "
        f"average error {metrics['rf_mae']} passengers per hour.",
    )
    return 1


def tick() -> int:
    """One pass of the autopilot. Returns the number of decisions made."""
    settings = get_autopilot_settings()
    _last_tick["at"] = _now()
    if settings["mode"] == "off":
        return 0

    if settings["mode"] == "autopilot":
        steps = (_progress_trips, _dispatch_trips, _simulate_boarding, _call_passengers, _create_trips)
        count = 0
        for step in steps:
            try:
                count += step(settings)
            except Exception:
                log.exception("Autopilot step %s failed", step.__name__)
    else:
        count = 0
        try:
            count += _prepare_recommendations()
        except Exception:
            log.exception("Autopilot recommendation step failed")

    try:
        count += _maybe_retrain()
    except Exception:
        log.exception("Autopilot retrain step failed")
    return count


# ---------------------------------------------------------------- background loop

def _acquire_lease() -> bool:
    """Only one process should run the autopilot even if several workers are started."""
    now = _now()
    try:
        get_database()["system_settings"].find_one_and_update(
            {"_id": "autopilot_lease", "$or": [{"expires_at": {"$lt": now}}, {"holder": HOLDER}]},
            {"$set": {"holder": HOLDER, "expires_at": now + timedelta(seconds=LEASE_SECONDS)}},
            upsert=True,
        )
        return True
    except DuplicateKeyError:
        return False


def _loop():
    while not _stop.is_set():
        try:
            if _acquire_lease():
                tick()
        except Exception:
            log.exception("Autopilot loop error")
        _stop.wait(TICK_SECONDS)


def start_autopilot():
    global _thread
    if _thread and _thread.is_alive():
        return
    get_database()["autopilot_decisions"].create_index([("at", -1)])
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="autopilot", daemon=True)
    _thread.start()
    log.info("Autopilot loop started (%s)", HOLDER)


def stop_autopilot():
    _stop.set()


# ---------------------------------------------------------------- status, forecast, demo

def _scope(staff: dict) -> dict:
    return {} if staff["role"] == "super_admin" else {"terminal_id": staff.get("terminal_id")}


def autopilot_status(staff: dict) -> dict:
    db = get_database()
    scope = _scope(staff)
    return {
        "settings": get_autopilot_settings(),
        "running": bool(_thread and _thread.is_alive()),
        "tick_seconds": TICK_SECONDS,
        "last_tick_at": _last_tick["at"].isoformat() if _last_tick["at"] else None,
        "stats": {
            "unassigned_waiting": db["queues"].count_documents(
                {**scope, "status": {"$in": ["waiting", "called"]}, "trip_reference": {"$exists": False}}
            ),
            "scheduled_trips": db["trips"].count_documents({**scope, "status": "scheduled"}),
            "in_transit_trips": db["trips"].count_documents({**scope, "status": "in_transit"}),
            "free_buses": db["buses"].count_documents({**scope, "is_active": True, "status": "available"}),
            "pending_recommendations": db["dispatch_recommendations"].count_documents({**scope, "status": "pending"}),
        },
    }


def forecast_overview(staff: dict, hours: int = 6) -> dict:
    """Predicted passengers per route for the next hours, next to the current queue."""
    hours = max(1, min(int(hours), 12))
    db = get_database()
    start = _now().replace(minute=0, second=0, microsecond=0)
    status = model_status()

    routes = []
    for route in db["routes"].find({**_scope(staff), "is_active": True}).sort("destination", 1):
        rid = str(route["_id"])
        series = [
            {"at": (start + timedelta(hours=h)).isoformat(), "predicted": predict_passengers(rid, start + timedelta(hours=h))}
            for h in range(hours)
        ]
        routes.append({
            "route_id": rid,
            "origin": route["origin"],
            "destination": route["destination"],
            "waiting": db["queues"].count_documents({"route_id": rid, "status": {"$in": ["waiting", "called"]}}),
            "series": series,
            "peak_at": max(series, key=lambda item: item["predicted"])["at"] if series else None,
        })
    return {"model_trained": bool(status.get("trained")), "routes": routes}


_FIRST = ["Chinedu", "Ngozi", "Emeka", "Amaka", "Ifeanyi", "Chioma", "Obinna", "Adaeze", "Uche", "Nneka",
          "Tunde", "Aisha", "Musa", "Fatima", "Kelechi", "Ebuka", "Onyinye", "Kenechukwu", "Ijeoma", "Somto"]
_LAST = ["Okafor", "Nwosu", "Eze", "Okeke", "Chukwu", "Ibe", "Nnamdi", "Obi", "Umeh", "Agu",
         "Adeyemi", "Bello", "Ojo", "Ude", "Onwuka", "Nwankwo", "Ogbu", "Anyanwu", "Mba", "Orji"]


def simulate_rush(route_id: str, count: int, staff: dict) -> dict:
    """Demo: create `count` passengers with funded cards and book them all on one route."""
    if not ObjectId.is_valid(route_id):
        raise HTTPException(status_code=400, detail="Invalid route")
    db = get_database()
    route = db["routes"].find_one({"_id": ObjectId(route_id)})
    if not route or not route.get("is_active", True):
        raise HTTPException(status_code=404, detail="Route not found or inactive")

    count = max(1, min(int(count), 40))
    created = failed = 0
    for _ in range(count):
        try:
            passenger = register_passenger(
                PassengerCreate(
                    full_name=f"{random.choice(_FIRST)} {random.choice(_LAST)}",
                    phone="080" + "".join(random.choices("0123456789", k=8)),
                    terminal_id=route["terminal_id"],
                ),
                staff,
            )
            db["passengers"].update_one({"_id": passenger["_id"]}, {"$set": {"simulated": True}})
            card = issue_card(CardIssue(passenger_id=str(passenger["_id"]), initial_balance="50000"), staff)
            create_booking(BookingCreate(card_number=card["card_number"], route_id=str(route["_id"])), staff)
            created += 1
        except Exception:
            log.exception("Rush simulation: one passenger failed")
            failed += 1

    _log(
        "rush_simulated",
        f"Rush hour simulated: {created} passengers booked for {route['destination']}.",
        route["terminal_id"],
        {"route_id": route_id, "count": created},
    )
    return {"created": created, "failed": failed}