"""Standalone verifier for an evidence-bundle crate directory.

Run from the unzipped crate directory::

    python -m evidence.verify --crate-dir campaigns/<id>/

Performs three checks:

1. Reads ``manifest.json`` (in-toto Statement v1) and recomputes
   sha256 of every ``subject[]`` entry; mismatches = fail.
2. Reads ``manifest.json.dsse`` and verifies the Ed25519 signature
   against the public key in ``public.key``. Bad signature = fail.
3. Reads ``evidence.json`` and validates it as an ``EvidenceBundle``.

Exit code 0 on success, 1 on any failure. Pure stdlib + PyNaCl;
no orchestrator runtime needed.
"""
from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

from core.evidence import DsseEnvelope, EvidenceBundle, InTotoStatement
from evidence.signing import sha256_file, verify_envelope


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crate-dir", type=Path, required=True)
    args = parser.parse_args()

    crate_dir = args.crate_dir.resolve()
    errors: list[str] = []

    manifest_path = crate_dir / "manifest.json"
    dsse_path = crate_dir / "manifest.json.dsse"
    public_key_path = crate_dir / "public.key"
    evidence_path = crate_dir / "evidence.json"

    for required in (manifest_path, dsse_path, public_key_path, evidence_path):
        if not required.exists():
            errors.append(f"missing: {required.relative_to(crate_dir)}")

    if errors:
        for e in errors:
            print(f"FAIL  {e}")
        return 1

    # 1. recompute manifest digests
    statement = InTotoStatement.model_validate_json(manifest_path.read_text())
    for subj in statement.subject:
        target = crate_dir / subj.name
        if not target.exists():
            errors.append(f"manifest references missing file: {subj.name}")
            continue
        actual = sha256_file(target)
        expected = subj.digest["sha256"]
        if actual != expected:
            errors.append(
                f"sha256 mismatch on {subj.name}: expected {expected[:12]}…, got {actual[:12]}…"
            )

    # 2. verify the DSSE envelope
    envelope = DsseEnvelope.model_validate_json(dsse_path.read_text())
    public_key = base64.b64decode(public_key_path.read_text().strip())
    if not verify_envelope(envelope, public_key):
        errors.append("DSSE envelope signature did not verify")

    # 3. validate evidence.json shape
    try:
        EvidenceBundle.model_validate_json(evidence_path.read_text())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"evidence.json failed schema validation: {exc}")

    if errors:
        print("VERIFICATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"OK  crate at {crate_dir} verifies cleanly")
    print(f"    statement subjects: {len(statement.subject)}")
    print(f"    keyid: {envelope.signatures[0].keyid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
