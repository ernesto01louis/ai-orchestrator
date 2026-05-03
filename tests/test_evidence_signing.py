"""Tests for DSSE envelope signing/verification (Phase 1.2)."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evidence.signing import (
    SigningKey,
    build_statement,
    canonical_json,
    keyid_from_public,
    make_subject,
    sha256_bytes,
    sign_statement,
    verify_envelope,
)


@pytest.fixture
def key() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture
def statement():
    return build_statement(
        subjects=[make_subject("evidence.json", sha256_bytes(b"hello"))],
        builder_id="https://ai-orchestrator.io/builder/v0.1",
        builder_version={"ai-orchestrator": "0.1.2"},
        invocation_id="C1",
        started=datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
        finished=datetime(2026, 5, 3, 12, 1, 0, tzinfo=timezone.utc),
        external_parameters={"campaign": "smoke"},
        internal_parameters={},
        resolved_dependencies=[],
    )


def test_sign_then_verify_succeeds(key, statement):
    env = sign_statement(statement, key)
    assert verify_envelope(env, key.public_bytes())


def test_keyid_is_truncated_sha256(key):
    expected = keyid_from_public(key.public_bytes())
    assert key.keyid() == expected
    assert len(expected) == 16
    int(expected, 16)  # is hex


def test_tampered_payload_fails_verification(key, statement):
    env = sign_statement(statement, key)
    bad_payload = bytearray(base64.b64decode(env.payload))
    bad_payload[0] ^= 0xFF
    env_bad = env.model_copy(
        update={"payload": base64.b64encode(bytes(bad_payload)).decode()}
    )
    assert not verify_envelope(env_bad, key.public_bytes())


def test_tampered_signature_fails_verification(key, statement):
    env = sign_statement(statement, key)
    bad_sig = bytearray(base64.b64decode(env.signatures[0].sig))
    bad_sig[0] ^= 0xFF
    env_sig = env.model_copy(deep=True)
    env_sig.signatures[0].sig = base64.b64encode(bytes(bad_sig)).decode()
    assert not verify_envelope(env_sig, key.public_bytes())


def test_wrong_key_fails_verification(key, statement):
    env = sign_statement(statement, key)
    other = SigningKey.generate()
    assert not verify_envelope(env, other.public_bytes())


def test_keypair_persistence_round_trip(tmp_path: Path):
    key = SigningKey.generate()
    key.write(tmp_path)
    seed_file = tmp_path / "ed25519.seed"
    public_file = tmp_path / "ed25519.pub"
    assert seed_file.stat().st_mode & 0o777 == 0o600
    # Public file mode varies with umask in CI; just assert it's at most 644.
    assert public_file.stat().st_mode & 0o777 <= 0o644
    loaded = SigningKey.load(tmp_path)
    assert loaded.keyid() == key.keyid()
    assert loaded.public_bytes() == key.public_bytes()


def test_load_missing_key_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="Signing seed not found"):
        SigningKey.load(tmp_path)


def test_canonical_json_is_stable():
    """Same input MUST produce byte-identical output across calls."""
    a = canonical_json({"b": 1, "a": 2, "c": [3, 2, 1]})
    b = canonical_json({"a": 2, "c": [3, 2, 1], "b": 1})
    assert a == b
    assert a == b'{"a":2,"b":1,"c":[3,2,1]}'
