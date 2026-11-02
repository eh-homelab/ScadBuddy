"""Sign-in (#4975 phase 2, spec 2026-11-01 §5.1, §5.2, §6.2, §9.2): the OIDC login, the
session, the person's own sessions, and the login configuration.

In ``SCADBUDDY_AUTH_MODE=off`` (the only mode until phase 3) every route here is open, as
every route is today; the gate's route classes (phase 3) make the configuration and
session routes a signed-in person's only. A signed-in person is recorded but nothing is
refused for lacking a session, so a broken configuration costs nothing.

Never logged or answered: a token, the client secret, the login cookie's contents.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Path, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from scadbuddy.api.deps import EventsDep, SettingsStoreDep, StateDep
from scadbuddy.auth import break_glass as break_glass_codes
from scadbuddy.auth.component import AuthComponent, AuthDep, CallerDep
from scadbuddy.auth.config import LoginConfig, seal
from scadbuddy.auth.gate import SESSION_COOKIE
from scadbuddy.auth.login_state import (
    LOGIN_COOKIE,
    LOGIN_LIFETIME,
    new_state,
    open_state,
    seal_state,
)
from scadbuddy.auth.models import ABSOLUTE_LIFETIME, AuthSession, Caller, Person
from scadbuddy.auth.oidc import OidcError, check_url
from scadbuddy.core.events import AuthSessionRevoked, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.core.proxies import client_address
from scadbuddy.core.secrets import SealError
from scadbuddy.core.settings import AuthMode

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

PROBLEMS = "https://scadbuddy.dev/problems/auth/"
CALLBACK_PATH = "/api/v1/auth/callback"
#: What the popup posts its opener (§6.2).
SIGNED_IN_MESSAGE = "scadbuddy:signed-in"
SIGN_IN_FAILED_MESSAGE = "scadbuddy:sign-in-failed"
#: The longest user agent kept for the person's list.
USER_AGENT_CHARS = 300


def _problem(status: int, slug: str, detail: str) -> ApiError:
    return ApiError(status, detail, type_=f"{PROBLEMS}{slug}")


def _public_origin(store: SettingsStoreDep) -> str | None:
    url = store.load().public_url
    if not url:
        return None
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


class SignedInUser(BaseModel):
    principal: str
    name: str | None
    email: str | None


class AuthSessionView(BaseModel):
    """What the page asks first (§5.1 step 4): whether it is signed in, and whether it can
    be."""

    signed_in: bool
    user: SignedInUser | None = None
    mode: AuthMode
    #: Whether the sign-in button is offered.
    login_enabled: bool
    expires_at: datetime | None = None
    #: How the session was made: a ``break_glass`` one is shown with a banner.
    kind: Literal["login", "delegated", "break_glass"] | None = None


def _person(caller: Caller) -> AuthSession | None:
    return caller.session if isinstance(caller, Person) else None


@router.get(
    "/auth/session", response_model=AuthSessionView, summary="Who this browser is signed in as"
)
async def get_session(caller: CallerDep, auth: AuthDep, state: StateDep) -> AuthSessionView:
    config, _ = await asyncio.to_thread(auth.load_config)
    session = _person(caller)
    return AuthSessionView(
        signed_in=session is not None,
        user=None
        if session is None
        else SignedInUser(principal=session.principal, name=session.name, email=session.email),
        mode=state.settings.auth_mode,
        login_enabled=config.enabled,
        expires_at=None
        if session is None
        else min(session.idle_expires_at, session.absolute_expires_at),
        kind=None if session is None else session.kind,
    )


def _cookie(response: Response, name: str, value: str, max_age: int, samesite: str) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite=samesite,  # type: ignore[arg-type]
    )


def _clear(response: Response, name: str, samesite: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite=samesite)  # type: ignore[arg-type]


@router.get(
    "/auth/login",
    summary="Start signing in through the identity provider",
    status_code=302,
    response_class=RedirectResponse,
    responses={409: {"description": "The login is not configured or switched on"}},
)
async def login(
    auth: AuthDep,
    settings_store: SettingsStoreDep,
    return_to: Annotated[str, Query(max_length=2000)] = "/",
    popup: bool = False,
) -> RedirectResponse:
    """Redirects to the issuer's authorization endpoint with PKCE ``S256`` and a sealed
    login cookie holding ``state``, ``nonce`` and the verifier (§5.1 step 1).
    ``return_to`` is a same-origin path; anything else becomes ``/``."""
    config, envelope = await asyncio.to_thread(auth.load_config)
    origin = _public_origin(settings_store)
    kek = auth.kek
    if not config.enabled or envelope is None or kek is None or origin is None:
        raise _problem(409, "login-off", "sign-in is not switched on in Settings")
    try:
        provider = await auth.oidc.provider(config.issuer)
    except OidcError as error:
        raise _problem(502, "issuer-unavailable", str(error)) from None
    state = new_state(return_to, popup)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": f"{origin}{CALLBACK_PATH}",
            "scope": "openid profile email",
            "state": state.state,
            "nonce": state.nonce,
            "code_challenge": state.challenge,
            "code_challenge_method": "S256",
        }
    )
    separator = "&" if "?" in provider.authorization_endpoint else "?"
    response = RedirectResponse(f"{provider.authorization_endpoint}{separator}{query}", 302)
    # Lax: the callback is a top-level navigation from the IdP's site (§5.1 step 1).
    _cookie(
        response, LOGIN_COOKIE, seal_state(kek, state), int(LOGIN_LIFETIME.total_seconds()), "lax"
    )
    return response


def _page(
    *,
    status: int,
    title: str,
    message: str,
    origin: str | None,
    popup: bool,
    return_to: str | None = None,
    signed_in: bool = False,
) -> HTMLResponse:
    """The callback's answer: a small same-origin page that goes on to ``return_to``, or
    in a popup tells its opener and closes (§5.1 step 2, §6.2). Its one script runs under
    a nonce of its own; the page is never framed."""
    nonce = secrets.token_urlsafe(16)
    script = ""
    if origin is not None and (popup or (signed_in and return_to)):
        data = {
            "popup": popup,
            "origin": origin,
            "message": {
                "type": SIGNED_IN_MESSAGE if signed_in else SIGN_IN_FAILED_MESSAGE,
                **({} if signed_in else {"detail": message}),
            },
            "next": f"{origin}{return_to}" if signed_in and return_to else None,
        }
        # JSON in a script element: "<" escaped so no value can close it.
        payload = json.dumps(data).replace("<", "\\u003c")
        script = (
            f'<script nonce="{nonce}">(function(){{var d={payload};'
            "if(d.popup&&window.opener){window.opener.postMessage(d.message,d.origin);"
            "if(d.message.type==='" + SIGNED_IN_MESSAGE + "'){window.close();return;}}"
            "if(d.next){window.location.replace(d.next);}})();</script>"
        )
    link = (
        f'<p><a href="{html.escape(f"{origin}{return_to}")}">Continue</a></p>'
        if signed_in and origin is not None and return_to
        else ""
    )
    body = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        '<body style="font-family:system-ui,sans-serif;max-width:32rem;'
        'margin:3rem auto;padding:0 1rem">'
        f'<h1 style="font-size:1.25rem">{html.escape(title)}</h1><p>{html.escape(message)}</p>'
        f"{link}{script}</body></html>"
    )
    return HTMLResponse(
        body,
        status_code=status,
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline';"
                " base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get(
    "/auth/callback",
    summary="Where the identity provider sends the browser back",
    response_class=HTMLResponse,
    include_in_schema=True,
)
async def callback(
    request: Request,
    auth: AuthDep,
    settings_store: SettingsStoreDep,
    state: StateDep,
    code: Annotated[str | None, Query(max_length=4000)] = None,
    state_param: Annotated[str | None, Query(alias="state", max_length=200)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> HTMLResponse:
    """Checks the login cookie's ``state``, redeems the code with the verifier and the
    client secret, checks the ID token and the sign-in rule, and starts a session (§5.1
    step 2). The login cookie is cleared whatever happens."""
    origin = _public_origin(settings_store)
    kek = auth.kek
    cookie = request.cookies.get(LOGIN_COOKIE)
    login = None if cookie is None or kek is None else open_state(kek, cookie)
    popup = login.popup if login is not None else False

    def refuse(status: int, message: str) -> HTMLResponse:
        page = _page(
            status=status, title="Sign-in failed", message=message, origin=origin, popup=popup
        )
        _clear(page, LOGIN_COOKIE, "lax")
        return page

    if login is None:
        return refuse(400, "This sign-in expired or was started elsewhere. Start it again.")
    if state_param is None or not secrets.compare_digest(state_param, login.state):
        return refuse(400, "This sign-in's state does not match. Start it again.")
    if error is not None:
        return refuse(400, f"The identity provider refused the sign-in ({error}).")
    if code is None:
        return refuse(400, "The identity provider sent no code.")
    config, envelope = await asyncio.to_thread(auth.load_config)
    if not config.enabled or envelope is None or kek is None or origin is None:
        return refuse(409, "Sign-in is not switched on in Settings.")
    try:
        secret = auth.config.secret(kek, config, envelope)
    except SealError:
        return refuse(500, "The saved client secret cannot be opened; save it again in Settings.")
    try:
        provider = await auth.oidc.provider(config.issuer)
        id_token = await auth.oidc.redeem(
            provider,
            code=code,
            verifier=login.verifier,
            redirect_uri=f"{origin}{CALLBACK_PATH}",
            client_id=config.client_id,
            client_secret=secret,
        )
        claims = await auth.oidc.verify(
            provider, id_token, client_id=config.client_id, nonce=login.nonce
        )
    except OidcError as failure:
        logger.warning("sign-in failed: %s", failure)
        return refuse(400, f"Sign-in failed: {failure}.")
    if not config.allows(claims):
        # The person's own subject, so the owner can add it to the rule (D3).
        return refuse(
            403,
            f"{claims['sub']} at {config.issuer} may not sign in to ScadBuddy. Add the"
            " subject, or the group it is in, to the sign-in rule in Settings.",
        )
    name = claims.get("name") or claims.get("preferred_username")
    email = claims.get("email")
    trusted = state.settings.trusted_proxy_networks
    peer = request.client.host if request.client else None
    token, session = await asyncio.to_thread(
        auth.sessions.create,
        issuer=config.issuer,
        subject=claims["sub"],
        kind="login",
        name=str(name)[:200] if name else None,
        email=str(email)[:320] if email else None,
        client_address=client_address(peer, request.headers.get("x-forwarded-for"), trusted),
        user_agent=(request.headers.get("user-agent") or "")[:USER_AGENT_CHARS] or None,
    )
    page = _page(
        status=200,
        title="Signed in",
        message=f"Signed in as {session.name or session.subject}.",
        origin=origin,
        popup=login.popup,
        return_to=login.return_to,
        signed_in=True,
    )
    _clear(page, LOGIN_COOKIE, "lax")
    _cookie(page, SESSION_COOKIE, token, int(ABSOLUTE_LIFETIME.total_seconds()), "strict")
    return page


@router.get(
    "/auth/break-glass",
    summary="Sign in with a break-glass code",
    response_class=HTMLResponse,
)
async def break_glass(
    request: Request,
    auth: AuthDep,
    settings_store: SettingsStoreDep,
    state: StateDep,
    code: Annotated[str, Query(max_length=100)],
) -> HTMLResponse:
    """Spends a code ``python -m scadbuddy.auth.break_glass`` printed and starts a
    ``break_glass`` session for an hour (§9.3), with the IdP down or the login
    misconfigured. Every use is logged."""
    origin = _public_origin(settings_store) or str(request.base_url).rstrip("/")
    if not await asyncio.to_thread(break_glass_codes.redeem, auth.pool, code):
        return _page(
            status=400,
            title="Sign-in failed",
            message="This break-glass link is used, expired or not one. Mint another in the pod.",
            origin=origin,
            popup=False,
        )
    peer = request.client.host if request.client else None
    address = client_address(
        peer, request.headers.get("x-forwarded-for"), state.settings.trusted_proxy_networks
    )
    token, _ = await asyncio.to_thread(
        auth.sessions.create,
        issuer=break_glass_codes.ISSUER,
        subject=break_glass_codes.SUBJECT,
        kind="break_glass",
        name="Break-glass",
        client_address=address,
        user_agent=(request.headers.get("user-agent") or "")[:USER_AGENT_CHARS] or None,
        lifetime=break_glass_codes.SESSION_LIFETIME,
    )
    # The audit: who used one, from where. Never the code.
    logger.warning("a break-glass sign-in was used", extra={"client_address": address})
    page = _page(
        status=200,
        title="Signed in",
        message="Signed in with a break-glass link, for an hour.",
        origin=origin,
        popup=False,
        return_to="/",
        signed_in=True,
    )
    lifetime = int(break_glass_codes.SESSION_LIFETIME.total_seconds())
    _cookie(page, SESSION_COOKIE, token, lifetime, "strict")
    return page


class LogoutResult(BaseModel):
    #: D7: where to send the browser to end the IdP's session too, when the login's
    #: ``rp_logout`` is on and the issuer publishes an ``end_session_endpoint``.
    end_session_url: str | None = None


def _revoked(events: EventsDep, session: AuthSession) -> None:
    emit(events, AuthSessionRevoked(principal=session.principal, session_id=session.id_hash.hex()))


@router.post("/auth/logout", response_model=LogoutResult, summary="Sign this browser out")
async def logout(
    response: Response,
    caller: CallerDep,
    auth: AuthDep,
    events: EventsDep,
    settings_store: SettingsStoreDep,
) -> LogoutResult:
    """Revokes this browser's session and clears its cookie (§5.1 step 3). Signed out
    already, it answers the same."""
    _clear(response, SESSION_COOKIE, "strict")
    session = _person(caller)
    if session is None:
        return LogoutResult()
    if await asyncio.to_thread(auth.sessions.revoke, session.id_hash):
        _revoked(events, session)
    config, _ = await asyncio.to_thread(auth.load_config)
    origin = _public_origin(settings_store)
    if not (config.rp_logout and config.enabled and origin):
        return LogoutResult()
    try:
        provider = await auth.oidc.provider(config.issuer)
    except OidcError:
        return LogoutResult()
    if provider.end_session_endpoint is None:
        return LogoutResult()
    # No ID token is kept (§5.1), so the client id names the RP instead of
    # id_token_hint (RP-Initiated Logout 1.0 §2).
    query = urlencode({"client_id": config.client_id, "post_logout_redirect_uri": f"{origin}/"})
    separator = "&" if "?" in provider.end_session_endpoint else "?"
    return LogoutResult(end_session_url=f"{provider.end_session_endpoint}{separator}{query}")


class AuthSessionInfo(BaseModel):
    """One of the person's signed-in sessions."""

    #: The hex SHA-256 the row is keyed by: it names the session, and is not the cookie.
    id: str
    current: bool
    kind: Literal["login", "delegated", "break_glass"]
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    client_address: str | None
    user_agent: str | None


