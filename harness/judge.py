"""LLM-as-judge for RAG answers and for the few IF checks that cannot be verified by code,
plus export/import of the blind human-check sample.

* The judge model is just another entry in configs/models.yaml (any OpenAI-compatible
  endpoint — GPT, Claude via Anthropic's OpenAI layer, or an on-prem model in a closed
  network).  configs/judge.yaml selects it.
* Prompts are published in prompts/tr/judge_*.md.  The judge never sees model names;
  items are shuffled; decoding is greedy where the endpoint allows it.
* Cache: verdicts are stored per judge SIGNATURE (prompt template + judge model + mode + decoding +
  max_tokens) in judge/<tag>__<signature>.jsonl, keyed by variant|uid|sha256(answer).  Changing the
  template or the judge starts a new file; a re-generated answer is never matched with an old verdict.
* Transport: `sync` (streaming chat, one request per answer) or `batch` (Anthropic Message Batches, half
  price, results within ~1 h / at most 24 h — harness/anthropic_batch.py).  The transport is recorded per
  verdict but is not part of the signature: same model, same prompt, same decoding.
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import random
import sys
from pathlib import Path

from .anthropic_batch import BatchState, message_params, message_text, run_batches
from .budget import price_line
from .capabilities import build_chat_payload, load_caps
from .client import USABLE, is_final, utc_now, with_retries
from .config import ROOT, Variant
from .prompts import load_prompt, render
from .runner import RunContext
from .scoring.jsonx import parse_json_lenient
from .store import ResultWriter, read_jsonl, safe_name, sha256_bytes, sha256_obj, stable_shuffle

VERDICT_SCORE = {"correct": 1.0, "partially_correct": 0.5, "incorrect": 0.0, "refused_incorrectly": 0.0,
                 "refused_correctly": 1.0, "hallucinated": 0.0, "no_answer": 0.0}


def content_sha(text: str) -> str:
    return sha256_bytes((text or "").encode("utf-8"))[:16]


def judge_scores(j: dict, answerable: bool) -> dict:
    """Turn the judge's structured output into the published metrics (computed here, not by the judge)."""
    claims = j.get("claims") or []
    labels = [c.get("label") for c in claims if isinstance(c, dict)]
    n = len(labels)
    sup = labels.count("supported")
    con = labels.count("contradicted")
    uns = labels.count("unsupported")
    kf = [k.get("label") for k in (j.get("key_facts") or []) if isinstance(k, dict)]
    verdict = j.get("verdict", "incorrect")
    if verdict not in VERDICT_SCORE:
        verdict = "incorrect"
    refusal = bool(j.get("refusal"))
    if verdict == "no_answer":
        hallucination = None                    # nothing was said, so nothing was hallucinated
    else:
        hallucination = (con + uns) > 0 if answerable else verdict == "hallucinated"
    return {
        "verdict": verdict, "correctness": VERDICT_SCORE[verdict],
        "faithfulness": (sup / n) if n else (1.0 if refusal else None),
        "hallucination": hallucination,
        "contradiction": con > 0,
        "key_fact_recall": ((kf.count("covered") + 0.5 * kf.count("partial")) / len(kf)) if kf else None,
        "n_claims": n, "language_ok": j.get("language_ok", True),
    }


def _context_from_messages(messages: list) -> tuple[str, str]:
    user = next(m["content"] for m in messages if m["role"] == "user")
    ctx, _, q = user.partition("\n\nSoru: ")
    return ctx.replace("Belgeler:\n\n", "", 1), q


def in_sample(key: str, fraction: float | None) -> bool:
    """Deterministic per-answer sample (independent of the judge), so two judges see the SAME answers."""
    if not fraction or fraction >= 1:
        return True
    uid_part = "|".join(key.split("|")[:2])
    return int(sha256_bytes(uid_part.encode())[:8], 16) % 10_000 < fraction * 10_000


def batch_capable(spec) -> bool:
    return (spec.options or {}).get("batch_api") == "anthropic"


# ----------------------------------------------------------------------------- spend cap
PILOT_FROM, PILOT_FRACTION, PILOT_MIN, PILOT_MAX = 60, 0.05, 20, 100


def pilot_note() -> str:
    return (f"from {PILOT_FROM} answers on, a pilot batch of {PILOT_FRACTION:.0%} ({PILOT_MIN}–{PILOT_MAX} answers) is "
            "judged and priced before the rest is sent")


