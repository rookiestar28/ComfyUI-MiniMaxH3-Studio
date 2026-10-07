#!/usr/bin/env bash
set -euo pipefail

resume=0
backend_args=()
for arg in "$@"; do
  case "$arg" in
    --resume) resume=1; backend_args=(--resume) ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$repo_root/.venv-wsl/bin/python"

cd "$repo_root"

if [[ ! -x "$python_bin" ]]; then
  echo "Missing project Python at .venv-wsl/bin/python. Create .venv-wsl and install -e '.[dev]'." >&2
  exit 2
fi
if ! command -v node >/dev/null 2>&1; then
  echo "Node.js 24 is required. Install the pinned major and retry." >&2
  exit 2
fi
if ! command -v pnpm >/dev/null 2>&1; then
  echo "pnpm 11.3.0 is required. Run: corepack enable && corepack prepare pnpm@11.3.0 --activate" >&2
  exit 2
fi
if [[ "$(node -p "process.versions.node.split('.')[0]")" != "24" ]]; then
  echo "Node.js 24 is required; found $(node -v)." >&2
  exit 2
fi
export PYTHONPATH="$repo_root"
export PRE_COMMIT_HOME="$repo_root/.tmp/pre-commit-home"
export PLAYWRIGHT_OUTPUT_DIR="$repo_root/.tmp/playwright-output"

run_step() {
  local name="$1"
  shift
  if [[ "$resume" == "1" ]]; then
    local decision=0
    "$python_bin" scripts/gate_stages.py should-run "$name" --resume || decision=$?
    # IMPORTANT: only status 1 is a cache hit; lookup failures must never skip a check.
    if [[ "$decision" == "1" ]]; then
      printf '\n==> %s  [skipped: inputs unchanged since its recorded PASS]\n' "$name"
      return 0
    elif [[ "$decision" != "0" ]]; then
      echo "Gate cache lookup failed: $decision" >&2
      exit "$decision"
    fi
  fi
  printf '\n==> %s\n' "$name"
  "$@"
  if [[ "$resume" == "1" ]]; then
    "$python_bin" scripts/gate_stages.py record "$name"
  fi
}

run_step "workspace link guard" "$python_bin" scripts/workspace_link_guard.py check
# Frontend evidence-policy fixtures create bounded children here, including in a fresh worktree.
mkdir -p .planning
run_step "shipped artifact integrity" bash -c \
  '"$1" scripts/supply_chain_manifest.py --emit validate && "$1" scripts/build_provenance.py --check' \
  _ "$python_bin"
# Compiled identity is checked once by test_build_provenance in the backend suite below.
run_step "pre-commit once (includes secret scan, lint, format, and typing)" \
  "$python_bin" -m pre_commit run --all-files --show-diff-on-failure
run_step "package import" "$python_bin" -c "import comfyui_h3_context"

run_step "frontend formatting" pnpm --dir frontend exec prettier --check .
run_step "frontend static contract" pnpm --dir frontend exec tsc --noEmit
run_step "frontend unit tests" pnpm --dir frontend run test
run_step "backend product tests" \
  "$python_bin" -m scripts.gate_backend "${backend_args[@]}"
run_step "security audit" "$python_bin" scripts/security_audit.py
run_step "frontend hermetic smoke" "$python_bin" scripts/gate_stages.py browser-smoke

if [[ "$resume" == "1" ]]; then
  "$python_bin" scripts/gate_stages.py summary --resume
else
  "$python_bin" scripts/gate_stages.py summary
fi

printf '\nFULL GATE: PASS (product regressions + 20-case hermetic smoke; coverage/tooling campaigns excluded)\n'
