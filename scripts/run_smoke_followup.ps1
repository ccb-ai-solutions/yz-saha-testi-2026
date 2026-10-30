<#
Follow-up to the smoke test of 10 Oct (RUN_PLAN.md, "Smoke follow-up"): the two steps that failed, the probe
re-check of the thinking-budget switch, and a short smoke of the reasoning modes.  Log: results\logs\followup-<time>.log

  powershell -ExecutionPolicy Bypass -File scripts\run_smoke_followup.ps1          # DRY RUN: prints every table, sends nothing
  powershell -ExecutionPolicy Bypass -File scripts\run_smoke_followup.ps1 -Yes     # executes it

What it does, in order (a step that fails is listed at the end and the script goes on; if the offline tests fail,
nothing is sent):
  1. offline tests (-SkipTests skips them)
  2. builds the 22 ASR clips from the FLEURS download that is already cached (no new download, no tokens)
  3. re-reads NVIDIA's model list and EVREN's quota (GET only, no tokens) - both hit a DNS error on 10 Oct
  4. probes again the 12 models that think by default and have a budget switch to test: the think_min test now
     uses a harder prompt and the reasoning_effort values EVREN publishes (EVREN free, NVIDIA free, OVHcloud cents)
  5. writes the budget with the new probe results (offline)
  6. the ASR smoke (4 requests) and the judge on the 40 EVREN smoke answers (one Anthropic batch, ~$0.5)
  7. a 5-question RAG smoke of the nothink / think_min modes (EVREN, OVHcloud, NVIDIA) and of Bedrock's default
     mode, with the new 8,192-token answer budget, in new sessions smoke-modes-* - not judged
  8. the smoke report (offline)
#>
param([switch]$Yes, [switch]$SkipTests)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating .venv and installing requirements.txt (one time)..." -ForegroundColor Cyan
    python -m venv .venv
    & $py -m pip install --upgrade pip
    & $py -m pip install -r requirements.txt
}

$logDir = Join-Path $Root "results\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$log = Join-Path $logDir "followup-$stamp.log"
$Y = @()
if ($Yes) { $Y = @("--yes") }
$mode = "DRY RUN (nothing is sent)"
if ($Yes) { $mode = "EXECUTING (requests are sent)" }
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

# only check that a value is present (never print it): keys in .env or in the environment
function Has-Key([string]$name) {
    if ([bool](Get-Item -Path "env:$name" -ErrorAction SilentlyContinue).Value) { return $true }
    if (-not (Test-Path ".env")) { return $false }
    $pattern = '^\s*(export\s+)?' + $name + '\s*=\s*["'']?[^\s#"'']'
    return [bool](Select-String -Path ".env" -Pattern $pattern -Quiet)
}
$hasNvidia = Has-Key "NVIDIA_API_KEY"

Note "trbench smoke follow-up  |  $mode  |  log: $log"

# ------------------------------------------------------------------ 1. offline tests
if (-not $SkipTests) {
    Note "`n==== 1. offline tests (local mock gateway, no network)"
    & $py -m pytest tests -q -p no:cacheprovider 2>&1 | Tee-Log
    if ($LASTEXITCODE -ne 0) {
        Note "!! the offline tests failed (exit code $LASTEXITCODE) - see the log."
        if ($Yes) {
            Note "Nothing was sent. Send the log to Claude before running with -Yes again."
            exit 1
        }
        $failed.Add("offline tests")
    }
}

# ------------------------------------------------------------------ 2. ASR clips (cached download, offline)
Note "`n==== 2. ASR clips from the cached FLEURS download (offline)"
if (Test-Path "data\asr\manifest.jsonl") {
    Note "data\asr\manifest.jsonl exists - nothing to build"
} elseif (-not (Test-Path "data\asr\_fleurs_cache\dev.tar.gz")) {
    Note "the FLEURS download is not cached: build_asr_clips.py would download it again (~200 MB, no tokens)"
    if ($Yes) {
        & $py scripts/build_asr_clips.py 2>&1 | Tee-Log
        if ($LASTEXITCODE -ne 0) { $failed.Add("build_asr_clips.py (the ASR smoke step will fail)") }
    }
} elseif ($Yes) {
    & $py scripts/build_asr_clips.py 2>&1 | Tee-Log
    if ($LASTEXITCODE -ne 0) { $failed.Add("build_asr_clips.py (the ASR smoke step will fail)") }
} else {
    Note "would build: 20 clips of ~29 s + 2 of ~60 s from data\asr\_fleurs_cache (scripts/build_asr_clips.py)"
}

