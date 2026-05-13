#!/usr/bin/env bash
# Decrypt .env.sops → /run/ai-orchestrator/.env (memfs) at service start.
#
# Wired into the systemd unit as ExecStartPre. The decrypted file lives
# in tmpfs (RuntimeDirectory=ai-orchestrator) so it never touches disk
# and is auto-cleaned when the service stops.
#
# Idempotent + fail-tolerant:
# - No-op when .env.sops is missing (legacy installs that still use a
#   plaintext .env keep working).
# - No-op when SOPS isn't installed (operator hasn't run install_sops.sh
#   yet — service starts using the existing .env).
# - Exits non-zero only if .env.sops exists, sops is installed, and
#   decryption actually fails — that's a configuration bug worth
#   surfacing to systemd.
#
# See docs/SOPS.md for the operator workflow.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_SOPS="${REPO_ROOT}/.env.sops"
KEY_FILE="${SOPS_AGE_KEY_FILE:-/etc/ai-orchestrator/age/key.txt}"
TARGET_DIR="${ORCHESTRATOR_RUNTIME_DIR:-/run/ai-orchestrator}"
TARGET="${TARGET_DIR}/.env"

if [[ ! -f "${ENV_SOPS}" ]]; then
    echo "[decrypt_env] ${ENV_SOPS} absent — legacy plaintext .env path active."
    exit 0
fi
if ! command -v sops >/dev/null 2>&1; then
    echo "[decrypt_env] sops not installed — run scripts/install_sops.sh. Skipping decrypt." >&2
    exit 0
fi
if [[ ! -f "${KEY_FILE}" ]]; then
    echo "[decrypt_env] age key file missing at ${KEY_FILE} — run scripts/install_sops.sh." >&2
    exit 1
fi

mkdir -p "${TARGET_DIR}"
SOPS_AGE_KEY_FILE="${KEY_FILE}" sops --decrypt --output "${TARGET}" "${ENV_SOPS}"
chmod 600 "${TARGET}"
echo "[decrypt_env] decrypted ${ENV_SOPS} → ${TARGET}"
