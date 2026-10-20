from typing import Any, Literal

from bson import ObjectId
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from .database import db
from .historical import analyze_historical_values
from .parser import parse_config
from .persistence import (
    get_analysis as fetch_project_analysis,
    get_configuration_version,
    list_analyses as list_project_analyses,
    list_configuration_versions,
    save_analysis,
    save_configuration_version,
)
from .risk_engine import assess_risk

app = FastAPI(
    title="ConfigGuard API",
    version="0.1.0",
    description="Configuration Change Risk Analysis for CI/CD Pipelines",
)

class ConfigurationVersionRequest(BaseModel):
    configuration: dict[str, Any]
    format: Literal["yaml", "yml", "json", "env"]


class HistoricalAnalysisRequest(BaseModel):
    configuration: dict[str, Any]
    format: Literal["yaml", "yml", "json", "env"] = "json"
    min_observations: int = Field(default=3, ge=1, le=100)


def _serialize_document(document: dict) -> dict:
    """Convert MongoDB identifiers and BSON values to API-friendly JSON."""
    document["id"] = str(document.pop("_id"))
    return jsonable_encoder(document)


def _get_document(collection, document_id: str, resource_name: str) -> dict:
    if not ObjectId.is_valid(document_id):
        raise HTTPException(status_code=400, detail=f"Invalid {resource_name} ID.")
    document = collection.find_one({"_id": ObjectId(document_id)})
    if document is None:
        raise HTTPException(status_code=404, detail=f"{resource_name} not found.")
    return _serialize_document(document)

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "configguard"}


@app.get("/api/config-versions")
def list_config_versions(limit: int = Query(default=50, ge=1, le=200)):
    documents = db["config_versions"].find().sort("created_at", -1).limit(limit)
    return [_serialize_document(document) for document in documents]


@app.get("/api/config-versions/{version_id}")
def get_config_version(version_id: str):
    return _get_document(db["config_versions"], version_id, "Configuration version")


@app.get("/api/analyses")
def list_analyses(limit: int = Query(default=50, ge=1, le=200)):
    documents = db["risk_analyses"].find().sort("created_at", -1).limit(limit)
    return [_serialize_document(document) for document in documents]


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: str):
    return _get_document(db["risk_analyses"], analysis_id, "Risk analysis")


@app.post("/api/projects/{project_id}/config-versions", status_code=201)
def create_configuration_version(project_id: str, request: ConfigurationVersionRequest):
    if not project_id.strip():
        raise HTTPException(status_code=400, detail="Project ID cannot be empty.")
    version_id = save_configuration_version(project_id, request.configuration, request.format)
    return get_configuration_version(project_id, version_id)


@app.get("/api/projects/{project_id}/config-versions")
def get_project_configuration_history(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
):
    return list_configuration_versions(project_id, limit)


@app.get("/api/projects/{project_id}/config-versions/{version_id}")
def get_project_configuration_version(project_id: str, version_id: str):
    version = get_configuration_version(project_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Configuration version not found.")
    return version


@app.get("/api/projects/{project_id}/analyses")
def get_project_analyses(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
):
    return list_project_analyses(project_id, limit)


@app.get("/api/projects/{project_id}/analyses/{analysis_id}")
def get_project_analysis(project_id: str, analysis_id: str):
    analysis = fetch_project_analysis(project_id, analysis_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    return analysis


@app.post("/api/projects/{project_id}/historical-analysis", status_code=201)
def create_historical_analysis(project_id: str, request: HistoricalAnalysisRequest):
    if not project_id.strip():
        raise HTTPException(status_code=400, detail="Project ID cannot be empty.")
    history = list_configuration_versions(project_id, limit=200)
    prior_configurations = [item["configuration"] for item in history]
    result = analyze_historical_values(
        request.configuration,
        prior_configurations,
        min_observations=request.min_observations,
    )
    proposed_version_id = save_configuration_version(
        project_id,
        request.configuration,
        request.format,
    )
    result.update({
        "project_id": project_id,
        "proposed_version_id": proposed_version_id,
        "baseline_version_ids": [item["id"] for item in history],
    })
    analysis_id = save_analysis(project_id, result)
    return {"id": analysis_id, **jsonable_encoder(result)}

@app.post("/api/analyze")
async def analyze(
    old_config: UploadFile = File(...),
    new_config: UploadFile = File(...),
):
    try:
        old_text = (await old_config.read()).decode("utf-8")
        new_text = (await new_config.read()).decode("utf-8")
        old_data = parse_config(old_config.filename or "", old_text)
        new_data = parse_config(new_config.filename or "", new_text)
        result = assess_risk(old_data, new_data)
        before_version_id = save_configuration_version(
            "default", old_data, (old_config.filename or "").rsplit(".", 1)[-1]
        )
        after_version_id = save_configuration_version(
            "default", new_data, (new_config.filename or "").rsplit(".", 1)[-1]
        )
        save_analysis("default", {
            **result.model_dump(mode="json"),
            "before_version_id": before_version_id,
            "after_version_id": after_version_id,
        })
        return result.model_dump()
    except UnicodeDecodeError:
        return JSONResponse(
            status_code=400,
            content={"detail": "Configuration files must be UTF-8 text files."},
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
