from datetime import datetime, timedelta, timezone
from hashlib import sha256
import secrets
from threading import RLock
from typing import Any
from uuid import uuid4

from bson import ObjectId
from fastapi.encoders import jsonable_encoder
from pymongo.errors import DuplicateKeyError, OperationFailure

from .database import get_database

_write_lock = RLock()
_RECOVERY_AGE = timedelta(minutes=5)


class PersistenceWriteError(RuntimeError):
    """A bundle write failed; the message never includes database credentials."""


def _serialize(document: dict[str, Any]) -> dict[str, Any]:
    document["id"] = str(document.pop("_id"))
    return jsonable_encoder(document)


def _serialize_ci(document: dict[str, Any]) -> dict[str, Any]:
    result = _serialize(document)
    result.pop("owner_user_id", None)
    return result


def _version_document(project_id: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "configuration": jsonable_encoder(item["configuration"]),
        "format": item["format"].lower(),
        "lifecycle": item.get("lifecycle", "proposed"),
        "created_at": datetime.now(timezone.utc),
    }


def save_configuration_version(
    project_id: str,
    configuration: dict[str, Any],
    format: str,
    lifecycle: str = "proposed",
) -> str:
    """Save a single configuration version. New versions are proposals by default."""
    if lifecycle not in {"proposed", "deployed"}:
        raise ValueError("lifecycle must be 'proposed' or 'deployed'.")
    document = _version_document(project_id, {
        "configuration": configuration,
        "format": format,
        "lifecycle": lifecycle,
    })
    return str(get_database()["config_versions"].insert_one(document).inserted_id)


def _recover_stale_operations(database) -> None:
    """Compensate abandoned non-transactional writes after a grace period."""
    cutoff = datetime.now(timezone.utc) - _RECOVERY_AGE
    operations = database["write_operations"].find({
        "state": {"$in": ["pending", "rollback_pending"]},
        "created_at": {"$lt": cutoff},
    })
    for operation in operations:
        operation_id = operation["operation_id"]
        try:
            database["config_versions"].delete_many({"write_operation_id": operation_id})
            database["risk_analyses"].delete_many({"write_operation_id": operation_id})
            database["write_operations"].update_one(
                {"operation_id": operation_id},
                {"$set": {"state": "rolled_back", "recovered_at": datetime.now(timezone.utc)}},
            )
        except Exception:
            # Keep rollback_pending visible to the next recovery attempt.
            database["write_operations"].update_one(
                {"operation_id": operation_id},
                {"$set": {"state": "rollback_pending"}},
            )


def _visible_query(database, query: dict[str, Any]) -> dict[str, Any]:
    pending = [
        operation["operation_id"]
        for operation in database["write_operations"].find({
            "state": {"$in": ["pending", "rollback_pending"]}
        })
    ]
    if pending:
        query = {**query, "write_operation_id": {"$nin": pending}}
    return query


def _find_versions(database, project_id: str, limit: int, lifecycle: str | None):
    query: dict[str, Any] = {"project_id": project_id}
    if lifecycle:
        query["lifecycle"] = lifecycle
    query = _visible_query(database, query)
    return database["config_versions"].find(query).sort("created_at", -1).limit(limit)


def list_configuration_versions(
    project_id: str,
    limit: int = 50,
    lifecycle: str | None = None,
) -> list[dict[str, Any]]:
    database = get_database()
    with _write_lock:
        _recover_stale_operations(database)
        return [_serialize(document) for document in _find_versions(database, project_id, limit, lifecycle)]


def list_deployed_configuration_versions(project_id: str, limit: int = 200) -> list[dict[str, Any]]:
    return list_configuration_versions(project_id, limit=limit, lifecycle="deployed")


def get_configuration_version(project_id: str, version_id: str) -> dict[str, Any] | None:
    if not ObjectId.is_valid(version_id):
        return None
    database = get_database()
    with _write_lock:
        _recover_stale_operations(database)
        document = database["config_versions"].find_one(_visible_query(database, {
            "_id": ObjectId(version_id),
            "project_id": project_id,
        }))
        return _serialize(document) if document else None


