"""Latency / throughput sweeps — "as experienced" by a client in Türkiye.

Design (see METHODOLOGY.md §4):
  * three workloads with fixed prompt subsets (identical in every session):
      chat  short prompt → short answer        (interactive feel)
      rag   ~1.5k-token grounded prompt         (prefill-heavy, the enterprise case)
      long  short prompt → ~600-word answer     (decode-heavy)
  * cache busting: chunk order (rag) / a request tag (chat, long) change every session AND every variant
    (salt = variant key), so the server's prefix cache can only ever reuse the fixed system prompt — the
    same for every model, and never a previous mode's identical prompt on the same model.
  * warm-up requests are recorded but excluded; the first one per model is the "cold" observation.
  * single-stream stage: ONE stream per model — a model's reasoning modes take turns on that stream
    (rotated per prompt), different models run in the same wall-clock window (default) or strict round-robin.
  * concurrency sweep: one model at a time, randomised model order per level, closed-loop workers, TPM
    pre-check so the 500k TPM cap is never the bottleneck being measured.  No retries inside a measured cell;
    every request records how many were in flight when it started (steady-state filtering in the analysis).
  * a latency session holds results/.latency.lock: quality runs on the same endpoint pause meanwhile.
  * time budgets (suites.yaml time_budget_s) are a safety net for a pathological slowdown (10 Oct evening: GLM-5.3
    at 1.5 tokens/s): a model's stream that passes its budget sends nothing more in that stage (the request in
    flight finishes) and the summary lists it under budget_stops; at normal speed no budget is reached.  Time spent
    waiting out a network outage is not charged to a budget.  A model that used up a single-stream stage's budget
    is left out of the session's later single-stream stages and of its sweep cells (since 16 Oct; budget_stops with
    reason 'slow', sweep cells 'skipped'; the summary's slow_models): on 15 Oct afternoon chat and rag waited 16 and
    18 min for GLM-5.3 after the other models had finished, and the sweep ran out of time.
  * stage order: OCR timing, warm-up, then single-stream chat → rag → long and the sweep.  A session whose window is
    at most early_sweep_below_minutes long (since 16 Oct every slot: 300 ≥ the morning's 280 min) runs the sweep right
    after the last workload it sweeps (rag), before 'long': a window that runs out then cuts the end of 'long' —
    every model keeps most of its long prompts — instead of the sweep's highest levels, and a model that uses up its
    'long' budget loses no sweep cells (15 Oct afternoon: DeepSeek-V4.1-Flash, 15 of 20 long prompts in 65 min).
    Fixed by the session's first part (part_start stage_order).
  * session end (session_window, --max-minutes, --until): a session started during the day stops sending by
    day_end_tsi minus a margin, one started in the evening after evening_max_minutes (the laptop's hours, 14 Oct);
    every stage checks it (wall clock) before each request, the summary records deadline_tsi / deadline_hit.
  * OCR timing (ocr_probe): first, before the warm-up, each OCR model on the measured endpoint reads a fixed set
    of pages one at a time, then with a few requests in flight — seconds per page and pages per minute by time of
    day (records in latency_ocr/).  Each request carries a unique JPEG comment / PNG text chunk, so no cache keyed
    on the request body can answer it; the pixels are untouched.
  * resumable (since 15 Oct): a session is one time-of-day WINDOW, fixed when it is first started.  Started again
    with the same id inside that window (the scheduled task re-starts every 10 minutes, run_latency_session.ps1
    restarts a crashed process), it carries on where it stopped: single-stream requests that have a measurement are
    not sent again, a sweep / OCR cell that did not finish is measured again from its start (a cell is one
    contiguous measurement), the warm-up is repeated (shorter).  Each start is a "part"; every record carries its
    part and try, and requests of a later part or try get fresh cache-busting tags.  latency_progress.jsonl (append
    only) records what each part finished.  Started after its window, a session sends nothing: its summary is
    written from what it has (partial).
  * network outages (NetGuard): a request that ends without an HTTP answer (no connection, failed name lookup,
    broken stream, timeout) is followed by one check of the endpoint — GET /models on a fresh connection, no
    tokens.  It answers → the failure was the server's and stays a measurement.  It does not → this machine is
    offline: the record is labelled outage (client = the name lookup failed, else unknown) and left out of every
    statistic, every stream waits while the check repeats every 15 s, and the request is sent again once the
    endpoint answers (14 Oct 16:15: a Wi-Fi drop turned 42 requests into failures within one second).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import mimetypes
import os
import random
import statistics
import time
import zlib
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..capabilities import Capabilities, build_chat_payload, load_caps
from ..client import (CLIENT_ERROR, CONN_ERROR, OK, STREAM_BROKEN, STREAM_INCOMPLETE, TIMEOUT_CONNECT, TIMEOUT_IDLE,
                      TIMEOUT_READ, TIMEOUT_TOTAL, TIMEOUT_TTFT, USABLE, ApiClient, ChatResult, utc_now)
from ..config import DATA_DIR, Variant
from ..locks import LatencyLock, power_status
from ..prompts import load_prompt
from ..quota import TSI, read_quota, weighted_tokens
from ..rag_data import load_derived, load_questions
from ..runner import RunContext, unprobed_refusal, unavailable_refusal
from ..store import (HARNESS_VERSION, ResultWriter, code_hash, ensure_trailing_newline, read_jsonl, safe_name,
                     sha256_obj, stable_seed, stable_shuffle)

WARMUP_PROMPTS = ["Merhaba, nasılsın?", "Bir cümleyle kendini tanıt.", "Bugün hava nasıl olabilir? Kısaca.",
                  "Bana bir sayı söyle.", "Türkiye'nin başkenti neresidir?"]

# A request that ended without an HTTP answer — or with one but then no data: the network between this machine and
# the endpoint may be the cause (the endpoint check decides).
TRANSPORT_FAILURES = frozenset({CONN_ERROR, TIMEOUT_CONNECT, TIMEOUT_READ, TIMEOUT_IDLE, TIMEOUT_TTFT, TIMEOUT_TOTAL,
                                STREAM_BROKEN, STREAM_INCOMPLETE, CLIENT_ERROR})
# ... and one that certainly happened on this side: the endpoint's name could not be resolved (14 Oct 16:15:
# "[Errno 11001] getaddrinfo failed" for every request of every stream), or this machine has no route / no network.
LOCAL_NETWORK_MARKERS = ("getaddrinfo", "name or service not known", "nodename nor servname", "name resolution",
                         "no address associated", "errno 11001", "errno 11002", "errno 11004",
                         "network is unreachable", "unreachable network", "no route to host", "network is down",
                         "winerror 10050", "winerror 10051", "winerror 10065", "errno 100]", "errno 101]", "errno 113]")


def local_network_error(error: str | None) -> bool:
    """The error text says this machine could not even start the connection: name lookup failed, no route, no network."""
    e = (error or "").lower()
    return any(m in e for m in LOCAL_NETWORK_MARKERS)


class LoopLag:
    """Measures event-loop scheduling delay: if the client itself is saturated, timings are invalid."""

    def __init__(self, interval: float = 0.05):
        self.interval, self.samples, self._task = interval, [], None

    async def _run(self):
        while True:
            t = time.perf_counter()
            await asyncio.sleep(self.interval)
            self.samples.append((time.perf_counter() - t - self.interval) * 1000)

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> dict:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        s = sorted(self.samples) or [0.0]
        return {"p50_ms": round(s[len(s) // 2], 2), "p99_ms": round(s[int(len(s) * 0.99) - 1 if len(s) > 1 else 0], 2),
                "max_ms": round(s[-1], 2), "n": len(self.samples)}


def _jsonl(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


_RAG_CACHE: dict = {}


def _rag_data():
    if not _RAG_CACHE:
        chunks, gold, contexts = load_derived()
        _RAG_CACHE.update(by_id={c["chunk_id"]: c for c in chunks}, contexts=contexts,
                          questions=[q for q in load_questions() if q["answerable"]])
    return _RAG_CACHE


def prompt_ids(workload: str, n: int) -> list[str]:
    """The fixed prompt subset of a workload (identical in every session)."""
    if workload in ("chat", "long"):
        rows = _jsonl(DATA_DIR / "latency" / f"{workload}.jsonl")
        return [r["id"] for r in stable_shuffle(rows, "latency-subset", workload)[:n]]
    if workload == "rag":
        return [q["id"] for q in stable_shuffle(_rag_data()["questions"], "latency-subset", "rag")[:n]]
    raise ValueError(f"unknown workload {workload}")


def build_prompts(session_seed: str, workload: str, n: int, *, cache_buster: bool = True,
                  count: int | None = None, salt: str = "") -> list[dict]:
    """Fixed prompt subset per workload (same in every session); only the cache-busting element varies.

    count > n cycles through the subset; every cycle gets a fresh tag / chunk order so that no two
    requests in a session share more than the fixed system prompt.
    """
    count = count or n
    if workload in ("chat", "long"):
        rows = _jsonl(DATA_DIR / "latency" / f"{workload}.jsonl")
        rows = stable_shuffle(rows, "latency-subset", workload)[:n]
        system = load_prompt("latency_system.md")
        out = []
        for i in range(count):
            r = rows[i % len(rows)]
            tag = (f"(Talep kodu: {stable_seed(session_seed, salt, workload, r['id'], i // len(rows)) % 10**6:06d})\n"
                   if cache_buster else "")
            out.append({"prompt_id": r["id"], "messages": [{"role": "system", "content": system},
                                                         {"role": "user", "content": tag + r["prompt"]}]})
        return out
    if workload == "rag":
        d = _rag_data()
        qs = stable_shuffle(d["questions"], "latency-subset", "rag")[:n]
        system = load_prompt("rag_system.md")
        out = []
        for i in range(count):
            q = qs[i % len(qs)]
            ids = d["contexts"][q["id"]]["gold"] + d["contexts"][q["id"]]["negatives"]
            order = (stable_shuffle(ids, session_seed, salt, "latency-rag", q["id"], i // len(qs))
                     if cache_buster else ids)
            body = "\n\n".join(f"[K{j + 1}] {d['by_id'][c]['text']}" for j, c in enumerate(order))
            out.append({"prompt_id": q["id"], "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Belgeler:\n\n{body}\n\nSoru: {q['question']}"}]})
        return out
    raise ValueError(f"unknown workload {workload}")


def _summ(vals: list[float]) -> dict:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return {"n": 0}
    q = lambda p: v[min(len(v) - 1, int(round(p * (len(v) - 1))))]
    return {"n": len(v), "p50": round(q(0.5), 3), "p90": round(q(0.9), 3), "min": round(v[0], 3), "max": round(v[-1], 3)}


def cache_busted_image(path: Path, nonce: str) -> bytes:
    """The page image with a unique comment (JPEG COM segment after SOI / PNG tEXt chunk after IHDR): the request
    body differs every time, the pixels do not.  Other formats are returned unchanged."""
    b = path.read_bytes()
    tag = f"trbench {nonce}".encode("ascii")
    if b[:2] == b"\xff\xd8":
        return b[:2] + b"\xff\xfe" + (len(tag) + 2).to_bytes(2, "big") + tag + b[2:]
    if b[:8] == b"\x89PNG\r\n\x1a\n" and b[12:16] == b"IHDR":
        data = b"Comment\x00" + tag
        chunk = len(data).to_bytes(4, "big") + b"tEXt" + data + zlib.crc32(b"tEXt" + data).to_bytes(4, "big")
        return b[:33] + chunk + b[33:]
    return b


class KeepAwake:
    """Windows: no idle sleep while a session runs (SetThreadExecutionState ES_CONTINUOUS | ES_SYSTEM_REQUIRED on
    this thread; the screen may still turn off, closing the lid still sleeps).  Released when the session ends —
    the session end time bounds how long the PC is kept awake.  A no-op elsewhere."""

    def __enter__(self):
        self.ok = False
        if os.name == "nt":
            try:
                import ctypes
                self.ok = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001))
            except (AttributeError, OSError):
                self.ok = False
        return self

    def __exit__(self, *exc):
        if self.ok:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        return False


def session_deadline(cfg: dict, now: datetime | None = None, max_minutes: float | None = None) -> datetime | None:
    """When a session must stop sending (TSİ): --max-minutes after the start, and/or the session_window rules — a
    session started before day_end_tsi stops at day_end_tsi minus margin_minutes, one started later after
    evening_max_minutes, and any session stops before_next_slot_minutes before the next latency slot of the same day
    (slots_tsi), so that slot's session never finds this one still running.  The earliest applies; None = no end."""
    now = now or datetime.now(TSI)
    ends = [now + timedelta(minutes=float(max_minutes))] if max_minutes else []
    w = cfg.get("session_window") or {}

    def at(hhmm: str) -> datetime:
        hh, mm = (int(x) for x in str(hhmm).split(":"))
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if w.get("day_end_tsi"):
        day_end = at(w["day_end_tsi"]) - timedelta(minutes=float(w.get("margin_minutes", 10)))
        if now < day_end:
            ends.append(day_end)
        elif w.get("evening_max_minutes"):
            ends.append(now + timedelta(minutes=float(w["evening_max_minutes"])))
    gap = timedelta(minutes=float(w.get("before_next_slot_minutes", 20)))
    later = [at(x) for x in w.get("slots_tsi") or [] if at(x) - gap > now + timedelta(minutes=5)]
    if later:
        ends.append(min(later) - gap)
    return min(ends) if ends else None


