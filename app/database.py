from pymongo import MongoClient
from pymongo.database import Database
from app.config import get_settings

_settings = get_settings()
_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(_settings.mongodb_uri)
    return _client


def get_database() -> Database:
    client = get_client()
    return client[_settings.mongodb_db_name]


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None