from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from bson import ObjectId
import sys
import types

from app import authentication, main, persistence
from app.session import SignedSessionMiddleware
from tests.test_persistence import FakeDatabase


client = TestClient(main.app, raise_server_exceptions=False)


def cookie_for(user_id: str | None = None, csrf: str = "synthetic-csrf-token", **extra) -> str:
    secret = main.app.user_middleware[0].kwargs["secret_key"]
    signer = SignedSessionMiddleware(None, secret)
    data = dict(extra)
    if user_id is not None:
        session_id, _ = persistence.create_auth_session(user_id, csrf)
        data["session_id"] = session_id
    return signer._encode(data)


def clean_client():
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    client.headers.pop("Cookie", None)


def set_session(user_id: str, csrf: str = "synthetic-csrf-token", **extra):
    client.cookies.clear()
    client.headers["Cookie"] = f"configguard_session={cookie_for(user_id, csrf, **extra)}"


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class FakeGitHub:
    async def authorize_redirect(self, request, redirect_uri):
        assert redirect_uri == "http://localhost:8000/api/auth/github/callback"
        request.session["oauth_state_synthetic"] = "state-from-library"
        return RedirectResponse("https://github.com/login/oauth/authorize?state=synthetic", status_code=302)

    async def authorize_access_token(self, request):
        assert request.session.get("oauth_state_synthetic") == "state-from-library"
        return {"access_token": "synthetic-oauth-token-never-persist"}

    async def get(self, path, *, token, params=None):
        assert token["access_token"] == "synthetic-oauth-token-never-persist"
        if path == "user":
            return FakeResponse({"id": 123456, "login": "sample-user", "avatar_url": "https://avatars.githubusercontent.com/u/123"})
        assert path == "user/repos"
        return FakeResponse([
            {"id": 987, "full_name": "sample-user/public-app", "private": False, "html_url": "https://github.com/sample-user/public-app", "owner": {"login": "sample-user"}, "default_branch": "main"},
            {"id": 654, "full_name": "sample-user/private-app", "private": True, "html_url": "https://github.com/sample-user/private-app", "owner": {"login": "sample-user"}, "default_branch": "main"},
        ])


def test_github_login_callback_lists_only_public_repositories_and_does_not_store_token(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: database)
    monkeypatch.setattr(main, "github_client", lambda: FakeGitHub())
    monkeypatch.setenv("GITHUB_REDIRECT_URI", "http://localhost:8000/api/auth/github/callback")
    clean_client()

    login = client.get("/api/auth/github/login", follow_redirects=False)
    assert login.status_code == 302
    assert "configguard_session" in login.headers["set-cookie"]
    callback = client.get("/api/auth/github/callback", follow_redirects=False)
    assert callback.status_code == 303
    assert callback.headers["location"] == "http://localhost:5173/"
    cookie = client.cookies.get("configguard_session")
    assert "synthetic-oauth-token-never-persist" not in cookie

    session_response = client.get("/api/auth/me")
    assert session_response.status_code == 200, session_response.text
    session = session_response.json()
    assert session["authenticated"] is True
    assert session["user"]["login"] == "sample-user"
    assert "synthetic-oauth-token-never-persist" not in str(database["users"].documents)
    repositories = client.get("/api/github/repositories").json()
    assert [repository["full_name"] for repository in repositories] == ["sample-user/public-app"]

    client.headers["X-CSRF-Token"] = session["csrf_token"]
    selected = client.post("/api/projects/select", json={"repository_id": 987})
    assert selected.status_code == 200
    project = selected.json()
    assert project["repository"] == "sample-user/public-app"
    assert "owner_user_id" not in project
    assert database["projects"].documents[0]["owner_user_id"] == str(database["users"].documents[0]["_id"])
    assert client.get("/api/projects").json()[0]["id"] == project["id"]
    clean_client()


