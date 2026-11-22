"""
Phase 10: Demand prediction with an honest baseline comparison.

Trains a RandomForestRegressor AND a trivial mean-by-hour baseline
on the same train/test split. Reports both. If RF doesn't beat the
baseline, the metrics will show that — we don't hide it.
"""
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd
from fastapi import HTTPException
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split

from app.config import get_settings
from app.database import get_database
from app.intelligence.baseline import HourlyMeanBaseline

MODEL_PATH = Path(__file__).with_name("demand_model.joblib")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings():
    return get_settings()


# ---------- feature engineering ----------

def _hour_frame_from_bookings() -> pd.DataFrame:
    """Aggregate real bookings into hourly passenger counts per route."""
    rows = []
    for booking in get_database()["bookings"].find({"status": {"$ne": "cancelled"}}):
        created = booking.get("created_at")
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        rows.append({
            "route_id": booking["route_id"],
            "day_of_week": created.weekday(),
            "hour": created.hour,
            "passengers": 1,
        })
    if not rows:
        return pd.DataFrame(
            columns=["route_id", "day_of_week", "hour", "passengers"]
        )
    frame = pd.DataFrame(rows)
    return (
        frame.groupby(["route_id", "day_of_week", "hour"], as_index=False)["passengers"]
        .sum()
    )


def _sample_frame() -> pd.DataFrame:
    """
    Fabricated demonstration history. ONLY called when SEED_SAMPLE_DEMAND=true.
    Every inserted record is tagged source='sample' so it can be deleted later.
    """
    db = get_database()
    routes = list(db["routes"].find({"is_active": True}))
    if not routes:
        raise HTTPException(
            status_code=400,
            detail="Create at least one active route before training",
        )

    rng = np.random.default_rng(42)
    rows = []
    start = _now()
    from datetime import timedelta
    start = start - timedelta(days=28)

    for route_index, route in enumerate(routes):
        for day in range(28):
            current = start + timedelta(days=day)
            for hour in (6, 7, 8, 9, 12, 15, 16, 17, 18):
                peak = 8 if hour in (7, 8, 16, 17) else 3
                weekend = 0.6 if current.weekday() >= 5 else 1
                count = max(0, int(rng.normal(peak * weekend + route_index, 1.2)))
                rows.append({
                    "route_id": str(route["_id"]),
                    "day_of_week": current.weekday(),
                    "hour": hour,
                    "passengers": count,
                    "source": "sample",
                })
                db["demand_records"].insert_one({
                    "route_id": str(route["_id"]),
                    "day_of_week": current.weekday(),
                    "hour": hour,
                    "passengers": count,
                    "recorded_at": current.replace(
                        hour=hour, minute=0, second=0, microsecond=0
                    ),
                    "source": "sample",
                })
    return pd.DataFrame(rows)


# ---------- train ----------

def _last_run_at() -> Optional[datetime]:
    run = (
        get_database()["model_training_runs"]
        .find_one({}, sort=[("trained_at", -1)])
    )
    if not run:
        return None
    at = run.get("trained_at")
    if at and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at


def _save_run(doc: dict) -> None:
    db = get_database()
    db["model_training_runs"].insert_one(doc)
    db["model_training_runs"].create_index([("trained_at", -1)])


