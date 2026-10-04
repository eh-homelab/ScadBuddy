import base64
import json
from pathlib import Path

import pytest

from scadbuddy_durable.secrets import Envelope, SealError, kek_from_base64, open_bytes, open_secret

VECTORS = json.loads((Path(__file__).parents[2] / "agent/test/fixtures/secret-vectors.json").read_text())


def b64(s: str) -> bytes:
    return base64.b64decode(s)


@pytest.mark.parametrize("v", VECTORS["vectors"], ids=lambda v: v["name"])
def test_opens_every_vector(v: dict[str, str]) -> None:
    kek = kek_from_base64(VECTORS["kek_base64"])
    assert kek.id == VECTORS["kek_id"]
    env = Envelope(b64(v["secret_sealed_b64"]), b64(v["dek_sealed_b64"]), v["kek_id"])
    assert open_secret(kek, env, v["aad"]) == v["plaintext"]


@pytest.mark.parametrize("p", VECTORS["payload"], ids=lambda p: p["subject"])
def test_opens_every_payload_vector(p: dict[str, str]) -> None:
    sealed = b64(p["sealed_b64"])
    assert open_bytes(b64(p["dek_b64"]), sealed, f"ai_payload:{p['subject']}") == b64(p["plaintext_b64"])


def test_a_changed_aad_or_byte_fails() -> None:
    v = VECTORS["vectors"][0]
    kek = kek_from_base64(VECTORS["kek_base64"])
    env = Envelope(b64(v["secret_sealed_b64"]), b64(v["dek_sealed_b64"]), v["kek_id"])
    with pytest.raises(SealError):
        open_secret(kek, env, v["aad"] + "x")
    tampered = bytearray(env.secret_sealed)
    tampered[-1] ^= 1
    with pytest.raises(SealError):
        open_secret(kek, Envelope(bytes(tampered), env.dek_sealed, env.kek_id), v["aad"])


def test_kek_file_format_matches_typescript() -> None:
    with pytest.raises(ValueError):
        kek_from_base64("not base64!")
    with pytest.raises(ValueError):
        kek_from_base64(base64.b64encode(b"x" * 16).decode())
