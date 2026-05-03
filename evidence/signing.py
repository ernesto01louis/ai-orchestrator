"""DSSE envelope construction + Ed25519 signing/verification.

Implements `DSSE`_ (Dead Simple Signing Envelope) using `PyNaCl`_'s
Ed25519 — same algorithm as minisign but in pure Python so we never
shell out for the security-critical path.

Wire format (the value of ``manifest.json.dsse``)::

    {
      "payload": "<base64(canonical_json(InTotoStatement))>",
      "payloadType": "application/vnd.in-toto+json",
      "signatures": [
        {"keyid": "<sha256(pubkey)[:16]>", "sig": "<base64(ed25519_sig)>"}
      ]
    }

The Ed25519 signature is over the DSSE **PAE** (Pre-Authentication
Encoding), not the raw payload — this is what protects the bundle
against payload-type-swap attacks::

    PAE = b"DSSEv1 " + len(payloadType) + " " + payloadType
                     + " " + len(payload) + " " + payload

Independent verification (no Python required) is documented in
``RUNBOOK.md``: a 30-line Python verifier ships at ``evidence/verify.py``.

.. _DSSE: https://github.com/secure-systems-lab/dsse/blob/master/protocol.md
.. _PyNaCl: https://pynacl.readthedocs.io/
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from nacl import exceptions as nacl_exc
from nacl import signing as nacl_signing

from core.evidence import (
    DsseEnvelope,
    DsseSignature,
    InTotoStatement,
    SlsaBuildDefinition,
    SlsaBuilder,
    SlsaBuildMetadata,
    SlsaProvenance,
    SlsaRunDetails,
    Subject,
)

DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
_PAE_PREFIX = b"DSSEv1"

# Default signing-key location. Override via AI_ORCHESTRATOR_SIGNING_DIR.
DEFAULT_KEY_DIR = Path("/etc/ai-orchestrator/signing")
SEED_FILENAME = "ed25519.seed"
PUBLIC_FILENAME = "ed25519.pub"


# ── canonicalisation ─────────────────────────────────


def canonical_json(obj: Any) -> bytes:
    """RFC-8785-flavored canonical JSON: sorted keys, tight separators."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _dsse_pae(payload_type: str, payload: bytes) -> bytes:
    return (
        _PAE_PREFIX
        + b" "
        + str(len(payload_type)).encode()
        + b" "
        + payload_type.encode()
        + b" "
        + str(len(payload)).encode()
        + b" "
        + payload
    )


def keyid_from_public(public_key: bytes) -> str:
    """16-hex-char keyid: first 16 chars of sha256(pubkey)."""
    return hashlib.sha256(public_key).hexdigest()[:16]


# ── key material ─────────────────────────────────────


class SigningKey:
    """Wraps a PyNaCl Ed25519 SigningKey with file-based persistence.

    Seed and public-key are stored as base64-encoded files in a key
    directory. The seed file is chmod 600 (the install script sets
    that; ``ensure_keypair`` validates it). PyNaCl is the only library
    that ever touches the seed bytes.
    """

    def __init__(self, signing_key: nacl_signing.SigningKey):
        self._key = signing_key

    # construction --------------------------------------------------

    @classmethod
    def generate(cls) -> "SigningKey":
        return cls(nacl_signing.SigningKey.generate())

    @classmethod
    def load(cls, key_dir: Path = DEFAULT_KEY_DIR) -> "SigningKey":
        seed_path = key_dir / SEED_FILENAME
        if not seed_path.exists():
            raise FileNotFoundError(
                f"Signing seed not found at {seed_path}. "
                "Run scripts/install_signing_key.sh first."
            )
        seed = base64.b64decode(seed_path.read_text().strip())
        return cls(nacl_signing.SigningKey(seed))

    # serialisation -------------------------------------------------

    def write(self, key_dir: Path = DEFAULT_KEY_DIR) -> None:
        key_dir.mkdir(parents=True, exist_ok=True)
        seed_path = key_dir / SEED_FILENAME
        public_path = key_dir / PUBLIC_FILENAME
        seed_path.write_text(base64.b64encode(bytes(self._key)).decode() + "\n")
        seed_path.chmod(0o600)
        public_path.write_text(self.public_b64() + "\n")
        public_path.chmod(0o644)

    # accessors -----------------------------------------------------

    def public_bytes(self) -> bytes:
        return bytes(self._key.verify_key)

    def public_b64(self) -> str:
        return base64.b64encode(self.public_bytes()).decode()

    def keyid(self) -> str:
        return keyid_from_public(self.public_bytes())

    # signing -------------------------------------------------------

    def sign_pae(self, payload_type: str, payload: bytes) -> bytes:
        """Sign the DSSE PAE for ``payload`` of ``payload_type``."""
        return self._key.sign(_dsse_pae(payload_type, payload)).signature