class SpendCap:
    """What one judge command may spend (`judge` sets it to spend_cap_factor × its dry-run estimate, or --max-cost).

    Cost = the token counts the API reports × the judge's prices: the batch discount for batch results, full price
    for a repair turn.  A batch run of PILOT_FROM answers or more first sends a pilot batch (5 %, 20–100 answers,
    a random sample — the jobs are shuffled) and prices the rest on it; the rest is submitted only if the projected
    total stays under the cap.  The sync path checks before every group of requests.  limit None = no cap."""

    def __init__(self, reg, model_id: str, limit: float | None, est_per_request: float | None, currency: str = "USD"):
        self.reg, self.model_id, self.limit, self.currency = reg, model_id, limit, currency
        self.est_per_request = est_per_request
        self.spent = 0.0
        self.n = 0
        self.stopped: str | None = None
        self.next_command: str | None = None
        self.ledger: SpendLedger | None = None

    def event(self, kind: str, **kw) -> None:
        if self.ledger is not None:
            self.ledger.event(kind, **kw)

    def cost_of(self, meta: dict | None) -> float:
        meta = meta or {}
        total = 0.0
        for m, batch in ((meta, meta.get("transport") == "batch"), (meta.get("repair") or {}, False)):
            u = m.get("usage") or {}
            if u:
                c, _ = price_line(self.reg, self.model_id, int(u.get("prompt_tokens") or 0),
                                  int(u.get("completion_tokens") or 0), batch=batch)
                total += float(c or 0.0)
        return total

    def add(self, meta: dict | None) -> None:
        c = self.cost_of(meta)
        if c > 0:
            self.spent += c
            self.n += 1

    def observed(self, verdict_file: Path) -> tuple[int, float]:
        """(number, mean cost) of the priced verdicts of this judge signature so far (every run)."""
        costs = [c for c in (self.cost_of(r.get("judge_meta")) for r in read_jsonl(verdict_file) if r.get("final"))
                 if c > 0]
        return len(costs), (sum(costs) / len(costs) if costs else 0.0)

    def allows(self, n_more: int, per_request: float | None) -> bool:
        if self.limit is None or n_more <= 0:
            return True
        p = per_request if per_request is not None else (self.est_per_request or 0.0)
        return self.spent + n_more * p <= self.limit

    def stop(self, n_more: int, per_request: float | None, log) -> None:
        p = per_request if per_request is not None else (self.est_per_request or 0.0)
        projected = self.spent + n_more * p
        suggest = math.ceil(projected * 1.10 * 100) / 100           # the projection + 10 %, rounded up to cents
        self.next_command = continue_command(suggest)
        where = f" Record: {self.ledger.path}." if self.ledger is not None else ""
        self.stopped = (f"spend cap: {n_more} more answers at ≈{p:.4f} {self.currency} each would bring this command to "
                        f"≈{projected:.2f} {self.currency}, above its cap of {self.limit:.2f} {self.currency} (spent so far "
                        f"{self.spent:.2f}). Nothing more was sent; every verdict so far is saved.{where} To accept up to "
                        f"{suggest:.2f} {self.currency}, run: {self.next_command}")
        self.event("cap_stop", answers_left=n_more, per_verdict=round(p, 6), projected_total=round(projected, 4),
                   cap=self.limit, spent_this_run=round(self.spent, 4), next_command=self.next_command)
        log("!! [judge] " + self.stopped)

    def summary(self) -> dict:
        return {"spent": round(self.spent, 4), "priced_verdicts": self.n, "currency": self.currency,
                "limit": None if self.limit is None else round(self.limit, 4), "stopped": self.stopped,
                "next_command": self.next_command, "record": str(self.ledger.path) if self.ledger else None}


def python_cmd() -> str:
    """How to call this interpreter from the project folder (.\\.venv\\Scripts\\python.exe on the Windows PC)."""
    try:
        rel = Path(os.path.abspath(sys.executable)).relative_to(ROOT.resolve())    # a venv's python may be a link
    except (ValueError, OSError):
        return "python"
    return (".\\" if os.name == "nt" else "./") + str(rel)


def continue_command(max_cost: float) -> str:
    """The judge command as it was called, with --max-cost <max_cost> instead of any earlier one."""
    args, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a == "--max-cost":
            skip = True
            continue
        if not a.startswith("--max-cost="):
            args.append(a)
    return f"{python_cmd()} harness/run.py " + " ".join(args + ["--max-cost", f"{max_cost:.2f}"])


