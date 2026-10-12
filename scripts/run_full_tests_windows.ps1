# Resume is opt-in. Without -Resume this script behaves exactly as it did before M23-02: every
# stage runs and the cache is neither read nor written.
param([switch]$Resume, [string]$InstalledRuntime, [switch]$Preflight)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if ($Preflight -and ($Resume -or $InstalledRuntime)) {
    throw "-Preflight cannot be combined with -Resume or -InstalledRuntime."
}

function Invoke-WithLinkReceipt {
    param([Parameter(Mandatory = $true)][scriptblock]$Action)
    $Names = @("H3_GATE_LINK_RECEIPT", "H3_GATE_LINK_KEY", "H3_GATE_LINK_PID")
    $Previous = @{}
    foreach ($Name in $Names) { $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name) }
    try {
        if ($Preflight) {
            foreach ($Name in $Names) { [Environment]::SetEnvironmentVariable($Name, $null) }
        } else {
            $env:H3_GATE_LINK_RECEIPT = $script:LinkReceiptPath
            $env:H3_GATE_LINK_KEY = $script:LinkReceiptKey
            $env:H3_GATE_LINK_PID = [string]$PID
        }
        & $Action
    } finally {
        # CRITICAL: per-run receipt values must not enter unrelated Resume stage fingerprints.
        foreach ($Name in $Names) { [Environment]::SetEnvironmentVariable($Name, $Previous[$Name]) }
    }
}

function Invoke-CheckedStep {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    if ($script:Resume) {
        & $Python scripts\gate_stages.py should-run $Name --resume
        # IMPORTANT: only status 1 is a cache hit; lookup failures must never skip a check.
        if ($LASTEXITCODE -eq 1) {
            Write-Host "`n==> $Name  [skipped: inputs unchanged since its recorded PASS]"
            return
        }
        if ($LASTEXITCODE -ne 0) { throw "Gate cache lookup failed: $LASTEXITCODE" }
        $Expected = & $Python scripts\gate_stages.py before-run $Name
        if ($LASTEXITCODE -ne 0) { throw "Gate checkpoint start failed: $LASTEXITCODE" }
    }

    Write-Host "`n==> $Name"
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
    if ($script:Resume) {
        & $Python scripts\gate_stages.py record $Name --expected $Expected
        if ($LASTEXITCODE -ne 0) { throw "Gate cache record failed: $LASTEXITCODE" }
    }
}

Set-Location $RepoRoot

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Missing project Python at .venv\Scripts\python.exe. Create .venv and install -e .[dev]."
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js 24 is required. Install the pinned major and retry."
}
if (-not (Get-Command pnpm.cmd -ErrorAction SilentlyContinue)) {
    throw "pnpm 11.3.0 is required. Run: corepack enable; corepack prepare pnpm@11.3.0 --activate"
}

$NodeMajor = [int]((& node -p "process.versions.node.split('.')[0]").Trim())
if ($NodeMajor -ne 24) {
    throw "Node.js 24 is required; found $(& node -v)."
}

$env:PYTHONPATH = $RepoRoot
$env:PRE_COMMIT_HOME = Join-Path $RepoRoot ".tmp\pre-commit-home"
$env:PLAYWRIGHT_OUTPUT_DIR = Join-Path $RepoRoot ".tmp\playwright-output"

$LinkReceiptPath = Join-Path $RepoRoot (".tmp\gate-link-receipts\" + [guid]::NewGuid().ToString("N") + "\receipt.json")
$LinkReceiptKey = & $Python -c "import secrets; print(secrets.token_hex(32))"
if ($LASTEXITCODE -ne 0) { throw "Could not create ephemeral gate receipt identity." }
try {
    Invoke-CheckedStep "workspace link guard" {
        Invoke-WithLinkReceipt { & $Python scripts\workspace_link_guard.py check }
    }
    Invoke-CheckedStep "CI prerequisite contracts" {
        & $Python scripts\ci_preflight.py
    }
    # Frontend evidence-policy fixtures create bounded children here, including in a fresh worktree.
    [void](New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot ".planning"))
    Invoke-CheckedStep "shipped artifact integrity" {
        & $Python scripts\supply_chain_manifest.py --emit validate
        if ($LASTEXITCODE -eq 0) {
            & $Python scripts\build_provenance.py --check
        }
        # Compiled identity is checked once by test_build_provenance in the backend suite below.
    }
    if ($InstalledRuntime) {
        Write-Host "`n==> explicitly supplied installed runtime parity"
        & $Python scripts\runtime_parity.py --installed-runtime $InstalledRuntime
        if ($LASTEXITCODE -ne 0) {
            throw "explicitly supplied installed runtime parity failed with exit code $LASTEXITCODE"
        }
    }
    Invoke-CheckedStep "pre-commit once (includes secret scan, lint, format, and typing)" {
        & $Python -m pre_commit run --all-files --show-diff-on-failure
    }
    Invoke-CheckedStep "package import" {
        & $Python -c "import comfyui_h3_context"
    }

    Invoke-CheckedStep "frontend formatting" {
        & pnpm.cmd --dir frontend exec prettier --check .
    }
    if ($Preflight) {
        Invoke-CheckedStep "guard preflight tests" {
            Invoke-WithLinkReceipt { & $Python -m scripts.gate_backend --preflight }
        }
        Write-Host "`nGUARD PREFLIGHT: PASS (six prerequisite stages and declared guards only)"
        return
    }
    Invoke-CheckedStep "frontend static contract" {
        & pnpm.cmd --dir frontend exec tsc --noEmit
    }
    Invoke-CheckedStep "frontend unit tests" {
        & pnpm.cmd --dir frontend run test
    }
    Invoke-CheckedStep "backend product tests" {
        Invoke-WithLinkReceipt {
            if ($Resume) { & $Python -m scripts.gate_backend --resume }
            else { & $Python -m scripts.gate_backend }
        }
    }
    Invoke-CheckedStep "security audit" {
        & $Python scripts\security_audit.py
    }
    Invoke-CheckedStep "frontend hermetic smoke" {
        & $Python scripts\gate_stages.py browser-smoke
    }

    if ($Resume) {
        & $Python scripts\gate_stages.py summary --resume
    } else {
        & $Python scripts\gate_stages.py summary
    }
    if ($LASTEXITCODE -ne 0) { throw "Gate summary failed with exit code $LASTEXITCODE" }

    Write-Host "`nFULL GATE: PASS (product regressions + 20-case hermetic smoke; coverage/tooling campaigns excluded)"
} finally {
    $LinkReceiptKey = $null
    & $Python -m scripts.gate_link_receipt cleanup $LinkReceiptPath
    if ($LASTEXITCODE -ne 0) { throw "Gate receipt cleanup refused its location." }
}
