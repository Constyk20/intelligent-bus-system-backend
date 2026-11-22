"""
Phase 8: Passenger arrival simulation.

Periodically creates a demonstration passenger, funds a card, books a
route, and queues them — reusing arrival_service.simulate_arrival()
so the workflow is identical to a real operator.
Controlled by PASSENGER_ARRIVAL_ENABLED so tests are unaffected.

Phase 11: passenger_arrived events carry terminal_id so dashboards
scoped to the correct terminal receive them in real time.
simulation_error stays unscoped (super-admin only).
"""
import asyncio
import random
from datetime import datetime, timezone

from app.config import get_settings
from app.database import get_database
from app.schemas.trip import ArrivalSimulation
from app.services.arrival_service import simulate_arrival
from app.services.events import publish


_FIRST_NAMES = [
    "Chinedu", "Ngozi", "Emeka", "Adaeze", "Ifeanyi", "Chiamaka",
    "Obinna", "Nneka", "Kelechi", "Uche", "Amarachi", "Tunde",
    "Bola", "Yemi", "Fatima", "Ibrahim", "Blessing", "Daniel",
]
_LAST_NAMES = [
    "Okafor", "Adeyemi", "Ibrahim", "Eze", "Bello", "Okeke",
    "Adebayo", "Obi", "Nwosu", "Danjuma", "Chukwu", "Musa",
]


def _now():
    return datetime.now(timezone.utc)


def _fake_name() -> str:
    return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"


def _fake_phone() -> str:
    return f"080{random.randint(10000000, 99999999)}"


_passenger_task: asyncio.Task | None = None


def _pick_route() -> dict | None:
    db = get_database()
    routes = list(db["routes"].find({"is_active": True}))
    return random.choice(routes) if routes else None


def _system_staff() -> dict:
    return {
        "_id": "system_simulator",
        "role": "super_admin",
        "terminal_id": None,
    }


def _one_arrival() -> dict | None:
    route = _pick_route()
    if not route:
        return None
    payload = ArrivalSimulation(
        route_id=str(route["_id"]),
        full_name=_fake_name(),
        phone=_fake_phone(),
        initial_balance="5000.00",
    )
    try:
        result = simulate_arrival(payload, _system_staff())
        publish(
            "passenger_arrived",
            f"{result['passenger_name']} booked {route['origin']} to {route['destination']}",
            {
                "passenger_name": result["passenger_name"],
                "booking_reference": result["booking_reference"],
                "route": f"{route['origin']} to {route['destination']}",
            },
            terminal_id=route["terminal_id"],
        )
        return result
    except Exception as exc:
        # Unscoped on purpose — diagnostics for super-admins only
        publish("simulation_error", f"Passenger arrival failed: {exc}")
        return None


async def _run_loop():
    settings = get_settings()
    interval = max(5, settings.passenger_arrival_interval_seconds)
    publish("passenger_sim_started", f"Passenger simulator every {interval}s")
    try:
        while True:
            try:
                _one_arrival()
            except Exception as exc:
                publish("simulation_error", f"Passenger simulator tick failed: {exc}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        publish("passenger_sim_stopped", "Passenger simulator stopped")
        raise


def start_passenger_simulator() -> asyncio.Task | None:
    global _passenger_task
    settings = get_settings()
    if not settings.simulation_enabled or not settings.passenger_arrival_enabled:
        return None
    if _passenger_task and not _passenger_task.done():
        return _passenger_task
    _passenger_task = asyncio.create_task(_run_loop(), name="passenger-simulator")
    return _passenger_task


async def stop_passenger_simulator():
    global _passenger_task
    if _passenger_task and not _passenger_task.done():
        _passenger_task.cancel()
        try:
            await _passenger_task
        except asyncio.CancelledError:
            pass
    _passenger_task = None