def mark_configuration_deployed(project_id: str, version_id: str) -> dict[str, Any] | None:
    if not ObjectId.is_valid(version_id):
        return None
    database = get_database()
    with _write_lock:
        _recover_stale_operations(database)
        result = database["config_versions"].update_one(
            {"_id": ObjectId(version_id), "project_id": project_id},
            {"$set": {"lifecycle": "deployed", "deployed_at": datetime.now(timezone.utc)}},
        )
        if result.matched_count == 0:
            return None
        document = database["config_versions"].find_one({
            "_id": ObjectId(version_id),
            "project_id": project_id,
        })
        return _serialize(document) if document else None


def _transaction_unsupported(error: Exception) -> bool:
    if isinstance(error, (AttributeError, NotImplementedError)):
        return True
    if isinstance(error, OperationFailure) and error.code == 20:
        return True
    message = str(error).lower()
    return any(marker in message for marker in (
        "transaction numbers are only allowed",
        "transactions are not supported",
        "does not support transactions",
        "replica set member or mongos",
        "sessions are not supported",
        "does not support sessions",
    ))


def _insert_bundle(database, project_id, versions, analysis, links, session=None, operation_id=None):
    version_ids: list[str] = []
    for item in versions:
        document = _version_document(project_id, item)
        if operation_id:
            document["write_operation_id"] = operation_id
        inserted = database["config_versions"].insert_one(document, **({"session": session} if session else {}))
        version_ids.append(str(inserted.inserted_id))

    analysis_document = {
        **jsonable_encoder(analysis),
        "project_id": project_id,
        "created_at": datetime.now(timezone.utc),
    }
    for field, version_index in links.items():
        analysis_document[field] = version_ids[version_index]
    if operation_id:
        analysis_document["write_operation_id"] = operation_id
    analysis_id = database["risk_analyses"].insert_one(
        analysis_document,
        **({"session": session} if session else {}),
    ).inserted_id
    return version_ids, str(analysis_id)


def _write_with_journal(database, project_id, versions, analysis, links):
    operation_id = uuid4().hex
    operations = database["write_operations"]
    operations.insert_one({
        "operation_id": operation_id,
        "project_id": project_id,
        "state": "pending",
        "created_at": datetime.now(timezone.utc),
    })
    try:
        result = _insert_bundle(database, project_id, versions, analysis, links, operation_id=operation_id)
        operations.update_one(
            {"operation_id": operation_id},
            {"$set": {"state": "committed", "completed_at": datetime.now(timezone.utc)}},
        )
        return result
    except Exception:
        try:
            database["config_versions"].delete_many({"write_operation_id": operation_id})
            database["risk_analyses"].delete_many({"write_operation_id": operation_id})
            operations.update_one(
                {"operation_id": operation_id},
                {"$set": {"state": "rolled_back", "completed_at": datetime.now(timezone.utc)}},
            )
        except Exception:
            try:
                operations.update_one(
                    {"operation_id": operation_id},
                    {"$set": {"state": "rollback_pending"}},
                )
            except Exception:
                pass
            raise PersistenceWriteError(
                f"Bundle write failed and rollback is pending for operation {operation_id}."
            ) from None
        raise PersistenceWriteError(
            f"Bundle write failed; operation {operation_id} was rolled back."
        ) from None


def persist_analysis_bundle(
    project_id: str,
    versions: list[dict[str, Any]],
    analysis: dict[str, Any],
    links: dict[str, int],
) -> tuple[list[str], str]:
    """Atomically store linked records, or use a recoverable journaled saga.

    Atlas replica sets and sharded clusters use a MongoDB transaction. Standalone
    MongoDB deployments use a write journal, operation tags, compensating deletes,
    and stale-operation recovery. Pending records are hidden from API retrieval.
    """
    database = get_database()
    with _write_lock:
        _recover_stale_operations(database)
        try:
            client = database.client
            with client.start_session() as session:
                with session.start_transaction():
                    return _insert_bundle(database, project_id, versions, analysis, links, session=session)
        except Exception as error:
            if not _transaction_unsupported(error):
                raise
        return _write_with_journal(database, project_id, versions, analysis, links)


