import os
from threading import Lock

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

client = None
_database = None
_database_lock = Lock()


class MongoConfigurationError(RuntimeError):
    """Raised when a database operation is attempted without a configured URI."""


def get_database():
    """Create the Mongo client on first database use, not during app import."""
    global client, _database
    if _database is not None:
        return _database
    with _database_lock:
        if _database is not None:
            return _database
        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise MongoConfigurationError(
                "MongoDB is not configured. Set MONGODB_URI before using this operation."
            )
        database_name = os.getenv("MONGODB_DATABASE", "configguard")
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        _database = client[database_name]
        return _database


class _LazyDatabase:
    def __getitem__(self, collection_name):
        return get_database()[collection_name]


db = _LazyDatabase()


def check_database_connection():
    """Verify that MongoDB Atlas is reachable."""
    get_database()
    client.admin.command("ping")
    return True