def test_project_and_ci_reads_require_session_and_owner_scope(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: database)
    first_user, second_user = ObjectId(), ObjectId()
    for user_id in (first_user, second_user):
        database["users"].documents.append({"_id": user_id, "github_id": int(str(user_id)[:8], 16), "github_login": "synthetic"})
    project_id = str(ObjectId())
    database["projects"].documents.append({
        "_id": ObjectId(project_id), "owner_user_id": str(first_user),
        "repository": "sample-user/public-app",
    })
    ci_id = ObjectId()
    database["ci_analyses"].documents.append({
        "_id": ci_id, "project_id": project_id, "owner_user_id": str(first_user),
        "repository": "sample-user/public-app", "created_at": __import__("datetime").datetime.now(),
    })
    version_id = persistence.save_configuration_version(project_id, {"timeout": 30}, "json")
    analysis_id = persistence.save_analysis(project_id, {"classification": "SAFE"})
    clean_client()
    assert client.get(f"/api/projects/{project_id}/ci-analyses").status_code == 401
    assert client.get(f"/api/projects/{project_id}/config-versions").status_code == 401
    assert client.get(f"/api/projects/{project_id}/analyses").status_code == 401
    assert persistence.get_github_user(str(first_user))["github_login"] == "synthetic"
    set_session(str(first_user))
    assert client.get("/api/auth/me").json()["authenticated"] is True
    assert client.get(f"/api/projects/{project_id}/ci-analyses").status_code == 200
    assert client.get(f"/api/projects/{project_id}/config-versions/{version_id}").status_code == 200
    client.headers["X-CSRF-Token"] = "synthetic-csrf-token"
    activated = client.post("/api/projects/activate", json={"project_id": project_id})
    assert activated.json()["active_project_id"] == project_id
    set_session(str(first_user), active_project_id=project_id)
    assert client.get("/api/auth/me").json()["active_project_id"] == project_id

    set_session(str(second_user))
    assert client.get(f"/api/projects/{project_id}/ci-analyses").status_code == 404
    assert client.get(f"/api/projects/{project_id}/ci-analyses/{ci_id}").status_code == 404
    assert client.get(f"/api/projects/{project_id}/config-versions/{version_id}").status_code == 404
    assert client.get(f"/api/projects/{project_id}/analyses/{analysis_id}").status_code == 404
    clean_client()


def test_all_project_data_routes_enforce_owner_for_reads_and_writes(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: database)
    user_a, user_b = ObjectId(), ObjectId()
    database["users"].documents.extend([
        {"_id": user_a, "github_id": 1001, "github_login": "user-a"},
        {"_id": user_b, "github_id": 1002, "github_login": "user-b"},
    ])
    project_id = str(ObjectId())
    database["projects"].documents.append({
        "_id": ObjectId(project_id), "owner_user_id": str(user_a),
        "repository": "owner/private-project", "github_repository_id": 123,
        "created_at": __import__("datetime").datetime.now(),
    })
    version_id = persistence.save_configuration_version(project_id, {"timeout": 20}, "json")
    analysis_id = persistence.save_analysis(project_id, {"classification": "SAFE"})
    ci_id = ObjectId()
    database["ci_analyses"].documents.append({
        "_id": ci_id, "project_id": project_id, "owner_user_id": str(user_a),
        "repository": "owner/private-project", "pull_request_number": 9,
        "findings": [{"parameter": "SYNTHETIC_PRIVATE_REPORT_SENTINEL"}],
        "created_at": __import__("datetime").datetime.now(),
    })

    clean_client()
    set_session(str(user_a))
    assert [item["repository"] for item in client.get("/api/projects").json()] == ["owner/private-project"]
    assert client.get(f"/api/projects/{project_id}/config-versions").status_code == 200
    assert client.get(f"/api/projects/{project_id}/config-versions/{version_id}").status_code == 200
    assert client.get(f"/api/projects/{project_id}/analyses").status_code == 200
    assert client.get(f"/api/projects/{project_id}/analyses/{analysis_id}").status_code == 200
    own_ci_list = client.get(f"/api/projects/{project_id}/ci-analyses")
    own_ci_detail = client.get(f"/api/projects/{project_id}/ci-analyses/{ci_id}")
    assert own_ci_list.status_code == own_ci_detail.status_code == 200
    assert own_ci_list.json()[0]["id"] == str(ci_id)

    clean_client()
    set_session(str(user_b))
    client.headers["X-CSRF-Token"] = "synthetic-csrf-token"
    protected_reads = [
        client.get(f"/api/projects/{project_id}/config-versions"),
        client.get(f"/api/projects/{project_id}/config-versions/{version_id}"),
        client.get(f"/api/projects/{project_id}/analyses"),
        client.get(f"/api/projects/{project_id}/analyses/{analysis_id}"),
        client.get(f"/api/projects/{project_id}/ci-analyses"),
        client.get(f"/api/projects/{project_id}/ci-analyses/{ci_id}"),
        client.get(f"/api/config-versions?project_id={project_id}"),
        client.get(f"/api/config-versions/{version_id}?project_id={project_id}"),
        client.get(f"/api/analyses?project_id={project_id}"),
        client.get(f"/api/analyses/{analysis_id}?project_id={project_id}"),
    ]
    assert all(response.status_code == 404 for response in protected_reads)
    assert all("SYNTHETIC_PRIVATE_REPORT_SENTINEL" not in response.text for response in protected_reads)

    denied_activation = client.post("/api/projects/activate", json={"project_id": project_id})
    denied_save = client.post(
        f"/api/projects/{project_id}/config-versions",
        json={"configuration": {"timeout": 999}, "format": "json"},
    )
    denied_deploy = client.post(f"/api/projects/{project_id}/config-versions/{version_id}/deploy")
    denied_historical = client.post(
        f"/api/projects/{project_id}/historical-analysis",
        json={"configuration": {"timeout": 999}},
    )
    assert [response.status_code for response in (denied_activation, denied_save, denied_deploy, denied_historical)] == [404] * 4
    assert database["config_versions"].find_one({"_id": ObjectId(version_id)})["configuration"] == {"timeout": 20}
    assert len(database["config_versions"].documents) == 1
    clean_client()


