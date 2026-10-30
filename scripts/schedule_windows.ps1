# Register the EVREN latency sessions (time slots in TSİ) as Windows scheduled tasks.
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -Next 3            # the next 3 slots: plan + token table only
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -Next 3 -Yes       # registers them (a slot already registered is replaced)
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -StartDate 2026-10-19 [-Yes]   # 3 slots x 2 days from a date
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -List              # registered slots: session, state, next / last run
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -Remove            # unregister all of them
# -Next N takes the next N slots of -Times (10:00 / 15:00 / 21:00) on weekdays, from 15 minutes after now, skipping
# -SkipDates (default: the Republic Day holiday, 28-29 Oct); a slot that has already passed is never registered.
# Each session sends requests on its own (it runs with --yes) — registering with -Yes is your approval of all of them.
#
# Fallbacks (15 Oct; the 14 Oct 15:00 and 21:00 sessions were ended from outside — last result 3221225786 =
# 0xC000013A — and lost the rest of their window):
#   * each slot has a fixed session id (YYYYMMDD-HHMM-<morning|afternoon|evening>-slot) and a fixed end — the slot's
#     window as the harness computes it (10:00 -> 14:40, 15:00 -> 17:50, 21:00 -> 22:50) — passed to
#     run_latency_session.ps1 as -Session / -Until;
#   * the task starts again every -RepeatMinutes (10) until that end; while it runs, a repetition is ignored.  So a
#     session ended from outside (battery, Task Scheduler, a crash, a restart) carries on where it stopped within 10
#     minutes — finished requests are not sent again.  Once the session is finished the task disables itself;
#   * the task starts and keeps running on battery (Windows' default would stop it when the PC switches to battery);
#     -AcOnly: AC power only, as before;
#   * a start missed while the PC was off runs as soon as possible, inside the window (StartWhenAvailable);
#   * hidden window, WakeToRun, normal priority (Task Scheduler's default, 7, is below normal).
# To stop a slot for good: Disable-ScheduledTask -TaskName <name>, then Stop-ScheduledTask -TaskName <name>.
# Avoid 02:30-03:30 TSİ: the EVREN daily quota resets at 03:00.
param(
    [string]$StartDate = (Get-Date).AddDays(1).ToString("yyyy-MM-dd"),
    [string[]]$Times = @("10:00", "15:00", "21:00"),
    [int]$Days = 2,
    [int]$Next = 0,
    [string[]]$SkipDates = @("2026-10-28", "2026-10-29"),
    [int]$MinLeadMinutes = 15,
    [int]$RepeatMinutes = 10,
    [string]$Endpoint = "evren",
    [switch]$Yes,
    [switch]$AcOnly,
    [switch]$AllowBattery,          # accepted for old command lines: running on battery is the default now
    [switch]$List,
    [switch]$Remove
)
$DryRun = -not $Yes
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "scripts\run_latency_session.ps1"
$Prefix = "trbench-latency"
$py = Join-Path $Root ".venv\Scripts\python.exe"; if (-not (Test-Path $py)) { $py = "python" }
# "-Times 10:00,15:00" arrives as ONE string under -File: split on commas
$Times = @($Times | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$SkipDates = @($SkipDates | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

function SessionState([string]$sid) {
    # what the session's folder says (offline): complete / partial (why) / started / not started
    $m = Get-Item -Path (Join-Path $Root "results\track-*\$Endpoint\$sid\manifest.json") -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $m) { return "not started" }
    try { $s = (Get-Content -Path $m.FullName -Raw -Encoding UTF8 | ConvertFrom-Json).suites.latency.summary } catch { $s = $null }
    # a model that used up a single-stream time budget is left out of the session's later stages (16 Oct)
    $slow = @(if ($s -and $s.slow_models) { $s.slow_models.PSObject.Properties | ForEach-Object { "$($_.Name) in $($_.Value)" } })
    $note = if ($slow.Count) { "; time budget used up: " + ($slow -join ", ") } else { "" }
    if ($s -and $s.completed) { return "complete$note" }
    if ($s -and $s.end_reason) { return "partial ($($s.end_reason))$note" }
    return "started (no summary yet)"
}

if ($Remove) {
    Get-ScheduledTask | Where-Object { $_.TaskName -like "$Prefix*" } | ForEach-Object {
        Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
        Write-Host "removed $($_.TaskName)"
    }
    exit 0
}
if ($List) {
    $tasks = @(Get-ScheduledTask | Where-Object { $_.TaskName -like "$Prefix*" } | Sort-Object TaskName)
    if (-not $tasks) { Write-Host "no $Prefix tasks registered" }
    foreach ($t in $tasks) {
        $i = Get-ScheduledTaskInfo -TaskName $t.TaskName
        $last = if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { "$($i.LastRunTime) (result $($i.LastTaskResult))" } else { "never" }
        $a = ($t.Actions | Select-Object -First 1).Arguments
        $sess = if ($a -match "-Session\s+(\S+)") { "session $($Matches[1]): $(SessionState $Matches[1])" } else { "old registration without a fixed session — register this slot again" }
        Write-Host ("{0}  state {1}  next {2}  last {3}`n    {4}" -f $t.TaskName, $t.State, $i.NextRunTime, $last, $sess)
    }
    exit 0
}

$inv = [System.Globalization.CultureInfo]::InvariantCulture
$tz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Turkey Standard Time")
$nowTsi = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $tz)
$slots = @()
if ($Next -gt 0) {
    $day = $nowTsi.Date
    $guard = 0
    while ($slots.Count -lt $Next -and $guard -lt 60) {
        $guard++
        $weekend = ($day.DayOfWeek -eq [DayOfWeek]::Saturday) -or ($day.DayOfWeek -eq [DayOfWeek]::Sunday)
        if (-not $weekend -and ($SkipDates -notcontains $day.ToString("yyyy-MM-dd", $inv))) {
            foreach ($t in ($Times | Sort-Object)) {
                $s = [datetime]::ParseExact("$($day.ToString('yyyy-MM-dd', $inv)) $t", "yyyy-MM-dd HH:mm", $inv)
                if ($s -gt $nowTsi.AddMinutes($MinLeadMinutes) -and $slots.Count -lt $Next) { $slots += $s }
            }
        }
        $day = $day.AddDays(1)
    }
} else {
    for ($d = 0; $d -lt $Days; $d++) {
        foreach ($t in $Times) {
            $s = [datetime]::ParseExact("$StartDate $t", "yyyy-MM-dd HH:mm", $inv).AddDays($d)
            if ($s -le $nowTsi.AddMinutes(2)) {
                Write-Host "skipped TSİ $($s.ToString('yyyy-MM-dd HH:mm', $inv)): already past (a past slot would never run)"
                continue
            }
            $slots += $s
        }
    }
}
if (-not $slots) { Write-Host "no slot to register"; exit 1 }

