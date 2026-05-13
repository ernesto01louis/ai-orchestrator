#!/usr/bin/env bash
# Install SOPS + age and generate the orchestrator's encryption keypair.
#
# Phase 0 deferred item → PR 3 of the audit-response hardening pass.
#
# What this does:
# 1. Installs ``age`` (Debian apt) and ``sops`` (pinned binary from
#    https://github.com/getsops/sops/releases) on the orchestrator LXC.
# 2. Generates ``/etc/ai-orchestrator/age/key.txt`` (chmod 600,
#    root-owned) if it doesn't already exist.
# 3. Prints the public key — paste it into ``.sops.yaml`` to authorise
#    encryption against this orchestrator.
# 4. Idempotent: re-running is safe; existing keys are not overwritten.
#
# Operator usage:
#   sudo bash scripts/install_sops.sh
#
# After running:
#   - Edit ``.sops.yaml`` to add the printed public key to
#     ``creation_rules[0].age``.
#   - ``sops --encrypt --in-place .env`` once → produces ``.env``
#     written back encrypted; renames it to ``.env.sops`` and commits.
#   - The systemd unit's ExecStartPre hook
#     (``scripts/decrypt_env.sh``) decrypts ``.env.sops`` to
#     ``/run/ai-orchestrator/.env`` on every service start.
#
# Key rotation: re-run with ``--rotate`` to generate a fresh keypair
# alongside the existing one (sops supports multiple recipient keys).
# See ``docs/SOPS.md`` for the full operator workflow.

set -euo pipefail

# ---- constants ----------------------------------------------------------
SOPS_VERSION="v3.9.4"   # pin; ratchet only when a release has been smoke-tested
KEY_DIR="/etc/ai-orchestrator/age"
KEY_FILE="${KEY_DIR}/key.txt"
SOPS_BIN="/usr/local/bin/sops"

ARCH="$(uname -m)"
case "${ARCH}" in
    x86_64)  SOPS_ARCH="amd64" ;;
    aarch64) SOPS_ARCH="arm64" ;;
    *)
        echo "[install_sops] unsupported arch: ${ARCH}" >&2
        exit 1
        ;;
esac

# ---- 1. age ------------------------------------------------------------
if ! command -v age >/dev/null 2>&1; then
    echo "[install_sops] installing age via apt..."
    apt-get update -y
    apt-get install -y age
else
    echo "[install_sops] age already installed: $(age --version 2>&1 | head -1)"
fi

# ---- 2. sops -----------------------------------------------------------
if ! command -v sops >/dev/null 2>&1 || [[ "$(sops --version 2>&1 | head -1)" != *"${SOPS_VERSION#v}"* ]]; then
    echo "[install_sops] installing sops ${SOPS_VERSION} from getsops/sops..."
    TMP="$(mktemp -d)"
    trap 'rm -rf "${TMP}"' EXIT
    URL="https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.${SOPS_ARCH}"
    curl -fSL -o "${TMP}/sops" "${URL}"
    chmod +x "${TMP}/sops"
    install -m 0755 "${TMP}/sops" "${SOPS_BIN}"
else
    echo "[install_sops] sops already at desired version: $(sops --version | head -1)"
fi

# ---- 3. age keypair ----------------------------------------------------
install -d -m 0700 -o root -g root "${KEY_DIR}"
if [[ -f "${KEY_FILE}" ]]; then
    echo "[install_sops] age key already exists at ${KEY_FILE} — not overwriting."
else
    echo "[install_sops] generating fresh age keypair at ${KEY_FILE}..."
    age-keygen -o "${KEY_FILE}"
    chmod 600 "${KEY_FILE}"
fi

# Extract + print the public key for the operator to paste into .sops.yaml.
PUBKEY="$(grep -oE 'age1[0-9a-z]+' "${KEY_FILE}" | head -1)"
if [[ -z "${PUBKEY}" ]]; then
    echo "[install_sops] could not extract age public key from ${KEY_FILE}" >&2
    exit 1
fi

cat <<EOF

[install_sops] DONE.

age public key (paste into .sops.yaml creation_rules[0].age):

    ${PUBKEY}

Next steps:
  1. Edit .sops.yaml so creation_rules[0].age == "${PUBKEY}".
  2. First-time encrypt:
       sops --encrypt .env > .env.sops
  3. Verify decrypt works:
       SOPS_AGE_KEY_FILE=${KEY_FILE} sops --decrypt .env.sops | head
  4. Commit the .env.sops file (and the .sops.yaml change). The plain
     .env stays gitignored.
  5. Update the systemd unit's ExecStartPre to call
     scripts/decrypt_env.sh.

Operator handbook: docs/SOPS.md.
EOF
