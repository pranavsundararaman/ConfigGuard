from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

from bson import ObjectId
from fastapi.encoders import jsonable_encoder
from pymongo.errors import OperationFailure

from .database import get_database

_write_lock = RLock()
_RECOVERY_AGE = timedelta(minutes=5)


class PersistenceWriteError(RuntimeError):
    """A bundle write failed; the message never includes database credentials."""


def _serialize(document: dict[str, Any]) -> dict[str, Any]:
    document["id"] = str(document.pop("_id"))
    return jsonable_encoder(document)


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
