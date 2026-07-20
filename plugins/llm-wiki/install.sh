#!/usr/bin/env bash
# Install the Python dependencies for the llm-wiki plugin.
# Tries uv, then pip --user, then pip in that order.

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="${SCRIPT_DIR}/requirements.txt"

if [ ! -f "${REQ_FILE}" ]; then
  echo "ERROR: requirements.txt not found at ${REQ_FILE}" >&2
  exit 1
fi

PY_BIN="${PYTHON:-python3}"

if ! command -v "${PY_BIN}" >/dev/null 2>&1; then
  echo "ERROR: python3 not found on PATH. Install Python 3.10 or newer first." >&2
  exit 1
fi

PY_VER="$(${PY_BIN} -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MAJOR="${PY_VER%%.*}"
PY_MINOR="${PY_VER##*.}"

if [ "${PY_MAJOR}" -lt 3 ] || { [ "${PY_MAJOR}" -eq 3 ] && [ "${PY_MINOR}" -lt 10 ]; }; then
  echo "ERROR: Python 3.10+ required (found ${PY_VER})." >&2
  exit 1
fi

echo "==> Using ${PY_BIN} (Python ${PY_VER})"
echo "==> Installing dependencies from ${REQ_FILE}"

install_ok=0

try_install() {
  local label="$1"
  shift
  echo ""
  echo "--- Trying: ${label}"
  if "$@"; then
    echo "--- Success: ${label}"
    install_ok=1
    return 0
  else
    echo "--- Failed:  ${label} (will try next)"
    return 1
  fi
}

if command -v uv >/dev/null 2>&1; then
  try_install "uv pip install --system" \
    uv pip install -r "${REQ_FILE}" --system || true
fi

VENV_PATH="${LLMWIKI_VENV:-${HOME}/.llm-wiki-venv}"
VENV_USED=0

if [ "${install_ok}" -eq 0 ]; then
  echo ""
  echo "--- Trying: venv at ${VENV_PATH}"
  if "${PY_BIN}" -m venv "${VENV_PATH}" 2>&1 && \
     "${VENV_PATH}/bin/pip" install --quiet --upgrade pip 2>&1 && \
     "${VENV_PATH}/bin/pip" install -r "${REQ_FILE}" 2>&1; then
    echo "--- Success: venv at ${VENV_PATH}"
    install_ok=1
    VENV_USED=1
    echo ""
    echo "==> Venv created at ${VENV_PATH}"
    echo "==> Set LLMWIKI_VENV=${VENV_PATH} to override the default location."
    # Export so check-setup.sh validates the right interpreter
    export PYTHON="${VENV_PATH}/bin/python3"
  else
    echo "--- Failed:  venv at ${VENV_PATH} (will try next)"
  fi
fi

if [ "${install_ok}" -eq 0 ]; then
  try_install "pip install --user" \
    "${PY_BIN}" -m pip install --user -r "${REQ_FILE}" || true
fi

if [ "${install_ok}" -eq 0 ]; then
  try_install "pip install (default location)" \
    "${PY_BIN}" -m pip install -r "${REQ_FILE}" || true
fi

if [ "${install_ok}" -eq 0 ]; then
  cat <<'EOF' >&2

ERROR: All install strategies failed.

Common causes and remedies:
  * No PyPI access from this machine.
    - Point pip at your corporate mirror:
        pip config set global.index-url https://your-mirror.example.com/simple
    - Or set PIP_INDEX_URL in your shell:
        export PIP_INDEX_URL=https://your-mirror.example.com/simple
  * Behind a TLS-intercepting proxy.
    - Set the trusted-host and cert:
        pip config set global.trusted-host your-mirror.example.com
        export SSL_CERT_FILE=/path/to/corp-ca.pem
  * Air-gapped machine.
    - Download wheels on a connected machine and copy them over:
        pip download -r requirements.txt -d ./wheels
      Then install offline:
        pip install --no-index --find-links ./wheels -r requirements.txt

See plugins/llm-wiki/skills/ingest/references/setup.md for more.
EOF
  exit 1
fi

echo ""
echo "==> Running check-setup.sh"
bash "${SCRIPT_DIR}/check-setup.sh"

echo ""
echo "==> Done. Fill in .wikirc.json for your wiki and try /ingest."
