# One latency session (all EVREN chat models, configured modes) + UTF-8 log.  Called by the scheduled tasks
# (schedule_windows.ps1); can also be started by hand.  It sends requests (--yes): registering the scheduled tasks with
# schedule_windows.ps1 -Yes is the approval.
#
# Fallbacks (15 Oct — the 14 Oct 15:00 and 21:00 sessions were ended from outside and lost their remaining stages):
#   * the session has a fixed id (-Session; started by hand: a new one) and a fixed end (-Until; by hand: the end of
#     the window it starts in), so every start of it carries on the same measurement inside the same window;
#   * if the Python process ends with an error, it is started again after -RetrySeconds and carries on where it
#     stopped (requests that already have a measurement are not sent again) — until it reports the session finished
#     (exit 0: complete, or its window is over) or stopped on purpose (exit 3: Ctrl+C, its --max-minutes limit, or it
#     runs in another process); exit 4 (requests given up after repeated network outages) is started again too;
#   * if this script itself is ended from outside (Task Scheduler, battery, logoff), the task's next 10-minute
#     repetition starts it again with the same session id;
#   * a network outage is handled inside the session (it waits and sends the requests hit by it again);
#   * once the session is finished, its scheduled task (-TaskName) is disabled: later repetitions would only find it
#     finished, and would wake the PC for nothing.
# Log: results\logs\latency-<session>.log (every start of the session appends to it).
param(
    [string]$Endpoint = "evren",
    [string]$Session = "",
    [string]$Until = "",
    [string]$TaskName = "",
    [string]$Extra = "",
    [int]$RetrySeconds = 60,
    [int]$MaxStarts = 30
)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8     # keep Turkish characters intact in the log
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$logDir = Join-Path $Root "results\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
if (-not $Session) {
    # started by hand: a new session id and the end of the window it starts in (offline, nothing is sent)
    $w = $null
    try { $w = (& $py harness/run.py latency-window --at now 2>$null | Select-Object -Last 1) | ConvertFrom-Json } catch { $w = $null }
    if ($w -and $w.session) {
        $Session = $w.session
        if (-not $Until -and $w.end_tsi) { $Until = $w.end_tsi }
    } else {
        $Session = (Get-Date -Format "yyyyMMdd-HHmm") + "-manual-" + ("{0:x4}" -f (Get-Random -Maximum 65536))
    }
}
$log = Join-Path $logDir ("latency-" + $Session + ".log")
function Log([string]$m) { $m | Out-File -FilePath $log -Append -Encoding utf8 }
$cmd = @("harness/run.py", "run", "--suite", "latency", "--endpoint", $Endpoint, "--session", $Session, "--yes")
if ($Until) { $cmd += @("--until", $Until) }
if ($Extra) { $cmd += $Extra.Split(" ") }
$code = 1
for ($n = 1; $n -le $MaxStarts; $n++) {
    Log "== $(Get-Date -Format o) start $n of session $Session$(if ($Until) { " (window until $Until)" })"
    & $py @cmd 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
    $code = $LASTEXITCODE
    Log "== $(Get-Date -Format o) exit $code"
    if ($code -eq 0 -or $code -eq 3) { break }       # 0 = finished, 3 = stopped on purpose: do not start it again now
    Start-Sleep -Seconds $RetrySeconds
}
if ($code -eq 0 -and $TaskName) {
    try {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        Log "== $(Get-Date -Format o) session finished: task $TaskName disabled (no more starts of this slot)"
    } catch {
        Log "== $(Get-Date -Format o) session finished; task $TaskName could not be disabled ($($_.Exception.Message)) — its next starts only find it finished"
    }
}
exit $code
