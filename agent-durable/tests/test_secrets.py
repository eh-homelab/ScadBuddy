import base64
from pathlib import Path
from typing import Any

import pytest

from scadbuddy_durable.secrets import (
    Envelope,
    SealError,
    SecretKeyError,
    kek_from_base64,
    load_kek,
    open_secret,
)


def _env(v: dict[str, Any]) -> Envelope:
    return Envelope(
        secret_sealed=base64.b64decode(v["secret_sealed_b64"]),
        dek_sealed=base64.b64decode(v["dek_sealed_b64"]),
        kek_id=v["kek_id"],
    )


def test_kek_id_is_the_agents(vectors: dict[str, Any]) -> None:
    assert kek_from_base64(vectors["kek_b64"]).id == vectors["kek_id"]


def test_opens_every_v1_secret_vector(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    for v in vectors["secrets"]:
        assert open_secret(kek, _env(v), v["aad"]) == v["plaintext"], v["name"]


def test_a_wrong_aad_fails_authentication(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    v = vectors["secrets"][0]
    with pytest.raises(SealError, match="failed authentication"):
        open_secret(kek, _env(v), v["aad"] + "x")


def test_another_keys_envelope_is_named_not_tried(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(base64.b64encode(b"\x01" * 32).decode())
    v = vectors["secrets"][0]
    with pytest.raises(SealError, match=f"sealed with key {v['kek_id']}"):
        open_secret(kek, _env(v), v["aad"])


def test_a_truncated_envelope_is_malformed(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    v = vectors["secrets"][0]
    env = _env(v)
    short = Envelope(
        secret_sealed=env.secret_sealed, dek_sealed=env.dek_sealed[:20], kek_id=env.kek_id
    )
    with pytest.raises(SealError, match="malformed"):
        open_secret(kek, short, v["aad"])


@pytest.mark.parametrize("suffix", ["\n", "\r\n", "  \n"])
def test_key_file_whitespace_is_ignored(
    tmp_path: Path, vectors: dict[str, Any], suffix: str
) -> None:
    f = tmp_path / "k"
    f.write_text(vectors["kek_b64"] + suffix)
    assert load_kek(str(f)).id == vectors["kek_id"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (base64.b64encode(b"\x00" * 33).decode(), "decodes to 33 bytes, not 32"),
        ("not base64!!", "is not base64"),
        ("00" * 32, "decodes to 48 bytes, not 32"),
    ],
)
def test_a_bad_key_file_is_refused_by_name(tmp_path: Path, content: str, message: str) -> None:
    f = tmp_path / "k"
    f.write_text(content)
    with pytest.raises(SecretKeyError) as raised:
        load_kek(str(f))
    assert message in str(raised.value)
    assert "SCADBUDDY_SECRET_KEY_FILE" in str(raised.value)
    assert content not in str(raised.value)


def test_unset_and_unreadable_key_files_name_the_variable(tmp_path: Path) -> None:
    with pytest.raises(SecretKeyError, match="SCADBUDDY_SECRET_KEY_FILE is not set"):
        load_kek(None)
    with pytest.raises(SecretKeyError, match="SCADBUDDY_SECRET_KEY_FILE cannot be read"):
        load_kek(str(tmp_path / "missing"))


def test_the_key_never_appears_in_repr(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    assert repr(kek.key) not in repr(kek)