class SpendLedger:
    """judge/spend/<file stem>.jsonl — what every judge command planned, submitted, spent and why it ended, one JSON
    line per event, appended and flushed at once: a run that is stopped, interrupted or killed still leaves its start
    and every submission.  Verdicts (judge/<stem>.jsonl) and batch ids (<stem>.batches.json) are saved as before."""

    def __init__(self, path: Path, base: dict):
        self.path, self.base = path, base
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **kw) -> None:
        rec = {"at": utc_now(), "event": kind, **self.base, **kw}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            f.flush()


async def recorded_run(ctx: RunContext, judge: Judge, out_path: Path, w: ResultWriter, suite: str, keys: list[str],
                       n_no_answer: int, cap: SpendCap | None, body) -> None:
    """Run one judge command between a 'start' and an 'end' event in its spend record (also when it is interrupted)."""
    ledger = SpendLedger(out_path.parent / "spend" / f"{out_path.stem}.jsonl",
                         {"run": f"{utc_now()[:19]}Z-{os.getpid()}", "suite": suite, "session": out_path.parents[2].name,
                          "signature": judge.signature, "judge_model": judge.spec.api_model, "transport": judge.transport})
    if cap is not None:
        cap.ledger = ledger
    est = (cap.est_per_request * len(keys)) if cap is not None and cap.est_per_request is not None else None
    ledger.event("start", command=f"{python_cmd()} harness/run.py " + " ".join(sys.argv[1:]), answers_to_judge=len(keys),
                 empty_answers=n_no_answer, verdicts_already_saved=len(w.done),
                 estimate=None if est is None else round(est, 4), cap=None if cap is None else cap.limit,
                 currency=None if cap is None else cap.currency)
    status, err = "finished", None
    try:
        await body()
    except BaseException as e:                      # Ctrl+C, a network failure, a crash: the record says where it stopped
        status, err = "interrupted", f"{type(e).__name__}: {e}"[:300]
        raise
    finally:
        state = out_path.with_name(out_path.stem + ".batches.json")
        open_b = len(BatchState(state).open()) if state.exists() else 0
        left = sum(1 for k in keys if k not in w.done)
        if status == "finished":
            status = ("stopped_by_spend_cap" if cap is not None and cap.stopped else
                      "waiting_for_open_batches" if open_b else "incomplete" if left else "finished")
        nxt = {"stopped_by_spend_cap": cap.next_command if cap is not None else None,
               "waiting_for_open_batches": "run the same command again later: open batches are collected, nothing is re-sent",
               "incomplete": "run the same command again: only the answers without a verdict are sent",
               "interrupted": "run the same command again: saved verdicts and open batches are kept, nothing is sent twice",
               }.get(status)
        n_all, mean_all = cap.observed(out_path) if cap is not None else (0, 0.0)
        ledger.event("end", status=status, error=err,
                     spent_this_run=None if cap is None else round(cap.spent, 4),
                     priced_this_run=None if cap is None else cap.n, answers_left=left, open_batches=open_b,
                     spent_all_runs=round(n_all * mean_all, 4), priced_all_runs=n_all, next=nxt)
        if cap is not None:
            ctx.log(f"[judge] {status}: spent {cap.spent:.2f} {cap.currency} in this run ({cap.n} verdicts priced"
                    f"{'' if cap.limit is None else f', cap {cap.limit:.2f}'}); this judge on this session so far "
                    f"{n_all * mean_all:.2f} {cap.currency} ({n_all} verdicts); answers left {left}; record {ledger.path}")


