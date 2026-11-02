"""The e2e stack's identity provider (#4975 phase 7, spec 2026-11-01 §11): the backend's
own fake issuer (``backend/tests/auth/fake_oidc.py`` ``FakeIssuer``), served over HTTP for
the edge (``e2e-stack/edge/edge.py``) to put behind ``https://idp.e2e.test``.

One fake, so what the backend's tests check and what the browsers sign in through are the
same code: discovery, the JWKS, ``/authorize`` signing in ``FAKE_OIDC_SUBJECT`` with no
form, the token endpoint checking the client secret, the redirect URI and PKCE,
``/logout``, and ``POST /__misbehave`` to make the next ID tokens wrong.

Run from the test image's venv with the repository's ``backend/`` on ``PYTHONPATH``
(``e2e-stack/compose.auth.yaml``).
"""

from __future__ import annotations

import os

import uvicorn

from tests.auth.fake_oidc import FakeIssuer


def main() -> None:
    issuer = FakeIssuer(
        issuer=os.environ.get("FAKE_OIDC_ISSUER", "https://idp.e2e.test"),
        client_id=os.environ.get("FAKE_OIDC_CLIENT_ID", "scadbuddy"),
        client_secret=os.environ["FAKE_OIDC_CLIENT_SECRET"],
        user={
            "sub": os.environ.get("FAKE_OIDC_SUBJECT", "alice"),
            "name": os.environ.get("FAKE_OIDC_NAME", "Alice"),
            "email": os.environ.get("FAKE_OIDC_EMAIL", "alice@example.test"),
        },
    )
    uvicorn.run(issuer.app, host="0.0.0.0", port=int(os.environ.get("PORT", "9000")))


if __name__ == "__main__":
    main()
