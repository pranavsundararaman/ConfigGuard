from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from fastapi.encoders import jsonable_encoder

from .database import db


def _serialize(document: dict[str, Any]) -> dict[str, Any]:
    document["id"] = str(document.pop("_id"))
    return jsonable_encoder(document)


def save_configuration_version(
    project_id: str,
    configuration: dict[str, Any],
    format: str,
) -> str:
    document = {
        "project_id": project_id,
        "configuration": jsonable_encoder(configuration),
        "format": format.lower(),
        "created_at": datetime.now(timezone.utc),
    }
    return str(db["config_versions"].insert_one(document).inserted_id)


def list_configuration_versions(project_id: str, limit: int = 50) -> list[dict[str, Any]]:
    documents = (
        db["config_versions"]
        .find({"project_id": project_id})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [_serialize(document) for document in documents]


def get_configuration_version(project_id: str, version_id: str) -> dict[str, Any] | None:
    if not ObjectId.is_valid(version_id):
        return None
    document = db["config_versions"].find_one({
        "_id": ObjectId(version_id),
        "project_id": project_id,
    })
    return _serialize(document) if document else None


def save_analysis(project_id: str, result: dict[str, Any]) -> str:
    document = {
        **jsonable_encoder(result),
        "project_id": project_id,
        "created_at": datetime.now(timezone.utc),
    }
    return str(db["risk_analyses"].insert_one(document).inserted_id)


def list_analyses(project_id: str, limit: int = 50) -> list[dict[str, Any]]:
    documents = (
        db["risk_analyses"]
        .find({"project_id": project_id})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [_serialize(document) for document in documents]


def get_analysis(project_id: str, analysis_id: str) -> dict[str, Any] | None:
    if not ObjectId.is_valid(analysis_id):
        return None
    document = db["risk_analyses"].find_one({
        "_id": ObjectId(analysis_id),
        "project_id": project_id,
    })
    return _serialize(document) if document else None
