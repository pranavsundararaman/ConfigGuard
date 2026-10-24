import json
import os
import re
import secrets
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from pydantic import ValidationError
import yaml

from .ci_models import CIAnalysisIngest, sanitize_ci_name
from .database import MongoConfigurationError
from .historical import MIN_HISTORICAL_OBSERVATIONS, analyze_historical_values
from .parser import normalize_historical_configuration, parse_config
from .persistence import (
    get_analysis as fetch_project_analysis,
    get_configuration_version,
    list_analyses as list_project_analyses,
    get_ci_analysis,
    list_ci_analyses,
    list_deployed_configuration_versions,
    list_configuration_versions,
    mark_configuration_deployed,
    persist_analysis_bundle,
    save_configuration_version,
    upsert_ci_analysis,
)
from .risk_engine import assess_risk

app = FastAPI(
    title="ConfigGuard API",
    version="0.1.0",
    description="Configuration Change Risk Analysis for CI/CD Pipelines",
)

_CI_BODY_LIMIT = 64 * 1024
_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ci_bearer = HTTPBearer(auto_error=False)


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


def _check_ci_token(credentials: HTTPAuthorizationCredentials | None, setting: str) -> None:
    expected = os.getenv(setting)
    if not expected:
        raise HTTPException(status_code=503, detail="CI integration is not configured.")
    supplied = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
    if not secrets.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
        raise HTTPException(status_code=401, detail="CI integration authentication failed.")


def _require_ci_ingest_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_ci_bearer),
) -> None:
    _check_ci_token(credentials, "CONFIGGUARD_CI_TOKEN")


def _require_ci_read_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_ci_bearer),
) -> None:
    _check_ci_token(credentials, "CONFIGGUARD_CI_READ_TOKEN")


async def _read_ci_payload(request: Request) -> CIAnalysisIngest:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _CI_BODY_LIMIT:
            raise HTTPException(status_code=413, detail="CI analysis payload is too large.")
    try:
        raw = json.loads(body)
        return CIAnalysisIngest.model_validate(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValidationError):
        # Pydantic errors can include attacker-controlled values; return no input excerpts.
        raise HTTPException(status_code=422, detail="Invalid CI analysis payload.") from None


def _validate_ci_project(project_id: str) -> None:
    if not _PROJECT_ID_PATTERN.fullmatch(project_id):
        raise HTTPException(status_code=422, detail="Invalid project identifier.")


@app.post(
    "/api/projects/{project_id}/ci-analyses",
    status_code=200,
    description="Authenticated with `Authorization: Bearer <CONFIGGUARD_CI_TOKEN>`. Only sanitised metadata and findings are accepted.",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": CIAnalysisIngest.model_json_schema()}},
        }
    },
)
async def ingest_ci_analysis(
    project_id: str,
    _: None = Depends(_require_ci_ingest_token),
    payload: CIAnalysisIngest = Depends(_read_ci_payload),
):
    _validate_ci_project(project_id)
    cleaned_payload = payload.model_dump(mode="json")
    # Names are untrusted even after schema validation; normalize them again at storage.
    for finding in cleaned_payload["findings"]:
        finding["file_path"] = sanitize_ci_name(finding["file_path"], path=True)
        finding["parameter"] = sanitize_ci_name(finding["parameter"])
    try:
        saved, created = upsert_ci_analysis(project_id, cleaned_payload)
    except MongoConfigurationError:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="CI analysis storage is unavailable.") from None
    return {"analysis": saved, "created": created}


@app.get(
    "/api/projects/{project_id}/ci-analyses",
    dependencies=[Depends(_require_ci_read_token)],
    description="Requires `Authorization: Bearer <CONFIGGUARD_CI_READ_TOKEN>` and returns reports scoped to this project.",
)
def get_project_ci_analyses(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
):
    _validate_ci_project(project_id)
    try:
        return list_ci_analyses(project_id, limit)
    except MongoConfigurationError:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="CI analysis storage is unavailable.") from None


@app.get(
    "/api/projects/{project_id}/ci-analyses/{analysis_id}",
    dependencies=[Depends(_require_ci_read_token)],
    description="Requires `Authorization: Bearer <CONFIGGUARD_CI_READ_TOKEN>` and returns a report scoped to this project.",
)
def get_project_ci_analysis(project_id: str, analysis_id: str):
    _validate_ci_project(project_id)
    try:
        analysis = get_ci_analysis(project_id, analysis_id)
    except MongoConfigurationError:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="CI analysis storage is unavailable.") from None
    if analysis is None:
        raise HTTPException(status_code=404, detail="CI analysis not found.")
    return analysis


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
