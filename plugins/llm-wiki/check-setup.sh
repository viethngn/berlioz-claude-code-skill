#!/usr/bin/env bash
# Verify the environment is ready for the llm-wiki plugin.
# Optionally pass a .wikirc.json path to validate the config too.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Resolve interpreter: explicit PYTHON env var → default venv → system python3
PY_BIN="${PYTHON:-}"
if [ -z "${PY_BIN}" ]; then
  _VENV_PATH="${LLMWIKI_VENV:-${HOME}/.llm-wiki-venv}"
  if [ -x "${_VENV_PATH}/bin/python3" ]; then
    PY_BIN="${_VENV_PATH}/bin/python3"
  fi
fi
PY_BIN="${PY_BIN:-python3}"

status=0
ok()   { printf "  \033[32mOK\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!!\033[0m %s\n" "$1"; status=1; }
bad()  { printf "  \033[31mMISSING\033[0m %s\n" "$1"; status=1; }

echo "== llm-wiki check-setup =="
echo ""

echo "Python:"
if command -v "${PY_BIN}" >/dev/null 2>&1; then
  PY_VER="$(${PY_BIN} -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "unknown")"
  PY_MAJOR="$(${PY_BIN} -c 'import sys; print(sys.version_info[0])' 2>/dev/null || echo "0")"
  PY_MINOR="$(${PY_BIN} -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo "0")"
  if [ "${PY_MAJOR}" -ge 3 ] && [ "${PY_MINOR}" -ge 10 ]; then
    ok "${PY_BIN} (Python ${PY_VER})"
  else
    bad "${PY_BIN} is Python ${PY_VER} but we require 3.10+"
  fi
else
  bad "${PY_BIN} not found on PATH"
fi

echo ""
echo "Python packages:"
check_pkg() {
  local mod="$1"
  local display="$2"
  if "${PY_BIN}" -c "import ${mod}" >/dev/null 2>&1; then
    local ver
    ver="$("${PY_BIN}" -c "import ${mod}; print(getattr(${mod}, '__version__', '?'))" 2>/dev/null || echo "?")"
    ok "${display} (${ver})"
  else
    bad "${display} — install with: bash ${SCRIPT_DIR}/install.sh"
  fi
}

check_pkg requests          "requests"
check_pkg markdownify       "markdownify"
check_pkg bs4               "beautifulsoup4"
check_pkg trafilatura       "trafilatura"
check_pkg pypdf             "pypdf"
check_pkg docx              "python-docx"
check_pkg openpyxl          "openpyxl"
check_pkg pptx              "python-pptx"
check_pkg google.genai      "google-genai"
check_pkg PIL               "pillow"

echo ""
echo "System tools:"
if command -v git >/dev/null 2>&1; then
  ok "git ($(git --version 2>/dev/null | head -n1))"
else
  bad "git not on PATH — required for the git-backed diff persistence"
fi

if command -v bash >/dev/null 2>&1; then
  ok "bash"
else
  bad "bash not on PATH"
fi

CONFIG_PATH="${1:-}"
if [ -n "${CONFIG_PATH}" ]; then
  echo ""
  echo "Config file: ${CONFIG_PATH}"
  if [ ! -f "${CONFIG_PATH}" ]; then
    bad "file not found"
  else
    if "${PY_BIN}" -c "import json,sys; json.load(open(sys.argv[1]))" "${CONFIG_PATH}" >/dev/null 2>&1; then
      ok "valid JSON"
    else
      bad "invalid JSON"
    fi
    if grep -q "REPLACE_ME" "${CONFIG_PATH}" 2>/dev/null; then
      warn "contains REPLACE_ME placeholders — fill them in before running /ingest"
    fi
    if grep -q "example.com" "${CONFIG_PATH}" 2>/dev/null; then
      warn "still points at example.com URLs — replace with your real endpoints"
    fi
    # auto_push + git.remote sanity
    _AUTO_PUSH="$("${PY_BIN}" -c "
import json,sys
d=json.load(open(sys.argv[1]))
print(str(d.get('auto_push',False)).lower())
" "${CONFIG_PATH}" 2>/dev/null || echo "false")"
    if [ "${_AUTO_PUSH}" = "true" ]; then
      _GIT_REMOTE="$("${PY_BIN}" -c "
import json,sys
d=json.load(open(sys.argv[1]))
print((d.get('git') or {}).get('remote','origin'))
" "${CONFIG_PATH}" 2>/dev/null || echo "origin")"
      warn "auto_push=true — make sure 'git remote ${_GIT_REMOTE}' is configured (run: git remote -v)"
    fi
    # slack.token placeholder check
    _SLACK_TOKEN="$("${PY_BIN}" -c "
import json,sys
d=json.load(open(sys.argv[1]))
print((d.get('slack') or {}).get('token',''))
" "${CONFIG_PATH}" 2>/dev/null || echo "")"
    if echo "${_SLACK_TOKEN}" | grep -qE "REPLACE_ME|xoxp-REPLACE"; then
      warn "slack.token looks like a placeholder — fill in your User OAuth token for Slack ingest"
    fi
  fi
fi

echo ""
if [ "${status}" -eq 0 ]; then
  printf "\033[32m== All checks passed ==\033[0m\n"
else
  printf "\033[33m== Some checks did not pass — see notes above ==\033[0m\n"
fi
exit "${status}"
