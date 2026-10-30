#!/usr/bin/env bash
# One latency session + log (Linux / macOS; on Windows: run_latency_session.ps1 via schedule_windows.ps1).
#   run_latency_session.sh [ENDPOINT] [SLOT]     SLOT = the slot's start, HH:MM TSİ (e.g. 10:00); none = started by hand
# With SLOT, the session id and its end are fixed by the slot (YYYYMMDD-HHMM-<slot>-slot, the slot's window), so every
# start inside the window carries on the same session — nothing finished is sent again.  Cron example (machine clock
# in UTC, TSİ = UTC+3): start the 10:00 slot every 10 minutes until its end; a start while it runs exits at once
# (the session's lock), a start after the window or after it finished sends nothing:
#   */10 7-11 * * 1-5  /path/to/model-comparison-turkish/scripts/run_latency_session.sh evren 10:00
# If the Python process stops with an error, it is started again after 60 s (exit 0 = finished, 3 = stopped on
# purpose or running elsewhere: no restart).  It sends requests (--yes): installing the cron entry is the approval.
# Dry run first: harness/run.py run --suite latency --endpoint evren
set -u
ENDPOINT="${1:-evren}"
SLOT="${2:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
mkdir -p results/logs
if [ -n "$SLOT" ]; then AT="$(TZ=Europe/Istanbul date +%F) $SLOT"; else AT="now"; fi
WIN="$("$PY" harness/run.py latency-window --at "$AT" | tail -n 1)"
SESSION="$(printf '%s' "$WIN" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["session"])')"
UNTIL="$(printf '%s' "$WIN" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("end_tsi") or "")')"
LOG="results/logs/latency-$SESSION.log"
ARGS=(harness/run.py run --suite latency --endpoint "$ENDPOINT" --session "$SESSION" --yes)
[ -n "$UNTIL" ] && ARGS+=(--until "$UNTIL")
for n in $(seq 1 30); do
  echo "== $(date -Is) start $n of session $SESSION${UNTIL:+ (window until $UNTIL)}" >> "$LOG"
  "$PY" "${ARGS[@]}" >> "$LOG" 2>&1
  code=$?
  echo "== $(date -Is) exit $code" >> "$LOG"
  { [ "$code" -eq 0 ] || [ "$code" -eq 3 ]; } && break
  sleep 60
done
exit "$code"
