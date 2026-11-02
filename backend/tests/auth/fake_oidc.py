"""A fake OIDC issuer for the login's tests (#4975 phase 2, spec 2026-11-01 §11): discovery,
a JWKS, a token endpoint that checks the client secret, the redirect URI and the PKCE
verifier, and an end-session endpoint. Served in-process through ``httpx.ASGITransport``,
so no port is opened. ``misbehave`` makes the next ID tokens wrong in one way each.

The browser's leg (the authorize page) is :meth:`FakeIssuer.authorize`: a test reads the
query ``/auth/login`` redirected to and asks for a code, as a signed-in user's browser
would get one. A real browser gets the same at ``GET /authorize`` (signed in as ``user``
with no form, redirected back with the code), and ``GET /logout`` ends at the
``post_logout_redirect_uri``: the e2e stack serves this app as its IdP container
(``e2e-stack/fake-oidc/``), where ``POST /__misbehave`` sets :attr:`misbehave`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet, OctKey, RSAKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

ISSUER = "https://idp.test"
CLIENT_ID = "scadbuddy"
CLIENT_SECRET = "s3cret-for-tests"

Misbehaviour = Literal[
    "iss", "aud", "nonce", "expired", "alg_none", "hs256", "unknown_kid", "no_sub", "future_iat"
]


@dataclass
class _Grant:
    redirect_uri: str
    nonce: str
    challenge: str
    claims: dict[str, Any]


@dataclass
class FakeIssuer:
    issuer: str = ISSUER
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    #: The claims the next sign-in's ID token carries besides the protocol's own.
    user: dict[str, Any] = field(
        default_factory=lambda: {"sub": "alice", "name": "Alice", "email": "a@example.test"}
    )
    misbehave: Misbehaviour | None = None
    #: Whether the discovery document names an end-session endpoint.
    end_session: bool = True
    #: The discovery document's ``issuer``, when it should lie.
    claimed_issuer: str | None = None
    token_requests: list[dict[str, str]] = field(default_factory=list)
    jwks_fetches: int = 0

    def __post_init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
        self.stranger = RSAKey.generate_key(2048, parameters={"kid": "k2"})
        self._grants: dict[str, _Grant] = {}
        self.app = Starlette(
            routes=[
                Route("/.well-known/openid-configuration", self._discovery),
                Route("/jwks", self._jwks),
                Route("/token", self._token, methods=["POST"]),
                Route("/authorize", self._authorize_page),
                Route("/logout", self._logout),
                Route("/__misbehave", self._set_misbehaviour, methods=["POST"]),
            ]
        )

    def transport(self) -> httpx.AsyncBaseTransport:
        return httpx.ASGITransport(app=self.app)

    def authorize(self, location: str) -> dict[str, str]:
        """What the authorize endpoint would send the browser back with: the callback's
        query (``code`` and ``state``), for the ``/auth/login`` redirect ``location``."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        assert query["response_type"] == "code"
        assert query["client_id"] == self.client_id
        assert query["code_challenge_method"] == "S256"
        assert "openid" in query["scope"].split()
        code = secrets.token_urlsafe(16)
        self._grants[code] = _Grant(
            redirect_uri=query["redirect_uri"],
            nonce=query["nonce"],
            challenge=query["code_challenge"],
            claims=dict(self.user),
        )
        return {"code": code, "state": query["state"]}

    async def _authorize_page(self, request: Request) -> Response:
        try:
            answer = self.authorize(str(request.url))
        except (AssertionError, KeyError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        redirect_uri = request.query_params["redirect_uri"]
        separator = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{separator}{urlencode(answer)}", status_code=302)

    async def _logout(self, request: Request) -> Response:
        after = request.query_params.get("post_logout_redirect_uri")
        if after:
            return RedirectResponse(after, status_code=302)
        return Response("signed out", media_type="text/plain")

    async def _set_misbehaviour(self, request: Request) -> Response:
        body = await request.json()
        self.misbehave = body.get("misbehave") if isinstance(body, dict) else None
        return JSONResponse({"misbehave": self.misbehave})

    async def _discovery(self, request: Request) -> Response:
        doc: dict[str, Any] = {
            "issuer": self.claimed_issuer or self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
        }
        if self.end_session:
            doc["end_session_endpoint"] = f"{self.issuer}/logout"
        return JSONResponse(doc)

    async def _jwks(self, request: Request) -> Response:
        self.jwks_fetches += 1
        return JSONResponse(KeySet([self.key]).as_dict(private=False))

    async def _token(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        self.token_requests.append(form)
        expected = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        grant = self._grants.pop(form.get("code", ""), None)
        if grant is None or form.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if form.get("redirect_uri") != grant.redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != grant.challenge:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse({"id_token": self._id_token(grant), "token_type": "Bearer"})

    def _id_token(self, grant: _Grant) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.client_id,
            "iat": now,
            "exp": now + 300,
            "nonce": grant.nonce,
            **grant.claims,
        }
        wrong = self.misbehave
        if wrong == "iss":
            claims["iss"] = "https://other.test"
        elif wrong == "aud":
            claims["aud"] = "another-client"
        elif wrong == "nonce":
            claims["nonce"] = "not-this-one"
        elif wrong == "expired":
            claims["exp"] = now - 120
        elif wrong == "future_iat":
            claims["iat"] = now + 600
        elif wrong == "no_sub":
            claims.pop("sub", None)
        if wrong == "alg_none":

            def b64(data: dict[str, Any]) -> str:
                raw = json.dumps(data).encode()
                return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

            return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."
        if wrong == "hs256":
            secret = OctKey.import_key(self.client_secret.encode())
            return jwt.encode({"alg": "HS256"}, claims, secret)
        key = self.stranger if wrong == "unknown_kid" else self.key
        return jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)