def parse_tsi(s: str | None, today: datetime | None = None) -> datetime | None:
    """'HH:MM' (today, TSİ), 'YYYY-MM-DD HH:MM' / ISO (TSİ unless it carries an offset) -> aware datetime; None for
    an empty value."""
    if not s:
        return None
    s = str(s).strip()
    today = today or datetime.now(TSI)
    if len(s) <= 5 and ":" in s:
        hh, mm = (int(x) for x in s.split(":"))
        return today.astimezone(TSI).replace(hour=hh, minute=mm, second=0, microsecond=0)
    d = datetime.fromisoformat(s.replace(" ", "T").replace("Z", "+00:00"))
    return d.replace(tzinfo=TSI) if d.tzinfo is None else d.astimezone(TSI)


def ocr_probe_pages(n: int) -> list[dict]:
    """The fixed page subset of the OCR timing (identical in every session); [] when the pages are not built."""
    from .pipeline import ocr_manifest
    try:
        pages = ocr_manifest()
    except FileNotFoundError:
        return []
    return stable_shuffle(pages, "latency-subset", "ocr")[:n]


def latency_config_hash(reg, cfg: dict, profile: str, workloads: list[str], n: int) -> tuple[str, dict]:
    """Everything that makes two latency sessions comparable — but nothing session-specific."""
    doc = {"workloads": workloads, "n": n, "max_tokens": cfg.get("max_tokens"), "profile": profile,
           "decoding": reg.decoding(profile), "levels": cfg.get("concurrency_levels"),
           "sweep_workloads": cfg.get("sweep_workloads"), "sweep_modes": cfg.get("sweep_modes"),
           "mode_workloads": cfg.get("mode_workloads"), "schedule": cfg.get("schedule", "parallel"),
           "warmup": [cfg.get("warmup_requests"), cfg.get("warmup_max_tokens")],
           "sweep_requests": [cfg.get("sweep_min_requests"), cfg.get("sweep_requests_per_stream")],
           "cache_buster": cfg.get("cache_buster", True),
           "time_budget_s": cfg.get("time_budget_s"), "ocr_probe": cfg.get("ocr_probe"),
           "session_window": cfg.get("session_window"),
           "prompt_ids": {w: prompt_ids(w, n) for w in workloads}}
    return sha256_obj(doc)[:16], doc


def stage_order(workloads: list[str], sweep_workloads: list[str] | None, early: bool = False) -> list[str]:
    """The measured stages after the OCR timing and the warm-up: each workload's single-stream stage, then 'sweep'.
    early (every slot since 16 Oct): the sweep right after the last workload it sweeps — its TPM pre-check needs that
    workload's single-stream speed — and before the remaining single-stream stages."""
    order = list(workloads)
    swept = [i for i, w in enumerate(order) if w in (sweep_workloads or [])]
    if not early or not swept:
        return order + ["sweep"]
    return order[:swept[-1] + 1] + ["sweep"] + order[swept[-1] + 1:]


def variants_for_workload(keys: list[str], workload: str, mode_workloads: dict | None) -> list[str]:
    mw = mode_workloads or {}
    out = []
    for k in keys:
        mode = k.rsplit(":", 1)[1] if ":" in k else "default"
        if mode not in mw or workload in mw[mode]:
            out.append(k)
    return out


