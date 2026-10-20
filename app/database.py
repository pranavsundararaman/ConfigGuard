import os

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "configguard")

if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is missing from your .env file")

client = None
db = None


def get_database():
    """Create the Mongo client on first database use, not during app import."""
    global client, db
    if db is None:
        if not MONGODB_URI:
            raise RuntimeError("MONGODB_URI is missing from your .env file")
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        db = client[MONGODB_DATABASE]
    return db


class _LazyDatabase:
    def __getitem__(self, collection_name):
        return get_database()[collection_name]


db = _LazyDatabase()


def check_database_connection():
    """Verify that MongoDB Atlas is reachable."""
    get_database()
    client.admin.command("ping")
    return True
