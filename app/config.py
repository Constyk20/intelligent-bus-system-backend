from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    mongodb_uri: str
    mongodb_db_name: str = "intelligent_bus_management"
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 480

    # ---- Phase 8: simulation ----
    simulation_enabled: bool = False
    simulation_tick_seconds: int = 10
    passenger_arrival_interval_seconds: int = 30
    passenger_arrival_enabled: bool = False

    # ---- Phase 10: ML demand model ----
    seed_sample_demand: bool = False         # if True, fabricate rows when real data is low
    min_train_interval_seconds: int = 60     # refuse retraining within N seconds

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()