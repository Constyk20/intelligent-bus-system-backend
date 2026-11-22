from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.database import get_database
from app.intelligence.demand_model import (
    model_status,
    predict_passengers,
    train_demand_model,
)
from app.routers.auth import require_roles

router = APIRouter(prefix="/intelligence", tags=["Demand Prediction"])
MANAGERS = ("super_admin", "terminal_admin")


class PredictionResponse(BaseModel):
    route_id: str
    predicted_passengers: float
    trained: bool


@router.post("/train")
def train(
    _: Annotated[dict, Depends(require_roles("super_admin"))],
    force: bool = Query(False, description="Override the retrain cooldown"),
):
    return train_demand_model(force=force)


@router.get("/status")
def status(_: Annotated[dict, Depends(require_roles(*MANAGERS))]):
    return model_status()


@router.get("/metrics/history")
def metrics_history(
    _: Annotated[dict, Depends(require_roles(*MANAGERS))],
    limit: int = Query(10, ge=1, le=50),
):
    """
    Return the last N training runs so the defense can show model
    progression and RF-vs-baseline comparison over time.
    """
    rows = (
        get_database()["model_training_runs"]
        .find({}, {"_id": 0, "trained_at_dt": 0})
        .sort("trained_at", -1)
        .limit(limit)
    )
    return {"count": limit, "runs": list(rows)}


@router.get("/predict/{route_id}", response_model=PredictionResponse)
def predict(
    route_id: str,
    _: Annotated[dict, Depends(require_roles(*MANAGERS))],
):
    trained = model_status().get("trained", False)
    return PredictionResponse(
        route_id=route_id,
        predicted_passengers=predict_passengers(route_id) if trained else 0,
        trained=trained,
    )