async def capped_batches(ctx: RunContext, judge: Judge, out_path: Path, w: ResultWriter, requests: list[tuple],
                         handle, cap: SpendCap | None, *, wait: bool, poll_s: float) -> dict:
    """Anthropic batches under the spend cap.  Batches an earlier run submitted are collected first (never sent
    again); answers that were in them and failed wait for the next run, as before."""
    client = ctx.client(judge.spec.endpoint)
    state = out_path.with_name(out_path.stem + ".batches.json")
    stats: dict = {}

    def merge(s: dict) -> None:
        for k, v in (s or {}).items():
            if isinstance(v, dict):
                stats[k] = {**stats.get(k, {}), **{e: stats.get(k, {}).get(e, 0) + n for e, n in v.items()}}
            elif isinstance(v, int) and k != "open_batches":
                stats[k] = stats.get(k, 0) + v
            else:
                stats[k] = v

    async def send(chunk: list[tuple], kind: str, **info) -> None:
        before = len(BatchState(state).batches)
        if chunk and cap is not None:
            cap.event(f"{kind}_submitting", answers=len(chunk), **info)
        merge(await run_batches(client, state, chunk, handle, wait=wait, poll_s=poll_s, log=ctx.log))
        if chunk and cap is not None:
            cap.event(f"{kind}_submitted", answers=len(chunk),
                      batch_ids=[b["id"] for b in BatchState(state).batches[before:]])

    pending = BatchState(state).pending_keys()
    if pending:
        if cap is not None:
            cap.event("collecting_earlier_batches", answers=len(pending))
        await send([], "collect")
        if BatchState(state).open():
            return stats
    todo = [(k, p) for k, p in requests if k not in pending and k not in w.done]
    if not todo:
        return stats
    if cap is None or cap.limit is None:
        await send(todo, "all")
        return stats
    n_obs, per = cap.observed(out_path)
    piloted = False
    if n_obs < PILOT_MIN and len(todo) >= PILOT_FROM:
        piloted = True
        k = max(PILOT_MIN, min(PILOT_MAX, round(len(todo) * PILOT_FRACTION)))
        if not cap.allows(k, None):
            cap.stop(k, None, ctx.log)
            return stats
        pilot, todo = todo[:k], todo[k:]
        ctx.log(f"[judge] spend cap {cap.limit:.2f} {cap.currency}: a pilot batch of {k} answers first; the other "
                f"{len(todo)} are priced on it before they are sent")
        await send(pilot, "pilot", estimate=round(k * (cap.est_per_request or 0.0), 4))
        if BatchState(state).open():
            ctx.log("[judge] the rest goes out once the pilot has been collected and priced — run the same command again")
            return stats
        n_obs, per = cap.observed(out_path)
        cap.event("pilot_priced", priced_verdicts=n_obs, per_verdict=round(per, 6), spent_this_run=round(cap.spent, 4),
                  projected_total=round(cap.spent + len(todo) * per, 4), cap=cap.limit)
        ctx.log(f"[judge] pilot: {n_obs} priced verdicts, ≈{per:.4f} {cap.currency} each; spent so far "
                f"{cap.spent:.2f} {cap.currency}; projected total {cap.spent + len(todo) * per:.2f} of the cap "
                f"{cap.limit:.2f}")
    per_req = per if n_obs else None
    if not cap.allows(len(todo), per_req):
        cap.stop(len(todo), per_req, ctx.log)
        return stats
    p = per_req if per_req is not None else (cap.est_per_request or 0.0)
    await send(todo, "rest" if piloted else "all", per_verdict=round(p, 6),
               projected_total=round(cap.spent + len(todo) * p, 4), cap=cap.limit)
    return stats


async def capped_sync(jobs: list, run, cap: SpendCap | None, group: int, log) -> None:
    """Synchronous judging; with a cap in groups of `group` (the judge's concurrency), checked before each group."""
    if cap is None or cap.limit is None:
        await asyncio.gather(*(run(j) for j in jobs))
        return
    for i in range(0, len(jobs), max(1, group)):
        chunk = jobs[i:i + max(1, group)]
        per = cap.spent / cap.n if cap.n else None
        if not cap.allows(len(chunk), per):
            cap.stop(len(jobs) - i, per, log)
            return
        await asyncio.gather(*(run(j) for j in chunk))


