import json
import logging
import os
import re
import secrets
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from pydantic import ValidationError
import yaml

from .ci_models import CIAnalysisIngest, sanitize_ci_name
from .database import MongoConfigurationError
from .authentication import github_client, require_csrf, require_current_user, require_user_project
from .session import SignedSessionMiddleware
from .historical import MIN_HISTORICAL_OBSERVATIONS, analyze_historical_values
from .parser import normalize_historical_configuration, parse_config
from .persistence import (
    get_analysis as fetch_project_analysis,
    create_auth_session,
    delete_auth_session,
    get_auth_session,
    get_configuration_version,
    list_analyses as list_project_analyses,
    get_ci_analysis,
    get_project,
    get_github_user,
    list_ci_analyses,
    list_github_repositories,
    list_user_projects,
    list_deployed_configuration_versions,
    list_configuration_versions,
    mark_configuration_deployed,
    persist_analysis_bundle,
    save_configuration_version,
    select_repository_project,
    upsert_github_user,
    upsert_ci_analysis,
)
from .risk_engine import assess_risk

app = FastAPI(
    title="ConfigGuard API",
    version="0.1.0",
    description="Configuration Change Risk Analysis for CI/CD Pipelines",
)
# Uvicorn routes this logger through its configured stderr handler. Using an
# application-named logger can leave diagnostics dependent on root propagation
# under custom logging configurations.
_logger = logging.getLogger("uvicorn.error")


def _oauth_callback_error_category(stage: str, exc: Exception) -> str:
    """Map callback failures to fixed safe labels; never include provider text."""
    exception_type = type(exc).__name__
    exception_module = type(exc).__module__
    if stage == "oauth_session_cookie_validation":
        return "session_cookie_unavailable"
    if exception_type == "MismatchingStateError":
        return "state_validation_failed"
    if stage == "oauth_client_configuration":
        return "configuration_error"
    if stage == "oauth_token_exchange":
        if exception_type in {"OAuthError", "InvalidGrantError", "MissingTokenError"}:
            return "provider_rejected_token_exchange"
        if exception_module.startswith(("httpx", "httpcore", "socket", "ssl")):
            return "external_http_error"
        return "token_exchange_error"
    if stage in {"github_profile_lookup", "github_repository_lookup"}:
        if exception_module.startswith(("httpx", "httpcore", "socket", "ssl")):
            return "external_http_error"
        return "provider_response_error"
    if stage in {"github_user_persistence", "server_session_creation"}:
        return "persistence_error"
    if stage == "oauth_state_validation":
        return "state_validation_failed"
    return "callback_error"
if os.getenv("APP_ENV", "development").lower() == "production" and not os.getenv("CONFIGGUARD_SESSION_SECRET"):
    raise RuntimeError("CONFIGGUARD_SESSION_SECRET must be configured in production.")
app.add_middleware(
    SignedSessionMiddleware,
    secret_key=os.getenv("CONFIGGUARD_SESSION_SECRET") or secrets.token_urlsafe(48),
    cookie_name="configguard_session",
    max_age=60 * 60 * 8,
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


class RepositorySelectionRequest(BaseModel):
    repository_id: int = Field(gt=0, strict=True)


class ProjectActivationRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)


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


@app.get("/api/auth/github/login")
async def github_login(request: Request):
    github = github_client()
    redirect_uri = os.environ["GITHUB_REDIRECT_URI"]
    try:
        return await github.authorize_redirect(request, redirect_uri)
    except Exception:
        raise HTTPException(status_code=503, detail="GitHub sign-in could not be started.") from None


