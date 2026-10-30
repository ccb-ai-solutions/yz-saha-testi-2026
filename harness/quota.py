"""Quota awareness: read /v1/quota, keep requests under the TPM limit, stop before the daily cap.

EVREN: 15M tokens/day per account (all models, all keys), 500k TPM, cached prompt
tokens weighted at 3 %, daily reset 03:00 TSİ.  Other endpoints simply have no
quota_path / limits and the governor becomes a no-op.
"""
from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .client import OK, ApiClient

TSI = timezone(timedelta(hours=3), "TSİ")   # Türkiye has used fixed UTC+3 since 2016


@dataclass
class QuotaInfo:
    raw: dict = field(default_factory=dict)
    daily_limit: int | None = None
    daily_remaining: int | None = None
    reset_at: str | None = None
    read_at: str = ""


def _walk(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{prefix}.{k}" if prefix else str(k))
    else:
        yield prefix, obj


def _key_rank(k: str) -> int:
    """Prefer daily counters; never take per-minute (TPM/RPM) counters for the daily quota."""
    if any(x in k for x in ("minute", "dakika", "tpm", "rpm", "per_min")):
        return -1
    if any(x in k for x in ("daily", "gunluk", "günlük", "day", "gün")):
        return 2
    return 1


def parse_quota(raw: dict) -> QuotaInfo:
    q = QuotaInfo(raw=raw, read_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    rem: tuple[int, int] | None = None      # (rank, value)
    lim: tuple[int, int] | None = None
    reset_rank = -1
    for key, val in _walk(raw):
        k = key.lower()
        rank = _key_rank(k)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and rank >= 0:
            if "remaining" in k or "kalan" in k:
                if rem is None or rank > rem[0]:
                    rem = (rank, int(val))
            elif "limit" in k or "quota" in k or "total" in k:
                if lim is None or rank > lim[0]:
                    lim = (rank, int(val))
        # EVREN (11 Oct) returns both `reset_at` (its 5-minute token window) and `daily_reset_at`: keep the daily one
        elif isinstance(val, str) and ("reset" in k or "sifirlan" in k or "sıfırlan" in k) and rank > reset_rank:
            q.reset_at, reset_rank = val, rank
    q.daily_remaining = rem[1] if rem else None
    q.daily_limit = lim[1] if lim else None
    return q


async def read_quota(client: ApiClient) -> QuotaInfo | None:
    if not client.ep.quota_path:
        return None
    r = await client.get_json(client.ep.quota_path)
    if r.outcome != OK or not isinstance(r.data, dict):
        return QuotaInfo(raw={"error": r.outcome, "detail": r.error})
    q = parse_quota(r.data)
    hdr = r.headers.get("x-evren-daily-remaining-tokens")
    if q.daily_remaining is None and hdr is not None:
        try:
            q.daily_remaining = int(float(hdr))
        except ValueError:
            pass
    return q


def next_reset_tsi(hour: int = 3) -> datetime:
    now = datetime.now(TSI)
    reset = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return reset


def weighted_tokens(usage: dict | None, cached_weight: float = 1.0) -> int:
    """Tokens as the quota counts them (cached prompt prefix discounted)."""
    if not usage:
        return 0
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    cached = 0
    det = usage.get("prompt_tokens_details") or {}
    if isinstance(det, dict):
        cached = int(det.get("cached_tokens") or 0)
    return int(round((pt - cached) + cached * cached_weight + ct))


class TokenGovernor:
    """Sliding 60-second window limiter for tokens per minute and/or requests per minute (e.g. NVIDIA's free
    endpoints: ~40 requests/min per key across all models).  acquire() before a request, settle() after it."""

    def __init__(self, tpm_limit: int | None, *, safety: float = 0.85, cached_weight: float = 1.0,
                 rpm_limit: int | None = None):
        self.budget = int(tpm_limit * safety) if tpm_limit else None
        self.rpm = int(rpm_limit) if rpm_limit else None
        self.cached_weight = cached_weight
        self.window: collections.deque = collections.deque()   # [t, tokens, id]
        self.lock = asyncio.Lock()
        self.waited_s = 0.0
        self._next_id = 0

    def _purge(self, now: float) -> None:
        while self.window and now - self.window[0][0] >= 60.0:
            self.window.popleft()

    def used(self) -> int:
        self._purge(time.monotonic())
        return sum(e[1] for e in self.window)

    async def acquire(self, estimate: int) -> int | None:
        if self.budget is None and self.rpm is None:
            return None
        estimate = min(estimate, self.budget) if self.budget is not None else 0
        while True:
            async with self.lock:
                now = time.monotonic()
                self._purge(now)
                tokens_ok = self.budget is None or sum(e[1] for e in self.window) + estimate <= self.budget
                requests_ok = self.rpm is None or len(self.window) < self.rpm
                if tokens_ok and requests_ok:
                    self._next_id += 1
                    self.window.append([now, estimate, self._next_id])
                    return self._next_id
                wait = 60.0 - (now - self.window[0][0]) + 0.05 if self.window else 0.25
            wait = max(0.25, min(wait, 60.0))
            self.waited_s += wait
            await asyncio.sleep(wait)

    def settle(self, ticket: int | None, usage: dict | None) -> None:
        if ticket is None or not usage or self.budget is None:
            return
        actual = weighted_tokens(usage, self.cached_weight)
        for e in self.window:
            if e[2] == ticket:
                e[1] = actual
                break
