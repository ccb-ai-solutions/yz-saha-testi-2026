<#
Phase 7 (added 30 Oct) - YTU COSMOS's Turkish guardrail, ytu-ce-cosmos/modernbert-tr-guardrail, on this PC.
A classifier run in-process (harness/local/backends.py LocalGuardClassifier): no API, no tokens, no cost.  The same
131 prompts as the other guard models, sent as they are.  The rule is fixed before the run: a prompt is unsafe when
the model's unsafe probability is above 0.5 (configs/models.yaml); the analysis also reports AUROC, which needs no
threshold.  The first run downloads the model from Hugging Face (about 0.6 GB) into the usual cache.

  powershell -ExecutionPolicy Bypass -File scripts\run_phase7.ps1          # dry run: prints the plan, loads nothing
  powershell -ExecutionPolicy Bypass -File scripts\run_phase7.ps1 -Yes     # runs it (a minute or two)

Result: results\track-b\local\cosmos-guard\guard_classifier\modernbert-tr-guardrail__default.jsonl
Log: results\logs\phase7-<time>.log
#>
param([switch]$Yes)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$logDir = Join-Path $Root "results\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("phase7-" + (Get-Date -Format "yyyyMMdd-HHmm") + ".log")
$Y = @()
if ($Yes) { $Y = @("--yes") }

function Note([string]$m) {
    Write-Host $m -ForegroundColor Cyan
    $m | Out-File -FilePath $log -Append -Encoding utf8
}

function Tee-Log {
    process {
        $s = "$_"
        Write-Host $s
        $s | Out-File -FilePath $log -Append -Encoding utf8
    }
}

$mode = "DRY RUN (nothing is loaded or run)"
if ($Yes) { $mode = "EXECUTING (the model runs on this PC)" }
Note "trbench phase 7 (COSMOS Turkish guardrail, local) |  $mode  |  log: $log"

$a = @("run", "--suite", "guard", "--models", "modernbert-tr-guardrail", "--session", "cosmos-guard",
       "--concurrency", "1") + $Y
$line = ">>> python harness/run.py " + ($a -join " ")
Write-Host $line -ForegroundColor DarkGray
$line | Out-File -FilePath $log -Append -Encoding utf8
& $py harness/run.py @a 2>&1 | Tee-Log
$code = $LASTEXITCODE

if ($code -ne 0) {
    Note "`n!! the run ended with exit code $code - send the log to Claude: $log"
} elseif ($Yes) {
    Note "`nTell Claude 'phase 7 finished' - the log is $log"
} else {
    Note "`nDry run finished. If the plan looks right, run the same line with -Yes."
}
