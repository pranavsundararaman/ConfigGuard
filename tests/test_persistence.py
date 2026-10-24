from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys

import pytest
from bson import ObjectId

from app import persistence


def _matches(document, query):
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$lt" in expected and (actual is None or actual >= expected["$lt"]):
                return False
        elif actual != expected:
            return False
    return True


class FakeCursor(list):
    def sort(self, key, direction):
        self[:] = sorted(self, key=lambda item: item[key], reverse=direction < 0)
        return self

    def limit(self, count):
        return FakeCursor(self[:count])


class FakeCollection:
    def __init__(self, fail_insert=False, fail_delete=False):
        self.documents = []
        self.fail_insert = fail_insert
        self.fail_delete = fail_delete
        self.indexes = []

    def create_index(self, fields, **kwargs):
        self.indexes.append((fields, kwargs))
        return kwargs.get("name", "fake_index")

    def insert_one(self, document, **kwargs):
        if self.fail_insert:
            raise RuntimeError("injected insert failure")
        stored = {**document, "_id": ObjectId()}
        self.documents.append(stored)
        return type("InsertResult", (), {"inserted_id": stored["_id"]})()

    def find(self, query=None):
        query = query or {}
        return FakeCursor([doc.copy() for doc in self.documents if _matches(doc, query)])

    def find_one(self, query, projection=None):
        document = next((doc.copy() for doc in self.documents if _matches(doc, query)), None)
        if document is not None and projection:
            for field, enabled in projection.items():
                if not enabled:
                    document.pop(field, None)
        return document

    def update_one(self, query, update, upsert=False):
        document = next((doc for doc in self.documents if _matches(doc, query)), None)
        if document is None:
            if upsert:
                stored = {**query, **update.get("$setOnInsert", {}), **update.get("$set", {}), "_id": ObjectId()}
                self.documents.append(stored)
                return type("UpdateResult", (), {"matched_count": 0, "upserted_id": stored["_id"]})()
            return type("UpdateResult", (), {"matched_count": 0, "upserted_id": None})()
        document.update(update.get("$set", {}))
        return type("UpdateResult", (), {"matched_count": 1, "upserted_id": None})()

    def delete_many(self, query):
        if self.fail_delete:
            raise RuntimeError("injected delete failure")
        before = len(self.documents)
        self.documents[:] = [doc for doc in self.documents if not _matches(doc, query)]
        return type("DeleteResult", (), {"deleted_count": before - len(self.documents)})()


class UnsupportedTransactionClient:
    def start_session(self):
        raise NotImplementedError("transactions unavailable")


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    @contextmanager
    def start_transaction(self):
        yield self


class TransactionClient:
    def start_session(self):
        return FakeSession()


class FakeDatabase(dict):
    def __init__(self, client=None):
        super().__init__()
        self.client = client or UnsupportedTransactionClient()

    def __getitem__(self, name):
        if name not in self:
            self[name] = FakeCollection()
        return super().__getitem__(name)


def _bundle():
    return persistence.persist_analysis_bundle(
        "payments",
        [
            {"configuration": {"timeout": 30}, "format": "json", "lifecycle": "deployed"},
            {"configuration": {"timeout": 60}, "format": "json", "lifecycle": "proposed"},
        ],
        {"classification": "REVIEW_REQUIRED"},
        {"before_version_id": 0, "after_version_id": 1},
    )


def test_configuration_versions_are_project_scoped_and_lifecycle_aware(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: fake_db)

    deployed_id = persistence.save_configuration_version("payments", {"timeout": 30}, "JSON", "deployed")
    proposed_id = persistence.save_configuration_version("payments", {"timeout": 60}, "json")
    other_id = persistence.save_configuration_version("search", {"timeout": 60}, "yaml")

    assert persistence.get_configuration_version("payments", deployed_id)["lifecycle"] == "deployed"
    assert persistence.get_configuration_version("payments", other_id) is None
    assert [row["id"] for row in persistence.list_deployed_configuration_versions("payments")] == [deployed_id]
    assert persistence.mark_configuration_deployed("payments", proposed_id)["lifecycle"] == "deployed"
    assert persistence.mark_configuration_deployed("payments", other_id) is None


