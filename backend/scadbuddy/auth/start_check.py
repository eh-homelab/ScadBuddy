"""The start check of ``SCADBUDDY_AUTH_MODE=enforce`` (#4975 phase 8, spec 2026-11-01 §9.1):
the API refuses to start unless the login is usable, saying what is missing, so a deploy
with a broken configuration fails its readiness instead of serving a page no one can sign
in to.

Usable is what ``PUT /auth/config`` checks before it switches the login on (§9.2): the
issuer and client id (``SCADBUDDY_AUTH_OIDC_*``), the KEK and the public URL, the login
switched on with a client secret sealed for that issuer and client id, a sign-in rule (D3),
and the issuer's discovery and keys answering now. The way back from a refused start is
``SCADBUDDY_AUTH_MODE=observe`` and a rollout; break-glass (§9.3) needs a running API.
"""

from __future__ import annotations

import asyncio

from scadbuddy.auth.component import AuthComponent
from scadbuddy.auth.oidc import OidcError, check_url


class LoginNotUsableError(RuntimeError):
    """``enforce`` without a usable login; the message names every missing part."""


async def login_problems(auth: AuthComponent, public_url: str | None) -> list[str]:
    """What keeps the login from being usable, or nothing."""
    config, envelope = await asyncio.to_thread(auth.load_config)
    problems = [
        what
        for what, ok in (
            ("SCADBUDDY_AUTH_OIDC_ISSUER is not set", config.issuer),
            ("SCADBUDDY_AUTH_OIDC_CLIENT_ID is not set", config.client_id),
            (
                "SCADBUDDY_SECRET_KEY_FILE is not set (the client secret is sealed under it)",
                auth.kek is not None,
            ),
            (
                "the public URL is not set (the identity provider sends the browser back to it)",
                public_url,
            ),
            ("sign-in is not switched on in Settings → Sign-in", config.enabled),
            (
                "no client secret is saved for this issuer and client id",
                envelope is not None,
            ),
            (
                "no sign-in rule (allowed subjects, or a group claim and value)",
                config.has_rule(),
            ),
        )
        if not ok
    ]
    if config.issuer:
        try:
            check_url(config.issuer, "issuer")
            provider = await auth.oidc.provider(config.issuer, fresh=True)
            await auth.oidc.keys(provider)
        except OidcError as error:
            problems.append(f"the issuer cannot be used: {error}")
    return problems


async def check_enforce(auth: AuthComponent, public_url: str | None) -> None:
    """Raise :class:`LoginNotUsableError` naming what is missing, or return."""
    problems = await login_problems(auth, public_url)
    if problems:
        raise LoginNotUsableError(
            "SCADBUDDY_AUTH_MODE=enforce needs a usable login: "
            + "; ".join(problems)
            + ". Configure and test sign-in with SCADBUDDY_AUTH_MODE=off or observe first"
            ' (README, "Signing in").'
        )
