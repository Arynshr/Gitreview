#!/usr/bin/env bash
# install_validation_deps.sh
#
# Detects and installs the external tools gitscribe's validation layer
# shells out to (validation/analysis/scanners/*.py, deterministic.py's
# bundle). Safe to run repeatedly - every tool is checked via `command
# -v` first and skipped if already present.
#
# What this installs:
#   ruff, bandit, pip-audit, semgrep - Python packages with CLI entrypoints
#   betterleaks, osv-scanner         - external Go binaries
#
# What this deliberately does NOT install: llama-server.
# validation/sandbox.py documents why: it downloads/tracks *model*
# files but refuses to install the llama-server *binary* itself, since
# that would mean running a third-party binary this tool can't
# cryptographically verify the provenance of. Overriding that here
# would contradict an existing, deliberate trust-boundary decision in
# the codebase, not fix a gap. Run `gitscribe sandbox init` for model
# setup once llama-server is on PATH via whatever route you trust
# (a package manager, a signed release you've checked yourself, etc.).
#
# Usage:
#   ./install_validation_deps.sh          # detect + install anything missing
#   ./install_validation_deps.sh --check  # detect only, never installs
#
# Exit code: 0 if every tool ends up available (or --check found all
# present), 1 if something is still missing at the end.

set -uo pipefail

CHECK_ONLY=false
if [[ "${1:-}" == "--check" || "${1:-}" == "-n" ]]; then
  CHECK_ONLY=true
fi

STATUS=0
declare -a SUMMARY=()

log()  { printf '  %s\n' "$*" >&2; }
note() { printf '%s\n' "$*" >&2; }

have() { command -v "$1" >/dev/null 2>&1; }

os_family() {
  case "$(uname -s)" in
    Darwin) echo "macos" ;;
    Linux)  echo "linux" ;;
    *)      echo "unknown" ;;
  esac
}

# --- Python-CLI tools (ruff, bandit, pip-audit) -----------------------------
#
# Prefer `uv tool install`: installs into an isolated environment per
# tool and puts the binary on PATH, without touching the project's own
# venv/dependency set - the same reasoning applies here as for any other
# global CLI tool. Falls back to pipx, then plain pip, in that order.
install_python_cli_tool() {
  local binary="$1" package="$2"

  if have "$binary"; then
    SUMMARY+=("$binary: already installed ($(command -v "$binary"))")
    return 0
  fi

  if $CHECK_ONLY; then
    SUMMARY+=("$binary: MISSING (would install '$package')")
    STATUS=1
    return 1
  fi

  note "installing $package ($binary not found on PATH)..."

  if have uv; then
    uv tool install "$package" >&2 && { SUMMARY+=("$binary: installed via uv tool install"); return 0; }
  fi

  if have pipx; then
    pipx install "$package" >&2 && { SUMMARY+=("$binary: installed via pipx"); return 0; }
  fi

  if have pip3; then
    pip3 install --user "$package" >&2 && { SUMMARY+=("$binary: installed via pip3 --user"); return 0; }
  fi

  SUMMARY+=("$binary: FAILED to install (no uv/pipx/pip3 succeeded)")
  STATUS=1
  return 1
}

# --- Betterleaks -------------------------------------------------------------
#
# Not a Python package - external Go binary. Tries the package manager
# most likely to already be on this machine first; falls back to `go
# install` if a Go toolchain is present; otherwise reports the manual
# steps (Homebrew tap, dnf, Docker) rather than guessing further.
install_betterleaks() {
  if have betterleaks; then
    SUMMARY+=("betterleaks: already installed ($(command -v betterleaks))")
    return 0
  fi

  if $CHECK_ONLY; then
    SUMMARY+=("betterleaks: MISSING")
    STATUS=1
    return 1
  fi

  note "installing betterleaks (not found on PATH)..."
  local family; family="$(os_family)"

  if [[ "$family" == "macos" ]] && have brew; then
    brew install betterleaks >&2 && { SUMMARY+=("betterleaks: installed via brew"); return 0; }
  fi

  if [[ "$family" == "linux" ]] && have brew; then
    brew install betterleaks/tap/betterleaks >&2 && { SUMMARY+=("betterleaks: installed via brew tap"); return 0; }
  fi

  if [[ "$family" == "linux" ]] && have dnf; then
    sudo dnf install -y betterleaks >&2 && { SUMMARY+=("betterleaks: installed via dnf"); return 0; }
  fi

  if have go; then
    go install github.com/betterleaks/betterleaks@latest >&2 \
      && { SUMMARY+=("betterleaks: installed via 'go install' (check \$GOPATH/bin is on PATH)"); return 0; }
  fi

  SUMMARY+=(
    "betterleaks: FAILED to auto-install. Manual options:"
    "    brew install betterleaks/tap/betterleaks"
    "    docker pull ghcr.io/betterleaks/betterleaks:latest"
    "    git clone https://github.com/betterleaks/betterleaks && cd betterleaks && make build"
  )
  STATUS=1
  return 1
}

# --- OSV-Scanner --------------------------------------------------------
#
# Another external Go binary (Google's OSV.dev vulnerability database
# scanner). Same install-chain shape as betterleaks above, different
# package coordinates.
install_osv_scanner() {
  if have osv-scanner; then
    SUMMARY+=("osv-scanner: already installed ($(command -v osv-scanner))")
    return 0
  fi

  if $CHECK_ONLY; then
    SUMMARY+=("osv-scanner: MISSING")
    STATUS=1
    return 1
  fi

  note "installing osv-scanner (not found on PATH)..."
  local family; family="$(os_family)"

  if [[ "$family" == "macos" ]] && have brew; then
    brew install osv-scanner >&2 && { SUMMARY+=("osv-scanner: installed via brew"); return 0; }
  fi

  if have go; then
    go install github.com/google/osv-scanner/v2/cmd/osv-scanner@latest >&2 \
      && { SUMMARY+=("osv-scanner: installed via 'go install' (check \$GOPATH/bin is on PATH)"); return 0; }
  fi

  SUMMARY+=(
    "osv-scanner: FAILED to auto-install. Manual options:"
    "    brew install osv-scanner"
    "    go install github.com/google/osv-scanner/v2/cmd/osv-scanner@latest"
    "    download a release binary from https://github.com/google/osv-scanner/releases"
  )
  STATUS=1
  return 1
}

# --- llama-server: detect only, never install (see header) -----------------
check_llama_server() {
  if have llama-server; then
    SUMMARY+=("llama-server: found ($(command -v llama-server)) - run 'gitscribe sandbox init' to fetch the model")
  else
    SUMMARY+=("llama-server: not found - gitscribe will not install this (see script header). validation.ai will fail closed until it's on PATH and a model is downloaded via 'gitscribe sandbox init'.")
  fi
}

main() {
  note "gitscribe validation dependency check$($CHECK_ONLY && echo ' (--check: detect only)')"
  note ""

  install_python_cli_tool ruff ruff
  install_python_cli_tool bandit bandit
  install_python_cli_tool pip-audit pip-audit
  install_python_cli_tool semgrep semgrep
  install_betterleaks
  install_osv_scanner
  check_llama_server

  note ""
  note "Summary:"
  for line in "${SUMMARY[@]}"; do
    log "$line"
  done
  note ""

  if [[ "$STATUS" -ne 0 ]]; then
    note "One or more required tools are missing. See above for manual install steps."
  else
    note "All checked tools are present."
  fi

  exit "$STATUS"
}

main "$@"