def train_demand_model(force: bool = False) -> dict:
    settings = _settings()

    # Retrain guard — protects demos from accidental churn
    last = _last_run_at()
    if last and not force:
        age = (_now() - last).total_seconds()
        if age < settings.min_train_interval_seconds:
            bundle = _load()
            if bundle is not None:
                return {
                    **bundle["metrics"],
                    "skipped": True,
                    "message": (
                        f"Model trained {int(age)}s ago; "
                        f"retrain is allowed after {settings.min_train_interval_seconds}s "
                        f"(pass force=true to override)."
                    ),
                }

    real = _hour_frame_from_bookings()
    sample_rows = 0
    frame = real

    if len(real) < 24:
        if not settings.seed_sample_demand:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Only {len(real)} hourly aggregates exist. "
                    f"Produce more bookings, or set SEED_SAMPLE_DEMAND=true "
                    f"to train on demonstration data."
                ),
            )
        sample = _sample_frame()
        sample_rows = len(sample)
        frame = sample if real.empty else pd.concat([real, sample], ignore_index=True)

    if frame["route_id"].nunique() < 1 or len(frame) < 10:
        raise HTTPException(status_code=400, detail="Not enough demand rows to train")

    features = pd.get_dummies(
        frame[["route_id", "day_of_week", "hour"]], columns=["route_id"]
    )
    target = frame["passengers"].astype(float)

    x_train, x_test, y_train, y_test = train_test_split(
        features, target, test_size=0.25, random_state=42
    )

    # ---- RandomForest ----
    rf = RandomForestRegressor(n_estimators=100, random_state=42)
    rf.fit(x_train, y_train)
    rf_pred = rf.predict(x_test)

    # ---- Baseline (mean-by-hour) ----
    baseline = HourlyMeanBaseline().fit(pd.concat([x_train.assign(passengers=y_train)], ignore_index=False))
    # HourlyMeanBaseline expects passengers column; build it cleanly
    train_view = x_train.copy()
    train_view["passengers"] = y_train.values
    baseline = HourlyMeanBaseline().fit(train_view)
    base_pred = baseline.predict(x_test)

    metrics = {
        "rows": int(len(frame)),
        "sample_rows": int(sample_rows),
        "real_rows": int(len(real)),
        "routes": int(frame["route_id"].nunique()),
        "rf_mae": round(float(mean_absolute_error(y_test, rf_pred)), 3),
        "rf_r2": round(float(r2_score(y_test, rf_pred)), 3),
        "baseline_mae": round(float(mean_absolute_error(y_test, base_pred)), 3),
        "baseline_r2": round(float(r2_score(y_test, base_pred)), 3),
        "trained_at": _now().isoformat(),
        "seed_sample": bool(settings.seed_sample_demand),
    }
    # Direction of comparison, defensible statement for the report
    metrics["rf_beats_baseline"] = metrics["rf_mae"] < metrics["baseline_mae"]

    # Backward-compat aliases so old callers reading metrics["mae"] / ["r2"] keep working
    metrics["mae"] = metrics["rf_mae"]
    metrics["r2"] = metrics["rf_r2"]

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {"model": rf, "columns": list(features.columns), "metrics": metrics},
        MODEL_PATH,
    )

    _save_run({
        "type": "model_metrics",
        **metrics,
        "trained_at_dt": _now(),
    })
    # Keep the legacy shape so /analytics that reads demand_records still works
    get_database()["demand_records"].insert_one({"type": "model_metrics", **metrics})

    return metrics


# ---------- predict ----------

def _load():
    if not MODEL_PATH.exists():
        return None
    return joblib.load(MODEL_PATH)


def _predict_with_flag(
    route_id: str, when: Optional[datetime] = None
) -> tuple[float, bool]:
    """Internal: returns (prediction, route_was_known_to_model)."""
    bundle = _load()
    if bundle is None:
        return 0.0, False
    when = when or _now()

    expected_col = f"route_id_{route_id}"
    route_known = expected_col in bundle["columns"]

    row = {
        "day_of_week": when.weekday(),
        "hour": when.hour,
    }
    data = pd.DataFrame([row])
    for column in bundle["columns"]:
        if column.startswith("route_id_"):
            data[column] = 1 if column == expected_col else 0
    data = data.reindex(columns=bundle["columns"], fill_value=0)

    predicted = float(bundle["model"].predict(data)[0])
    return round(max(0, predicted), 1), route_known


def predict_passengers(route_id: str, when: Optional[datetime] = None) -> float:
    """Public API used by scheduling_service — unchanged signature."""
    predicted, _ = _predict_with_flag(route_id, when)
    return predicted


def model_status() -> dict:
    bundle = _load()
    if bundle is None:
        return {"trained": False, "message": "No model has been trained"}
    return {"trained": True, **bundle["metrics"]}