# ------------------------------------------------------------------ 3. GET-only reads that failed on 10 Oct
Note "`n==== 3. catalogue and quota (GET, no tokens)"
if ($hasNvidia) { Run (@("ping", "--endpoint", "nvidia") + $Y) }
Run (@("quota", "--endpoint", "evren") + $Y)

# ------------------------------------------------------------------ 4. probe again: the thinking-budget test
Note "`n==== 4. probe again (think_min: harder prompt + EVREN's published reasoning_effort values)"
Run (@("probe", "--models", "deepseek-v4-flash,deepseek-v4.1-flash,glm-5.3,mimo-v2.6-pro,qwen3.8-flash-next", "--reprobe") + $Y)
Run (@("probe", "--models", "qwen3.8-27b,qwen3.5-9b,qwen3.5-397b,gpt-oss-120b,gpt-oss-20b", "--reprobe") + $Y)
if ($hasNvidia) {
    Run (@("probe", "--models", "muse-glimmer-30b,nemotron-3.5-lightning", "--reprobe") + $Y)
}

# ------------------------------------------------------------------ 5. budget with the new probe results
Note "`n==== 5. budget with the new probe results (offline)"
Run @("budget", "--campaign", "configs/campaign.yaml", "--md", "results/logs/budget-after-reprobe-$stamp.md")

# ------------------------------------------------------------------ 6. the two failed smoke steps
Note "`n==== 6. ASR smoke and the judge on the 40 EVREN smoke answers"
Run (@("run", "--suite", "asr", "--models", "qwen3-asr-1.7b,whisper-large-v3-ovh", "--limit", "2", "--session", "smoke") + $Y)
# only the default-mode answers of 10 Oct (the mode smoke of step 7 is not judged)
$judged = "gemma-4-31b:default,qwen3-vl-30b:default,qwen3.8-flash-next:default,deepseek-v4-flash:default," +
          "deepseek-v4.1-flash:default,glm-5.3:default,mimo-v2.6-pro:default,auto:default"
Run (@("judge", "--session", "smoke", "--suite", "rag_qa", "--variants", $judged, "--poll-seconds", "20") + $Y)

# ------------------------------------------------------------------ 7. reasoning modes, end to end
# New sessions: the answer budget is now 8,192 tokens (11 Oct) and it is part of every plan item, so the smoke
# sessions of 10 Oct (4,096) refuse the new plan.  Bedrock's default mode checks that it accepts the larger budget.
Note "`n==== 7. RAG smoke of the reasoning modes (5 questions; a model without the mode is skipped with the reason)"
Run (@("run", "--suite", "rag_qa", "--endpoint", "evren", "--limit", "5", "--repeats", "1", "--modes", "nothink,think_min", "--session", "smoke-modes") + $Y)
Run (@("run", "--suite", "rag_qa", "--endpoint", "ovh", "--limit", "5", "--repeats", "1", "--modes", "nothink,think_min", "--session", "smoke-modes-ovh") + $Y)
Run (@("run", "--suite", "rag_qa", "--endpoint", "bedrock", "--limit", "5", "--repeats", "1", "--modes", "default", "--session", "smoke-modes-bedrock") + $Y)
if ($hasNvidia) {
    Run (@("run", "--suite", "rag_qa", "--endpoint", "nvidia", "--limit", "5", "--repeats", "1", "--modes", "nothink,think_min", "--session", "smoke-modes-nvidia") + $Y)
}

# ------------------------------------------------------------------ 8. report
if ($Yes) {
    Note "`n==== 8. smoke report (offline)"
    & $py analysis/analyze.py --include-limited --out "report/smoke-$stamp" 2>&1 | Tee-Log
    if ($LASTEXITCODE -ne 0) { $failed.Add("analysis/analyze.py (smoke report)") }
    if ($failed.Count -gt 0) {
        Note "`nFinished with $($failed.Count) failed step(s):"
        foreach ($f in $failed) { Note "  - $f" }
    } else {
        Note "`nDone."
    }
    Note "Tell Claude 'follow-up finished' - the log is $log"
} else {
    if ($failed.Count -gt 0) {
        Note "`n$($failed.Count) dry-run step(s) reported an error:"
        foreach ($f in $failed) { Note "  - $f" }
    }
    Note "`nDry run finished. The report is written only with -Yes."
    Note "If the tables look right:  powershell -ExecutionPolicy Bypass -File scripts\run_smoke_followup.ps1 -Yes"
}
