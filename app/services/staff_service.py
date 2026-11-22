from datetime import datetime, timezone
from typing import Optional
from bson import ObjectId
from pymongo.collection import Collection
from fastapi import HTTPException, status

from app.database import get_database
from app.security import hash_password, verify_password, create_access_token
from app.schemas.staff import StaffCreate, StaffBootstrap, StaffResponse


def get_staff_collection() -> Collection:
    return get_database()["staff_users"]


def ensure_indexes():
    """Create unique index on email (safe to call multiple times)"""
    col = get_staff_collection()
    col.create_index("email", unique=True)


def staff_to_response(doc: dict) -> StaffResponse:
    return StaffResponse(
        id=str(doc["_id"]),
        email=doc["email"],
        full_name=doc["full_name"],
        role=doc["role"],
        terminal_id=doc.get("terminal_id"),
        is_active=doc.get("is_active", True),
        created_at=doc["created_at"],
        last_login=doc.get("last_login"),
    )


def create_staff(data: StaffCreate, created_by: Optional[str] = None) -> StaffResponse:
    col = get_staff_collection()

    if col.find_one({"email": data.email.lower()}):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    # Basic role validation for terminal assignment
    if data.role in ("terminal_admin", "booking_operator") and not data.terminal_id:
        # We allow None for now; later phases will enforce terminal_id
        pass

    now = datetime.now(timezone.utc)
    doc = {
        "email": data.email.lower(),
        "password_hash": hash_password(data.password),
        "full_name": data.full_name.strip(),
        "role": data.role,
        "terminal_id": data.terminal_id,
        "is_active": True,
        "created_at": now,
        "updated_at": now,
        "last_login": None,
        "created_by": created_by,
    }

    result = col.insert_one(doc)
    doc["_id"] = result.inserted_id
    return staff_to_response(doc)


def bootstrap_super_admin(data: StaffBootstrap) -> StaffResponse:
    """Create the first Super Admin. Fails if any staff already exists."""
    col = get_staff_collection()
    if col.count_documents({}) > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bootstrap is only allowed when no staff users exist"
        )

    staff_data = StaffCreate(
        email=data.email,
        password=data.password,
        full_name=data.full_name,
        role="super_admin",
        terminal_id=None,
    )
    return create_staff(staff_data)


def authenticate_staff(email: str, password: str) -> Optional[dict]:
    col = get_staff_collection()
    doc = col.find_one({"email": email.lower()})
    if not doc:
        return None
    if not doc.get("is_active", True):
        return None
    if not verify_password(password, doc["password_hash"]):
        return None
    return doc


def update_last_login(staff_id: str):
    col = get_staff_collection()
    col.update_one(
        {"_id": ObjectId(staff_id)},
        {"$set": {"last_login": datetime.now(timezone.utc)}}
    )


def get_staff_by_id(staff_id: str) -> Optional[dict]:
    col = get_staff_collection()
    try:
        return col.find_one({"_id": ObjectId(staff_id)})
    except Exception:
        return None