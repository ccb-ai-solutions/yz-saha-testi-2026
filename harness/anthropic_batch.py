"""Anthropic Message Batches transport for the judge — half the token price; results usually arrive within an
hour and at most after 24 h (then the request expires and is re-submitted on the next run).

Flow: submit → persist the batch id (judge/<stem>.batches.json) → poll → download the JSONL results → hand each
result back to the caller, which writes the verdict exactly as the synchronous path does.  An interrupted
`judge` run resumes: open batches are polled and collected first, and an answer that sits in an open batch is
never submitted twice.  The verdict signature does not change with the transport (same model, prompt and
decoding); every verdict records `transport: batch` and its batch id.

API (docs.claude.com, checked 2026-10-10): POST /v1/messages/batches {"requests":[{"custom_id","params"}]},
GET /v1/messages/batches/{id}, GET /v1/messages/batches/{id}/results (JSONL).  custom_id must match
^[a-zA-Z0-9_-]{1,64}$; at most 100,000 requests or 256 MB per batch; results are kept for 29 days.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Awaitable, Callable

from .client import OK, ApiClient
from .store import replace_retry, sha256_bytes

MAX_REQUESTS_PER_BATCH = 10_000              # API limit: 100,000
MAX_BYTES_PER_BATCH = 64 * 1024 * 1024       # API limit: 256 MB
CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def custom_id_for(key: str) -> str:
    """Our answer keys contain '|' and ':' — the API allows only [A-Za-z0-9_-]{1,64}."""
    return "j" + sha256_bytes(key.encode("utf-8"))[:40]


def message_params(model: str, prompt: str, max_tokens: int, decoding: dict, drop: "set | frozenset" = frozenset()) -> dict:
    """Native Messages API parameters for one judge request (the same prompt the synchronous path sends).

    `drop` = the parameters the capability probe found the model rejects (e.g. `temperature` / `top_p`, deprecated
    for Claude Opus 5.5 and Sonnet 5.5).  The synchronous path drops them already; without this every batch request
    errors (smoke test of 10 Oct: 40 of 40 "`temperature` is deprecated for this model")."""
    p: dict = {"model": model, "max_tokens": int(max_tokens), "messages": [{"role": "user", "content": prompt}]}
    if decoding.get("temperature") is not None and "temperature" not in drop:
        p["temperature"] = float(decoding["temperature"])
    elif decoding.get("top_p") is not None and "top_p" not in drop:
        p["top_p"] = float(decoding["top_p"])
    return p


def message_text(message: dict) -> str:
    return "".join(b.get("text", "") for b in (message.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "text")


def message_usage(message: dict) -> dict:
    u = message.get("usage") or {}
    return {"prompt_tokens": u.get("input_tokens"), "completion_tokens": u.get("output_tokens"),
            "cache_read_input_tokens": u.get("cache_read_input_tokens")}


class BatchState:
    """judge/<stem>.batches.json — every batch we created, with its custom_id → answer-key map."""

    def __init__(self, path: Path):
        self.path = path
        self.doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"batches": []}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.doc, ensure_ascii=False, indent=1), encoding="utf-8")
        replace_retry(tmp, self.path)

    @property
    def batches(self) -> list[dict]:
        return self.doc["batches"]

    def open(self) -> list[dict]:
        return [b for b in self.batches if not b.get("collected")]

    def pending_keys(self) -> set[str]:
        return {k for b in self.open() for k in b["map"].values()}

    def add(self, batch_id: str, mapping: dict[str, str], info: dict) -> dict:
        b = {"id": batch_id, "map": mapping, "n": len(mapping), "collected": False, **info}
        self.batches.append(b)
        return b


Handler = Callable[[str, "dict | None", dict], Awaitable[None]]


async def run_batches(client: ApiClient, state_path: Path, jobs: list[tuple[str, dict]], handle: Handler, *,
                      wait: bool = True, poll_s: float = 30.0, log=print) -> dict:
    """Submit `jobs` ([(answer_key, messages_params)]) that are not already in an open batch, then poll and collect.

    handle(key, message | None, meta) is awaited once per collected result; message is None for errored, expired,
    canceled or missing results (the caller records them as non-final so the next run re-submits them)."""
    st = BatchState(state_path)
    stats: dict = {"submitted": 0, "succeeded": 0, "not_succeeded": 0, "open_batches": 0}
    errors: dict[str, int] = {}
    pending = st.pending_keys()
    new = [(k, p) for k, p in jobs if k not in pending]
    if pending:
        log(f"[batch] {len(pending)} answers are already in open batches — they are collected, not re-submitted")

    async def submit(chunk: list[tuple[str, dict]]) -> None:
        reqs = [{"custom_id": custom_id_for(k), "params": p} for k, p in chunk]
        r = await client.request_json("POST", "/messages/batches", json_body={"requests": reqs}, timeout=900)
        if r.outcome != OK or not isinstance(r.data, dict) or not r.data.get("id"):
            raise RuntimeError(f"batch submission failed: {r.outcome} {r.http_status} {r.error}")
        st.add(r.data["id"], {custom_id_for(k): k for k, _ in chunk},
               {"created_at": r.data.get("created_at"), "expires_at": r.data.get("expires_at"),
                "status": r.data.get("processing_status")})
        st.save()                                   # persist immediately: the batch is billed from now on
        stats["submitted"] += len(chunk)
        log(f"[batch] submitted {r.data['id']} with {len(chunk)} requests")

    chunk: list[tuple[str, dict]] = []
    size = 0
    for k, p in new:
        b = len(json.dumps(p, ensure_ascii=False).encode("utf-8")) + 120
        if chunk and (len(chunk) >= MAX_REQUESTS_PER_BATCH or size + b > MAX_BYTES_PER_BATCH):
            await submit(chunk)
            chunk, size = [], 0
        chunk.append((k, p))
        size += b
    if chunk:
        await submit(chunk)

    async def collect(b: dict) -> None:
        r = await client.get_text(f"/messages/batches/{b['id']}/results", timeout=900)
        if r.outcome != OK or not isinstance(r.data, str):
            log(f"[batch] could not download results of {b['id']}: {r.outcome} {r.error} — will retry")
            return
        seen: set[str] = set()
        berr: dict[str, int] = {}
        for line in r.data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("custom_id")
            key = b["map"].get(cid)
            if key is None or cid in seen:
                continue
            seen.add(cid)
            res = row.get("result") or {}
            meta = {"transport": "batch", "batch_id": b["id"]}
            if res.get("type") == "succeeded" and isinstance(res.get("message"), dict):
                msg = res["message"]
                stats["succeeded"] += 1
                await handle(key, msg, {**meta, "outcome": "ok", "served_model": msg.get("model"),
                                        "usage": message_usage(msg), "stop_reason": msg.get("stop_reason")})
            else:
                stats["not_succeeded"] += 1
                err = (res.get("error") or {}).get("error") or res.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                label = f"{res.get('type', 'unknown')}: {msg}" if msg else str(res.get("type", "unknown"))
                berr[label[:300]] = berr.get(label[:300], 0) + 1
                await handle(key, None, {**meta, "outcome": f"batch_{res.get('type', 'unknown')}", "error": msg})
        for cid, key in b["map"].items():           # a result line that never came back: treat as not done
            if cid not in seen:
                stats["not_succeeded"] += 1
                berr["missing from the results file"] = berr.get("missing from the results file", 0) + 1
                await handle(key, None, {"transport": "batch", "batch_id": b["id"], "outcome": "batch_missing"})
        b["collected"] = True
        st.save()
        log(f"[batch] collected {b['id']}: {len(seen)} results")
        for label, n in berr.items():
            errors[label] = errors.get(label, 0) + n
        for label, n in sorted(berr.items(), key=lambda kv: -kv[1])[:5]:
            log(f"[batch]   not succeeded ×{n}: {label}")

    while True:
        open_ = st.open()
        if not open_:
            break
        for b in open_:
            r = await client.get_json(f"/messages/batches/{b['id']}")
            if r.outcome != OK or not isinstance(r.data, dict):
                log(f"[batch] status of {b['id']} unavailable ({r.outcome}) — retrying later")
                continue
            b["status"] = r.data.get("processing_status")
            b["request_counts"] = r.data.get("request_counts") or {}
            if b["status"] == "ended":
                await collect(b)
            else:
                rc = b["request_counts"]
                done = sum(int(rc.get(x) or 0) for x in ("succeeded", "errored", "canceled", "expired"))
                log(f"[batch] {b['id']}: {b['status']}, {done}/{b['n']} done")
        st.save()
        if not st.open():
            break
        if not wait:
            log("[batch] still processing — run the same judge command again later to collect (nothing is re-sent)")
            break
        await asyncio.sleep(poll_s)
    stats["open_batches"] = len(st.open())
    if errors:
        stats["errors"] = dict(sorted(errors.items(), key=lambda kv: -kv[1])[:5])
    return stats
