"""A flow run's token (#4975 phase 5, spec 2026-11-01 §7.3): what lets the projects
worker's credential assert a run's starter as the author of the run's host calls.

The API mints it when it starts the run and carries it in the run's sealed start payload
(`workflows/project.py` `FlowStart.run_token`); each host call sends it in
:data:`RUN_TOKEN_HEADER`, and the API believes the call's author header only when the token
is the run's and the author is the run's `started_by`. A MAC of the run id under a key
derived from the KEK, as `slicing/transfer_token.py` derives its own. The format:
``<run id>.<hex HMAC-SHA256>``. A token is a bearer secret: never log one.
"""

from __future__ import annotations

import hashlib
import hmac

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from scadbuddy.core.secrets import Kek

RUN_TOKEN_HEADER = "x-scadbuddy-run-token"
#: The HKDF label of :func:`run_token_key`.
RUN_TOKEN_KEY_INFO = b"scadbuddy:flows:run-token:v1"
_MAX_RUN_ID = 64


def run_token_key(kek: Kek) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=RUN_TOKEN_KEY_INFO).derive(
        kek.key
    )


def _mac(key: bytes, run_id: str) -> str:
    return hmac.new(key, b"flow-run\n" + run_id.encode("ascii"), hashlib.sha256).hexdigest()


def mint_run_token(key: bytes, run_id: str) -> str:
    return f"{run_id}.{_mac(key, run_id)}"


def run_of_token(key: bytes, token: str) -> str | None:
    """The run id ``token`` was minted for, or None when it is not one of ours."""
    run_id, dot, mac = token.partition(".")
    if not dot or not run_id or len(run_id) > _MAX_RUN_ID or not run_id.isascii():
        return None
    if not hmac.compare_digest(mac.encode("ascii", "replace"), _mac(key, run_id).encode()):
        return None
    return run_id