def test_repository_selection_uses_only_the_signed_in_users_repository_snapshot(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: database)
    user_a, user_b = ObjectId(), ObjectId()
    database["users"].documents.extend([
        {"_id": user_a, "github_id": 2001, "github_login": "user-a", "public_repositories": [
            {"id": 111, "full_name": "owner/a-repo", "html_url": "https://github.com/owner/a-repo", "default_branch": "main"},
        ]},
        {"_id": user_b, "github_id": 2002, "github_login": "user-b", "public_repositories": [
            {"id": 222, "full_name": "owner/b-repo", "html_url": "https://github.com/owner/b-repo", "default_branch": "main"},
        ]},
    ])
    clean_client()
    set_session(str(user_b))
    client.headers["X-CSRF-Token"] = "synthetic-csrf-token"

    denied = client.post("/api/projects/select", json={"repository_id": 111})
    assert denied.status_code == 404
    assert denied.json() == {"detail": "Repository not found."}
    assert database["projects"].documents == []

    allowed = client.post("/api/projects/select", json={"repository_id": 222})
    assert allowed.status_code == 200
    assert allowed.json()["repository"] == "owner/b-repo"
    assert database["projects"].documents[0]["owner_user_id"] == str(user_b)
    assert database["projects"].documents[0]["github_repository_id"] == 222
    clean_client()


def test_expired_server_session_cannot_read_user_data(monkeypatch):
    from datetime import datetime, timedelta, timezone

    database = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: database)
    user_id = ObjectId()
    database["users"].documents.append({"_id": user_id, "github_id": 3001, "github_login": "expired-user"})
    project_id = str(ObjectId())
    database["projects"].documents.append({
        "_id": ObjectId(project_id), "owner_user_id": str(user_id), "repository": "owner/expired-repo",
    })
    clean_client()
    set_session(str(user_id))
    stored_session = database["auth_sessions"].documents[0]
    stored_session["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    response = client.get("/api/projects")
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}
    assert client.get(f"/api/projects/{project_id}/ci-analyses").status_code == 401
    clean_client()


def test_unauthenticated_requests_cannot_access_user_data_or_write(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: database)
    client.cookies.clear()
    client.headers.pop("Cookie", None)
    client.headers.pop("X-CSRF-Token", None)
    project_id = str(ObjectId())

    protected_reads = [
        client.get("/api/github/repositories"),
        client.get("/api/projects"),
        client.get("/api/config-versions?project_id=" + project_id),
        client.get("/api/analyses?project_id=" + project_id),
        client.get(f"/api/projects/{project_id}/config-versions"),
        client.get(f"/api/projects/{project_id}/analyses"),
        client.get(f"/api/projects/{project_id}/ci-analyses"),
    ]
    assert all(response.status_code == 401 for response in protected_reads)

    protected_writes = [
        client.post("/api/projects/select", json={"repository_id": 123}),
        client.post("/api/projects/activate", json={"project_id": project_id}),
        client.post(
            f"/api/projects/{project_id}/config-versions",
            json={"configuration": {"timeout": 5}, "format": "json"},
        ),
        client.post(f"/api/projects/{project_id}/historical-analysis", json={"configuration": {"timeout": 5}}),
    ]
    assert all(response.status_code == 401 for response in protected_writes)
    assert database["projects"].documents == []
    assert database["config_versions"].documents == []
    clean_client()


