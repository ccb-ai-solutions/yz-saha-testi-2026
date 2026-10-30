<#
Phase 3f - GLM-5.3 without thinking, through EVREN's documented "thinking": false (RUN_PLAN.md, "Phase 3f").  Until
now GLM-5.3 had no nothink mode: chat_template_kwargs.enable_thinking=false wrote the reasoning into the answer, so the
probe refused it.  On 15 Oct EVREN documented a request parameter that turns thinking off for every model; this phase
probes it and, if it works, adds glm-5.3:nothink to the RAG table like every other nothink system.  It stays out of
the latency sessions (configs/suites.yaml, latency.exclude_variants).  Four steps, one after the other.  Without -Yes
every step is a DRY RUN: it prints the systems, requests, tokens and cost and sends nothing.

  powershell -ExecutionPolicy Bypass -File scripts\run_phase3f.ps1 -Step probe   # 1. GLM-5.3's switches again (~20-30 small requests)
  powershell -ExecutionPolicy Bypass -File scripts\run_phase3f.ps1 -Step smoke   # 2. 5 RAG questions in nothink (session smoke-glm-nothink)
  powershell -ExecutionPolicy Bypass -File scripts\run_phase3f.ps1 -Step rag     # 3. the 100 RAG questions x 3 repeats into evren-glm
  powershell -ExecutionPolicy Bypass -File scripts\run_phase3f.ps1 -Step judge   # 4. the judge on its answers (run_phase5.ps1 -Sessions evren-glm)

After steps 1 and 2 tell Claude: the probe must accept the switch ("nothink via {'thinking': False}" in the log) and
the smoke answers are checked before step 3.  Until the probe has accepted it, the dry runs of steps 2 and 3 list
nothing to send (the last probe found no nothink switch for GLM-5.3).  EVREN is free until 1 Nov; the judge is
Anthropic's batch API (about $1.4).  While a latency session runs on this PC, steps 2-4 pause by themselves (latency
lock); the probe does not, so it refuses to start then.  An interrupted step resumes with the same command.
Log: results\logs\phase3f-<step>-<time>.log
#>
param([Parameter(Mandatory = $true)][ValidateSet("probe", "smoke", "rag", "judge")][string]$Step,
      [switch]$Yes)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Model = "glm-5.3"

if ($Step -eq "judge") {
    # phase 5's judge for session evren-glm: verdicts that exist are cached, so only the new nothink answers are sent
    & (Join-Path $PSScriptRoot "run_phase5.ps1") -Sessions "evren-glm" -SkipTests -Yes:$Yes
    exit $LASTEXITCODE
}

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$logDir = Join-Path $Root "results\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("phase3f-" + $Step + "-" + (Get-Date -Format "yyyyMMdd-HHmm") + ".log")
$Y = @()
if ($Yes) { $Y = @("--yes") }
$failed = New-Object System.Collections.Generic.List[string]

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

function LatencySessionRunning {
    # the latency lock is live while its heartbeat is under a minute old (RUN_PLAN.md, "Is a session running?")
    $lp = Join-Path $Root "results\.latency.lock"
    if (-not (Test-Path $lp)) { return $null }
    try { $l = Get-Content -Path $lp -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$l.heartbeat_epoch
    if ($age -lt 60) { return [string]$l.session }
    return $null
}

$mode = "DRY RUN (nothing is sent)"
if ($Yes) { $mode = "EXECUTING (requests are sent)" }
Note "trbench phase 3f (GLM-5.3 nothink through thinking:false) - step '$Step'  |  $mode  |  log: $log"

if ($Step -eq "probe") {
    $running = LatencySessionRunning
    if ($Yes -and $running) {
        Note "!! latency session $running is measuring right now - the probe is not sent (it would load this PC during"
        Note "   the measurement). Run the same line again once it has finished (scripts\schedule_windows.ps1 -List)."
        exit 3
    }
    Run (@("probe", "--models", $Model, "--reprobe") + $Y)
}
if ($Step -eq "smoke") {
    Run (@("run", "--suite", "rag_qa", "--models", $Model, "--modes", "nothink", "--limit", "5", "--repeats", "1",
           "--session", "smoke-glm-nothink") + $Y)
}
if ($Step -eq "rag") {
    Run (@("run", "--suite", "rag_qa", "--models", $Model, "--modes", "nothink", "--session", "evren-glm") + $Y)
}

if ($failed.Count -gt 0) {
    Note "`n$($failed.Count) command(s) failed:"
    foreach ($f in $failed) { Note "  - $f" }
}
if ($Yes) {
    Note "`nTell Claude 'phase 3f $Step finished' - the log is $log"
} else {
    Note "`nDry run finished. If the tables look right, run the same line with -Yes."
}
