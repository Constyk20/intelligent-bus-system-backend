from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import close_client, get_database
from app.routers import (
    auth, bookings, buses, drivers, passengers, queues, routes,
    scheduling, terminals, tickets, trips, intelligence, analytics, autopilot,
)
from app.services.autopilot_service import start_autopilot, stop_autopilot
from app.services.events import set_event_loop
from app.simulation import (
    start_bus_simulator, stop_bus_simulator,
    start_passenger_simulator, stop_passenger_simulator,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_database()
    try:
        db.command("ping")
        print("MongoDB connection successful")
    except Exception as e:
        print(f"MongoDB connection failed: {e}")

    # Phase 11: register the running loop with the event bus for WebSockets
    set_event_loop(asyncio.get_running_loop())

    # Phase 8: start simulation background tasks
    start_bus_simulator()
    start_passenger_simulator()

    # Autopilot engine. It idles while its mode is "off", so starting it is safe.
    start_autopilot()

    yield

    # Graceful shutdown (autopilot first so it stops creating work)
    stop_autopilot()
    await stop_passenger_simulator()
    await stop_bus_simulator()
    close_client()


app = FastAPI(
    title="Intelligent Bus Scheduling & Queue Management System",
    description="Centralized terminal operations dashboard backend",
    version="0.6.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all routers
app.include_router(auth.router)
app.include_router(terminals.router)
app.include_router(routes.router)
app.include_router(buses.router)
app.include_router(drivers.router)
app.include_router(passengers.router)
app.include_router(bookings.router)
app.include_router(tickets.router)
app.include_router(queues.router)
app.include_router(trips.router)
app.include_router(scheduling.router)
app.include_router(intelligence.router)
app.include_router(analytics.router)
app.include_router(autopilot.router)


@app.get("/")
def root():
    return {
        "message": "Intelligent Bus Management System API",
        "status": "running",
        "phase": "Phase 12 - Autopilot",
    }


@app.get("/health")
def health_check():
    try:
        get_database().command("ping")
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}