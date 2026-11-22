import os

os.environ["MONGODB_DB_NAME"] = "intelligent_bus_management_test"

import pytest
from bson import ObjectId

from app.database import get_database


@pytest.fixture()
def db():
    database = get_database()
    assert database.name == "intelligent_bus_management_test"
    for name in database.list_collection_names():
        database[name].delete_many({})
    yield database
    for name in database.list_collection_names():
        database[name].delete_many({})


@pytest.fixture()
def staff():
    return {"_id": ObjectId(), "role": "super_admin", "terminal_id": None}