def save_analysis(project_id: str, result: dict[str, Any]) -> str:
    """Save a single standalone analysis record."""
    document = {
        **jsonable_encoder(result),
        "project_id": project_id,
        "created_at": datetime.now(timezone.utc),
    }
    return str(get_database()["risk_analyses"].insert_one(document).inserted_id)


def list_analyses(project_id: str, limit: int = 50) -> list[dict[str, Any]]:
    database = get_database()
    with _write_lock:
        _recover_stale_operations(database)
        documents = database["risk_analyses"].find(_visible_query(database, {"project_id": project_id}))
        return [_serialize(document) for document in documents.sort("created_at", -1).limit(limit)]


def get_analysis(project_id: str, analysis_id: str) -> dict[str, Any] | None:
    if not ObjectId.is_valid(analysis_id):
        return None
    database = get_database()
    with _write_lock:
        _recover_stale_operations(database)
        document = database["risk_analyses"].find_one(_visible_query(database, {
            "_id": ObjectId(analysis_id),
            "project_id": project_id,
        }))
        return _serialize(document) if document else None


def upsert_ci_analysis(project_id: str, payload: dict[str, Any], owner_user_id: str | None = None) -> tuple[dict[str, Any], bool]:
    """Persist one sanitised CI report, deduplicating retries by PR head SHA."""
    database = get_database()
    collection = database["ci_analyses"]
    unique_fields = [
        ("project_id", 1),
        ("repository", 1),
        ("pull_request_number", 1),
        ("head_sha", 1),
    ]
    collection.create_index(unique_fields, unique=True, name="ci_analysis_delivery_unique")
    query = {
        "project_id": project_id,
        "repository": payload["repository"],
        "pull_request_number": payload["pull_request_number"],
        "head_sha": payload["head_sha"],
    }
    document = {**payload, **query, "created_at": datetime.now(timezone.utc)}
    if owner_user_id is not None:
        document["owner_user_id"] = owner_user_id
    with _write_lock:
        try:
            result = collection.update_one(query, {"$setOnInsert": document}, upsert=True)
        except DuplicateKeyError:
            # A concurrent retry may insert after this request's upsert lookup.
            result = None
        saved = collection.find_one(query)
        if saved is None:
            raise PersistenceWriteError("CI analysis could not be stored.")
        return _serialize_ci(saved), bool(result and result.upserted_id is not None)


def list_ci_analyses(project_id: str, limit: int = 50, owner_user_id: str | None = None) -> list[dict[str, Any]]:
    database = get_database()
    with _write_lock:
        query = {"project_id": project_id}
        if owner_user_id is not None:
            query["owner_user_id"] = owner_user_id
        documents = database["ci_analyses"].find(query)
        return [_serialize_ci(document) for document in documents.sort("created_at", -1).limit(limit)]


def get_ci_analysis(project_id: str, analysis_id: str, owner_user_id: str | None = None) -> dict[str, Any] | None:
    if not ObjectId.is_valid(analysis_id):
        return None
    database = get_database()
    with _write_lock:
        query = {
            "_id": ObjectId(analysis_id),
            "project_id": project_id,
        }
        if owner_user_id is not None:
            query["owner_user_id"] = owner_user_id
        document = database["ci_analyses"].find_one(query)
        return _serialize_ci(document) if document else None


