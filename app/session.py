"""Small signed-cookie session middleware using Python's standard HMAC implementation.

During OAuth the cookie carries transient OAuth state. After login it carries a
random server-side session ID and the active project ID. User identity and CSRF
state live in the database; GitHub access tokens must never enter this cookie.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from typing import Any


class SignedSessionMiddleware:
    def __init__(self, app, secret_key: str, *, max_age: int = 28800, cookie_name: str = "configguard_session"):
        if not isinstance(secret_key, str) or len(secret_key) < 32:
            raise ValueError("CONFIGGUARD_SESSION_SECRET must contain at least 32 characters.")
        self.app = app
        self.key = secret_key.encode("utf-8")
        self.max_age = max_age
        self.cookie_name = cookie_name
        self.secure = os.getenv("APP_ENV", "development").lower() == "production"

    def _encode(self, session: dict[str, Any]) -> str:
        payload = json.dumps({"exp": int(time.time()) + self.max_age, "session": session}, separators=(",", ":"), sort_keys=True).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self.key, encoded, hashlib.sha256).digest()
        return encoded.decode("ascii") + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

    def _decode(self, value: str) -> dict[str, Any]:
        encoded_text, separator, signature_text = value.partition(".")
        if not separator:
            return {}
        encoded = encoded_text.encode("ascii")
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        expected = hmac.new(self.key, encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return {}
        payload = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
        expires = payload.get("exp") if isinstance(payload, dict) else None
        if type(expires) is not int or expires < int(time.time()) or not isinstance(payload.get("session"), dict):
            return {}
        return payload["session"]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        cookies = {}
        for part in headers.get(b"cookie", b"").decode("latin-1").split(";"):
            name, _, value = part.strip().partition("=")
            cookies[name] = value
        raw_cookie = cookies.get(self.cookie_name, "")
        try:
            initial = self._decode(raw_cookie) if raw_cookie and len(raw_cookie) <= 4096 else {}
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            initial = {}
        # Expose only a boolean to request handlers so OAuth can distinguish a
        # rejected/expired signed cookie from a valid cookie with bad state.
        # Never expose the cookie, its contents, or a signature fingerprint.
        scope.setdefault("state", {})["configguard_session_valid"] = bool(initial)
        session = dict(initial)
        scope["session"] = session

        async def send_with_session(message):
            if message["type"] == "http.response.start":
                changed = session != initial
                headers_out = list(message.get("headers", []))
                if not session and raw_cookie:
                    cookie_options = f"{self.cookie_name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
                    if self.secure:
                        cookie_options += "; Secure"
                    headers_out.append((b"set-cookie", cookie_options.encode("ascii")))
                elif changed:
                    cookie_options = f"{self.cookie_name}={self._encode(session)}; Path=/; Max-Age={self.max_age}; HttpOnly; SameSite=Lax"
                    if self.secure:
                        cookie_options += "; Secure"
                    headers_out.append((b"set-cookie", cookie_options.encode("ascii")))
                message = {**message, "headers": headers_out}
            await send(message)

        await self.app(scope, receive, send_with_session)
