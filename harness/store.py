"""Sessions, manifests, hashing and resumable JSONL result storage.

Layout (one directory per session = one run at one time of day):

  results/<track>/<endpoint>/<session_id>/
      manifest.json                    code hash, data hashes, config snapshot, quota, RTT, timings
      plans/<suite>.jsonl              the exact request plan (identical for every system under test)
      <suite>/<model>__<mode>.jsonl    one record per (item, repeat) — append-only, resumable

Every record carries the plan hash so the analysis can refuse to compare runs
that did not see identical inputs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import random
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import CONFIG_DIR, DATA_DIR, RESULTS_DIR, ROOT
from .quota import TSI

HARNESS_VERSION = "0.1.0"


# ----------------------------------------------------------------------------- hashing
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_obj(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


def tree_hash(paths: Iterable[Path], base: Path = ROOT) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        try:
            rel = p.resolve().relative_to(base.resolve())
        except ValueError:
            rel = Path(p.name)
        h.update(str(rel).replace("\\", "/").encode())
        h.update(sha256_file(p).encode())
    return h.hexdigest()


def code_hash() -> str:
    files = list((ROOT / "harness").rglob("*.py")) + list((ROOT / "prompts").rglob("*.md"))
    return tree_hash(files)


def data_hash(subdir: str) -> str:
    d = DATA_DIR / subdir
    files = [p for p in d.rglob("*") if p.is_file() and p.suffix in (".jsonl", ".json", ".md", ".txt", ".csv")
             and "_cache" not in p.parts]
    return tree_hash(files, base=DATA_DIR) if files else "missing"


def stable_seed(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def stable_shuffle(items: list, *parts: Any) -> list:
    out = list(items)
    random.Random(stable_seed(*parts)).shuffle(out)
    return out


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", s)


def replace_retry(src: Path, dst: Path, attempts: int = 40, delay: float = 0.05) -> None:
    """os.replace that survives Windows sharing violations (another process briefly has the target open)."""
    import time
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


# ----------------------------------------------------------------------------- session
def time_slot(dt: datetime) -> str:
    h = dt.astimezone(TSI).hour
    return "night" if h < 6 else "morning" if h < 12 else "afternoon" if h < 18 else "evening"


class Session:
    def __init__(self, track: str, endpoint: str, session_id: str | None = None, *, create: bool = True):
        now = datetime.now(timezone.utc)
        if session_id is None:
            session_id = f"{now.astimezone(TSI):%Y%m%d-%H%M}-{time_slot(now)}-{secrets.token_hex(2)}"
        self.id = session_id
        self.track = track
        self.endpoint = endpoint
        self.dir = RESULTS_DIR / f"track-{track}" / endpoint / session_id
        if not create and not (self.dir / "manifest.json").exists():
            raise FileNotFoundError(f"session {self.dir} does not exist")
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "plans").mkdir(exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        self._own_suites: set[str] = set()
        self._own_events: list[dict] = []
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        else:
            self.manifest = {
                "session_id": self.id, "track": track, "endpoint": endpoint,
                "created_utc": now.isoformat(timespec="seconds"),
                "created_tsi": now.astimezone(TSI).isoformat(timespec="seconds"),
                "time_slot_tsi": time_slot(now), "harness_version": HARNESS_VERSION,
                "environment": {"python": sys.version.split()[0], "platform": platform.platform(),
                                "machine": platform.machine(), "hostname_hash": sha256_bytes(platform.node().encode())[:12]},
                "suites": {}, "events": []}
            self.save()

    @property
    def seed(self) -> int:
        return stable_seed("session", self.id)

    def save(self) -> None:
        """Write the manifest.  Several processes may work in one session (e.g. two suites in two terminals), so
        the copy on disk is re-read first and only this process's own changes (suites it registered, events it
        added) are laid over it — nobody's registrations are lost."""
        disk = None
        if self.manifest_path.exists():
            try:
                disk = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                disk = None
        if disk:
            merged = disk
            suites = dict(disk.get("suites") or {})
            for k in self._own_suites:
                suites[k] = self.manifest["suites"][k]
            merged["suites"] = suites
            seen = {json.dumps(e, sort_keys=True, ensure_ascii=False) for e in merged.get("events") or []}
            merged["events"] = list(merged.get("events") or []) + [
                e for e in self._own_events if json.dumps(e, sort_keys=True, ensure_ascii=False) not in seen]
            self.manifest = merged
        tmp = self.manifest_path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_retry(tmp, self.manifest_path)

    def event(self, kind: str, **data) -> None:
        e = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **data}
        self.manifest["events"].append(e)
        self._own_events.append(e)
        self.save()

    def write_plan(self, suite: str, items: list[dict], meta: dict | None = None) -> str:
        """Store the exact request plan (atomically) and its metadata; refuse a changed plan on resume.

        meta (plans/<suite>.meta.json) records how the plan was built — above all `limit`: a plan built with
        --limit is a smoke/partial plan and is excluded from comparisons unless explicitly included."""
        p = self.dir / "plans" / f"{suite}.jsonl"
        h = sha256_obj(items)
        if p.exists():
            old = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            if sha256_obj(old) != h:
                raise RuntimeError(
                    f"plan for suite '{suite}' differs from the one already stored in session {self.id}. "
                    "Data, --limit, --repeats or configuration changed mid-session — start a new session instead of resuming.")
        else:
            tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for it in items:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
            replace_retry(tmp, p)
        mp = self.dir / "plans" / f"{suite}.meta.json"
        if meta is not None or not mp.exists():
            item_ids = {it.get("item_id", it.get("uid")) for it in items if isinstance(it, dict)}
            doc = {"plan": suite, "plan_hash": h, "n_requests": len(items), "n_items": len(item_ids),
                   "written_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **(meta or {})}
            if mp.exists():
                prev = json.loads(mp.read_text(encoding="utf-8"))
                doc["written_utc"] = prev.get("written_utc", doc["written_utc"])
            tmp = mp.parent / f"{mp.name}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            replace_retry(tmp, mp)
        return h

    def suite_dir(self, suite: str) -> Path:
        d = self.dir / suite
        d.mkdir(parents=True, exist_ok=True)
        return d

    def register_suite(self, suite: str, info: dict) -> None:
        if self.manifest_path.exists():                     # start from the latest copy (another process may have written)
            try:
                latest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if suite in (latest.get("suites") or {}) and suite not in self._own_suites:
                    self.manifest["suites"][suite] = latest["suites"][suite]
            except (OSError, ValueError):
                pass
        cur = self.manifest["suites"].get(suite, {})
        cur.update(info)
        self.manifest["suites"][suite] = cur
        self._own_suites.add(suite)
        self.save()


def config_snapshot() -> dict:
    """Configuration as used (no secrets: only env-var *names* are stored)."""
    snap = {}
    for p in sorted(CONFIG_DIR.glob("*.yaml")):
        snap[p.name] = {"sha256": sha256_file(p), "text": p.read_text(encoding="utf-8")}
    return snap


# ----------------------------------------------------------------------------- writer
def ensure_trailing_newline(p: Path) -> None:
    """A process killed in the middle of a write leaves the last line of an append-only JSONL file cut off: end that
    line, so the next record starts on a line of its own (the cut line is skipped when read) instead of being glued
    to it and lost as well."""
    try:
        with open(p, "rb+") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return
            f.seek(-1, os.SEEK_END)
            last = f.read(1)
            if last != b"\n":
                f.seek(0, os.SEEK_END)
                f.write(b"\n")
    except FileNotFoundError:
        pass


class ResultWriter:
    """Append-only JSONL with resume support keyed by record['key'].  `retry_outcomes` (set by `run
    --retry-outcomes`) names final outcomes to send again — e.g. local_error after a broken local setup; the new
    record is appended and the analysis keeps the latest final record per key."""

    retry_outcomes: frozenset = frozenset()

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.done: set[str] = set()
        last: dict[str, str] = {}
        if path.exists():
            ensure_trailing_newline(path)
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue   # a half-written last line after a crash
                if rec.get("final", True):
                    self.done.add(rec["key"])
                    last[rec["key"]] = str((rec.get("result") or {}).get("outcome") or "")
        if self.retry_outcomes:
            self.done -= {k for k, o in last.items() if o in self.retry_outcomes}

    async def write(self, rec: dict) -> None:
        async with self.lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            if rec.get("final", True):
                self.done.add(rec["key"])

    def write_sync(self, rec: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if rec.get("final", True):
            self.done.add(rec["key"])


def read_jsonl(p: Path) -> list[dict]:
    out = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def write_jsonl(p: Path, rows: Iterable[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    replace_retry(tmp, p)


def plan_meta(sdir: Path) -> dict[str, dict]:
    """{plan name: metadata} for one session (empty dict for sessions written before plan metadata existed)."""
    out = {}
    d = sdir / "plans"
    for p in d.glob("*.jsonl") if d.exists() else []:
        mp = p.with_suffix(".meta.json")
        meta = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
        if "n_requests" not in meta:
            meta["n_requests"] = sum(1 for l in p.read_text(encoding="utf-8").splitlines() if l.strip())
        out[p.stem] = meta
    return out
