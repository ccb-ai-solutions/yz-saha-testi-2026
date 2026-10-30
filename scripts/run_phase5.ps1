<#
Phase 5 - the judge (RUN_PLAN.md, "Phase 5").  Claude Opus 5.5 grades repeat 0 of every RAG answer through Anthropic's
Message Batches API (half price), Claude Sonnet 5.5 grades the same deterministic 4 % for calibration, and Opus judges
the 4 tone checks of IFEval.  Log: results\logs\phase5-<time>.log
Without -Yes every step is a DRY RUN: it prints the answers, tokens, cost and the SPEND CAP of each step, sends nothing.

  powershell -ExecutionPolicy Bypass -File scripts\run_phase5.ps1                 # dry run of every step
  powershell -ExecutionPolicy Bypass -File scripts\run_phase5.ps1 -Yes            # executes, one step after the other
  powershell -ExecutionPolicy Bypass -File scripts\run_phase5.ps1 -Yes -NoWait    # submits without waiting; run the
                                                                                   # same command again to go on

Spend cap: every judge step stops itself before it spends more than 1.5 x its own estimate (configs/judge.yaml,
spend_cap_factor).  A step of 60+ answers first judges a pilot batch of 5 % (20-100 answers) and prices the rest on it;
the rest is sent only if the projected total fits under the cap.  If a step stops on its cap, this script runs NO further
judge step (each would pay for a pilot first) and repeats at the end what the step said: what it spent, what the rest
would cost, and the exact command that continues it with a higher --max-cost.

Everything is written down, in each session's rag_qa\judge and ifeval\judge folder:
  <stem>.jsonl          every verdict, the moment it arrives (with its token usage)
  <stem>.batches.json   every batch id, the moment it is submitted (an open batch is collected later, never re-sent)
  spend\<stem>.jsonl    the spend record: start (estimate, cap), each submission, the pilot's price per verdict, a cap
                        stop with its numbers and the command to continue, and the end (status, spent, answers left)
plus this log.  An interrupted run resumes with the same command: saved verdicts are kept, nothing is sent twice.

Run it after phase 4's rerun has finished (unfinished answers are not judged).  Afterwards, offline:
  python harness/run.py human-export --session <session> --judge-model judge-anthropic     # blind 10 % sheets
  python analysis/analyze.py                                                               # report
#>
param([switch]$Yes, [switch]$SkipTests, [switch]$NoWait,
      [string]$Sessions = "evren-a,evren-glm,ovh-a,bedrock-a,nvidia-a")

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
$log = Join-Path $logDir "phase5-$stamp.log"
$Y = @()
if ($Yes) { $Y = @("--yes") }
$NW = @()
if ($NoWait) { $NW = @("--no-wait") }
$mode = "DRY RUN (nothing is sent)"
if ($Yes) { $mode = "EXECUTING (requests are sent)" }
$failed = New-Object System.Collections.Generic.List[string]
$script:capHit = $false
$script:capLines = New-Object System.Collections.Generic.List[string]

function Note([string]$m) {
    Write-Host $m -ForegroundColor Cyan
    $m | Out-File -FilePath $log -Append -Encoding utf8
}

function Tee-Log {
    process {
        $line = "$_"
        # only the harness's own line counts (a cap stop prints exactly one, with the numbers and the next command)
        if ($line -match "^!! \[judge\] spend cap") {
            $script:capHit = $true
            $script:capLines.Add($line)
        }
        Write-Host $line
        $line | Out-File -FilePath $log -Append -Encoding utf8
    }
}

function Run([string[]]$a) {
    if ($script:capHit) {
        Note "-- skipped (a judge step stopped on its spend cap): python harness/run.py $($a -join ' ')"
        return
    }
    $cmd = ">>> python harness/run.py " + ($a -join " ")
    Write-Host $cmd -ForegroundColor DarkGray
    $cmd | Out-File -FilePath $log -Append -Encoding utf8
    & $py harness/run.py @a 2>&1 | Tee-Log
    if ($LASTEXITCODE -ne 0) {
        $msg = "exit code $LASTEXITCODE : " + ($a -join " ")
        $failed.Add($msg)
        if ($script:capHit) {
            Note "!! this step stopped on its spend cap - the remaining judge steps are skipped (summary at the end)"
        } else {
            Note "!! step failed ($msg) - continuing with the next step"
        }
    }
}

Note "trbench phase 5 (judge) |  $mode  |  log: $log"

# ------------------------------------------------------------------ offline tests
if (-not $SkipTests) {
    Note "`n==== offline tests (local mock gateway, no network)"
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

# ------------------------------------------------------------------ the judge, session by session
# NB: PowerShell variable names ignore case - never reuse a name in another case
$sessionList = $Sessions.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
foreach ($sn in $sessionList) {
    Note "`n==== session $sn"
    $J = @("judge", "--session", $sn)
    Note "-- Opus 5.5: repeat 0 of every RAG answer"
    Run ($J + @("--suite", "rag_qa", "--judge-repeats", "0") + $NW + $Y)
    Note "-- Sonnet 5.5: the same 4 % of the answers (calibration)"
    Run ($J + @("--suite", "rag_qa", "--judge-repeats", "0", "--sample", "0.04", "--judge-model",
                "judge-anthropic-sonnet") + $NW + $Y)
    Note "-- Opus 5.5: the 4 tone checks of IFEval"
    Run ($J + @("--suite", "ifeval") + $NW + $Y)
}

# ------------------------------------------------------------------ summary
if ($script:capHit) {
    Note "`nSTOPPED ON THE SPEND CAP - no further judge step was run.  What the step said:"
    foreach ($c in $script:capLines) { Note "  $c" }
    Note "Nothing beyond its pilot was sent for that step and every verdict so far is saved; the numbers are in the step's"
    Note "spend record (named above).  Decide first: run the 'To accept up to ...' command above to continue that"
    Note "step, then this script again for the remaining steps (finished ones send nothing) - or ask Claude."
}
if ($failed.Count -gt 0) {
    Note "`n$($failed.Count) step(s) did not finish cleanly:"
    foreach ($f in $failed) { Note "  - $f" }
} elseif (-not $script:capHit) {
    Note "`nDone."
}
if ($Yes) {
    if ($NoWait) {
        Note "Batches were submitted without waiting: run the same command again later to collect them (after a pilot it"
        Note "then prices and sends the rest).  Nothing is ever sent twice."
    }
    Note "Tell Claude 'phase 5 finished' - the log is $log"
} else {
    Note "`nDry run finished. If the tables and the spend caps look right, run the same command with -Yes."
}