class Judge:
    def __init__(self, ctx: RunContext, template_name: str, model_override: str | None = None,
                 transport: str | None = None):
        cfg = dict(ctx.reg.judge or {})
        if model_override:
            cfg["model"] = model_override
        if not cfg.get("model"):
            raise RuntimeError("configs/judge.yaml: set 'model' to a model id from configs/models.yaml")
        self.ctx, self.cfg = ctx, cfg
        self.variant = Variant(cfg["model"], cfg.get("mode", "default"))
        self.spec = ctx.reg.model(self.variant.model_id)
        ctx.reg.check_network_policy([self.spec.endpoint])
        self.caps = load_caps(ctx.reg, self.variant.model_id)
        self.sem = asyncio.Semaphore(cfg.get("concurrency", 8))
        self.template = load_prompt(template_name)
        self.decoding = ctx.reg.decoding(cfg.get("decoding", "greedy"))
        self.max_tokens = cfg.get("max_tokens", 4096)
        self.transport = transport or cfg.get("transport", "sync")
        if self.transport == "batch" and not batch_capable(self.spec):
            raise RuntimeError(f"judge {self.spec.id}: the batch transport needs Anthropic's API "
                               f"(options.batch_api: anthropic in configs/models.yaml) — use --transport sync")
        if self.transport not in ("sync", "batch"):
            raise RuntimeError(f"unknown judge transport {self.transport!r} (sync | batch)")

    @property
    def tag(self) -> str:
        return safe_name(f"{self.spec.endpoint}-{self.spec.api_model}-{self.variant.mode}")

    @property
    def signature(self) -> str:
        return sha256_obj({"template": self.template, "endpoint": self.spec.endpoint, "model": self.spec.api_model,
                           "mode": self.variant.mode, "decoding": self.decoding, "max_tokens": self.max_tokens,
                           "seed": self.cfg.get("seed", 20261009)})[:12]

    @property
    def file_stem(self) -> str:
        return f"{self.tag}__{self.signature}"

    async def ask(self, prompt: str) -> tuple[dict | None, dict]:
        payload, eff = build_chat_payload(self.spec, self.caps, self.variant.mode,
                                          [{"role": "user", "content": prompt}], max_tokens=self.max_tokens,
                                          decoding=self.decoding, seed=self.cfg.get("seed", 20261009), stream=True)
        client = self.ctx.client(self.spec.endpoint)
        async with self.sem:
            res = await with_retries(lambda: client.stream_chat(payload), self.ctx.opts.retry)
            usage = dict(res.usage or {})
            ok, obj = parse_json_lenient(res.content) if res.outcome in USABLE else (False, None)
            if res.outcome in USABLE and not ok:   # one repair attempt
                payload2 = dict(payload)
                payload2["messages"] = payload["messages"] + [
                    {"role": "assistant", "content": res.content[:4000]},
                    {"role": "user", "content": "Return ONLY the JSON object described above, nothing else."}]
                res = await with_retries(lambda: client.stream_chat(payload2), self.ctx.opts.retry)
                ok, obj = parse_json_lenient(res.content) if res.outcome in USABLE else (False, None)
                for k in ("prompt_tokens", "completion_tokens"):       # both turns are billed
                    usage[k] = int(usage.get(k) or 0) + int((res.usage or {}).get(k) or 0)
        meta = {"outcome": res.outcome, "served_model": res.served_model, "usage": usage or res.usage,
                "transport": "sync"}
        return (obj if ok and isinstance(obj, dict) else None), meta

    async def repair(self, prompt: str, bad_text: str) -> tuple[dict | None, dict]:
        """A batch result that is not valid JSON gets the same single repair turn as the sync path (sent sync)."""
        payload, _ = build_chat_payload(self.spec, self.caps, self.variant.mode,
                                        [{"role": "user", "content": prompt},
                                         {"role": "assistant", "content": (bad_text or "")[:4000]},
                                         {"role": "user", "content": "Return ONLY the JSON object described above, nothing else."}],
                                        max_tokens=self.max_tokens, decoding=self.decoding,
                                        seed=self.cfg.get("seed", 20261009), stream=True)
        client = self.ctx.client(self.spec.endpoint)
        async with self.sem:
            res = await with_retries(lambda: client.stream_chat(payload), self.ctx.opts.retry)
        ok, obj = parse_json_lenient(res.content) if res.outcome in USABLE else (False, None)
        meta = {"outcome": res.outcome, "served_model": res.served_model, "usage": res.usage, "transport": "batch+repair"}
        return (obj if ok and isinstance(obj, dict) else None), meta

    def batch_params(self, prompt: str) -> dict:
        return message_params(self.spec.api_model, prompt, self.max_tokens, self.decoding, drop=self.caps.drop)

    async def parse_batch_message(self, prompt: str, message: dict, meta: dict) -> tuple[dict | None, dict]:
        text = message_text(message)
        ok, obj = parse_json_lenient(text)
        if ok and isinstance(obj, dict):
            return obj, meta
        j, rmeta = await self.repair(prompt, text)
        return j, {**meta, "repair": rmeta}


def _repeat_ok(rec: dict, repeats: set[int] | None) -> bool:
    return repeats is None or int(rec.get("repeat", 0)) in repeats


