"""Cross-process coordination: a latency session pauses quality runs started from other terminals.

A latency measurement must not share the account's TPM budget or the server queue with a quality run, and it
must not share this machine's CPU and network with one either (both move client-side timings).  The latency
runner writes results/.latency.lock with a heartbeat; quality runs check it before every request and wait while
a fresh lock covers them.  scope "machine" (default, configs/suites.yaml latency.lock_scope) pauses every
quality run on this machine; scope "endpoint" pauses only runs on the measured endpoints.  Liveness is decided by
the heartbeat age only (no PID checks — they are unreliable across Windows/Linux and containers).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from .config import RESULTS_DIR
from .store import replace_retry

LOCK_PATH = RESULTS_DIR / ".latency.lock"
HEARTBEAT_S = 15.0
STALE_S = 75.0


def read_lock(path: Path = LOCK_PATH) -> dict | None:
    d = None
    for _ in range(5):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            break
        except FileNotFoundError:
            return None
        except (PermissionError, ValueError):                # Windows: the owner is replacing the file right now
            time.sleep(0.05)
        except OSError:
            return None
    if d is None:
        return None
    if time.time() - float(d.get("heartbeat_epoch", 0)) > STALE_S:
        return None                                        # stale: the owner died or was killed
    return d


def power_status() -> dict | None:
    """Windows: on AC power or on battery (GetSystemPowerStatus) — a laptop on battery may throttle its CPU and
    network adapter, and Windows stops a scheduled task when the PC switches to battery.  None elsewhere."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        class _SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        st = _SPS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return None
        return {"ac_power": {0: False, 1: True}.get(st.ACLineStatus),
                "battery_percent": None if st.BatteryLifePercent == 255 else int(st.BatteryLifePercent)}
    except (AttributeError, OSError):
        return None


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    replace_retry(tmp, path)


class SessionBusy(RuntimeError):
    """The same latency session is running in another process right now (its lock has a fresh heartbeat)."""


class LatencyLock:
    """Held by a latency session for its whole duration (async context manager).

    Another session holding it: wait up to wait_s (the previous slot finishing its last requests), then give up.  The
    SAME session holding it from another process: either that process is still running (a manual start next to the
    scheduled one) — SessionBusy — or it was killed a moment ago and its heartbeat has not gone stale yet: wait at most
    STALE_S for that, then carry on."""

    def __init__(self, session_id: str, endpoints: list[str], path: Path = LOCK_PATH, scope: str = "machine",
                 wait_s: float = 0.0, log=print, mirror: Path | None = None):
        if scope not in ("machine", "endpoint"):
            raise ValueError(f"latency lock scope must be 'machine' or 'endpoint', not {scope!r}")
        self.session_id, self.endpoints, self.path, self.scope = session_id, sorted(set(endpoints)), path, scope
        self.wait_s, self.log, self.waited_s = wait_s, log, 0.0
        self.mirror = mirror        # the session's own copy of every heartbeat: a killed session leaves its last one
        self._task: asyncio.Task | None = None

    def _doc(self) -> dict:
        return {"session": self.session_id, "endpoints": self.endpoints, "scope": self.scope, "pid": os.getpid(),
                "heartbeat_epoch": time.time(), "power": power_status()}

    def _beat(self) -> None:
        doc = self._doc()
        _write(self.path, doc)
        if self.mirror is not None:
            _write(self.mirror, doc)

    async def __aenter__(self) -> "LatencyLock":
        t0 = time.monotonic()
        said_same = False
        while True:                      # a previous session still finishing its last requests: wait (bounded)
            cur = read_lock(self.path)
            if not cur or (cur.get("session") == self.session_id and cur.get("pid") in (None, os.getpid())):
                break
            if cur.get("session") == self.session_id:
                if time.monotonic() - t0 >= STALE_S + HEARTBEAT_S:
                    raise SessionBusy(f"latency session {self.session_id} is already running (process "
                                      f"{cur.get('pid')}) — not started a second time")
                if not said_same:
                    self.log(f"[latency] session {self.session_id} still has a fresh lock from process {cur.get('pid')}"
                             f" — waiting up to {STALE_S + HEARTBEAT_S:.0f} s to see whether that process ended")
                    said_same = True
                await asyncio.sleep(min(5.0, max(0.1, STALE_S + HEARTBEAT_S - (time.monotonic() - t0))))
                continue
            if time.monotonic() - t0 >= self.wait_s:
                raise RuntimeError(f"another latency session ({cur.get('session')}) is running on "
                                   f"{cur.get('endpoints')} — latency sessions must not overlap")
            if self.waited_s == 0.0:
                self.log(f"[latency] session {cur.get('session')} is still running — waiting up to "
                         f"{self.wait_s / 60:.0f} min before this one starts")
            await asyncio.sleep(min(15.0, max(0.1, self.wait_s - (time.monotonic() - t0))))
            self.waited_s = time.monotonic() - t0
        self._beat()

        async def beat():
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                try:
                    self._beat()
                except OSError:                            # one missed beat is harmless (stale only after 75 s)
                    pass
        self._task = asyncio.create_task(beat())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.mirror is not None:          # ended normally: the session's copy says so (a killed one cannot)
            try:
                _write(self.mirror, {**self._doc(), "released": True})
            except OSError:
                pass
        cur = read_lock(self.path)
        if cur is None or cur.get("session") == self.session_id:
            try:
                self.path.unlink()
            except OSError:
                pass


def lock_covers(lock: dict | None, endpoint: str, own_session: str | None = None) -> bool:
    if not lock or lock.get("session") == own_session:
        return False
    if lock.get("scope", "machine") == "machine":
        return True
    return endpoint in (lock.get("endpoints") or [])


class LatencyGate:
    """Used by quality runs: wait while a latency session's lock covers this run (see the module docstring)."""

    def __init__(self, own_session: str, log=print, path: Path = LOCK_PATH):
        self.own, self.log, self.path = own_session, log, path
        self._checked = 0.0
        self._lock: dict | None = None
        self.paused_s = 0.0

    def _current(self) -> dict | None:
        now = time.monotonic()
        if now - self._checked > 5.0:
            self._lock, self._checked = read_lock(self.path), now
        return self._lock

    async def wait(self, endpoint: str) -> None:
        announced = False
        while True:
            cur = self._current()
            if not lock_covers(cur, endpoint, self.own):
                return
            if not announced:
                what = ("this machine" if cur.get("scope", "machine") == "machine"
                        else f"'{endpoint}'")
                self.log(f"[pause] latency session {cur.get('session')} is measuring {cur.get('endpoints')} — "
                         f"quality requests on {what} wait until it finishes")
                announced = True
            await asyncio.sleep(10.0)
            self.paused_s += 10.0
            self._checked = 0.0