# ── DSSE envelope sign / verify ──────────────────────


def sign_statement(statement: InTotoStatement, key: SigningKey) -> DsseEnvelope:
    """Sign an in-toto Statement and return a DSSE envelope."""
    payload = canonical_json(statement.model_dump(by_alias=True, mode="json"))
    sig = key.sign_pae(DSSE_PAYLOAD_TYPE, payload)
    return DsseEnvelope(
        payload=base64.b64encode(payload).decode(),
        payloadType=DSSE_PAYLOAD_TYPE,
        signatures=[
            DsseSignature(keyid=key.keyid(), sig=base64.b64encode(sig).decode()),
        ],
    )


def verify_envelope(envelope: DsseEnvelope, public_key: bytes) -> bool:
    """Verify the envelope's signature against ``public_key``.

    Returns True iff at least one signature in the envelope matches
    the public key's keyid AND the Ed25519 verification succeeds. Any
    other state (no matching keyid, bad signature, malformed b64)
    returns False.
    """
    expected_keyid = keyid_from_public(public_key)
    try:
        payload = base64.b64decode(envelope.payload, validate=True)
    except (ValueError, base64.binascii.Error):
        return False

    pae = _dsse_pae(envelope.payloadType, payload)
    verify = nacl_signing.VerifyKey(public_key)

    for sig_entry in envelope.signatures:
        if sig_entry.keyid != expected_keyid:
            continue
        try:
            sig_bytes = base64.b64decode(sig_entry.sig, validate=True)
            verify.verify(pae, sig_bytes)
            return True
        except (nacl_exc.BadSignatureError, ValueError, base64.binascii.Error):
            return False
    return False


# ── in-toto Statement helpers ────────────────────────


def make_subject(path: str, sha256_hex: str) -> Subject:
    return Subject(name=path, digest={"sha256": sha256_hex})


def build_statement(
    subjects: list[Subject],
    *,
    builder_id: str,
    builder_version: dict[str, str],
    invocation_id: str,
    started: datetime,
    finished: datetime,
    external_parameters: dict,
    internal_parameters: dict,
    resolved_dependencies: list[Subject],
    byproducts: list[Subject] | None = None,
) -> InTotoStatement:
    """Construct an in-toto Statement v1 with a SLSA Provenance v1.0 predicate.

    Mandatory fields per the SLSA Provenance spec are surfaced as
    keyword arguments here so callers can't accidentally omit them.
    """
    return InTotoStatement(
        subject=subjects,
        predicate=SlsaProvenance(
            buildDefinition=SlsaBuildDefinition(
                externalParameters=external_parameters,
                internalParameters=internal_parameters,
                resolvedDependencies=resolved_dependencies,
            ),
            runDetails=SlsaRunDetails(
                builder=SlsaBuilder(id=builder_id, version=builder_version),
                metadata=SlsaBuildMetadata(
                    invocationId=invocation_id,
                    startedOn=started,
                    finishedOn=finished,
                ),
                byproducts=byproducts or [],
            ),
        ),
    )


# ── manifest helper ──────────────────────────────────


def sha256_file(path: Path, chunk_size: int = 65536) -> str:
    """Stream-hash a file; returns lowercase hex sha256."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