def rag_jobs(judge: "Judge", suite_dir: Path, variants: list[str] | None, done: set,
             repeats: set[int] | None = None, sample: float | None = None) -> list[tuple]:
    plan = {it["uid"]: it for it in read_jsonl(suite_dir.parent / "plans" / "rag_qa.jsonl")}
    jobs = []
    for f in sorted(suite_dir.glob("*.jsonl")):
        for rec in read_jsonl(f):
            if variants and rec["variant"] not in variants:
                continue
            if not rec.get("final", True) or not is_final((rec.get("result") or {}).get("outcome", "")):
                continue                            # infrastructure failures are never judged
            if not _repeat_ok(rec, repeats):
                continue
            item = plan.get(rec["key"])
            if item is None:
                continue
            res = rec["result"]
            meta = item["meta"]
            text = res.get("content") or ""
            key = f"{rec['variant']}|{rec['key']}|{content_sha(text)}"
            if key in done or not in_sample(key, sample):
                continue
            if res["outcome"] not in USABLE or not text.strip():
                jobs.append((key, rec, None, {"verdict": "no_answer", "claims": [], "key_facts": [], "refusal": False}))
                continue
            context, _ = _context_from_messages(item["messages"])
            prompt = render(judge.template, question=meta["question"], answerable="yes" if meta["answerable"] else "no",
                            context=context, reference=meta["reference"],
                            key_facts="\n".join(f"- {k}" for k in meta["key_facts"]) or "(none — not answerable)",
                            answer=text)
            jobs.append((key, rec, prompt, None))
    return stable_shuffle(jobs, "judge-order")                   # blind + order-randomised (same order for every judge)


async def judge_rag(ctx: RunContext, suite_dir: Path, variants: list[str] | None = None, *,
                    sample: float | None = None, repeats: set[int] | None = None,
                    model_override: str | None = None, transport: str | None = None,
                    wait: bool = True, poll_s: float = 30.0, cap: SpendCap | None = None) -> dict:
    """Judge every (variant, item, repeat) answer of a rag_qa session directory (or a deterministic sample)."""
    judge = Judge(ctx, "judge_rag.md", model_override, transport)
    plan = {it["uid"]: it for it in read_jsonl(suite_dir.parent / "plans" / "rag_qa.jsonl")}
    out_path = suite_dir / "judge" / f"{judge.file_stem}.jsonl"
    w = ResultWriter(out_path)
    jobs = rag_jobs(judge, suite_dir, variants, w.done, repeats, sample)
    ctx.log(f"[judge] {len(jobs)} answers to judge with {judge.spec.api_model} via {judge.transport} "
            f"(signature {judge.signature}; cached: {len(w.done)})")
    counts = {"ok": 0, "judge_failed": 0, "no_answer": 0}

    async def finish(job, j, meta):
        key, rec, prompt, _ = job
        answerable = plan[rec["key"]]["meta"]["answerable"]
        if cap is not None:
            cap.add(meta)
        base = {"key": key, "variant": rec["variant"], "item_id": rec["item_id"], "repeat": rec["repeat"],
                "uid": rec["key"], "session": rec.get("session"), "plan_hash": rec.get("plan_hash"),
                "content_sha": key.rsplit("|", 1)[1], "signature": judge.signature, "judge_tag": judge.tag,
                "judge_model": judge.spec.api_model}
        if j is None:
            counts["judge_failed"] += 1
            await w.write({**base, "final": False, "judge": None, "judge_meta": meta})
            return
        await w.write({**base, "final": True, "prompt_sha": sha256_bytes((prompt or "").encode())[:16],
                       "judge": j, "scores": judge_scores(j, answerable), "judge_meta": meta})

    async def run(job):
        key, rec, prompt, fixed = job
        if fixed is not None:
            counts["no_answer"] += 1
            return await finish(job, fixed, {"outcome": "skipped_no_answer"})
        j, meta = await judge.ask(prompt)
        if j is not None:
            counts["ok"] += 1
        await finish(job, j, meta)

    stats = None
    keys = [j[0] for j in jobs if j[3] is None]

    async def body():
        nonlocal stats
        if judge.transport == "sync":
            await capped_sync(jobs, run, cap, int(judge.cfg.get("concurrency", 8)), ctx.log)
            return
        await asyncio.gather(*(run(j) for j in jobs if j[3] is not None))      # empty answers need no judge
        by_key = {j[0]: j for j in jobs if j[3] is None}

        async def handle(key, message, meta):
            job = by_key.get(key) or _rag_job_for_key(judge, suite_dir, key)
            if job is None or key in w.done:
                return
            if message is None:
                return await finish(job, None, meta)
            j, meta2 = await judge.parse_batch_message(job[2], message, meta)
            if j is not None:
                counts["ok"] += 1
            await finish(job, j, meta2)

        stats = await capped_batches(ctx, judge, out_path, w, [(j[0], judge.batch_params(j[2])) for j in by_key.values()],
                                     handle, cap, wait=wait, poll_s=poll_s)

    await recorded_run(ctx, judge, out_path, w, "rag_qa", keys, len(jobs) - len(keys), cap, body)
    return {"judged": counts, "file": str(out_path), "signature": judge.signature, "transport": judge.transport,
            **({"batch": stats} if stats else {}), **({"spend": cap.summary()} if cap is not None else {})}