def _public_repository_snapshot(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise ValueError("Invalid repository response.")
    repositories: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("private") is not False:
            continue
        repository_id = item.get("id")
        full_name = item.get("full_name")
        html_url = item.get("html_url")
        owner = item.get("owner")
        if (
            type(repository_id) is not int
            or not isinstance(full_name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", full_name)
            or not isinstance(html_url, str)
            or urlsplit(html_url).scheme != "https"
            or urlsplit(html_url).hostname != "github.com"
            or urlsplit(html_url).path.rstrip("/") != f"/{full_name}"
            or not isinstance(owner, dict)
            or not isinstance(owner.get("login"), str)
            or owner["login"].casefold() != full_name.split("/", 1)[0].casefold()
        ):
            continue
        repositories.append({
            "id": repository_id,
            "full_name": full_name,
            "owner": owner["login"][:100],
            "name": full_name.split("/", 1)[1],
            "html_url": html_url,
            "default_branch": item.get("default_branch", "")[:200] if isinstance(item.get("default_branch", ""), str) else "",
        })
    return repositories[:5000]


@app.get("/api/auth/github/callback")
async def github_callback(request: Request):
    stage = "oauth_client_configuration"
    try:
        github = github_client()
        stage = "oauth_token_exchange"
        try:
            token = await github.authorize_access_token(request)
        except Exception as exc:
            # Authlib performs state retrieval/validation before making the
            # token request. Distinguish that rejection without inspecting or
            # logging callback parameters or exception text.
            if type(exc).__name__ == "MismatchingStateError":
                stage = (
                    "oauth_state_validation"
                    if getattr(request.state, "configguard_session_valid", False)
                    else "oauth_session_cookie_validation"
                )
            raise
        stage = "github_profile_lookup"
        profile_response = await github.get("user", token=token)
        if profile_response.status_code != 200:
            raise ValueError("GitHub profile request failed.")
        profile = profile_response.json()
        github_id = profile.get("id") if isinstance(profile, dict) else None
        login = profile.get("login") if isinstance(profile, dict) else None
        if type(github_id) is not int or not isinstance(login, str) or not login:
            raise ValueError("GitHub profile response was invalid.")

        repositories: list[dict[str, Any]] = []
        stage = "github_repository_lookup"
        for page in range(1, 51):
            repository_response = await github.get(
                "user/repos",
                token=token,
                params={"visibility": "public", "sort": "full_name", "per_page": 100, "page": page},
            )
            if repository_response.status_code != 200:
                raise ValueError("GitHub repository request failed.")
            page_items = repository_response.json()
            page_repositories = _public_repository_snapshot(page_items)
            repositories.extend(page_repositories)
            if not isinstance(page_items, list) or len(page_items) < 100:
                break
        avatar_url = profile.get("avatar_url")
        if not isinstance(avatar_url, str) or urlsplit(avatar_url).scheme != "https" or urlsplit(avatar_url).hostname not in {"github.com", "avatars.githubusercontent.com"}:
            avatar_url = None
        stage = "github_user_persistence"
        user = upsert_github_user(github_id, login, avatar_url, repositories)
        # Authlib keeps OAuth state in the signed cookie. Replace it with a
        # random, revocable server-side session after identity validation.
        stage = "server_session_creation"
        session_id, _csrf_token = create_auth_session(user["id"])
        stage = "session_rotation"
        request.session.clear()
        request.session["session_id"] = session_id
        return RedirectResponse("http://localhost:5173/", status_code=303)
    except HTTPException as exc:
        _logger.warning(
            "GitHub OAuth callback failed stage=%s exception_type=%s category=%s",
            stage,
            type(exc).__name__,
            _oauth_callback_error_category(stage, exc),
        )
        request.session.clear()
        raise
    except MongoConfigurationError as exc:
        _logger.error(
            "GitHub OAuth callback failed stage=%s exception_type=%s category=%s",
            stage,
            type(exc).__name__,
            _oauth_callback_error_category(stage, exc),
        )
        request.session.clear()
        raise
    except Exception as exc:
        # Do not log exception text or traceback: HTTP/OAuth errors can contain
        # callback parameters, tokens, request headers, or provider responses.
        _logger.warning(
            "GitHub OAuth callback failed stage=%s exception_type=%s category=%s",
            stage,
            type(exc).__name__,
            _oauth_callback_error_category(stage, exc),
        )
        request.session.clear()
        raise HTTPException(status_code=502, detail="GitHub sign-in could not be completed.") from None


@app.get("/api/auth/me")
def auth_me(request: Request):
    session_id = request.session.get("session_id")
    if not isinstance(session_id, str):
        return {"authenticated": False, "user": None, "csrf_token": None}
    auth_session = get_auth_session(session_id)
    if auth_session is None:
        request.session.clear()
        return {"authenticated": False, "user": None, "csrf_token": None}
    user = get_github_user(auth_session["user_id"])
    if user is None:
        request.session.clear()
        return {"authenticated": False, "user": None, "csrf_token": None}
    return {
        "authenticated": True,
        "user": {"login": user["github_login"], "avatar_url": user.get("avatar_url")},
        "csrf_token": auth_session["csrf_token"],
        "active_project_id": request.session.get("active_project_id"),
    }


@app.post("/api/auth/logout")
def auth_logout(request: Request, _: dict = Depends(require_csrf)):
    session_id = request.session.get("session_id")
    if isinstance(session_id, str):
        delete_auth_session(session_id)
    request.session.clear()
    return {"authenticated": False}


@app.get("/api/github/repositories")
def github_repositories(user: dict = Depends(require_current_user)):
    return list_github_repositories(user["id"])


@app.get("/api/projects")
def user_projects(user: dict = Depends(require_current_user)):
    return list_user_projects(user["id"])


@app.post("/api/projects/select")
def select_project(request: Request, body: RepositorySelectionRequest, user: dict = Depends(require_csrf)):
    available_repositories = list_github_repositories(user["id"])
    repository = next((
        item for item in available_repositories
        if isinstance(item, dict) and item.get("id") == body.repository_id
    ), None)
    if repository is None:
        raise HTTPException(status_code=404, detail="Repository not found.")
    project = select_repository_project(user["id"], repository)
    request.session["active_project_id"] = project["id"]
    return project


@app.post("/api/projects/activate")
def activate_project(request: Request, body: ProjectActivationRequest, user: dict = Depends(require_csrf)):
    require_user_project(body.project_id, user)
    request.session["active_project_id"] = body.project_id
    return {"active_project_id": body.project_id}


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
        project = get_project(project_id)
        if project is None or project.get("repository") != cleaned_payload["repository"]:
            raise HTTPException(status_code=404, detail="Project and repository association not found.")
        saved, created = upsert_ci_analysis(project_id, cleaned_payload, project["owner_user_id"])
    except HTTPException:
        raise
    except MongoConfigurationError:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="CI analysis storage is unavailable.") from None
    return {"analysis": saved, "created": created}


@app.get(
    "/api/projects/{project_id}/ci-analyses",
    description="Requires a GitHub-authenticated user session and returns reports scoped to an owned project.",
)
def get_project_ci_analyses(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(require_current_user),
):
    _validate_ci_project(project_id)
    require_user_project(project_id, user)
    try:
        return list_ci_analyses(project_id, limit, user["id"])
    except MongoConfigurationError:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="CI analysis storage is unavailable.") from None


