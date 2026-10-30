<#
Phase 3e - OCR again after the change to EVREN's /v1/ocr on 16 Oct at 00:43 (RUN_PLAN.md, "Phase 3e").  The
analysis leaves out everything read from /v1/ocr before the change (configs/service_changes.yaml) - this script
measures it again, into new sessions.  Without
-Yes every step is a DRY RUN: it prints the systems, requests and tokens and sends nothing.

  powershell -ExecutionPolicy Bypass -File scripts\run_phase3e.ps1 -Step ocr          # 1. dots-ocr + deepseek-ocr-2, the 30 pages
  powershell -ExecutionPolicy Bypass -File scripts\run_phase3e.ps1 -Step extract      # 2. invoice extraction over both outputs (EVREN chat models)
  powershell -ExecutionPolicy Bypass -File scripts\run_phase3e.ps1 -Step extract-glm  # 2b. the same for GLM-5.3 (slow: its own terminal)

Step 1 first, alone and to its end: steps 2 and 2b build their plans from its output, so they refuse to start (and
their dry runs print no numbers) until step 1 has read every page with both models (scripts\check_ocr_session.py).
Then 2 and 2b can run at the same time in two terminals.  Run a step's line, read the tables, then the same line
with -Yes.
Nothing here is judged by the LLM judge (OCR and extraction are scored by code); EVREN is free until 1 Nov.
While a latency session runs on this PC, the steps pause by themselves (latency lock) and carry on afterwards.  An
interrupted step resumes with the same command: finished requests are never sent twice.
Log: results\logs\phase3e-<step>-<time>.log
#>
param([Parameter(Mandatory = $true)][ValidateSet("ocr", "extract", "extract-glm")][string]$Step,
      [switch]$Yes)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$logDir = Join-Path $Root "results\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("phase3e-" + $Step + "-" + (Get-Date -Format "yyyyMMdd-HHmm") + ".log")
$Y = @()
if ($Yes) { $Y = @("--yes") }
$failed = New-Object System.Collections.Generic.List[string]
$OcrSession = "evren-ocr"
$OcrModels = @("dots-ocr", "deepseek-ocr-2")

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

function Run([string[]]$a) {
    $line = ">>> python harness/run.py " + ($a -join " ")
    Write-Host $line -ForegroundColor DarkGray
    $line | Out-File -FilePath $log -Append -Encoding utf8
    & $py harness/run.py @a 2>&1 | Tee-Log
    if ($LASTEXITCODE -ne 0) {
        $msg = "exit code $LASTEXITCODE : " + ($a -join " ")
        $failed.Add($msg)
        Note "!! step failed ($msg) - continuing with the next step"
    }
}

$mode = "DRY RUN (nothing is sent)"
if ($Yes) { $mode = "EXECUTING (requests are sent)" }
Note "trbench phase 3e (OCR after the change to EVREN's /v1/ocr) - step '$Step'  |  $mode  |  log: $log"

if ($Step -eq "ocr") {
    Run (@("run", "--suite", "ocr", "--models", ($OcrModels -join ","), "--session", $OcrSession) + $Y)
}
if ($Step -eq "extract" -or $Step -eq "extract-glm") {
    # the extraction's plan is built from the OCR text: never from a step 1 that is still running or left pages out
    & $py scripts/check_ocr_session.py $OcrSession @OcrModels 2>&1 | Tee-Log
    if ($LASTEXITCODE -ne 0) {
        Note "!! Step 1 (-Step ocr) has not read every page yet: wait until it has finished; if it finished with pages"
        Note "   missing, tell Claude. Nothing was sent. (Once step 1 is complete this step sends 36 requests per chat"
        Note "   model and OCR model - the 12 scanned invoices x 3 repeats - about 0.5M tokens per OCR model.)"
        exit 3
    }
    foreach ($src in $OcrModels) {
        $Src = @("--suite", "docextract", "--doc-source", ("ocr:" + $src), "--ocr-session", $OcrSession)
        if ($Step -eq "extract") {
            Run (@("run") + $Src + @("--endpoint", "evren", "--exclude-models", "glm-5.3", "--session", $OcrSession) + $Y)
        } else {
            Run (@("run") + $Src + @("--models", "glm-5.3", "--session", ($OcrSession + "-glm")) + $Y)
        }
    }
}

if ($failed.Count -gt 0) {
    Note "`n$($failed.Count) command(s) failed:"
    foreach ($f in $failed) { Note "  - $f" }
}
if ($Yes) {
    Note "`nTell Claude 'phase 3e $Step finished' - the log is $log"
} else {
    Note "`nDry run finished. If the tables look right, run the same line with -Yes."
}