def test_legacy_ownerless_projects_and_ci_reports_are_not_exposed(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: database)
    user_id = ObjectId()
    database["users"].documents.append({"_id": user_id, "github_id": 4001, "github_login": "new-user"})
    legacy_project_id = "legacy-v25-project"
    database["projects"].documents.append({
        "_id": legacy_project_id, "repository": "legacy-owner/legacy-repository",
        "created_at": __import__("datetime").datetime.now(),
    })
    database["ci_analyses"].documents.append({
        "_id": ObjectId(), "project_id": legacy_project_id,
        "repository": "legacy-owner/legacy-repository",
        "findings": [{"parameter": "LEGACY_PRIVATE_REPORT_SENTINEL"}],
        "created_at": __import__("datetime").datetime.now(),
    })
    clean_client()
    set_session(str(user_id))

    project_read = client.get(f"/api/projects/{legacy_project_id}/ci-analyses")
    assert project_read.status_code == 404
    assert project_read.json() == {"detail": "Project not found."}
    assert "LEGACY_PRIVATE_REPORT_SENTINEL" not in project_read.text
    assert client.get("/api/projects").json() == []
    clean_client()


def test_browser_writes_require_csrf_and_logout_clears_session(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: database)
    user_id = ObjectId()
    database["users"].documents.append({"_id": user_id, "github_id": 100, "github_login": "synthetic"})
    clean_client()
    set_session(str(user_id))

    denied_save = client.post(
        "/api/projects/payments/config-versions",
        json={"configuration": {"timeout": 30}, "format": "json"},
    )
    assert denied_save.status_code == 403
    assert database["config_versions"].documents == []
    replayable_cookie = client.headers["Cookie"]

    rejected = client.post("/api/auth/logout")
    assert rejected.status_code == 403
    client.headers["X-CSRF-Token"] = "synthetic-csrf-token"
    accepted = client.post("/api/auth/logout")
    assert accepted.status_code == 200
    assert "Max-Age=0" in accepted.headers.get("set-cookie", "")
    clean_client()
    assert client.get("/api/auth/me").json()["authenticated"] is False
    client.headers["Cookie"] = replayable_cookie
    assert client.get("/api/projects").status_code == 401
    clean_client()


def test_invalid_session_signature_is_rejected():
    clean_client()
    client.cookies.set("configguard_session", "not-a-valid-signed-session")
    response = client.get("/api/projects")
    assert response.status_code == 401
    clean_client()


def test_oauth_configuration_failure_is_generic_and_does_not_echo_secrets(monkeypatch, caplog):
    import logging

    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GITHUB_REDIRECT_URI", raising=False)
    caplog.set_level(logging.WARNING, logger="uvicorn.error")
    response = client.get("/api/auth/github/login")
    assert response.status_code == 503
    assert response.json() == {"detail": "GitHub sign-in is not configured."}

    callback = client.get("/api/auth/github/callback?code=synthetic-code&state=synthetic-state")
    assert callback.status_code == 503
    assert callback.json() == {"detail": "GitHub sign-in is not configured."}
    assert "stage=oauth_client_configuration" in caplog.text
    assert "exception_type=HTTPException" in caplog.text
    assert "category=configuration_error" in caplog.text
    assert "synthetic-code" not in caplog.text
    assert "synthetic-state" not in caplog.text


def test_oauth_client_uses_read_only_profile_scope_pkce_and_post_auth(monkeypatch):
    captured = {}

    class FakeOAuth:
        def register(self, **kwargs):
            captured.update(kwargs)

        def create_client(self, name):
            return name

    authlib_module = types.ModuleType("authlib")
    authlib_module.__path__ = []
    integrations_module = types.ModuleType("authlib.integrations")
    integrations_module.__path__ = []
    starlette_module = types.ModuleType("authlib.integrations.starlette_client")
    starlette_module.OAuth = FakeOAuth
    monkeypatch.setitem(sys.modules, "authlib", authlib_module)
    monkeypatch.setitem(sys.modules, "authlib.integrations", integrations_module)
    monkeypatch.setitem(sys.modules, "authlib.integrations.starlette_client", starlette_module)
    monkeypatch.setattr(authentication, "oauth", None)
    monkeypatch.setattr(authentication, "_registered", False)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "synthetic-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "synthetic-client-secret")
    monkeypatch.setenv("GITHUB_REDIRECT_URI", "https://configguard.example/api/auth/github/callback")

    assert authentication.github_client() == "github"
    assert captured["client_kwargs"] == {
        "scope": "read:user",
        "code_challenge_method": "S256",
        "token_endpoint_auth_method": "client_secret_post",
    }


