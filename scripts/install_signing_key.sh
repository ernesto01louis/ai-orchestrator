#!/usr/bin/env bash
# install_signing_key.sh
#
# One-shot setup for the orchestrator's evidence-bundle signing key.
# Generates a fresh Ed25519 keypair (via the Python signing module so
# the wire format is identical to runtime) and writes it under
# ${KEY_DIR:-/etc/ai-orchestrator/signing}/.
#
# Idempotent: refuses to overwrite an existing seed file. Use --force
# to rotate a key (which invalidates every previously-signed bundle —
# don't do this casually).
#
# After running this once, ``GET /campaigns/{id}/evidence/verify`` and
# ``POST /campaigns/{id}/evidence/refresh`` start working.

set -euo pipefail

KEY_DIR="${KEY_DIR:-/etc/ai-orchestrator/signing}"
REPO_ROOT="${REPO_ROOT:-/opt/ai-orchestrator}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/venv/bin/python}"
FORCE=0

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      cat <<HELP
Usage: $0 [--force]

Environment overrides:
  KEY_DIR     destination dir (default /etc/ai-orchestrator/signing)
  REPO_ROOT   orchestrator checkout (default /opt/ai-orchestrator)
  PYTHON_BIN  python interpreter (default \$REPO_ROOT/venv/bin/python)

Files created:
  \$KEY_DIR/ed25519.seed   chmod 600 (PRIVATE — never commit)
  \$KEY_DIR/ed25519.pub    chmod 644 (public, ships with bundles)
HELP
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$KEY_DIR"

if [ -f "$KEY_DIR/ed25519.seed" ] && [ "$FORCE" -ne 1 ]; then
  echo "Signing key already exists at $KEY_DIR/ed25519.seed."
  echo "Use --force to rotate (invalidates all previously-signed bundles)."
  exit 0
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Python interpreter not found: $PYTHON_BIN" >&2
  exit 1
fi

"$PYTHON_BIN" - <<PY
from pathlib import Path
from evidence.signing import SigningKey
key = SigningKey.generate()
key.write(Path("$KEY_DIR"))
print(f"Generated signing keypair at $KEY_DIR")
print(f"  keyid: {key.keyid()}")
print(f"  public.b64: {key.public_b64()}")
PY

# Defensive: re-assert chmod even if Python's umask interfered
chmod 600 "$KEY_DIR/ed25519.seed"
chmod 644 "$KEY_DIR/ed25519.pub"

echo
echo "Done. Verify with:"
echo "  ls -la $KEY_DIR/"
echo "  curl -s http://127.0.0.1:8000/campaigns/<id>/evidence/verify | jq"