_JOB_CACHE: dict[tuple, dict] = {}


def _rag_job_for_key(judge: "Judge", suite_dir: Path, key: str) -> tuple | None:
    """Rebuild the job of an answer that was submitted in an earlier run (its batch is collected now)."""
    variant = key.split("|", 1)[0]
    ck = (str(suite_dir), judge.signature, variant)
    if ck not in _JOB_CACHE:
        _JOB_CACHE[ck] = {job[0]: job for job in rag_jobs(judge, suite_dir, [variant], set())}
    return _JOB_CACHE[ck].get(key)


def ifeval_jobs(judge: "Judge", suite_dir: Path, variants: list[str] | None, done: set,
                repeats: set[int] | None = None, sample: float | None = None) -> list[tuple]:
    plan = {it["uid"]: it for it in read_jsonl(suite_dir.parent / "plans" / "ifeval.jsonl")}
    jobs = []
    for f in sorted(suite_dir.glob("*.jsonl")):
        for rec in read_jsonl(f):
            if variants and rec["variant"] not in variants:
                continue
            if not rec.get("final", True) or not _repeat_ok(rec, repeats):
                continue
            item = plan.get(rec["key"])
            if not item:
                continue
            content = rec["result"].get("content") or ""
            for ci, c in enumerate(item["meta"]["checks"]):
                if c["type"] != "llm_judge":
                    continue
                key = f"{rec['variant']}|{rec['key']}|{ci}|{content_sha(content)}"
                if key in done or not in_sample(key, sample):
                    continue
                prompt = render(judge.template, criterion=c["criterion"], prompt=item["meta"]["prompt"], response=content)
                jobs.append((key, rec, ci, prompt, bool(content.strip())))
    return stable_shuffle(jobs, "judge-if")


async def judge_ifeval(ctx: RunContext, suite_dir: Path, variants: list[str] | None = None, *,
                       repeats: set[int] | None = None, sample: float | None = None,
                       model_override: str | None = None, transport: str | None = None,
                       wait: bool = True, poll_s: float = 30.0, cap: SpendCap | None = None) -> dict:
    judge = Judge(ctx, "judge_criterion.md", model_override, transport)
    out_path = suite_dir / "judge" / f"{judge.file_stem}.jsonl"
    w = ResultWriter(out_path)
    jobs = ifeval_jobs(judge, suite_dir, variants, w.done, repeats, sample)
    ctx.log(f"[judge] {len(jobs)} tone checks to judge with {judge.spec.api_model} via {judge.transport} "
            f"(signature {judge.signature}; cached: {len(w.done)})")
    counts = {"ok": 0, "judge_failed": 0, "no_answer": 0}

    async def finish(job, j, meta):
        key, rec, ci, _, _ = job
        if cap is not None:
            cap.add(meta)
        if meta.get("outcome") == "skipped":
            counts["no_answer"] += 1
        elif j is None:
            counts["judge_failed"] += 1
        else:
            counts["ok"] += 1
        await w.write({"key": key, "final": j is not None, "variant": rec["variant"], "item_id": rec["item_id"],
                       "repeat": rec["repeat"], "uid": rec["key"], "check_index": ci, "session": rec.get("session"),
                       "content_sha": key.rsplit("|", 1)[1], "signature": judge.signature, "judge_tag": judge.tag,
                       "judge": j, "judge_meta": meta})

    async def run(job):
        if not job[4]:
            return await finish(job, {"pass": False, "reason": "empty answer"}, {"outcome": "skipped"})
        j, meta = await judge.ask(job[3])
        await finish(job, j, meta)

    stats = None
    keys = [j[0] for j in jobs if j[4]]

    async def body():
        nonlocal stats
        if judge.transport == "sync":
            await capped_sync(jobs, run, cap, int(judge.cfg.get("concurrency", 8)), ctx.log)
            return
        await asyncio.gather(*(run(j) for j in jobs if not j[4]))
        by_key = {j[0]: j for j in jobs if j[4]}

        async def handle(key, message, meta):
            job = by_key.get(key)
            if job is None:
                variant = key.split("|", 1)[0]
                job = next((x for x in ifeval_jobs(judge, suite_dir, [variant], set()) if x[0] == key), None)
            if job is None or key in w.done:
                return
            if message is None:
                return await finish(job, None, meta)
            j, meta2 = await judge.parse_batch_message(job[3], message, meta)
            await finish(job, j, meta2)

        stats = await capped_batches(ctx, judge, out_path, w, [(j[0], judge.batch_params(j[3])) for j in by_key.values()],
                                     handle, cap, wait=wait, poll_s=poll_s)

    await recorded_run(ctx, judge, out_path, w, "ifeval", keys, len(jobs) - len(keys), cap, body)
    return {"judged": counts, "file": str(out_path), "signature": judge.signature, "transport": judge.transport,
            **({"batch": stats} if stats else {}), **({"spend": cap.summary()} if cap is not None else {})}


