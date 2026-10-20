from bson import ObjectId

from app import persistence


class FakeCursor(list):
    def sort(self, key, direction):
        self[:] = sorted(self, key=lambda item: item[key], reverse=direction < 0)
        return self

    def limit(self, count):
        return FakeCursor(self[:count])


class FakeCollection:
    def __init__(self):
        self.documents = []

    def insert_one(self, document):
        stored = {**document, "_id": ObjectId()}
        self.documents.append(stored)
        return type("InsertResult", (), {"inserted_id": stored["_id"]})()

    def find(self, query):
        return FakeCursor([doc.copy() for doc in self.documents if doc["project_id"] == query["project_id"]])

    def find_one(self, query):
        return next((doc.copy() for doc in self.documents if all(doc.get(k) == v for k, v in query.items())), None)


class FakeDatabase(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = FakeCollection()
        return super().__getitem__(name)


def test_configuration_versions_are_saved_and_scoped_by_project(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(persistence, "db", fake_db)

    version_id = persistence.save_configuration_version("payments", {"timeout": 30}, "JSON")
    other_id = persistence.save_configuration_version("search", {"timeout": 60}, "yaml")

    assert persistence.get_configuration_version("payments", version_id)["configuration"] == {"timeout": 30}
    assert persistence.get_configuration_version("payments", other_id) is None
    assert persistence.list_configuration_versions("payments")[0]["format"] == "json"


def test_analysis_is_saved_separately_with_project_id(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(persistence, "db", fake_db)

    analysis_id = persistence.save_analysis("payments", {"classification": "SAFE"})
    saved = fake_db["risk_analyses"].find_one({"_id": ObjectId(analysis_id)})

    assert saved["project_id"] == "payments"
    assert saved["classification"] == "SAFE"
