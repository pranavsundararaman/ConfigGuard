from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
import yaml

from .database import MongoConfigurationError
from .historical import MIN_HISTORICAL_OBSERVATIONS, analyze_historical_values
from .parser import normalize_historical_configuration, parse_config
from .persistence import (
    get_analysis as fetch_project_analysis,
    get_configuration_version,
    list_analyses as list_project_analyses,
    list_deployed_configuration_versions,
    list_configuration_versions,
    mark_configuration_deployed,
    persist_analysis_bundle,
    save_configuration_version,
)
from .risk_engine import assess_risk

app = FastAPI(
    title="ConfigGuard API",
    version="0.1.0",
    description="Configuration Change Risk Analysis for CI/CD Pipelines",
)


@app.exception_handler(MongoConfigurationError)
async def mongo_configuration_error_handler(request, exc: MongoConfigurationError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})

class ConfigurationVersionRequest(BaseModel):
    configuration: dict[str, Any]
    format: Literal["yaml", "yml", "json", "env"]


class HistoricalAnalysisRequest(BaseModel):
    configuration: dict[str, Any]
    format: Literal["yaml", "yml", "json", "env"] = "json"
    min_observations: int = Field(default=MIN_HISTORICAL_OBSERVATIONS, ge=MIN_HISTORICAL_OBSERVATIONS, le=100)


class ConfigurationParseRequest(BaseModel):
    content: str
    format: Literal["yaml", "yml", "json", "env"]


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "configguard"}


@app.post("/api/parse-config")
def parse_configuration(request: ConfigurationParseRequest):
    """Parse browser-provided configuration text with the backend parsers."""
    try:
        configuration = parse_config(f"configuration.{request.format}", request.content, strict_env=request.format == "env")
        return {"configuration": configuration}
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/config-versions")
def list_config_versions(
    project_id: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    return list_configuration_versions(project_id, limit)


@app.get("/api/config-versions/{version_id}")
def get_config_version(version_id: str, project_id: str = Query(min_length=1)):
    version = get_configuration_version(project_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Configuration version not found.")
    return version


@app.get("/api/analyses")
def list_analyses(
    project_id: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    return list_project_analyses(project_id, limit)


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: str, project_id: str = Query(min_length=1)):
    analysis = fetch_project_analysis(project_id, analysis_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Risk analysis not found.")
    return analysis


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


@app.post("/api/projects/{project_id}/config-versions/{version_id}/deploy")
def deploy_configuration_version(project_id: str, version_id: str):
    version = mark_configuration_deployed(project_id, version_id)
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
    history = list_deployed_configuration_versions(project_id, limit=200)
    try:
        proposed = normalize_historical_configuration(request.configuration, request.format)
        prior_configurations = [
            normalize_historical_configuration(item["configuration"], item.get("format", "json"))
            for item in history
        ]
        result = analyze_historical_values(
            proposed,
            prior_configurations,
            min_observations=request.min_observations,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result.update({
        "project_id": project_id,
        "baseline_version_ids": [item["id"] for item in history],
    })
    version_ids, analysis_id = persist_analysis_bundle(
        project_id,
        [{"configuration": request.configuration, "format": request.format, "lifecycle": "proposed"}],
        result,
        {"proposed_version_id": 0},
    )
    result["proposed_version_id"] = version_ids[0]
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
        persist_analysis_bundle(
            "default",
            [
                {
                    "configuration": old_data,
                    "format": (old_config.filename or "").rsplit(".", 1)[-1],
                    "lifecycle": "deployed",
                },
                {
                    "configuration": new_data,
                    "format": (new_config.filename or "").rsplit(".", 1)[-1],
                    "lifecycle": "proposed",
                },
            ],
            result.model_dump(mode="json"),
            {"before_version_id": 0, "after_version_id": 1},
        )
        return result.model_dump()
    except UnicodeDecodeError:
        return JSONResponse(
            status_code=400,
            content={"detail": "Configuration files must be UTF-8 text files."},
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