Write-Host "Per-session estimate (offline, nothing sent):"
& $py (Join-Path $Root "harness\run.py") run --suite latency --endpoint $Endpoint
# each slot's session id and window end, from the harness itself (offline): the same rules the session applies
$atArgs = @()
foreach ($s in $slots) { $atArgs += @("--at", $s.ToString("yyyy-MM-dd HH:mm", $inv)) }
$windows = @(& $py (Join-Path $Root "harness\run.py") latency-window @atArgs | Where-Object { $_ -like "{*" } |
    ForEach-Object { $_ | ConvertFrom-Json })
if ($windows.Count -ne $slots.Count) { Write-Host "could not compute the slots' windows (harness\run.py latency-window)"; exit 1 }
$power = if ($AcOnly) { "AC power only" } else { "runs on battery too" }

for ($k = 0; $k -lt $slots.Count; $k++) {
    $localTsi = $slots[$k]
    $win = $windows[$k]
    $minutes = [int]$win.minutes
    # convert TSİ wall-clock to this machine's local time (in case the PC is not set to Istanbul time)
    $utc = [System.TimeZoneInfo]::ConvertTimeToUtc($localTsi, $tz)
    $when = $utc.ToLocalTime()
    $name = "$Prefix-$($localTsi.ToString('yyyyMMdd-HHmm', $inv))"
    $label = "$($localTsi.ToString('ddd yyyy-MM-dd HH:mm', $inv)) TSİ until $($win.end_tsi.Substring(11, 5))"
    $argLine = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Script`" -Endpoint $Endpoint " +
               "-Session $($win.session) -Until $($win.end_tsi) -TaskName $name"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argLine -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Once -At $when -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) `
        -RepetitionDuration (New-TimeSpan -Minutes $minutes)
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew -Priority 5 `
        -ExecutionTimeLimit (New-TimeSpan -Minutes ($minutes + 30)) -DontStopOnIdleEnd `
        -AllowStartIfOnBatteries:(-not $AcOnly) -DontStopIfGoingOnBatteries:(-not $AcOnly)
    if ($DryRun) {
        Write-Host "[dry-run] $name  $label  session $($win.session)  (this PC: $when; restarts every $RepeatMinutes min until the end; $power)"
    } else {
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Write-Host "registered $name  $label  session $($win.session)  (this PC: $when; restarts every $RepeatMinutes min until the end; $power)"
    }
}
if ($DryRun) { Write-Host "Nothing registered. Re-run with -Yes to register these $($slots.Count) sessions." }
Write-Host "Logs: $Root\results\logs\latency-<session>.log   Check: scripts\schedule_windows.ps1 -List   Remove all: -Remove"