def test_bundle_uses_transaction_when_supported(monkeypatch):
    fake_db = FakeDatabase(client=TransactionClient())
    monkeypatch.setattr(persistence, "get_database", lambda: fake_db)

    version_ids, analysis_id = _bundle()

    assert len(version_ids) == 2
    assert fake_db["risk_analyses"].find_one({"_id": ObjectId(analysis_id)})["after_version_id"] == version_ids[1]
    assert fake_db["write_operations"].documents == []


def test_fallback_bundle_rolls_back_after_partial_insert_failure(monkeypatch):
    fake_db = FakeDatabase()
    fake_db["risk_analyses"].fail_insert = True
    monkeypatch.setattr(persistence, "get_database", lambda: fake_db)

    with pytest.raises(persistence.PersistenceWriteError, match="was rolled back"):
        _bundle()

    assert fake_db["config_versions"].documents == []
    assert fake_db["risk_analyses"].documents == []
    assert fake_db["write_operations"].documents[0]["state"] == "rolled_back"


def test_failed_compensation_is_journaled_and_recovered(monkeypatch):
    fake_db = FakeDatabase()
    fake_db["risk_analyses"].fail_insert = True
    fake_db["config_versions"].fail_delete = True
    monkeypatch.setattr(persistence, "get_database", lambda: fake_db)

    with pytest.raises(persistence.PersistenceWriteError, match="rollback is pending"):
        _bundle()
    operation = fake_db["write_operations"].documents[0]
    assert operation["state"] == "rollback_pending"

    fake_db["config_versions"].fail_delete = False
    operation["created_at"] = datetime.now(timezone.utc) - timedelta(minutes=6)
    persistence.list_configuration_versions("payments")

    assert fake_db["config_versions"].documents == []
    assert fake_db["write_operations"].documents[0]["state"] == "rolled_back"


def test_lazy_database_fails_only_when_used_without_uri(monkeypatch):
    from app import database

    monkeypatch.setattr(database, "_database", None)
    monkeypatch.setattr(database, "client", None)
    monkeypatch.delenv("MONGODB_URI", raising=False)

    with pytest.raises(RuntimeError, match="MONGODB_URI"):
        database.get_database()


def test_lazy_collection_access_delegates_to_real_database(monkeypatch):
    from app import database

    expected_collection = object()

    class FakeMongoDatabase:
        def __getitem__(self, name):
            assert name == "config_versions"
            return expected_collection

    actual_database = FakeMongoDatabase()

    class FakeMongoClient:
        def __init__(self, uri, serverSelectionTimeoutMS):
            assert uri == "mongodb://test.invalid"

        def __getitem__(self, name):
            assert name == "configguard-test"
            return actual_database

    monkeypatch.setattr(database, "_database", None)
    monkeypatch.setattr(database, "client", None)
    monkeypatch.setattr(database, "MongoClient", FakeMongoClient)
    monkeypatch.setenv("MONGODB_URI", "mongodb://test.invalid")
    monkeypatch.setenv("MONGODB_DATABASE", "configguard-test")

    assert database.db["config_versions"] is expected_collection
    assert database.get_database() is actual_database


def test_application_import_does_not_require_mongodb_uri():
    environment = os.environ.copy()
    environment.pop("MONGODB_URI", None)
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    repository_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, "app.main should import without database configuration"


def test_server_sessions_store_only_hashes_and_can_be_revoked(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: fake_db)

    session_id, csrf_token = persistence.create_auth_session("synthetic-user-id", "synthetic-csrf")
    stored = fake_db["auth_sessions"].documents[0]

    assert session_id not in str(stored)
    assert csrf_token == stored["csrf_token"]
    assert stored["user_id"] == "synthetic-user-id"
    assert persistence.get_auth_session(session_id)["user_id"] == "synthetic-user-id"

    persistence.delete_auth_session(session_id)
    assert persistence.get_auth_session(session_id) is None