def test_callback_state_mismatch_returns_generic_502_and_logs_safe_diagnostics(monkeypatch, caplog):
    from urllib.parse import parse_qs, urlsplit
    import logging

    monkeypatch.setenv("GITHUB_CLIENT_ID", "synthetic-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "synthetic-client-secret")
    monkeypatch.setenv("GITHUB_REDIRECT_URI", "https://configguard.example/api/auth/github/callback")
    monkeypatch.setattr(authentication, "oauth", None)
    monkeypatch.setattr(authentication, "_registered", False)
    monkeypatch.setattr(main, "github_client", authentication.github_client)
    clean_client()

    login = client.get("/api/auth/github/login", follow_redirects=False)
    assert login.status_code == 302
    authorize_params = parse_qs(urlsplit(login.headers["location"]).query)
    state = authorize_params["state"][0]
    assert authorize_params["redirect_uri"] == ["https://configguard.example/api/auth/github/callback"]
    assert authorize_params["code_challenge_method"] == ["S256"]
    assert authorize_params.get("code_challenge")
    # Simulate a proxy/browser path that loses the signed OAuth state cookie.
    clean_client()
    caplog.set_level(logging.WARNING, logger="uvicorn.error")
    response = client.get(
        "/api/auth/github/callback",
        params={"code": "synthetic-oauth-code", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "GitHub sign-in could not be completed."}
    assert "stage=oauth_session_cookie_validation" in caplog.text
    assert "exception_type=MismatchingStateError" in caplog.text
    assert "category=session_cookie_unavailable" in caplog.text
    diagnostic_records = [
        record for record in caplog.records
        if record.name == "uvicorn.error"
        and record.getMessage().startswith("GitHub OAuth callback failed ")
    ]
    assert len(diagnostic_records) == 1
    assert diagnostic_records[0].getMessage() == (
        "GitHub OAuth callback failed stage=oauth_session_cookie_validation "
        "exception_type=MismatchingStateError category=session_cookie_unavailable"
    )
    for sensitive_synthetic in (
        "synthetic-oauth-code", state, "synthetic-client-secret",
        "configguard_session", "access_token", "code_verifier",
    ):
        assert sensitive_synthetic not in caplog.text
    clean_client()


def test_rotated_session_secret_rejects_inflight_oauth_without_exposing_cookie(monkeypatch, caplog):
    from urllib.parse import parse_qs, urlsplit
    import logging

    class MismatchingStateError(Exception):
        pass

    class StateCheckingGitHub(FakeGitHub):
        async def authorize_access_token(self, request):
            if not request.state.configguard_session_valid:
                raise MismatchingStateError()
            return await super().authorize_access_token(request)

    monkeypatch.setenv("GITHUB_CLIENT_ID", "synthetic-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "synthetic-client-secret")
    monkeypatch.setenv("GITHUB_REDIRECT_URI", "http://localhost:8000/api/auth/github/callback")
    monkeypatch.setattr(main, "github_client", lambda: StateCheckingGitHub())
    clean_client()

    login = client.get("/api/auth/github/login", follow_redirects=False)
    assert login.status_code == 302
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]

    # Simulate a process restart after CONFIGGUARD_SESSION_SECRET was rotated:
    # the callback carries a cookie signed with the old key.
    middleware = main.app.middleware_stack
    while not isinstance(middleware, SignedSessionMiddleware):
        middleware = middleware.app
    original_key = middleware.key
    middleware.key = b"synthetic-new-session-secret-that-is-long-enough"
    caplog.set_level(logging.WARNING, logger="uvicorn.error")
    try:
        response = client.get(
            "/api/auth/github/callback",
            params={"code": "synthetic-oauth-code", "state": state},
            follow_redirects=False,
        )
    finally:
        middleware.key = original_key

    assert response.status_code == 502
    assert response.json() == {"detail": "GitHub sign-in could not be completed."}
    assert "stage=oauth_session_cookie_validation" in caplog.text
    assert "exception_type=MismatchingStateError" in caplog.text
    assert "category=session_cookie_unavailable" in caplog.text
    for sensitive in (state, "synthetic-oauth-code", "synthetic-client-secret", "configguard_session"):
        assert sensitive not in caplog.text
    clean_client()


def test_authlib_restores_oauth_state_pkce_verifier_and_redirect_uri(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    database = FakeDatabase()
    monkeypatch.setattr(persistence, "get_database", lambda: database)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "synthetic-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "synthetic-client-secret")
    monkeypatch.setenv("GITHUB_REDIRECT_URI", "http://localhost:8000/api/auth/github/callback")
    monkeypatch.setattr(authentication, "oauth", None)
    monkeypatch.setattr(authentication, "_registered", False)
    github = authentication.github_client()
    captured = {}

    async def fake_fetch_access_token(**params):
        captured.update(params)
        return {"access_token": "synthetic-access-token", "token_type": "Bearer"}

    async def fake_github_get(path, *, token, params=None):
        assert token["access_token"] == "synthetic-access-token"
        if path == "user":
            return FakeResponse({"id": 556677, "login": "synthetic-user", "avatar_url": None})
        assert path == "user/repos"
        return FakeResponse([])

    github.fetch_access_token = fake_fetch_access_token
    github.get = fake_github_get
    monkeypatch.setattr(main, "github_client", lambda: github)
    clean_client()

    login = client.get("/api/auth/github/login", follow_redirects=False)
    assert login.status_code == 302
    params = parse_qs(urlsplit(login.headers["location"]).query)
    callback = client.get(
        "/api/auth/github/callback",
        params={"code": "synthetic-code", "state": params["state"][0]},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == "http://localhost:5173/"
    assert captured["code"] == "synthetic-code"
    assert captured["redirect_uri"] == "http://localhost:8000/api/auth/github/callback"
    assert isinstance(captured.get("code_verifier"), str) and captured["code_verifier"]
    assert "access_token" not in str(database["users"].documents)
    assert client.get("/api/auth/me").json()["authenticated"] is True
    clean_client()


def test_callback_profile_failure_logs_stage_and_type_without_exception_text(monkeypatch, caplog):
    import logging

    class ProfileFailure(FakeGitHub):
        async def authorize_access_token(self, request):
            return {"access_token": "synthetic-profile-token"}

        async def get(self, path, *, token, params=None):
            raise RuntimeError("synthetic-provider-response-with-sensitive-data")

    monkeypatch.setattr(main, "github_client", lambda: ProfileFailure())
    clean_client()
    set_cookie = cookie_for(oauth_state_synthetic="valid-test-state")
    client.headers["Cookie"] = f"configguard_session={set_cookie}"
    caplog.set_level(logging.WARNING, logger="uvicorn.error")

    response = client.get("/api/auth/github/callback?code=synthetic-code&state=synthetic-state")

    assert response.status_code == 502
    assert "stage=github_profile_lookup" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert "category=provider_response_error" in caplog.text
    assert "synthetic-provider-response-with-sensitive-data" not in caplog.text
    assert "synthetic-profile-token" not in caplog.text
    assert "synthetic-code" not in caplog.text
    clean_client()


def test_callback_session_persistence_failure_logs_stage_without_database_details(monkeypatch, caplog):
    import logging

    class DatabaseFailureGitHub(FakeGitHub):
        async def authorize_access_token(self, request):
            return {"access_token": "synthetic-oauth-token-never-persist"}

    monkeypatch.setattr(main, "github_client", lambda: DatabaseFailureGitHub())
    monkeypatch.setattr(main, "upsert_github_user", lambda *args: {"id": "synthetic-user-id"})

    def fail_session(_user_id):
        raise RuntimeError("synthetic-database-connection-detail")

    monkeypatch.setattr(main, "create_auth_session", fail_session)
    clean_client()
    client.headers["Cookie"] = f"configguard_session={cookie_for(oauth_state_synthetic='state-from-library')}"
    caplog.set_level(logging.WARNING, logger="uvicorn.error")

    response = client.get("/api/auth/github/callback?code=synthetic-code&state=synthetic-state")

    assert response.status_code == 502
    assert "stage=server_session_creation" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert "category=persistence_error" in caplog.text
    assert "synthetic-database-connection-detail" not in caplog.text
    assert "synthetic-oauth-token-never-persist" not in caplog.text
    assert "synthetic-code" not in caplog.text
    clean_client()