def upsert_github_user(github_id: int, login: str, avatar_url: str | None, repositories: list[dict[str, Any]]) -> dict[str, Any]:
    """Store a GitHub identity and a public-repository snapshot; never store OAuth tokens."""
    database = get_database()
    users = database["users"]
    users.create_index([("github_id", 1)], unique=True, name="github_user_unique")
    users.update_one(
        {"github_id": github_id},
        {"$set": {
            "github_login": login[:100],
            "avatar_url": avatar_url,
            "public_repositories": repositories,
            "updated_at": datetime.now(timezone.utc),
        }, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    user = users.find_one({"github_id": github_id})
    if user is None:
        raise PersistenceWriteError("GitHub user could not be stored.")
    return _serialize(user)


def get_github_user(user_id: str, *, include_repositories: bool = False) -> dict[str, Any] | None:
    if not ObjectId.is_valid(user_id):
        return None
    projection = None if include_repositories else {"public_repositories": 0}
    user = get_database()["users"].find_one({"_id": ObjectId(user_id)}, projection)
    return _serialize(user) if user else None


def list_github_repositories(user_id: str) -> list[dict[str, Any]]:
    user = get_github_user(user_id, include_repositories=True)
    return user.get("public_repositories", []) if user else []


def list_user_projects(user_id: str) -> list[dict[str, Any]]:
    documents = get_database()["projects"].find({"owner_user_id": user_id})
    result = []
    for item in documents.sort("created_at", -1):
        project = _serialize(item)
        project.pop("owner_user_id", None)
        result.append(project)
    return result


def get_user_project(user_id: str, project_id: str) -> dict[str, Any] | None:
    identifier: Any = ObjectId(project_id) if ObjectId.is_valid(project_id) else project_id
    item = get_database()["projects"].find_one({"_id": identifier, "owner_user_id": user_id})
    return _serialize(item) if item else None


def get_project(project_id: str) -> dict[str, Any] | None:
    identifier: Any = ObjectId(project_id) if ObjectId.is_valid(project_id) else project_id
    item = get_database()["projects"].find_one({"_id": identifier})
    return _serialize(item) if item else None


def select_repository_project(user_id: str, repository: dict[str, Any]) -> dict[str, Any]:
    """Create or retrieve one private ConfigGuard project for a selected public repo."""
    database = get_database()
    collection = database["projects"]
    collection.create_index(
        [("owner_user_id", 1), ("github_repository_id", 1)],
        unique=True,
        name="user_github_repository_unique",
    )
    query = {"owner_user_id": user_id, "github_repository_id": repository["id"]}
    collection.update_one(query, {"$setOnInsert": {
        **query,
        "repository": repository["full_name"],
        "repository_url": repository["html_url"],
        "default_branch": repository.get("default_branch", ""),
        "created_at": datetime.now(timezone.utc),
    }}, upsert=True)
    project = collection.find_one(query)
    if project is None:
        raise PersistenceWriteError("Project could not be selected.")
    result = _serialize(project)
    result.pop("owner_user_id", None)
    return result


def create_auth_session(user_id: str, csrf_token: str | None = None) -> tuple[str, str]:
    """Create a revocable server-side session; only its random ID is sent in the cookie."""
    database = get_database()
    collection = database["auth_sessions"]
    collection.create_index([("session_hash", 1)], unique=True, name="auth_session_unique")
    collection.create_index([("expires_at", 1)], expireAfterSeconds=0, name="auth_session_ttl")
    session_id = secrets.token_urlsafe(32)
    csrf = csrf_token or secrets.token_urlsafe(32)
    collection.insert_one({
        "session_hash": sha256(session_id.encode("ascii")).hexdigest(),
        "user_id": user_id,
        "csrf_token": csrf,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=8),
    })
    return session_id, csrf


def get_auth_session(session_id: str) -> dict[str, Any] | None:
    if not isinstance(session_id, str) or len(session_id) > 128:
        return None
    try:
        digest = sha256(session_id.encode("ascii")).hexdigest()
    except UnicodeEncodeError:
        return None
    session = get_database()["auth_sessions"].find_one({"session_hash": digest})
    expires_at = session.get("expires_at") if session else None
    if not isinstance(expires_at, datetime):
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return None
    return session


def delete_auth_session(session_id: str) -> None:
    if isinstance(session_id, str) and len(session_id) <= 128:
        try:
            digest = sha256(session_id.encode("ascii")).hexdigest()
        except UnicodeEncodeError:
            return
        get_database()["auth_sessions"].delete_many({"session_hash": digest})