@app.get(
    "/api/projects/{project_id}/ci-analyses/{analysis_id}",
    description="Requires a GitHub-authenticated user session and returns a report scoped to an owned project.",
)
def get_project_ci_analysis(project_id: str, analysis_id: str, user: dict = Depends(require_current_user)):
    _validate_ci_project(project_id)
    require_user_project(project_id, user)
    try:
        analysis = get_ci_analysis(project_id, analysis_id, user["id"])
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
    user: dict = Depends(require_current_user),
):
    require_user_project(project_id, user)
    return list_configuration_versions(project_id, limit)


@app.get("/api/config-versions/{version_id}")
def get_config_version(version_id: str, project_id: str = Query(min_length=1), user: dict = Depends(require_current_user)):
    require_user_project(project_id, user)
    version = get_configuration_version(project_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Configuration version not found.")
    return version


@app.get("/api/analyses")
def list_analyses(
    project_id: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(require_current_user),
):
    require_user_project(project_id, user)
    return list_project_analyses(project_id, limit)


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: str, project_id: str = Query(min_length=1), user: dict = Depends(require_current_user)):
    require_user_project(project_id, user)
    analysis = fetch_project_analysis(project_id, analysis_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Risk analysis not found.")
    return analysis


@app.post("/api/projects/{project_id}/config-versions", status_code=201)
def create_configuration_version(project_id: str, request: ConfigurationVersionRequest, user: dict = Depends(require_csrf)):
    require_user_project(project_id, user)
    if not project_id.strip():
        raise HTTPException(status_code=400, detail="Project ID cannot be empty.")
    version_id = save_configuration_version(project_id, request.configuration, request.format)
    return get_configuration_version(project_id, version_id)


@app.get("/api/projects/{project_id}/config-versions")
def get_project_configuration_history(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(require_current_user),
):
    require_user_project(project_id, user)
    return list_configuration_versions(project_id, limit)


@app.get("/api/projects/{project_id}/config-versions/{version_id}")
def get_project_configuration_version(project_id: str, version_id: str, user: dict = Depends(require_current_user)):
    require_user_project(project_id, user)
    version = get_configuration_version(project_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Configuration version not found.")
    return version


@app.post("/api/projects/{project_id}/config-versions/{version_id}/deploy")
def deploy_configuration_version(project_id: str, version_id: str, user: dict = Depends(require_csrf)):
    require_user_project(project_id, user)
    version = mark_configuration_deployed(project_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Configuration version not found.")
    return version


@app.get("/api/projects/{project_id}/analyses")
def get_project_analyses(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(require_current_user),
):
    require_user_project(project_id, user)
    return list_project_analyses(project_id, limit)


@app.get("/api/projects/{project_id}/analyses/{analysis_id}")
def get_project_analysis(project_id: str, analysis_id: str, user: dict = Depends(require_current_user)):
    require_user_project(project_id, user)
    analysis = fetch_project_analysis(project_id, analysis_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    return analysis


@app.post("/api/projects/{project_id}/historical-analysis", status_code=201)
def create_historical_analysis(project_id: str, request: HistoricalAnalysisRequest, user: dict = Depends(require_csrf)):
    require_user_project(project_id, user)
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
    request: Request,
    old_config: UploadFile = File(...),
    new_config: UploadFile = File(...),
    user: dict = Depends(require_csrf),
):
    try:
        project_id = request.session.get("active_project_id")
        if not isinstance(project_id, str):
            raise HTTPException(status_code=400, detail="Select a repository before analyzing configurations.")
        require_user_project(project_id, user)
        old_text = (await old_config.read()).decode("utf-8")
        new_text = (await new_config.read()).decode("utf-8")
        old_data = parse_config(old_config.filename or "", old_text)
        new_data = parse_config(new_config.filename or "", new_text)
        result = assess_risk(old_data, new_data)
        persist_analysis_bundle(
            project_id,
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