# ============================================================================ outages, progress, session state
def _span(r: dict) -> tuple[float, float] | None:
    """(start, end) epoch seconds of a record's request."""
    res = r.get("result") or {}
    try:
        t0 = datetime.fromisoformat(str(res.get("wall_start")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    dur = res.get("t_end") if res.get("t_end") is not None else res.get("latency")
    return t0, t0 + float(dur or 0.0)


def label_legacy_outages(records: list[dict], before_s: float = 60.0, gap_s: float = 120.0) -> int:
    """Records written before the network check existed (no 'part' field) carry no outage label: label what was
    certainly this machine's network — every request whose server name could not be resolved, and every request that
    failed without an HTTP answer while that lasted or in the minute before (14 Oct 16:15: five streams broke in the
    same second, then every name lookup failed).  Returns how many records were labelled."""
    legacy = [r for r in records if "part" not in r and not r.get("outage")]
    dns = []
    for r in legacy:
        res = r.get("result") or {}
        sp = _span(r) if res.get("outcome") == CONN_ERROR and local_network_error(res.get("error")) else None
        if sp:
            dns.append(sp)
    dns.sort()
    windows: list[list[float]] = []
    for s, e in dns:
        if windows and s - windows[-1][1] <= gap_s:
            windows[-1][1] = max(windows[-1][1], e)
        else:
            windows.append([s, e])
    n = 0
    for r in legacy:
        res = r.get("result") or {}
        if res.get("outcome") not in TRANSPORT_FAILURES:
            continue
        sp = _span(r)
        if (res.get("outcome") == CONN_ERROR and local_network_error(res.get("error"))) or (
                sp and any(w0 - before_s <= sp[1] <= w1 + 1.0 for w0, w1 in windows)):
            r["outage"] = "client"
            n += 1
    return n


def record_outage(r: dict) -> str | None:
    """The outage label of a record (NetGuard since 15 Oct; label_legacy_outages for older records)."""
    if r.get("outage"):
        return str(r["outage"])
    res = r.get("result") or {}
    if res.get("outcome") == CONN_ERROR and local_network_error(res.get("error")):
        return "client"
    return None


def attempt_of(r: dict) -> tuple[int, int]:
    return int(r.get("part") or 1), int(r.get("try") or 0)


def latest_attempts(records: list[dict]) -> list[dict]:
    """The records a statistic may use: outage-labelled ones dropped; of a sweep / OCR cell only one attempt — the
    latest one no outage hit, else the latest (a cell measured again after an outage or a restart replaces the
    earlier attempt; one contiguous measurement, never mixed); of a single-stream request (workload, variant, prompt)
    the latest record."""
    hit_attempts = {(r.get("stage"), r.get("cell"), attempt_of(r)) for r in records
                    if r.get("stage") in ("sweep", "ocr") and record_outage(r)}
    keep = [r for r in records if not record_outage(r)]
    best: dict = {}
    for r in keep:
        if r.get("stage") in ("sweep", "ocr"):
            k = (r.get("stage"), r.get("cell"))
            rank = (k + (attempt_of(r),) not in hit_attempts, attempt_of(r))     # outage-free first, then latest
            best[k] = max(best.get(k, (False, (0, 0))), rank)
    out, unit = [], {}
    for r in keep:
        st = r.get("stage")
        if st in ("sweep", "ocr"):
            if attempt_of(r) == best[(st, r.get("cell"))][1]:
                out.append(r)
        elif st == "c1":
            k = (r.get("workload"), r.get("variant"), r.get("prompt_id"))
            prev = unit.get(k)
            if prev is None or (attempt_of(r), r.get("recorded_at") or "") >= (attempt_of(prev), prev.get("recorded_at") or ""):
                unit[k] = r
        else:
            out.append(r)
    return out + list(unit.values())


class Progress:
    """latency_progress.jsonl — what each part of a session started and finished (append-only JSON lines: part_start,
    rtt, quota, budget_stop, outage, cell_done, part_end); read back when the session is started again.  path None =
    in memory only (tests)."""

    def __init__(self, path: Path | None):
        self.path = path
        self.events: list[dict] = []
        if path is not None:
            ensure_trailing_newline(path)
            self.events = read_jsonl(path)

    def add(self, event: str, **data) -> dict:
        rec = {"event": event, "t": utc_now(), **data}
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.events.append(rec)
        return rec

    def of(self, event: str) -> list[dict]:
        return [e for e in self.events if e.get("event") == event]


class NetGuard:
    """Tells this machine's network outages from the endpoint's failures, and holds every stream while offline.

    check() is called after each request.  A request that failed without an HTTP answer (TRANSPORT_FAILURES)
    triggers one probe of its endpoint (several streams failing at once share it).  Probe answered → the failure was
    the server's: the request stays a measurement (None).  Not answered → offline: the request is labelled with the
    side ('client' when this machine could not even start the connection — name lookup, no route —, else 'unknown':
    this machine's network or the endpoint itself, the check cannot tell), ready() holds every stream while the
    probe repeats every interval_s, and the caller sends the request again once the endpoint answers.  A request
    that was running while the machine was offline is labelled too, whatever its outcome.  downtime() = offline
    seconds, which time budgets do not count.  give_up() (the session end, Ctrl+C) ends the waiting.  Nothing here
    may leave the streams waiting for good: the watcher always releases them, whatever fails inside it, and ready()
    lets them go if the watcher is gone."""

    def __init__(self, probe: Callable[[str], Awaitable[tuple[bool, str]]], *, interval_s: float = 15.0,
                 log: Callable = print, give_up: Callable[[], bool] = lambda: False,
                 on_outage: Callable[[dict], None] | None = None, on_back: Callable[[], None] | None = None):
        self.probe, self.interval_s, self.log, self.give_up = probe, interval_s, log, give_up
        self.on_outage, self.on_back = on_outage, on_back
        self.online = asyncio.Event()
        self.online.set()
        self.outages: list[dict] = []
        self._spans: list[dict] = []                    # {"s": perf start, "e": perf end or None, "side": ...}
        self._probing: dict[str, asyncio.Future] = {}   # endpoint -> the check in progress
        self._watch: asyncio.Task | None = None
        self._cur: dict = {}

    @property
    def offline(self) -> bool:
        return not self.online.is_set()

    @staticmethod
    def _safe(fn: Callable, *a):
        """A log line, a progress write or a callback that fails must never stall the streams."""
        try:
            return fn(*a)
        except Exception:                               # noqa: BLE001
            return None

    def downtime(self, t0: float, t1: float | None = None) -> float:
        t1 = time.perf_counter() if t1 is None else t1
        return sum(max(0.0, min(t1, sp["e"] if sp["e"] is not None else t1) - max(t0, sp["s"])) for sp in self._spans)

    def _side_during(self, t0: float, t1: float) -> str | None:
        for sp in self._spans:
            if (sp["e"] is None or sp["e"] > t0) and sp["s"] < t1:
                return sp["side"]
        return None

    async def ready(self) -> None:
        """Returns at once while online; otherwise when the endpoint answers again, the watcher gives up (session
        end) — or the watcher is gone (it releases the streams in any case; this is the second safety net)."""
        while not self.online.is_set():
            if self._watch is None or self._watch.done():
                self.online.set()
                break
            try:
                await asyncio.wait_for(self.online.wait(), timeout=max(0.05, self.interval_s))
            except asyncio.TimeoutError:
                pass

    async def _probe(self, endpoint: str) -> tuple[bool, str]:
        try:
            return await self.probe(endpoint)
        except Exception as exc:                        # noqa: BLE001 — a failing probe means: not reachable
            return False, f"{type(exc).__name__}: {exc}"

    async def check(self, outcome: str, error: str | None, t0: float, t1: float, endpoint: str) -> str | None:
        """The outage label of a request that ran from t0 to t1 (perf_counter), or None when it is a measurement."""
        side = self._side_during(t0, t1)
        if side or self.offline:
            return side or self._cur.get("side") or "unknown"
        if outcome not in TRANSPORT_FAILURES:
            return None
        local = local_network_error(error)
        if endpoint in self._probing:                    # another stream's failure is being checked: same answer
            reachable = await asyncio.shield(self._probing[endpoint])
        else:
            fut = asyncio.get_running_loop().create_future()
            self._probing[endpoint] = fut
            reachable, detail = False, "check cancelled"
            try:
                reachable, detail = await self._probe(endpoint)
            finally:
                self._probing.pop(endpoint, None)
                if not fut.done():
                    fut.set_result(reachable)
            if not reachable and not self.offline:
                self._start(t1, "client" if local or local_network_error(detail) else "unknown", endpoint,
                            f"{outcome}: {error or ''}", detail)
        if reachable:
            return "client" if local else None           # a name lookup that failed for a moment: still this side
        return self._cur.get("side") or ("client" if local else "unknown")

    async def ensure_online(self, endpoint: str) -> None:
        """Before a part starts: probe once; offline → wait like the streams would."""
        if self.offline:
            await self.ready()
            return
        reachable, detail = await self._probe(endpoint)
        if not reachable:
            self._start(time.perf_counter(), "client" if local_network_error(detail) else "unknown", endpoint,
                        "endpoint check at the start of the part", detail)
            await self.ready()

    def _start(self, t: float, side: str, endpoint: str, first_error: str, detail: str) -> None:
        self.online.clear()
        self._spans.append({"s": t, "e": None, "side": side})
        self._cur = {"start_tsi": datetime.now(TSI).isoformat(timespec="seconds"), "side": side, "endpoint": endpoint,
                     "first_error": first_error[:300], "check": detail[:300]}
        self._watch = asyncio.create_task(self._watch_loop(endpoint))          # before anything that might fail
        self._safe(self.log, f"[latency] network: {endpoint} cannot be reached from this machine ({detail[:160]}) — "
                             f"every stream waits and checks again every {self.interval_s:g} s; requests hit by it "
                             "are sent again afterwards")

    async def _watch_loop(self, endpoint: str) -> None:
        t0, ended = self._spans[-1]["s"], "endpoint answers again"
        try:
            while True:
                await asyncio.sleep(self.interval_s)
                if self._safe(self.give_up):
                    ended = "session end"
                    break
                reachable, _ = await self._probe(endpoint)
                if reachable:
                    break
        except asyncio.CancelledError:
            ended = "cancelled"
            raise
        except Exception as exc:                        # noqa: BLE001 — released below in any case
            ended = f"watcher error: {type(exc).__name__}"
        finally:
            self._release(t0, ended, endpoint)

    def _release(self, t0: float, ended: str, endpoint: str) -> None:
        t1 = time.perf_counter()
        if self._spans and self._spans[-1]["e"] is None:
            self._spans[-1]["e"] = t1
        o = {**self._cur, "end_tsi": datetime.now(TSI).isoformat(timespec="seconds"), "seconds": round(t1 - t0, 1),
             "ended": ended}
        self.outages.append(o)
        self._cur = {}
        if self.on_outage:
            self._safe(self.on_outage, o)
        self._safe(self.log, f"[latency] network: {endpoint} answers again after {t1 - t0:.0f} s — continuing"
                   if ended == "endpoint answers again" else f"[latency] network: waiting ended ({ended}, {t1 - t0:.0f} s)")
        if self.on_back:
            self._safe(self.on_back)
        self.online.set()


@dataclass
class SessionState:
    """A latency session as it is on disk — read without any network: progress log, records, summary, plan meta."""
    sdir: Path
    session_id: str
    created: datetime | None
    summary: dict
    progress: list[dict]
    meta: dict
    has_records: bool
    records: list[dict] = field(default_factory=list)
    ocr_records: list[dict] = field(default_factory=list)

    def events(self, name: str) -> list[dict]:
        return [e for e in self.progress if e.get("event") == name]

    @property
    def n_parts(self) -> int:
        return len(self.events("part_start")) or (1 if self.has_records else 0)

    @property
    def complete(self) -> bool:
        return bool(self.summary.get("completed")) or any(e.get("reason") == "complete" for e in self.events("part_end"))

    @property
    def last_part_end(self) -> dict:
        starts, ends = self.events("part_start"), {e.get("part"): e for e in self.events("part_end")}
        return ends.get(starts[-1].get("part"), {}) if starts else {}

    @property
    def variants(self) -> list[str]:
        s = self.events("part_start")
        return list((s[0].get("variants") if s else None) or self.meta.get("variants") or [])

    @property
    def plan_hash(self) -> str | None:
        s = self.events("part_start")
        if s and s[0].get("plan_hash"):
            return s[0]["plan_hash"]
        return next((r.get("plan_hash") for r in self.records + self.ocr_records if r.get("plan_hash")),
                    self.summary.get("plan_hash"))

    def window_end(self, cfg: dict) -> datetime | None:
        """The session's window end — fixed by its first part; a session from before 15 Oct: from its start time."""
        s = self.events("part_start")
        if s:
            return parse_tsi(s[0].get("window_end_tsi"))
        if self.summary.get("deadline_tsi"):
            return parse_tsi(self.summary["deadline_tsi"])
        return session_deadline(cfg, self.created) if self.created else None


def load_state(sdir: Path, manifest: dict | None = None, *, records: bool = True) -> SessionState:
    mp = sdir / "manifest.json"
    man = manifest if manifest is not None else (json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {})
    summary = ((man.get("suites") or {}).get("latency") or {}).get("summary") or {}
    files = sorted((sdir / "latency").glob("*.jsonl")) + sorted((sdir / "latency_ocr").glob("*.jsonl"))
    metap = sdir / "plans" / "latency.meta.json"
    st = SessionState(sdir=sdir, session_id=man.get("session_id") or sdir.name,
                      created=parse_tsi(man.get("created_tsi")) if man.get("created_tsi") else None,
                      summary=summary, progress=read_jsonl(sdir / "latency_progress.jsonl"),
                      meta=json.loads(metap.read_text(encoding="utf-8")) if metap.exists() else {},
                      has_records=any(f.stat().st_size > 0 for f in files))
    if records:
        for f in sorted((sdir / "latency").glob("*.jsonl")):
            st.records += read_jsonl(f)
        for f in sorted((sdir / "latency_ocr").glob("*.jsonl")):
            st.ocr_records += read_jsonl(f)
        label_legacy_outages(st.records)
        label_legacy_outages(st.ocr_records)
    return st


def session_status(st: SessionState, cfg: dict, now: datetime | None = None,
                   config_hash: str | None = None) -> tuple[str | None, str]:
    """(reason, message): reason is set when the session is FINISHED — nothing more may be sent in it: 'complete',
    'window_over' (its window, or --until, has passed), 'max_parts' (started too often), 'quota' (stopped on the
    daily quota in this window), 'config_changed' (a different configuration would mix two measurements).  None =
    a new session or one to resume."""
    now = now or datetime.now(TSI)
    sid = st.session_id
    if st.complete:
        return "complete", f"[latency] session {sid} is complete — nothing to send"
    ends = [x for x in (st.window_end(cfg) if st.n_parts else None, parse_tsi(cfg.get("until_tsi"))) if x]
    if ends and now >= min(ends):
        end = min(ends)
        day = f" on {end:%d %b}" if end.date() != now.date() else ""
        return "window_over", (f"[latency] session {sid}: its window ended at {end:%H:%M} TSİ{day} — nothing more is "
                               "sent; its summary is written from what it has (partial)")
    if not st.n_parts:
        return None, ""
    if config_hash and st.meta.get("config_hash") and st.meta["config_hash"] != config_hash:
        return "config_changed", (f"[latency] session {sid} was started with another latency configuration "
                                  f"({st.meta['config_hash']} -> {config_hash}): it is not resumed (two configurations "
                                  "would mix in one measurement); its summary is written from what it has (partial)")
    mp = int(cfg.get("max_parts", 12))
    if st.n_parts >= mp:
        return "max_parts", (f"[latency] session {sid} was started {st.n_parts} times (max_parts {mp}) — not started "
                             "again; its summary is written from what it has (partial)")
    if st.last_part_end.get("reason") == "quota":
        return "quota", (f"[latency] session {sid} stopped on the daily quota — not resumed in this window; its "
                         "summary is written from what it has (partial)")
    end = st.window_end(cfg)
    return None, (f"[latency] resuming session {sid}: part {st.n_parts + 1}"
                  + (f", window until {end:%H:%M} TSİ" if end else "")
                  + " — requests that already have a measurement are not sent again")


def _tsi_of(utc_iso: str | None) -> str | None:
    if not utc_iso:
        return None
    try:
        return datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).astimezone(TSI).isoformat(timespec="seconds")
    except ValueError:
        return None


def _cells_from_records(records: list[dict], stage: str) -> list[dict]:
    by: dict = {}
    for r in latest_attempts(records):
        if r.get("stage") != stage:
            continue
        c = by.setdefault(r.get("cell"), {"cell": r.get("cell"), "variant": r.get("variant"), "workload": r.get("workload"),
                                           "concurrency": r.get("concurrency"), "outcomes": {}, "from_records": True})
        o = (r.get("result") or {}).get("outcome")
        c["outcomes"][o] = c["outcomes"].get(o, 0) + 1
    return list(by.values())


def build_summary(st: SessionState, cfg: dict, end_reason: str) -> dict:
    """The session summary over all its parts (manifest suites.latency.summary) — from the progress log and the
    records: what finished, what did not and why.  Written at the end of every part and when a session is closed
    after its window."""
    starts = st.events("part_start")
    ends = {e.get("part"): e for e in st.events("part_end")}
    first = starts[0] if starts else {}
    last_rec: dict[int, str] = {}
    for r in st.records + st.ocr_records:
        p, t = int(r.get("part") or 1), r.get("recorded_at") or ""
        if t > last_rec.get(p, ""):
            last_rec[p] = t
    by_part = {int(s.get("part") or 1): s for s in starts}
    n_parts = max(by_part) if by_part else (1 if st.has_records else 0)
    parts = []
    for p in range(1, n_parts + 1):              # part 1 of a session begun before 15 Oct has no progress entry
        s = by_part.get(p) or {"started_tsi": st.created.isoformat(timespec="seconds") if (st.created and p == 1) else None}
        e = ends.get(p) or {}
        parts.append({"part": p, "started_tsi": s.get("started_tsi"),
                      "ended_tsi": e.get("ended_tsi") or _tsi_of(last_rec.get(p)),
                      "end": e.get("reason") or "ended from outside (no end record; time = its last request)",
                      "power_start": s.get("power_start"), "power_end": e.get("power_end")})
    variants = st.variants or sorted({r.get("variant") for r in st.records if r.get("variant")})
    conf = st.meta.get("config") or {}
    workloads = conf.get("workloads") or cfg.get("workloads") or ["chat", "rag", "long"]
    n = int(conf.get("n") or cfg.get("prompts_per_workload", 20))
    mw = conf.get("mode_workloads") if "mode_workloads" in conf else cfg.get("mode_workloads")
    good = [r for r in st.records if not record_outage(r)]
    c1 = {}
    for w in workloads:
        units = {(r.get("variant"), r.get("prompt_id")) for r in good if r.get("stage") == "c1" and r.get("workload") == w}
        c1[w] = {"done": len(units), "planned": n * len(variants_for_workload(variants, w, mw))}
    sweeps = [e.get("result") for e in st.events("cell_done") if e.get("kind") == "sweep"] if starts else \
        _cells_from_records(st.records, "sweep")
    ocr = [e.get("result") for e in st.events("cell_done") if e.get("kind") == "ocr"] if starts else \
        _cells_from_records(st.ocr_records, "ocr")
    outages = [{k: v for k, v in e.items() if k not in ("event", "t")} for e in st.events("outage")]
    hit = [r for r in st.records + st.ocr_records if record_outage(r)]
    legacy_hit = [r for r in hit if "part" not in r]
    if legacy_hit:                                    # sessions from before the network check: found afterwards
        ends_at = sorted(x[1] for x in map(_span, legacy_hit) if x)     # when the requests failed
        outages.append({"side": "client", "found_afterwards": True, "requests": len(legacy_hit),
                        "start_tsi": datetime.fromtimestamp(ends_at[0], TSI).isoformat(timespec="seconds") if ends_at else None,
                        "end_tsi": datetime.fromtimestamp(ends_at[-1], TSI).isoformat(timespec="seconds") if ends_at else None})
    rtt = st.events("rtt")
    q_start = [e for e in st.events("quota") if e.get("when") == "start"]
    q_end = [e for e in st.events("quota") if e.get("when") == "end"]
    lags = [e["client_loop_lag"] for e in ends.values() if isinstance(e.get("client_loop_lag"), dict)]
    lag = max(lags, key=lambda x: x.get("p99_ms") or 0) if lags else (st.summary.get("client_loop_lag") or {})
    window = st.window_end(cfg)
    last_deadline = (starts[-1].get("deadline_tsi") if starts else None) or (window.isoformat(timespec="minutes") if window else None)
    waited = sum(float(e.get("waited_for_previous_session_s") or 0) for e in ends.values())
    completed = end_reason == "complete"
    stops = ([{k: v for k, v in e.items() if k not in ("event", "t")} for e in st.events("budget_stop")]
             or list(st.summary.get("budget_stops") or []))
    slow: dict[str, str] = {}                         # model -> the single-stream stage in which it used up its budget
    for x in stops:
        if x.get("reason", "time_budget") == "time_budget" and x.get("stage") == "c1" and x.get("model"):
            slow.setdefault(x["model"], x.get("workload"))
    return {
        "plan_hash": st.plan_hash, "config_hash": first.get("config_hash") or st.meta.get("config_hash"),
        "variants": variants, "end_reason": end_reason, "completed": completed, "partial": not completed,
        "interrupted": end_reason in ("interrupted", "quota"), "deadline_hit": end_reason in ("deadline", "part_limit"),
        "deadline_tsi": last_deadline, "window_end_tsi": window.isoformat(timespec="minutes") if window else None,
        "parts": parts, "resumed": len(parts) > 1,
        "coverage": {"c1": c1, "sweep_cells": {"measured": sum(1 for c in sweeps if c and "skipped" not in c),
                                               "skipped": sum(1 for c in sweeps if c and "skipped" in c)},
                     "ocr_cells": len(ocr), "requests_hit_by_outages": len(hit)},
        "outages": outages, "budget_stops": stops, "slow_models": slow,
        "stage_order": first.get("stage_order") or [*workloads, "sweep"],
        "ocr_probe": ocr, "sweeps": sweeps,
        "rtt": rtt[-1].get("rtt") if rtt else st.summary.get("rtt"),
        "quota_start": q_start[0].get("quota") if q_start else st.summary.get("quota_start"),
        "quota_end": q_end[-1].get("quota") if q_end else st.summary.get("quota_end"),
        "client_loop_lag": lag, "time_budget_s": cfg.get("time_budget_s"),
        "kept_awake": all(e.get("kept_awake") for e in ends.values()) if ends else st.summary.get("kept_awake"),
        "power_start": parts[0]["power_start"] if parts else None, "power_end": parts[-1]["power_end"] if parts else None,
        "waited_for_previous_session_s": round(waited) if waited else None,
        "summarised_tsi": datetime.now(TSI).isoformat(timespec="seconds"),
    }


def close_session(session, cfg: dict, reason: str, st: SessionState | None = None) -> dict:
    """The summary of a FINISHED session (session_status) from what it has — nothing is sent.  Its end reason is the
    last part's own when that part ended normally (complete, deadline, quota, ...), else the reason it is finished
    now (window_over: the last part was ended from outside).  A summary that already says so is kept; a missing or
    stale one (e.g. the process was killed between the part's end record and its summary) is rebuilt — once: the
    scheduled task starting again after the end must not rewrite the manifest every 10 minutes."""
    st = st or load_state(session.dir, session.manifest, records=False)
    if reason == "complete" and st.summary.get("completed"):
        return st.summary
    last = st.last_part_end.get("reason")
    want = last if (last and reason in ("complete", "window_over", "quota")) else reason
    if st.summary and st.summary.get("end_reason") == want:
        return st.summary
    if not st.records and st.has_records:
        st = load_state(session.dir, session.manifest)
    summary = build_summary(st, cfg, want)
    session.register_suite("latency", {"summary": summary})
    return summary


def describe_summary(s: dict) -> str:
    """One line for the log: how far a session got."""
    cov = s.get("coverage") or {}
    c1 = ", ".join(f"{w} {x.get('done')}/{x.get('planned')}" for w, x in (cov.get("c1") or {}).items())
    sw = cov.get("sweep_cells") or {}
    hit = cov.get("requests_hit_by_outages")
    parts = s.get("parts") or []
    slow = ", ".join(f"{m} in '{w}'" for m, w in (s.get("slow_models") or {}).items())
    return (f"{'complete' if s.get('completed') else 'partial (' + str(s.get('end_reason')) + ')'}: single-stream {c1 or '–'}; "
            f"sweep cells measured {sw.get('measured', 0)}, skipped {sw.get('skipped', 0)}; OCR cells "
            f"{cov.get('ocr_cells', 0)}; {len(parts)} part(s)"
            + (f"; {hit} request(s) hit by a network outage (left out)" if hit else "")
            + (f"; time budget used up: {slow} (left out of the later stages)" if slow else ""))


# ============================================================================ runner
class LatencyRunner:
    def __init__(self, ctx: RunContext, variants: list[Variant], cfg: dict, ocr_models: list[str] | None = None):
        self.ctx, self.variants, self.cfg = ctx, variants, cfg
        self.ocr_models = list(ocr_models or [])
        self.budget_stops: list[dict] = []
        self.deadline: float | None = None          # wall clock (time.time()) at which the session stops sending
        self.deadline_hit = False
        self._stopped: set[tuple] = set()
        self.writers: dict[str, ResultWriter] = {}
        self.seq = 0
        self.part = 1
        self.plan_hash = None
        self.config_hash = None
        self.caps: dict[str, Capabilities] = {}
        self.c1_stats: dict[tuple, dict] = {}
        self._c1: dict[tuple, list] = {}
        self.inflight: dict[str, int] = defaultdict(int)
        self.endpoints: list[str] = []
        self.progress = Progress(None)
        net = cfg.get("network_guard") or {}
        self.unit_tries = int(net.get("unit_tries", 3))
        self.cell_tries = int(net.get("cell_tries", 3))
        self.guard = NetGuard(self._probe, interval_s=float(net.get("probe_interval_s", 15)), log=ctx.log,
                              give_up=self._give_up, on_outage=lambda o: self.progress.add("outage", part=self.part, **o),
                              on_back=self._fresh_pools)
        self._retired: list = []
        self.c1_done: set[tuple] = set()             # (workload, variant key, prompt id) with a measurement
        self.c1_spent: dict[tuple, float] = {}       # (workload, model id) -> budget seconds used by earlier parts
        self.carried_stops: set[tuple] = set()       # ("c1", workload, model id) stopped on its budget earlier
        self.slow_models: dict[str, str] = {}        # model id -> the single-stream stage that used up its budget:
                                                     # left out of the session's later stages and sweep cells
        self.slow_noted: set[tuple] = set()          # ("c1", workload, model id) whose skip is in the progress log
        self.stage_order: list[str] | None = None    # fixed by the session's first part
        self.cells_done: set[tuple] = set()          # ("sweep", workload, c, variant key) / ("ocr", model id, c)
        self.slow_before: set[str] = set()
        self.quota_stop = False
        self.left_out = 0                            # requests / cells given up after repeated outages (resumable)
        self.window_epoch: float | None = None       # the session window's end (wall clock); deadline may be earlier
        self._rewarm: set[str] = set()               # models whose next single-stream request follows an outage
        self._prompt_cache: dict[tuple, list] = {}

    def writer(self, v: Variant) -> ResultWriter:
        if v.key not in self.writers:
            self.writers[v.key] = ResultWriter(self.ctx.session.suite_dir("latency") /
                                               f"{safe_name(v.model_id)}__{v.mode}.jsonl")
        return self.writers[v.key]

    def payload(self, v: Variant, messages: list, max_tokens: int, seed: int) -> tuple[dict, dict]:
        spec = self.ctx.reg.model(v.model_id)
        return build_chat_payload(spec, self.caps[v.key], v.mode, messages, max_tokens=max_tokens,
                                  decoding=self.ctx.reg.decoding(self.ctx.opts.profile), seed=seed, stream=True)

    def salt(self, base: str, try_: int) -> str:
        """Cache-busting salt: the first try of part 1 keeps the original one (sessions stay comparable with those
        run before resuming existed); a later part or try gets its own, so nothing ever goes out twice."""
        return base if (self.part <= 1 and try_ == 0) else f"{base}|p{self.part}t{try_}"

    def prompt(self, workload: str, n: int, i: int, base_salt: str, try_: int) -> dict:
        key = (workload, n, self.salt(base_salt, try_))
        if key not in self._prompt_cache:
            self._prompt_cache[key] = build_prompts(self.ctx.session.id, workload, n,
                                                    cache_buster=self.cfg.get("cache_buster", True), salt=key[2])
        return self._prompt_cache[key][i]

    async def _probe(self, endpoint: str) -> tuple[bool, str]:
        """Can this machine reach the endpoint right now?  GET /models on a fresh connection (not the measured
        streams' pool, whose connections may be dead after an outage) — no tokens; any HTTP answer counts."""
        timeout = float((self.cfg.get("network_guard") or {}).get("probe_timeout_s", 15))
        async with ApiClient(self.ctx.reg.endpoints[endpoint], max_connections=2) as c:
            r = await c.get_json("/models", timeout=timeout)
        if r.http_status is not None:
            return True, f"HTTP {r.http_status}"
        return False, f"{r.outcome}: {r.error or ''}".strip()

    def _give_up(self) -> bool:
        return self.ctx.stop.is_set() or self.past_deadline()

    async def ready(self) -> None:
        """Wait while this machine is offline (NetGuard)."""
        await self.guard.ready()

    def _fresh_pools(self) -> None:
        """After an outage: new requests open new connections — the pooled ones may be dead without knowing it
        (a request on one would hang until its timeout).  The old clients are closed when the part ends.  Each
        model's next single-stream request is preceded by one discarded warm-up, so no measured request pays for
        the new connection."""
        clients = getattr(self.ctx, "clients", None)
        for ep in list(self.endpoints):
            old = clients.pop(ep, None) if isinstance(clients, dict) else None
            if old is not None:
                self._retired.append(old)
        self._rewarm = {v.model_id for v in self.variants}

    async def one(self, v: Variant, stage: str, workload: str, conc: int, cell: str, prompt: dict,
                  max_tokens: int, *, key: str | None = None, unit: str | None = None, try_: int = 0) -> ChatResult:
        spec = self.ctx.reg.model(v.model_id)
        self.seq += 1
        key = key or f"{stage}|{workload}|c{conc}|{cell}|p{self.part}|{self.seq}"
        payload, eff = self.payload(v, prompt["messages"], max_tokens, stable_seed(self.ctx.session.id, key))
        client = self.ctx.client(spec.endpoint)
        self.inflight[cell] += 1
        in_flight_at_start = self.inflight[cell]
        t0 = time.perf_counter()
        try:
            res = await client.stream_chat(payload)
        finally:
            self.inflight[cell] -= 1
        outage = await self.guard.check(res.outcome, res.error, t0, time.perf_counter(), spec.endpoint)
        gov = self.ctx.governor(spec.endpoint)
        gov.window.append([time.monotonic(), _weighted(res, gov.cached_weight), -1])   # observe-only accounting
        rec = {"key": key, "final": True, "suite": "latency",
               "session": self.ctx.session.id, "plan_hash": self.plan_hash, "config_hash": self.config_hash,
               "variant": v.key, "model_id": v.model_id, "mode": v.mode, "endpoint": spec.endpoint,
               "api_model": spec.api_model, "stage": stage, "workload": workload, "concurrency": conc, "cell": cell,
               "part": self.part, "try": try_, "unit": unit or cell,
               "in_flight_at_start": in_flight_at_start, "prompt_id": prompt["prompt_id"],
               "item_id": prompt["prompt_id"], "repeat": 0,
               "prompt_chars": sum(len(m["content"]) for m in prompt["messages"]),
               "prompt_words": sum(len(m["content"].split()) for m in prompt["messages"]),
               "effective_params": eff, "result": res.to_record(keep_reasoning=2000),
               "recorded_at": utc_now(), "harness_version": HARNESS_VERSION}
        if outage:
            rec["outage"] = outage
        await self.writer(v).write(rec)
        if stage == "c1" and not outage and res.outcome in USABLE and res.t_end:
            self._c1.setdefault((v.key, workload), []).append((res.t_end, _weighted(res, 1.0)))
        res.outage = outage
        return res

    # ------------------------------------------------------------------ stages
    async def rtt_baseline(self) -> dict:
        out = {}
        for ep in {self.ctx.reg.model(v.model_id).endpoint for v in self.variants}:
            c = self.ctx.client(ep)
            ts = []
            for _ in range(10):
                r = await c.get_json("/models", timeout=20)
                if r.latency is not None and r.outcome == OK:
                    ts.append(r.latency * 1000)
            out[ep] = {"get_models_ms": _summ(ts)}
        return out

    def by_model(self, variants: list[Variant]) -> dict[str, list[Variant]]:
        g: dict[str, list[Variant]] = defaultdict(list)
        for v in variants:
            g[v.model_id].append(v)
        return g

    def budget(self, key: str) -> float | None:
        """Seconds a model's stream may spend in one stage (warmup / chat / rag / long) or a cell (cell); None = no
        limit."""
        b = (self.cfg.get("time_budget_s") or {}).get(key)
        return float(b) if b else None

    def past_deadline(self) -> bool:
        if self.deadline is not None and time.time() > self.deadline:
            if not self.deadline_hit:
                self.ctx.log("[latency] session end reached (session_window / --max-minutes / --until): no new "
                             "requests from now on; requests in flight finish")
            self.deadline_hit = True
        return self.deadline_hit

    def over_budget(self, budget: float | None, t0: float, stage: str, workload: str, who: str, sent: int,
                    planned: int, spent0: float = 0.0, elapsed: float | None = None) -> bool:
        """True = send nothing more (session end or time budget).  Budget time = this part's time in the stage minus
        network outages, plus what earlier parts of the session used (spent0); `elapsed` overrides it."""
        el = elapsed if elapsed is not None else time.perf_counter() - t0 - self.guard.downtime(t0) + spent0
        if self.past_deadline():
            reason = "session_end"
        elif budget and el > budget:
            reason = "time_budget"
        else:
            return False
        if (stage, workload, who) not in self._stopped:
            self._stopped.add((stage, workload, who))
            stop = {"stage": stage, "workload": workload, "model": who, "sent": sent, "planned": planned,
                    "elapsed_s": round(el, 1), "budget_s": budget, "reason": reason}
            self.budget_stops.append(stop)
            self.progress.add("budget_stop", part=self.part, **stop)
            if reason == "time_budget" and stage == "c1":
                self.carried_stops.add(("c1", workload, who))
                self.slow_models.setdefault(who, workload)
            if reason == "time_budget":
                self.ctx.log(f"[latency] time budget: {who} stopped in {stage} '{workload}' after {sent} of {planned} "
                             f"requests ({el:.0f} s > {budget:.0f} s) — nothing more is sent to it in this stage"
                             + (" nor in the later stages of this session" if stage == "c1" else ""))
        return True

    def skip_slow(self, workload: str, n: int) -> None:
        """Note (once per session, in the progress log) that a model which used up a single-stream stage's time
        budget earlier in this session is left out of this workload's stage — single_stream, c1_left and has_work
        leave it out (15 Oct afternoon: chat and rag waited 16 and 18 min for GLM-5.3 at its budget)."""
        pids = prompt_ids(workload, n)
        for mid, vs in self.by_model(self.variants_for(workload)).items():
            first = self.slow_models.get(mid)
            key = ("c1", workload, mid)
            if first is None or key in self.carried_stops or key in self.slow_noted:
                continue
            done, planned = sum((workload, v.key, p) in self.c1_done for v in vs for p in pids), len(pids) * len(vs)
            if done >= planned:
                continue
            self.slow_noted.add(key)
            stop = {"stage": "c1", "workload": workload, "model": mid, "sent": done, "planned": planned,
                    "elapsed_s": None, "budget_s": self.budget(workload), "reason": "slow", "budget_used_in": first}
            self.budget_stops.append(stop)
            self.progress.add("budget_stop", part=self.part, **stop)
            self.ctx.log(f"[latency] {mid}: left out of '{workload}' — it used up its time budget in '{first}' earlier "
                         "in this session")

    async def warmup(self, n: int, variants: list[Variant] | None = None) -> None:
        budget = self.budget("warmup")

        async def per_model(mid: str, vs: list[Variant]):
            t0, sent = time.perf_counter(), 0
            for i in range(n):                      # one stream per model: its modes take turns
                for v in vs:
                    await self.ready()
                    if self.ctx.stop.is_set() or self.over_budget(budget, t0, "warmup", "warmup", mid, sent, n * len(vs)):
                        return
                    p = {"prompt_id": f"warmup-{i}",
                         "messages": [{"role": "user", "content": WARMUP_PROMPTS[i % len(WARMUP_PROMPTS)]}]}
                    await self.one(v, "warmup", "warmup", 1, f"warmup-{safe_name(v.model_id)}", p,
                                   self.cfg.get("warmup_max_tokens", 64), key=f"warmup|{v.key}|p{self.part}|{i}")
                    sent += 1
        await asyncio.gather(*(per_model(m, vs) for m, vs in self.by_model(variants or self.variants).items()))

    def variants_for(self, workload: str) -> list[Variant]:
        mw = self.cfg.get("mode_workloads") or {}
        return [v for v in self.variants if v.mode not in mw or workload in mw[v.mode]]

    def c1_left(self, workload: str, n: int) -> list[tuple]:
        """Single-stream requests of a workload still to send in this session (a model stopped on its time budget
        has none — in that stage and in every later one)."""
        pids = prompt_ids(workload, n)
        return [(v.key, pid) for v in self.variants_for(workload) for pid in pids
                if (workload, v.key, pid) not in self.c1_done and ("c1", workload, v.model_id) not in self.carried_stops
                and v.model_id not in self.slow_models]

    async def c1_unit(self, v: Variant, workload: str, n: int, i: int, pid: str, max_tokens: int) -> None:
        """One single-stream measurement; hit by a network outage → sent again (fresh tag) once the endpoint answers."""
        cell = f"c1-{workload}-{safe_name(v.model_id)}"
        if v.model_id in self._rewarm:                  # first request on this model after an outage: new connection
            self._rewarm.discard(v.model_id)
            await self.one(v, "warmup", "rewarm", 1, f"warmup-{safe_name(v.model_id)}",
                           {"prompt_id": "rewarm", "messages": [{"role": "user", "content": WARMUP_PROMPTS[0]}]},
                           self.cfg.get("warmup_max_tokens", 64),
                           key=f"warmup|{v.key}|p{self.part}|rewarm{len(self.guard.outages)}")
            await self.ready()
            if self.ctx.stop.is_set() or self.past_deadline():
                return
        for t in range(self.unit_tries):
            res = await self.one(v, "c1", workload, 1, cell, self.prompt(workload, n, i, v.key, t), max_tokens,
                                 key=f"c1|{workload}|{v.key}|{pid}|p{self.part}t{t}", unit=f"{workload}|{v.key}|{pid}",
                                 try_=t)
            if not getattr(res, "outage", None):
                self.c1_done.add((workload, v.key, pid))
                return
            await self.ready()
            if self.ctx.stop.is_set() or self.past_deadline():
                return
        self.left_out += 1
        self.ctx.log(f"[latency] {v.key} '{workload}' {pid}: hit by network outages {self.unit_tries} times — left for "
                     "the next start of this session")

    async def single_stream(self, workload: str, n: int, max_tokens: int, schedule: str) -> None:
        variants = self.variants_for(workload)
        pids = prompt_ids(workload, n)
        n = len(pids)                                   # each prompt of the subset once (a unit = workload, variant, prompt)
        budget = self.budget(workload)
        groups = self.by_model(variants)
        done0 = {m: sum((workload, v.key, p) in self.c1_done for v in vs for p in pids) for m, vs in groups.items()}
        self.skip_slow(workload, n)
        if schedule == "interleaved":
            rng = random.Random(stable_seed(self.ctx.session.id, "c1", workload))
            spent: dict[str, float] = defaultdict(float, {m: self.c1_spent.get((workload, m), 0.0) for m in groups})
            sent: dict[str, int] = defaultdict(int, done0)
            stopped: set[str] = {m for m in groups if ("c1", workload, m) in self.carried_stops or m in self.slow_models}
            planned = {m: n * len(vs) for m, vs in groups.items()}
            for i in range(n):
                order = list(variants)
                rng.shuffle(order)
                for v in order:
                    if (workload, v.key, pids[i]) in self.c1_done or v.model_id in stopped:
                        continue
                    await self.ready()
                    if self.ctx.stop.is_set() or self.past_deadline():
                        return
                    if budget and spent[v.model_id] > budget:
                        stopped.add(v.model_id)
                        self.over_budget(budget, 0.0, "c1", workload, v.model_id, sent[v.model_id],
                                         planned[v.model_id], elapsed=spent[v.model_id])
                        continue
                    t = time.perf_counter()
                    await self.c1_unit(v, workload, n, i, pids[i], max_tokens)
                    t1 = time.perf_counter()
                    spent[v.model_id] += (t1 - t) - self.guard.downtime(t, t1)
                    sent[v.model_id] += 1
        else:
            async def per_model(mid: str, vs: list[Variant]):
                if ("c1", workload, mid) in self.carried_stops or mid in self.slow_models:
                    return
                t0, sent, spent0 = time.perf_counter(), done0[mid], self.c1_spent.get((workload, mid), 0.0)
                for i in range(n):
                    k = i % len(vs)
                    for v in vs[k:] + vs[:k]:      # rotate the mode order so no mode always goes first
                        if (workload, v.key, pids[i]) in self.c1_done:
                            continue
                        await self.ready()
                        if self.ctx.stop.is_set() or self.over_budget(budget, t0, "c1", workload, mid, sent,
                                                                      n * len(vs), spent0):
                            return
                        await self.c1_unit(v, workload, n, i, pids[i], max_tokens)
                        sent += 1
            await asyncio.gather(*(per_model(m, vs) for m, vs in groups.items()))

    async def concurrency_cell(self, v: Variant, workload: str, conc: int, n_prompts: int, max_tokens: int,
                               n_requests: int, try_: int = 0) -> dict:
        cell = f"c{conc}-{workload}-{safe_name(v.key)}"
        # every request in the cell gets its own cache-busting tag / chunk order
        reqs = build_prompts(self.ctx.session.id, workload, n_prompts, cache_buster=True, count=n_requests,
                             salt=self.salt(cell, try_))
        queue: asyncio.Queue = asyncio.Queue()
        for i, r in enumerate(reqs):
            queue.put_nowait((i, r))
        outcomes: dict[str, int] = {}
        t0 = time.perf_counter()
        spec = self.ctx.reg.model(v.model_id)
        budget, stop_cell, hit = self.budget("cell"), asyncio.Event(), asyncio.Event()

        async def worker():
            while not queue.empty() and not self.ctx.stop.is_set() and not stop_cell.is_set() and not hit.is_set():
                if self.over_budget(budget, t0, "sweep", f"{workload} c{conc}", v.key, sum(outcomes.values()), n_requests):
                    stop_cell.set()
                    return
                i, p = queue.get_nowait()
                res = await self.one(v, "sweep", workload, conc, cell, p, max_tokens,
                                     key=f"sweep|{workload}|c{conc}|{v.key}|p{self.part}t{try_}|{i}", unit=cell, try_=try_)
                if getattr(res, "outage", None):
                    hit.set()                       # a cell is one contiguous measurement: measured again afterwards
                    continue
                outcomes[res.outcome] = outcomes.get(res.outcome, 0) + 1
                if res.outcome == "http_429_quota" and await self.ctx.quota_really_exhausted(spec.endpoint):
                    self.quota_stop = True
                    self.ctx.stop.set()

        await asyncio.gather(*(worker() for _ in range(conc)))
        out = {"cell": cell, "seconds": round(time.perf_counter() - t0, 2), "outcomes": outcomes}
        if try_ or self.part > 1:
            out.update(part=self.part, attempt=try_)
        if stop_cell.is_set():
            out["session_end" if self.deadline_hit else "budget_stop"] = True
        if hit.is_set():
            out["outage"] = True
        return out

    async def sweeps(self, workloads: list[str], n: int, maxt: dict) -> list[dict]:
        """The concurrency sweep: one model at a time per level, randomised order.  A model whose cell stopped on its
        time budget (it is that slow right now: 14 Oct, GLM-5.3 at 1.7 tokens/s) is not swept at the higher levels
        of this session — each would only hit the same budget and take the other models' time before the session
        end; nor is a model that used up a single-stream stage's budget in this session (slow_models).  The summary
        lists those cells as skipped.  Cells finished by an earlier part are not repeated; a cell hit by a network
        outage is measured again from its start once the endpoint answers (cell_tries)."""
        ctx, cfg = self.ctx, self.cfg
        out: list[dict] = []
        for (vk, w), durs in self._c1.items():
            if durs:
                self.c1_stats[(vk, w)] = {"e2e_p50": statistics.median(d[0] for d in durs),
                                          "tokens_p50": statistics.median(d[1] for d in durs)}
        slow: set[str] = set(self.slow_before)
        levels = [c for c in cfg.get("concurrency_levels", [2, 4, 8, 16]) if c > 1]
        sweep_modes = set(cfg.get("sweep_modes", ["default"]))

        def done(r: dict, w: str, c: int, v: Variant) -> None:
            out.append(r)
            self.cells_done.add(("sweep", w, c, v.key))
            self.progress.add("cell_done", kind="sweep", key=[w, c, v.key], part=self.part, result=r)

        for w in [x for x in cfg.get("sweep_workloads", ["rag"]) if x in workloads]:
            for c in levels:
                cand = [v for v in self.variants_for(w) if v.mode in sweep_modes]
                order = stable_shuffle(cand, ctx.session.id, "sweep-order", w, c)
                for v in order:
                    if ctx.stop.is_set() or self.past_deadline():
                        break
                    if ("sweep", w, c, v.key) in self.cells_done:
                        continue
                    base = {"variant": v.key, "workload": w, "concurrency": c}
                    if v.model_id in self.slow_models:
                        first = self.slow_models[v.model_id]
                        done({**base, "skipped": f"its single-stream stage '{first}' used up its time budget in this "
                                                 "session", "slow_after": first}, w, c, v)
                        continue
                    if v.key in slow:
                        done({**base, "skipped": "a lower concurrency level of this session hit its time budget"}, w, c, v)
                        continue
                    ep = ctx.reg.endpoints[ctx.reg.model(v.model_id).endpoint]
                    cap = (ep.limits or {}).get("max_concurrency")
                    if cap and c > int(cap):
                        done({**base, "skipped": f"provider allows {cap} concurrent requests"}, w, c, v)
                        continue
                    tpm = (ep.limits or {}).get("tpm")
                    st = self.c1_stats.get((v.key, w))
                    nreq = max(cfg.get("sweep_min_requests", 8), cfg.get("sweep_requests_per_stream", 2) * c)
                    if tpm and st:
                        predicted = c * st["tokens_p50"] * 60.0 / max(0.5, st["e2e_p50"])
                        if predicted > tpm * cfg.get("tpm_fraction", 0.8):
                            ctx.log(f"[latency] skip {v.key} c={c}: predicted {predicted:,.0f} TPM > cap")
                            done({**base, "skipped": "tpm_cap", "predicted_tpm": int(predicted)}, w, c, v)
                            continue
                        gov = ctx.governor(ep.name)
                        need = st["tokens_p50"] * nreq
                        while gov.budget and gov.used() + min(need, gov.budget * 0.5) > gov.budget:
                            await asyncio.sleep(2.0)   # pause BETWEEN cells only
                    r, used = None, 0
                    for t in range(self.cell_tries):
                        await self.ready()
                        if ctx.stop.is_set() or self.past_deadline():
                            break
                        r, used = await self.concurrency_cell(v, w, c, n, maxt[w], nreq, try_=t), t + 1
                        r.update(base)
                        if not r.get("outage"):
                            break
                        ctx.log(f"[latency] {v.key} c={c} {w}: a network outage hit this cell — it is measured again "
                                "from its start once the endpoint answers")
                    if r is None:
                        break
                    ctx.log(f"[latency] {v.key} c={c} {w}: {r['outcomes']} in {r['seconds']}s")
                    if r.get("outage") and used == self.cell_tries and not (ctx.stop.is_set() or self.past_deadline()):
                        self.left_out += 1           # hit on every try: left for the next start of the session
                        ctx.log(f"[latency] {v.key} c={c} {w}: hit by network outages {used} times — left for the "
                                "next start of this session")
                        out.append({**r, "outage_gave_up": used})
                        continue
                    if r.get("session_end") or r.get("outage") or ctx.stop.is_set():
                        out.append(r)                # cut short: a later part (inside the window) measures it again
                        continue
                    done(r, w, c, v)
                    if r.get("budget_stop"):
                        slow.add(v.key)
                        ctx.log(f"[latency] {v.key} hit its time budget at c={c}: its higher levels are skipped")
                    rl = sum(k for o, k in r["outcomes"].items() if o.startswith("http_429"))
                    await asyncio.sleep(60 if rl else cfg.get("cooldown_s", 3))   # let a TPM window clear
        return out

    # ------------------------------------------------------------------ OCR timing
    async def ocr_probe(self) -> list[dict]:
        pc = self.cfg.get("ocr_probe") or {}
        pages = ocr_probe_pages(int(pc.get("pages", 10)))
        if not pages:
            self.ctx.log("[latency] OCR timing skipped: no OCR pages (python scripts/build_ocr_pages.py)")
            return [{"skipped": "no OCR pages"}]
        levels = [int(c) for c in pc.get("concurrency_levels", [1, 4])]
        per_worker = int(pc.get("requests_per_worker", 3))
        out = []
        for mid in self.ocr_models:
            spec = self.ctx.reg.model(mid)
            w = ResultWriter(self.ctx.session.suite_dir("latency_ocr") / f"{safe_name(mid)}.jsonl")
            for c in levels:
                if self.ctx.stop.is_set() or self.past_deadline():
                    break
                if ("ocr", mid, c) in self.cells_done:
                    continue
                n_req = len(pages) if c <= 1 else max(len(pages), c * per_worker)
                r, used = None, 0
                for t in range(self.cell_tries):
                    await self.ready()
                    if self.ctx.stop.is_set() or self.past_deadline():
                        break
                    r, used = await self.ocr_cell(mid, spec, w, pages, c, n_req, try_=t), t + 1
                    r.update({"model": mid, "concurrency": c, "pages": [p["id"] for p in pages]})
                    if not r.get("outage"):
                        break
                    self.ctx.log(f"[latency] OCR {mid} c={c}: a network outage hit this cell — measured again")
                if r is None:
                    break
                out.append(r)
                self.ctx.log(f"[latency] OCR {mid} c={c}: {r['outcomes']} in {r['seconds']}s")
                if r.get("outage") and used == self.cell_tries and not (self.ctx.stop.is_set() or self.past_deadline()):
                    self.left_out += 1
                    r["outage_gave_up"] = used
                elif not (r.get("session_end") or r.get("outage") or self.ctx.stop.is_set()):
                    self.cells_done.add(("ocr", mid, c))
                    self.progress.add("cell_done", kind="ocr", key=[mid, c], part=self.part, result=r)
                await asyncio.sleep(self.cfg.get("cooldown_s", 3))
        return out

    async def ocr_cell(self, mid: str, spec, w: ResultWriter, pages: list[dict], conc: int, n_req: int,
                       try_: int = 0) -> dict:
        from ..scoring.basic import score_ocr
        base = DATA_DIR / "ocr"
        cell = f"ocr-c{conc}-{safe_name(mid)}"
        queue: asyncio.Queue = asyncio.Queue()
        for i in range(n_req):
            queue.put_nowait((i, pages[i % len(pages)]))
        client = self.ctx.client(spec.endpoint)
        extra = (spec.options or {}).get("extra")
        budget, stop_cell, hit = self.budget("cell"), asyncio.Event(), asyncio.Event()
        outcomes: dict[str, int] = {}
        t0 = time.perf_counter()
        nonce = f"{self.ctx.session.id}-{cell}" + ("" if (self.part <= 1 and try_ == 0) else f"-p{self.part}t{try_}")

        async def worker():
            while not queue.empty() and not self.ctx.stop.is_set() and not stop_cell.is_set() and not hit.is_set():
                if self.over_budget(budget, t0, "ocr", f"ocr c{conc}", mid, sum(outcomes.values()), n_req):
                    stop_cell.set()
                    return
                i, page = queue.get_nowait()
                img = base / page["image"]
                raw = cache_busted_image(img, f"{nonce}-{i}")
                mime = mimetypes.guess_type(str(img))[0] or "image/png"
                durl = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
                self.inflight[cell] += 1
                in_flight_at_start = self.inflight[cell]
                ts = time.perf_counter()
                try:
                    jr = await client.ocr(spec.api_model, durl, extra=extra)
                finally:
                    self.inflight[cell] -= 1
                outage = await self.guard.check(jr.outcome, jr.error, ts, time.perf_counter(), spec.endpoint)
                text = (jr.data or {}).get("text", "") if jr.outcome == OK and isinstance(jr.data, dict) else ""
                score = None
                if jr.outcome == OK:
                    sc = score_ocr((base / page["gt"]).read_text(encoding="utf-8"), text)
                    score = {k: sc.get(k) for k in ("cer_flex", "cer", "wer")}
                if outage:
                    hit.set()
                else:
                    outcomes[jr.outcome] = outcomes.get(jr.outcome, 0) + 1
                rec = {"key": f"ocr|c{conc}|{mid}|p{self.part}t{try_}|{i}", "final": True, "suite": "latency_ocr",
                       "session": self.ctx.session.id, "plan_hash": self.plan_hash,
                       "config_hash": self.config_hash, "variant": mid, "model_id": mid,
                       "endpoint": spec.endpoint, "api_model": spec.api_model, "stage": "ocr",
                       "workload": "ocr", "concurrency": conc, "cell": cell, "part": self.part, "try": try_,
                       "unit": cell, "in_flight_at_start": in_flight_at_start, "item_id": page["id"], "repeat": 0,
                       "category": page.get("category"), "image_bytes": len(raw),
                       "result": {"outcome": jr.outcome, "error": jr.error, "http_status": jr.http_status,
                                  "latency": jr.latency, "wall_start": jr.wall_start, "chars": len(text),
                                  "text": text},
                       "score": score, "recorded_at": utc_now(), "harness_version": HARNESS_VERSION}
                if outage:
                    rec["outage"] = outage
                await w.write(rec)

        await asyncio.gather(*(worker() for _ in range(conc)))
        out = {"cell": cell, "seconds": round(time.perf_counter() - t0, 2), "outcomes": outcomes}
        if try_ or self.part > 1:
            out.update(part=self.part, attempt=try_)
        if stop_cell.is_set():
            out["session_end" if self.deadline_hit else "budget_stop"] = True
        if hit.is_set():
            out["outage"] = True
        return out

    # ------------------------------------------------------------------ resume
    def adopt(self, st: SessionState) -> None:
        """Carry over what earlier parts of this session finished (records + progress log)."""
        for r in st.records:
            if r.get("stage") != "c1" or record_outage(r):
                continue
            res = r.get("result") or {}
            self.c1_done.add((r.get("workload"), r.get("variant"), r.get("prompt_id")))
            k = (r.get("workload"), r.get("model_id"))
            self.c1_spent[k] = self.c1_spent.get(k, 0.0) + float(res.get("t_end") or 0.0)
            if res.get("outcome") in USABLE and res.get("t_end"):
                self._c1.setdefault((r.get("variant"), r.get("workload")), []).append(
                    (res["t_end"], weighted_tokens(res.get("usage"), 1.0)))
        for e in st.events("budget_stop"):
            if e.get("reason", "time_budget") == "time_budget" and e.get("stage") == "c1":
                self.carried_stops.add(("c1", e.get("workload"), e.get("model")))
                self.slow_models.setdefault(e.get("model"), e.get("workload"))
            elif e.get("reason") == "slow":
                self.slow_noted.add((e.get("stage"), e.get("workload"), e.get("model")))
        for e in st.events("cell_done"):
            k = tuple(e.get("key") or ())
            if e.get("kind") == "sweep" and len(k) == 3:
                self.cells_done.add(("sweep", k[0], int(k[1]), k[2]))
                if (e.get("result") or {}).get("budget_stop"):
                    self.slow_before.add(k[2])
            elif e.get("kind") == "ocr" and len(k) == 2:
                self.cells_done.add(("ocr", k[0], int(k[1])))

    def has_work(self, v: Variant, workloads: list[str], n: int) -> bool:
        if v.model_id in self.slow_models:           # left out of the rest of the session: no warm-up either
            return False
        for w in workloads:
            if v in self.variants_for(w) and ("c1", w, v.model_id) not in self.carried_stops and any(
                    (w, v.key, p) not in self.c1_done for p in prompt_ids(w, n)):
                return True
        levels = [c for c in self.cfg.get("concurrency_levels", [2, 4, 8, 16]) if c > 1]
        return v.mode in set(self.cfg.get("sweep_modes", ["default"])) and any(
            ("sweep", w, c, v.key) not in self.cells_done and v.key not in self.slow_before
            for w in self.cfg.get("sweep_workloads", ["rag"]) if w in workloads and v in self.variants_for(w)
            for c in levels)

    def end_reason(self) -> str:
        """quota / interrupted (Ctrl+C) / deadline (the window's end) / part_limit (--max-minutes before it) /
        incomplete (requests or cells given up after repeated outages: the next start sends them) / complete."""
        if self.quota_stop:
            return "quota"
        if self.ctx.stop.is_set():
            return "interrupted"
        if self.deadline_hit:
            return "part_limit" if (self.window_epoch and time.time() < self.window_epoch - 1) else "deadline"
        if self.left_out:
            return "incomplete"
        return "complete"

    def order_for(self, st: SessionState, workloads: list[str], now: datetime, window: datetime | None) -> list[str]:
        """The session's stage order, fixed by its first part (a later part keeps it; a session begun before 16 Oct:
        the default).  A window of at most early_sweep_below_minutes runs the sweep before the single-stream stages
        that follow the workloads it sweeps."""
        sw = self.cfg.get("sweep_workloads", ["rag"])
        default = stage_order(workloads, sw)
        starts = st.events("part_start")
        if starts:
            kept = starts[0].get("stage_order")
            return list(kept) if kept and sorted(kept) == sorted(default) else default
        limit = self.cfg.get("early_sweep_below_minutes")
        short = bool(limit and window and (window - now).total_seconds() / 60 <= float(limit))
        return stage_order(workloads, sw, early=short)

    # ------------------------------------------------------------------ session
    async def run(self) -> dict:
        ctx, cfg = self.ctx, self.cfg
        workloads = cfg.get("workloads", ["chat", "rag", "long"])
        n = cfg.get("prompts_per_workload", 20)
        maxt = cfg.get("max_tokens", {"chat": 2048, "rag": 4096, "long": 8192})
        st = load_state(ctx.session.dir, ctx.session.manifest, records=False)
        keep = []
        for v in self.variants:
            self.caps[v.key] = load_caps(ctx.reg, v.model_id)
            refusal = (unprobed_refusal(ctx, self.caps[v.key], v.key, "latency")
                       or unavailable_refusal(self.caps[v.key], v.key, "latency"))
            if refusal:
                ctx.log(refusal)
            elif self.caps[v.key].mode(v.mode) is not None:
                keep.append(v)
        self.config_hash, cdoc = latency_config_hash(ctx.reg, cfg, ctx.opts.profile, workloads, n)
        reason, msg = session_status(st, cfg, config_hash=self.config_hash)
        if reason:                                    # nothing more to send (run.py normally stops before this)
            ctx.log(msg)
            return close_session(ctx.session, cfg, reason, st)
        endpoints = sorted({ctx.reg.model(v.model_id).endpoint for v in keep}
                           | {ctx.reg.model(m).endpoint for m in self.ocr_models})
        # the lock first: nothing of this part is written before it is held (the same session running in another
        # process -> SessionBusy, and that process's progress and summary stay untouched); then the state again, as
        # it is now
        lock = LatencyLock(ctx.session.id, endpoints, scope=self.cfg.get("lock_scope", "machine"),
                           wait_s=float(cfg.get("lock_wait_s", 1500)), log=ctx.log,
                           mirror=ctx.session.dir / "latency_heartbeat.json")
        async with lock:
            st = load_state(ctx.session.dir, ctx.session.manifest)
            reason, msg = session_status(st, cfg, config_hash=self.config_hash)
            if reason:
                ctx.log(msg)
                return close_session(ctx.session, cfg, reason, st)
            return await self._run_part(st, msg, keep, cdoc, workloads, n, maxt, endpoints, lock)

    async def _run_part(self, st: SessionState, msg: str, keep: list[Variant], cdoc: dict, workloads: list[str],
                        n: int, maxt: dict, endpoints: list[str], lock: LatencyLock) -> dict:
        ctx, cfg = self.ctx, self.cfg
        if st.n_parts and st.variants:
            new = [v.key for v in keep if v.key not in st.variants]
            gone = [k for k in st.variants if k not in {v.key for v in keep}]
            keep = [v for v in keep if v.key in st.variants]
            if new:
                ctx.log(f"[latency] not in this session (it started without them): {', '.join(new)}")
            if gone:
                ctx.log(f"[latency] not runnable now, left out of this part: {', '.join(gone)}")
        self.variants = keep
        if not self.variants:
            raise RuntimeError("no runnable variants (probe the models first)")
        self.plan_hash = st.plan_hash or sha256_obj({"session": ctx.session.id, "config": cdoc,
                                                     "variants": [v.key for v in self.variants]})
        ctx.session.write_plan("latency", [{"workload": w, "prompt_id": pid, "item_id": pid}
                                           for w in workloads for pid in cdoc["prompt_ids"][w]],
                               meta={"config_hash": self.config_hash, "config": cdoc,
                                     "variants": st.variants or [v.key for v in self.variants], "limit": None})
        self.progress = Progress(ctx.session.dir / "latency_progress.jsonl")
        self.adopt(st)
        self.part = st.n_parts + 1
        now = datetime.now(TSI)
        until = parse_tsi(cfg.get("until_tsi"))
        window = st.window_end(cfg) if st.n_parts else session_deadline(cfg, now)
        window = min(x for x in (window, until) if x) if (window or until) else None
        mm = cfg.get("max_minutes")
        end = min(x for x in (window, now + timedelta(minutes=float(mm)) if mm else None) if x) if (window or mm) else None
        self.deadline = end.timestamp() if end else None
        self.window_epoch = window.timestamp() if window else None
        self.stage_order = self.order_for(st, workloads, now, window)
        if st.n_parts:
            ctx.log(msg)
        if end is not None:
            ctx.log(f"[latency] this session stops sending at {end:%H:%M} TSİ at the latest")
        later = self.stage_order[self.stage_order.index("sweep") + 1:]
        if later:
            ctx.log(f"[latency] stage order: {' → '.join(self.stage_order)} — the sweep runs before "
                    + ", ".join(f"'{x}'" for x in later))
        self.endpoints = endpoints
        self.progress.add("part_start", part=self.part, started_tsi=now.isoformat(timespec="seconds"), pid=os.getpid(),
                          window_end_tsi=window.isoformat(timespec="minutes") if window else None,
                          deadline_tsi=end.isoformat(timespec="minutes") if end else None,
                          power_start=power_status(), plan_hash=self.plan_hash, config_hash=self.config_hash,
                          variants=[v.key for v in self.variants], stage_order=self.stage_order,
                          code_sha256=code_hash())
        lag = LoopLag()
        part_end: dict[str, Any] = {}
        if lock.waited_s:
            part_end["waited_for_previous_session_s"] = round(lock.waited_s)
        reason = "error"
        try:
            with KeepAwake() as awake:
                part_end["kept_awake"] = awake.ok
                reason = await self._measure(ctx, cfg, workloads, n, maxt, lag, part_end)
        except (KeyboardInterrupt, asyncio.CancelledError):
            reason = "interrupted"
            raise
        except Exception as exc:
            reason = f"error: {type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            for c in self._retired:
                with contextlib.suppress(Exception):       # closing a dead pool must not mask anything
                    await c.aclose()
            self.progress.add("part_end", part=self.part, ended_tsi=datetime.now(TSI).isoformat(timespec="seconds"),
                              reason=reason, power_end=power_status(), outages=len(self.guard.outages), **part_end)
            try:
                summary = build_summary(load_state(ctx.session.dir, ctx.session.manifest), cfg, reason)
                ctx.session.register_suite("latency", {"summary": summary})
            except Exception as exc:                       # noqa: BLE001 — the records and the progress log are kept
                ctx.log(f"!! [latency] the session summary could not be written ({type(exc).__name__}: {exc}); "
                        "it is written when the session is started again")
                summary = {"end_reason": reason}
        if (summary.get("client_loop_lag") or {}).get("p99_ms", 0) > 50:
            ctx.log("!! WARNING: client event loop lag p99 > 50 ms — this machine distorted the timings")
        return summary

    async def _measure(self, ctx, cfg, workloads, n, maxt, lag, part_end: dict) -> str:
        lag.start()
        try:
            for ep in self.endpoints:
                await self.guard.ensure_online(ep)
            if self._give_up():
                return self.end_reason()
            self.progress.add("rtt", part=self.part, rtt=await self.rtt_baseline())
            await self._quota("start")
            levels = [int(c) for c in (cfg.get("ocr_probe") or {}).get("concurrency_levels", [1, 4])]
            ocr_left = [m for m in self.ocr_models if any(("ocr", m, c) not in self.cells_done for c in levels)]
            if ocr_left and not ctx.stop.is_set():
                ctx.log(f"[latency] OCR timing: {', '.join(ocr_left)}")
                await self.ocr_probe()
            if self.part == 1:
                wv, nw = self.variants, cfg.get("warmup_requests", 20)
            else:
                wv = [v for v in self.variants if self.has_work(v, workloads, n)]
                nw = cfg.get("warmup_requests_resume", 5)
            if wv and not self._give_up():
                ctx.log(f"[latency] warm-up: {nw} requests per variant (discarded)"
                        + ("" if self.part == 1 else f" — {len(wv)} variant(s) with requests left"))
                await self.warmup(nw, wv)
            for stage in self.stage_order or stage_order(workloads, cfg.get("sweep_workloads", ["rag"])):
                if self._give_up():
                    break
                if stage == "sweep":
                    await self.sweeps(workloads, n, maxt)
                    continue
                w = stage
                self.skip_slow(w, n)
                left = self.c1_left(w, n)
                if not left:
                    continue
                nv = len(self.variants_for(w))
                ctx.log(f"[latency] single-stream '{w}': {n} prompts × {nv} variants, one stream per model "
                        f"({cfg.get('schedule', 'parallel')})"
                        + ("" if len(left) == n * nv else f" — {len(left)} still to send"))
                await self.single_stream(w, n, maxt[w], cfg.get("schedule", "parallel"))
            if not self.guard.offline:
                await self._quota("end")
        finally:
            part_end["client_loop_lag"] = await lag.stop()
        return self.end_reason()

    async def _quota(self, when: str) -> None:
        q = {}
        for ep in self.endpoints:
            qi = await read_quota(self.ctx.client(ep))
            q[ep] = qi.__dict__ if qi else None
        self.progress.add("quota", part=self.part, when=when, quota=q)
        left = [f"{ep}: {v['daily_remaining']:,}" for ep, v in q.items() if v and v.get("daily_remaining") is not None]
        if left:
            self.ctx.log(f"[latency] daily quota left at the {when} of this part — " + ", ".join(left) + " tokens")


def _weighted(res: ChatResult, cached_weight: float) -> int:
    return weighted_tokens(res.usage, cached_weight) if res.usage else 0
