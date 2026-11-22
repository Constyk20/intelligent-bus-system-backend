from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.security import (
    create_access_token,
    decode_access_token,
    oauth2_scheme,
)
from app.config import get_settings
from app.schemas.staff import (
    StaffCreate,
    StaffBootstrap,
    StaffResponse,
    TokenResponse,
)
from app.services.staff_service import (
    authenticate_staff,
    bootstrap_super_admin,
    create_staff,
    get_staff_by_id,
    staff_to_response,
    update_last_login,
    ensure_indexes,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])

settings = get_settings()


# ---------- Dependencies ----------

async def get_current_staff(token: Annotated[str, Depends(oauth2_scheme)]) -> dict:
    payload = decode_access_token(token)
    staff_id: str = payload.get("sub")
    if staff_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    staff = get_staff_by_id(staff_id)
    if staff is None or not staff.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Staff not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return staff


def require_roles(*allowed_roles: str):
    def role_checker(current_staff: Annotated[dict, Depends(get_current_staff)]):
        if current_staff["role"] not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required roles: {', '.join(allowed_roles)}"
            )
        return current_staff
    return role_checker


# ---------- Endpoints ----------

@router.post("/bootstrap", response_model=StaffResponse, status_code=status.HTTP_201_CREATED)
def bootstrap(data: StaffBootstrap):
    """
    Create the very first Super Administrator.
    This endpoint works ONLY when the staff_users collection is empty.
    After the first Super Admin is created, this endpoint permanently returns 400.
    """
    ensure_indexes()
    return bootstrap_super_admin(data)


@router.post("/login", response_model=TokenResponse)
def login(form_data: Annotated[OAuth2PasswordRequestForm, Depends()]):
    """
    Login with email + password.
    Use form fields: username = email, password = password
    (OAuth2PasswordRequestForm standard)
    """
    staff = authenticate_staff(form_data.username, form_data.password)
    if not staff:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    update_last_login(str(staff["_id"]))

    access_token = create_access_token(
        data={"sub": str(staff["_id"]), "role": staff["role"]},
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
    )

    return TokenResponse(
        access_token=access_token,
        staff=staff_to_response(staff),
    )


@router.get("/me", response_model=StaffResponse)
def get_me(current_staff: Annotated[dict, Depends(get_current_staff)]):
    """Return the currently authenticated staff member"""
    return staff_to_response(current_staff)


@router.post("/staff", response_model=StaffResponse, status_code=status.HTTP_201_CREATED)
def create_new_staff(
    data: StaffCreate,
    current_staff: Annotated[dict, Depends(require_roles("super_admin"))],
):
    """
    Create a new staff member.
    Only Super Administrator can call this endpoint.
    """
    return create_staff(data, created_by=str(current_staff["_id"]))