def _signed_in(caller: Caller) -> AuthSession:
    session = _person(caller)
    if session is None:
        raise _problem(401, "signed-out", "sign in to see your sessions")
    return session


@router.get(
    "/auth/sessions", response_model=list[AuthSessionInfo], summary="Your signed-in sessions"
)
async def list_sessions(caller: CallerDep, auth: AuthDep) -> list[AuthSessionInfo]:
    current = _signed_in(caller)
    sessions = await asyncio.to_thread(auth.sessions.list_live, current.principal)
    return [
        AuthSessionInfo(
            id=session.id_hash.hex(),
            current=session.id_hash == current.id_hash,
            kind=session.kind,
            created_at=session.created_at,
            last_seen_at=session.last_seen_at,
            expires_at=min(session.idle_expires_at, session.absolute_expires_at),
            client_address=session.client_address,
            user_agent=session.user_agent,
        )
        for session in sessions
    ]


@router.delete(
    "/auth/sessions/{session_id}",
    status_code=204,
    summary="Revoke one of your sessions",
    responses={404: {"description": "No such live session of yours"}},
)
async def revoke_session(
    session_id: Annotated[str, Path(pattern="^[0-9a-f]{64}$")],
    response: Response,
    caller: CallerDep,
    auth: AuthDep,
    events: EventsDep,
) -> None:
    current = _signed_in(caller)
    id_hash = bytes.fromhex(session_id)
    if not await asyncio.to_thread(auth.sessions.revoke, id_hash, principal=current.principal):
        raise _problem(404, "no-session", "no such live session of yours")
    emit(events, AuthSessionRevoked(principal=current.principal, session_id=session_id))
    if id_hash == current.id_hash:
        _clear(response, SESSION_COOKIE, "strict")