# ----------------------------------------------------------------------------- human check (blind sample)
HUMAN_COLUMNS = ["sample_id", "question", "answerable", "context", "reference_answer", "key_facts", "model_answer",
                 "human_verdict", "human_faithfulness_0_1", "human_notes"]


def export_human_sample(suite_dir: Path, judge_file_stem: str, fraction: float = 0.10,
                        seed: int = 20261009) -> tuple[Path, Path]:
    """Stratified (variant × answerable) blind sample.  The sheet hides the model and the judge's opinion."""
    plan = {it["uid"]: it for it in read_jsonl(suite_dir.parent / "plans" / "rag_qa.jsonl")}
    judged = read_jsonl(suite_dir / "judge" / f"{judge_file_stem}.jsonl")
    judged = [j for j in judged if j.get("final") and j.get("judge") and j["judge"].get("verdict") != "no_answer"]
    answers = {}
    for f in suite_dir.glob("*.jsonl"):
        for rec in read_jsonl(f):
            text = (rec.get("result") or {}).get("content", "") or ""
            answers[f"{rec['variant']}|{rec['key']}|{content_sha(text)}"] = text
    strata: dict = {}
    for j in judged:
        uid = j.get("uid") or j["key"].split("|")[1]
        strata.setdefault((j["variant"], plan[uid]["meta"]["answerable"]), []).append(j)
    rng = random.Random(seed)
    picked = []
    for k in sorted(strata, key=str):
        rows = strata[k]
        rng.shuffle(rows)
        picked += rows[: max(1, round(len(rows) * fraction))]
    rng.shuffle(picked)
    sheet = suite_dir / "human_check" / "sheet.csv"
    keyf = suite_dir / "human_check" / "KEY_do_not_share_with_annotators.csv"
    sheet.parent.mkdir(parents=True, exist_ok=True)
    with open(sheet, "w", encoding="utf-8-sig", newline="") as fs, open(keyf, "w", encoding="utf-8-sig", newline="") as fk:
        ws, wk = csv.writer(fs), csv.writer(fk)
        ws.writerow(HUMAN_COLUMNS)
        wk.writerow(["sample_id", "variant", "uid", "judge_verdict", "judge_signature", "content_sha"])
        for i, j in enumerate(picked, 1):
            uid = j.get("uid") or j["key"].split("|")[1]
            it = plan[uid]
            context, _ = _context_from_messages(it["messages"])
            sid = f"H{i:04d}"
            ws.writerow([sid, it["meta"]["question"], "yes" if it["meta"]["answerable"] else "no", context,
                         it["meta"]["reference"], " | ".join(it["meta"]["key_facts"]), answers.get(j["key"], ""),
                         "", "", ""])
            wk.writerow([sid, j["variant"], uid, j["judge"].get("verdict"), j.get("signature", ""),
                         j.get("content_sha", "")])
    return sheet, keyf
