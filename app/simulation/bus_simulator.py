"""
Phase 8: Bus movement simulation.
When a trip reaches 100%, it calls complete_trip() which marks
all passengers as 'arrived' at their destination.
"""
import asyncio
from datetime import datetime, timezone

from app.config import get_settings
from app.database import get_database
from app.services.events import publish


def _now():
    return datetime.now(timezone.utc)


_bus_task: asyncio.Task | None = None


def _step_for(duration_minutes: int, tick_seconds: int) -> float:
    if duration_minutes <= 0:
        duration_minutes = 30
    total_ticks = (duration_minutes * 60) / tick_seconds
    if total_ticks <= 0:
        return 100.0
    return max(0.5, 100.0 / total_ticks)


async def _tick_once(tick_seconds: int) -> int:
    db = get_database()
    trips = list(db["trips"].find({"status": "in_transit"}))
    advanced = 0

    for trip in trips:
        try:
            route = db["routes"].find_one({"_id": _oid(trip["route_id"])})
            duration = (route or {}).get("estimated_duration_minutes", 30)
            step = _step_for(duration, tick_seconds)

            current = float(trip.get("progress_percent", 0))
            new_progress = min(100.0, current + step)

            if new_progress >= 100:
                from app.services.trip_service import complete_trip
                complete_trip(trip["trip_reference"], _SYSTEM_STAFF)
                publish(
                    "trip_progress_completed",
                    f"{trip['trip_reference']} auto-completed by simulator",
                    {"trip_reference": trip["trip_reference"]},
                    terminal_id=trip["terminal_id"],
                )
            else:
                db["trips"].update_one(
                    {"_id": trip["_id"], "status": "in_transit"},
                    {"$set": {"progress_percent": round(new_progress, 1)}},
                )
                publish(
                    "trip_progress",
                    f"{trip['trip_reference']} at {round(new_progress, 1)}%",
                    {
                        "trip_reference": trip["trip_reference"],
                        "progress_percent": round(new_progress, 1),
                    },
                    terminal_id=trip["terminal_id"],
                )
            advanced += 1
        except Exception as exc:
            publish(
                "simulation_error",
                f"Trip simulator skipped {trip.get('trip_reference')}: {exc}",
                {"trip_reference": trip.get("trip_reference")},
            )
    return advanced


async def _run_loop():
    settings = get_settings()
    tick = max(1, settings.simulation_tick_seconds)
    publish("simulation_started", f"Bus simulator running every {tick}s")
    try:
        while True:
            try:
                await _tick_once(tick)
            except Exception as exc:
                publish("simulation_error", f"Bus simulator tick failed: {exc}")
            await asyncio.sleep(tick)
    except asyncio.CancelledError:
        publish("simulation_stopped", "Bus simulator stopped")
        raise


def start_bus_simulator() -> asyncio.Task | None:
    global _bus_task
    settings = get_settings()
    if not settings.simulation_enabled:
        return None
    if _bus_task and not _bus_task.done():
        return _bus_task
    _bus_task = asyncio.create_task(_run_loop(), name="bus-simulator")
    return _bus_task


async def stop_bus_simulator():
    global _bus_task
    if _bus_task and not _bus_task.done():
        _bus_task.cancel()
        try:
            await _bus_task
        except asyncio.CancelledError:
            pass
    _bus_task = None


def _oid(value: str):
    from bson import ObjectId
    try:
        return ObjectId(value)
    except Exception:
        return None


_SYSTEM_STAFF = {
    "_id": "system_simulator",
    "role": "super_admin",
    "terminal_id": None,
}