class RevokedSessions(BaseModel):
    revoked: int


@router.post(
    "/auth/sessions/revoke-all",
    response_model=RevokedSessions,
    summary="Sign out everywhere",
)
async def revoke_all_sessions(
    response: Response, caller: CallerDep, auth: AuthDep, events: EventsDep
) -> RevokedSessions:
    """Revokes every live session of the signed-in person, this one included."""
    current = _signed_in(caller)
    revoked = await asyncio.to_thread(auth.sessions.revoke_all, current.principal)
    emit(events, AuthSessionRevoked(principal=current.principal))
    _clear(response, SESSION_COOKIE, "strict")
    return RevokedSessions(revoked=revoked)


class LoginConfigView(BaseModel):
    #: ``issuer`` and ``client_id`` are the deployment's (``SCADBUDDY_AUTH_OIDC_*``).
    config: LoginConfig
    #: Whether a client secret is saved for this issuer and client id; never the secret.
    has_client_secret: bool
    #: The redirect URI to register at the IdP, from the public URL.
    redirect_uri: str | None
    #: Why the login cannot be switched on as things stand, or ``None``.
    cannot_enable_reason: str | None


class LoginConfigUpdate(BaseModel):
    """What Settings may change. The issuer and client id are not among it: they are the
    deployment's, so no caller can point the login at an issuer of its choosing."""

    enabled: bool = False
    allowed_subjects: list[str] = Field(default_factory=list, max_length=100)
    group_claim: str | None = Field(default=None, max_length=200)
    group_value: str | None = Field(default=None, max_length=200)
    rp_logout: bool = False
    #: A new client secret; ``None`` keeps the saved one (while the issuer and client id
    #: stay the same, since it is sealed for them).
    client_secret: str | None = Field(default=None, max_length=1000, repr=False)


