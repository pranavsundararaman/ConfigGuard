"""GitHub OAuth setup and browser-session dependencies."""

from __future__ import annotations

import os
import secrets
from threading import Lock
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request

_oauth_lock = Lock()
_registered = False
oauth = None


def github_client():
    global _registered, oauth
    client_id = os.getenv("GITHUB_CLIENT_ID")
    client_secret = os.getenv("GITHUB_CLIENT_SECRET")
    redirect_uri = os.getenv("GITHUB_REDIRECT_URI", "")
    try:
        parsed_redirect = urlsplit(redirect_uri)
        local_http = parsed_redirect.hostname in {"localhost", "127.0.0.1", "::1"} and parsed_redirect.scheme == "http"
        valid_redirect = (
            parsed_redirect.path == "/api/auth/github/callback"
            and parsed_redirect.username is None
            and parsed_redirect.password is None
            and parsed_redirect.query == ""
            and parsed_redirect.fragment == ""
            and (parsed_redirect.scheme == "https" or local_http)
        )
    except ValueError:
        valid_redirect = False
    if not client_id or not client_secret or not valid_redirect:
        raise HTTPException(status_code=503, detail="GitHub sign-in is not configured.")
    with _oauth_lock:
        if oauth is None:
            try:
                from authlib.integrations.starlette_client import OAuth
            except ImportError:
                raise HTTPException(status_code=503, detail="GitHub sign-in dependencies are unavailable.") from None
            oauth = OAuth()
        if not _registered:
            oauth.register(
                name="github",
                client_id=client_id,
                client_secret=client_secret,
                authorize_url="https://github.com/login/oauth/authorize",
                access_token_url="https://github.com/login/oauth/access_token",
                api_base_url="https://api.github.com/",
                client_kwargs={
                    "scope": "read:user",
                    "code_challenge_method": "S256",
                    "token_endpoint_auth_method": "client_secret_post",
                },
            )
            _registered = True
    return oauth.create_client("github")


def require_current_user(request: Request) -> dict:
    from .persistence import get_auth_session, get_github_user

    session_id = request.session.get("session_id")
    if not isinstance(session_id, str):
        raise HTTPException(status_code=401, detail="Authentication required.")
    auth_session = get_auth_session(session_id)
    if auth_session is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Authentication required.")
    user = get_github_user(auth_session["user_id"])
    if user is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Authentication required.")
    request.state.auth_session = auth_session
    return user


def require_csrf(request: Request, user: dict = Depends(require_current_user)) -> dict:
    expected = request.state.auth_session.get("csrf_token")
    supplied = request.headers.get("x-csrf-token", "")
    if not isinstance(expected, str) or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Request verification failed.")
    return user


def require_user_project(project_id: str, user: dict) -> dict:
    from .persistence import get_user_project

    project = get_user_project(user["id"], project_id)
    if project is None:
        # Do not reveal whether another user has selected this project.
        raise HTTPException(status_code=404, detail="Project not found.")
    return project
