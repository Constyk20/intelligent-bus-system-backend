"""
Phase 11: Event bus with in-memory async pub/sub.

- `publish()` stays SYNCHRONOUS so existing services can call it from
  sync FastAPI endpoints without changes. It appends to a thread-safe
  deque (for /analytics/events) AND schedules a broadcast onto the
  asyncio loop (for WebSocket clients), using loop.call_soon_threadsafe.
- `subscribe()` / `unsubscribe()` are used by the WebSocket handler to
  receive events instantly.
"""
import asyncio
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

_events: deque = deque(maxlen=200)
_events_lock = Lock()
_subscribers: set[asyncio.Queue] = set()
_hub_loop: Optional[asyncio.AbstractEventLoop] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once from FastAPI lifespan so sync publishers can hop into the loop."""
    global _hub_loop
    _hub_loop = loop


def _broadcast(event: dict) -> None:
    """Thread-safe: enqueue the event for every subscriber."""
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest and retry once
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except Exception:
                pass
        except Exception:
            pass


def publish(
    event_type: str,
    message: str,
    payload: Optional[dict] = None,
    terminal_id: Optional[str] = None,
) -> dict:
    """
    Record an event. Synchronous by design.
    `terminal_id` scopes the event so non-super-admins only see their terminal's events.
    """
    event = {
        "type": event_type,
        "message": message,
        "payload": payload or {},
        "terminal_id": terminal_id,
        "at": _now_iso(),
    }

    with _events_lock:
        _events.appendleft(event)

    # Bridge into the asyncio loop if we have one running
    loop = _hub_loop
    if loop is not None and not loop.is_closed():
        try:
            loop.call_soon_threadsafe(_broadcast, event)
        except RuntimeError:
            # Loop is closed (shutdown) — swallow
            pass

    return event


def recent_events(limit: int = 30, terminal_id: Optional[str] = None) -> list:
    """
    Return recent events. If `terminal_id` is given, return only events
    matching that terminal plus unscoped events from super-admin actions.
    """
    with _events_lock:
        snapshot = list(_events)

    if terminal_id is None:
        return snapshot[:limit]

    scoped = [
        e for e in snapshot
        if e.get("terminal_id") == terminal_id or e.get("terminal_id") is None
    ]
    return scoped[:limit]


# ---------- WebSocket subscription API ----------

async def subscribe(maxsize: int = 500) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    _subscribers.add(queue)
    return queue


async def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def subscriber_count() -> int:
    return len(_subscribers)