class LoginDiscoveryReport(BaseModel):
    issuer: str
    keys: int
    end_session: bool


def _blocker(auth: AuthComponent, origin: str | None) -> str | None:
    if auth.kek is None:
        return (
            "Set SCADBUDDY_SECRET_KEY_FILE: the client secret and the sign-in are sealed under it"
        )
    if origin is None:
        return "Set the public URL: the identity provider sends the browser back to it"
    return None


async def _view(auth: AuthComponent, settings_store: SettingsStoreDep) -> LoginConfigView:
    config, envelope = await asyncio.to_thread(auth.load_config)
    origin = _public_origin(settings_store)
    return LoginConfigView(
        config=config,
        has_client_secret=envelope is not None,
        redirect_uri=None if origin is None else f"{origin}{CALLBACK_PATH}",
        cannot_enable_reason=_blocker(auth, origin),
    )


@router.get("/auth/config", response_model=LoginConfigView, summary="The login's configuration")
async def get_login_config(auth: AuthDep, settings_store: SettingsStoreDep) -> LoginConfigView:
    return await _view(auth, settings_store)


@router.put(
    "/auth/config",
    response_model=LoginConfigView,
    summary="Save the login's configuration",
    responses={422: {"description": "The configuration cannot be switched on as it is"}},
)
async def put_login_config(
    body: LoginConfigUpdate, auth: AuthDep, settings_store: SettingsStoreDep
) -> LoginConfigView:
    """Saves the configuration (D1, D3, D7). Switched on, it runs discovery against the
    issuer first and saves nothing if that fails, so a typo cannot lock the login out
    (§9.2)."""
    current, envelope = await asyncio.to_thread(auth.load_config)
    config = LoginConfig.model_validate(
        {
            **body.model_dump(exclude={"client_secret"}),
            "issuer": current.issuer,
            "client_id": current.client_id,
        }
    )
    kek = auth.kek
    if body.client_secret:
        if kek is None:
            raise _problem(422, "no-key", _blocker(auth, None) or "")
        envelope = seal(kek, config, body.client_secret)
    if config.enabled:
        blocker = _blocker(auth, _public_origin(settings_store))
        if blocker is not None:
            raise _problem(422, "cannot-enable", blocker)
        missing = [
            what
            for what, ok in (
                ("SCADBUDDY_AUTH_OIDC_ISSUER", config.issuer),
                ("SCADBUDDY_AUTH_OIDC_CLIENT_ID", config.client_id),
                ("a client secret", envelope is not None),
                ("a sign-in rule (subjects, or a group claim and value)", config.has_rule()),
            )
            if not ok
        ]
        if missing:
            raise _problem(422, "incomplete", "switching sign-in on needs " + ", ".join(missing))
        try:
            check_url(config.issuer, "issuer")
            provider = await auth.oidc.provider(config.issuer, fresh=True)
            await auth.oidc.keys(provider)
        except OidcError as error:
            raise _problem(422, "discovery-failed", str(error)) from None
    await asyncio.to_thread(auth.config.save, config, envelope)
    return await _view(auth, settings_store)


@router.post(
    "/auth/config/test",
    response_model=LoginDiscoveryReport,
    summary="Check the issuer's discovery and keys",
    responses={422: {"description": "Discovery failed"}},
)
async def test_login_config(auth: AuthDep) -> LoginDiscoveryReport:
    """Fetches the deployment's issuer (``SCADBUDDY_AUTH_OIDC_ISSUER``), never one a
    caller names."""
    config, _ = await asyncio.to_thread(auth.load_config)
    if not config.issuer:
        raise _problem(422, "discovery-failed", "SCADBUDDY_AUTH_OIDC_ISSUER is not set")
    try:
        issuer = check_url(config.issuer, "issuer")
        provider = await auth.oidc.provider(issuer, fresh=True)
        keys = await auth.oidc.keys(provider)
    except OidcError as error:
        raise _problem(422, "discovery-failed", str(error)) from None
    return LoginDiscoveryReport(
        issuer=provider.issuer,
        keys=len(keys.keys),
        end_session=provider.end_session_endpoint is not None,
    )
