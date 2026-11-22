import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from app.routers.auth import get_current_staff, require_roles
from app.security import decode_access_token
from app.services.analytics_service import dashboard, reports
from app.services.events import recent_events, subscribe, unsubscribe
from app.services.live_snapshot import build_snapshot
from app.services.staff_service import get_staff_by_id

router = APIRouter(tags=["Analytics"])
READERS = ("super_admin", "terminal_admin", "booking_operator")
WS_READERS = set(READERS)


@router.get("/analytics/dashboard")
def dashboard_cards(staff: Annotated[dict, Depends(require_roles(*READERS))]):
    return dashboard(staff)


@router.get("/analytics/reports")
def analytics_reports(staff: Annotated[dict, Depends(require_roles(*READERS))]):
    return reports(staff)


@router.get("/analytics/events")
def events(
    staff: Annotated[dict, Depends(get_current_staff)],
    limit: int = Query(30, ge=1, le=200),
):
    terminal_id = None if staff["role"] == "super_admin" else staff.get("terminal_id")
    return recent_events(limit=limit, terminal_id=terminal_id)


def _ws_authenticate(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        staff_id = payload.get("sub")
        if not staff_id:
            return None
        staff = get_staff_by_id(staff_id)
    except Exception:
        return None
    if not staff or not staff.get("is_active", True):
        return None
    if staff.get("role") not in WS_READERS:
        return None
    return staff


def _matches_scope(event: dict, staff: dict) -> bool:
    if staff["role"] == "super_admin":
        return True
    terminal_id = staff.get("terminal_id")
    if not terminal_id:
        return False
    if event.get("terminal_id") is None:
        return False
    return event["terminal_id"] == terminal_id


@router.websocket("/ws/operations")
async def operations(websocket: WebSocket, token: str | None = Query(default=None)):
    staff = _ws_authenticate(token)
    if staff is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    queue = await subscribe()
    try:
        try:
            snapshot = build_snapshot(staff)
            await websocket.send_json({"type": "snapshot", **snapshot, "staff_role": staff["role"]})
        except Exception as exc:
            await websocket.send_json({"type": "error", "message": f"Snapshot failed: {exc}"})

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "heartbeat"})
                except Exception:
                    break
                continue

            if not _matches_scope(event, staff):
                continue
            try:
                await websocket.send_json({"type": "event", **event})
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await unsubscribe(queue)