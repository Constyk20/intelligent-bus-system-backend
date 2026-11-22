"""
Phase 10: A trivial baseline demand predictor.

The baseline predicts the *mean number of passengers* for the same
(day_of_week, hour) across all routes and all history. If a machine
learning model cannot beat this baseline on held-out data, we should
say so honestly rather than claim the ML adds value.
"""
from collections import defaultdict

import numpy as np
import pandas as pd


class HourlyMeanBaseline:
    """
    Predicts passengers for a (day_of_week, hour) bucket by averaging
    all training rows in that bucket across every route.
    """

    def __init__(self):
        self._by_hour: dict[tuple[int, int], float] = {}
        self._global_mean: float = 0.0

    def fit(self, frame: pd.DataFrame) -> "HourlyMeanBaseline":
        buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
        for row in frame.itertuples(index=False):
            buckets[(int(row.day_of_week), int(row.hour))].append(float(row.passengers))
        self._by_hour = {
            key: float(np.mean(values)) for key, values in buckets.items()
        }
        self._global_mean = float(frame["passengers"].mean()) if len(frame) else 0.0
        return self

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        preds = []
        for row in features.itertuples(index=False):
            key = (int(row.day_of_week), int(row.hour))
            preds.append(self._by_hour.get(key, self._global_mean))
        return np.array(preds, dtype=float)