from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status

from app.database import get_database


def _now():
    return datetime.now(timezone.utc)


def _oid(value: str, label: str = "id") -> ObjectId:
    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {label}")


def _parse_fare(fare: str) -> str:
    try:
        amount = Decimal(fare)
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Fare must be a decimal string such as 1500.00")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Fare must be greater than zero")
    return format(amount.quantize(Decimal("0.01")), "f")


def ensure_core_indexes():
    db = get_database()
    db["terminals"].create_index("code", unique=True)
    db["buses"].create_index("registration_number", unique=True)
    db["drivers"].create_index("license_number", unique=True)
    db["routes"].create_index([("terminal_id", 1), ("origin", 1), ("destination", 1)])
    db["buses"].create_index([("terminal_id", 1), ("status", 1), ("is_active", 1)])


def _require_terminal(terminal_id: str) -> dict:
    doc = get_database()["terminals"].find_one({"_id": _oid(terminal_id, "terminal_id")})
    if not doc or not doc.get("is_active", True):
        raise HTTPException(status_code=404, detail="Terminal not found or inactive")
    return doc


def assert_terminal_access(staff: dict, terminal_id: str):
    if staff["role"] == "super_admin":
        return
    if staff.get("terminal_id") != terminal_id:
        raise HTTPException(status_code=403, detail="You can only manage your assigned terminal")


# ---------- Terminals ----------

def create_terminal(data) -> dict:
    col = get_database()["terminals"]
    if col.find_one({"code": data.code.strip().upper()}):
        raise HTTPException(status_code=400, detail="Terminal code already exists")
    doc = {
        "name": data.name.strip(),
        "code": data.code.strip().upper(),
        "city": data.city.strip(),
        "state": data.state.strip(),
        "address": data.address,
        "is_active": True,
        "created_at": _now(),
        "updated_at": _now(),
    }
    doc["_id"] = col.insert_one(doc).inserted_id
    return doc


def list_terminals(active_only: bool = False) -> list:
    query = {"is_active": True} if active_only else {}
    return list(get_database()["terminals"].find(query).sort("name", 1))


def get_terminal(terminal_id: str) -> dict:
    doc = get_database()["terminals"].find_one({"_id": _oid(terminal_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Terminal not found")
    return doc


def update_terminal(terminal_id: str, data) -> dict:
    updates = {k: v for k, v in data.model_dump(exclude_unset=True).items()}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates["updated_at"] = _now()
    result = get_database()["terminals"].find_one_and_update(
        {"_id": _oid(terminal_id)},
        {"$set": updates},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Terminal not found")
    return result


# ---------- Routes ----------

def create_route(data, staff: dict) -> dict:
    assert_terminal_access(staff, data.terminal_id)
    _require_terminal(data.terminal_id)
    fare = _parse_fare(data.fare)
    doc = {
        "terminal_id": data.terminal_id,
        "origin": data.origin.strip(),
        "destination": data.destination.strip(),
        "fare": fare,
        "estimated_duration_minutes": data.estimated_duration_minutes,
        "distance_km": data.distance_km,
        "is_active": True,
        "created_at": _now(),
        "updated_at": _now(),
    }
    doc["_id"] = get_database()["routes"].insert_one(doc).inserted_id
    return doc


def list_routes(staff: dict, terminal_id: Optional[str] = None, active_only: bool = False) -> list:
    query = {}
    if staff["role"] != "super_admin":
        query["terminal_id"] = staff.get("terminal_id")
    elif terminal_id:
        query["terminal_id"] = terminal_id
    if active_only:
        query["is_active"] = True
    return list(get_database()["routes"].find(query).sort("destination", 1))


def update_route(route_id: str, data, staff: dict) -> dict:
    col = get_database()["routes"]
    existing = col.find_one({"_id": _oid(route_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Route not found")
    assert_terminal_access(staff, existing["terminal_id"])
    updates = {k: v for k, v in data.model_dump(exclude_unset=True).items()}
    if "fare" in updates:
        updates["fare"] = _parse_fare(updates["fare"])
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates["updated_at"] = _now()
    return col.find_one_and_update({"_id": existing["_id"]}, {"$set": updates}, return_document=True)


# ---------- Buses ----------

def create_bus(data, staff: dict) -> dict:
    assert_terminal_access(staff, data.terminal_id)
    _require_terminal(data.terminal_id)
    col = get_database()["buses"]
    reg = data.registration_number.strip().upper()
    if col.find_one({"registration_number": reg}):
        raise HTTPException(status_code=400, detail="Registration number already exists")
    doc = {
        "registration_number": reg,
        "terminal_id": data.terminal_id,
        "capacity": data.capacity,
        "model": data.model,
        "status": data.status,
        "is_active": True,
        "created_at": _now(),
        "updated_at": _now(),
    }
    doc["_id"] = col.insert_one(doc).inserted_id
    return doc


def list_buses(staff: dict, terminal_id: Optional[str] = None, status_filter: Optional[str] = None) -> list:
    query = {}
    if staff["role"] != "super_admin":
        query["terminal_id"] = staff.get("terminal_id")
    elif terminal_id:
        query["terminal_id"] = terminal_id
    if status_filter:
        query["status"] = status_filter
    return list(get_database()["buses"].find(query).sort("registration_number", 1))


def update_bus(bus_id: str, data, staff: dict) -> dict:
    col = get_database()["buses"]
    existing = col.find_one({"_id": _oid(bus_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Bus not found")
    assert_terminal_access(staff, existing["terminal_id"])
    updates = {k: v for k, v in data.model_dump(exclude_unset=True).items()}
    if "terminal_id" in updates:
        assert_terminal_access(staff, updates["terminal_id"])
        _require_terminal(updates["terminal_id"])
    if "status" in updates and updates["status"] == "inactive":
        updates["is_active"] = False
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates["updated_at"] = _now()
    return col.find_one_and_update({"_id": existing["_id"]}, {"$set": updates}, return_document=True)


# ---------- Drivers ----------

def create_driver(data, staff: dict) -> dict:
    assert_terminal_access(staff, data.terminal_id)
    _require_terminal(data.terminal_id)
    col = get_database()["drivers"]
    license_number = data.license_number.strip().upper()
    if col.find_one({"license_number": license_number}):
        raise HTTPException(status_code=400, detail="License number already exists")
    doc = {
        "full_name": data.full_name.strip(),
        "phone": data.phone.strip(),
        "license_number": license_number,
        "terminal_id": data.terminal_id,
        "is_active": True,
        "created_at": _now(),
        "updated_at": _now(),
    }
    doc["_id"] = col.insert_one(doc).inserted_id
    return doc


def list_drivers(staff: dict, terminal_id: Optional[str] = None) -> list:
    query = {}
    if staff["role"] != "super_admin":
        query["terminal_id"] = staff.get("terminal_id")
    elif terminal_id:
        query["terminal_id"] = terminal_id
    return list(get_database()["drivers"].find(query).sort("full_name", 1))


def update_driver(driver_id: str, data, staff: dict) -> dict:
    col = get_database()["drivers"]
    existing = col.find_one({"_id": _oid(driver_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Driver not found")
    assert_terminal_access(staff, existing["terminal_id"])
    updates = {k: v for k, v in data.model_dump(exclude_unset=True).items()}
    if "terminal_id" in updates:
        assert_terminal_access(staff, updates["terminal_id"])
        _require_terminal(updates["terminal_id"])
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates["updated_at"] = _now()
    return col.find_one_and_update({"_id": existing["_id"]}, {"$set": updates}, return_document=True)