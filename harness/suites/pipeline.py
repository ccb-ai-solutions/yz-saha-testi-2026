"""Pipeline suites that do not use chat completions: retrieval (+rerank), OCR, ASR, typed decisions (laya).

Every backend — EVREN endpoint, any OpenAI-compatible server, or a local
library (bge-m3, e5, Whisper, Tesseract) — is addressed through a model id in
configs/models.yaml, so the same suite compares hosted and on-prem components.
"""
from __future__ import annotations

import asyncio
import json
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from ..client import OK, RetryPolicy, image_to_data_url, utc_now
from ..config import DATA_DIR, ModelSpec
from ..rag_data import BM25, load_derived, load_questions
from ..runner import QuotaStop, RunContext, guarded
from ..scoring.basic import retrieval_metrics, score_asr, score_ocr
from ..store import HARNESS_VERSION, ResultWriter, safe_name, sha256_obj
from .quality import decision_spec


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


# A pipeline record is final when the service answered (OK) or rejected THIS input (it would fail the same
# way in production) — those count as errors (CER/WER 1.0).  Everything else (5xx, timeouts, 404, auth,
# parse errors) is infrastructure: non-final, re-run on resume, never scored.
PIPELINE_FINAL_FAILURES = {"http_400_content_filter", "http_400_context_length", "http_413", "http_422", "local_error"}


def pipeline_final(outcome: str) -> bool:
    return outcome == OK or outcome in PIPELINE_FINAL_FAILURES


# ============================================================================ retrieval
def retrieval_dataset(name: str = "rag", limit: int | None = None) -> tuple[list[dict], list[dict]]:
    """Return (corpus [{id,text}], queries [{id,text,gold:set}])."""
    if name == "rag":
        chunks, gold, _ = load_derived()
        corpus = [{"id": c["chunk_id"], "text": c["text"]} for c in chunks]
        queries = [{"id": q["id"], "text": q["question"], "gold": set(gold[q["id"]]), "type": q["type"]}
                   for q in load_questions() if q["answerable"]]
    elif name == "xquad_tr":
        p = DATA_DIR / "retrieval_ext" / "xquad_tr.json"
        if not p.exists():
            raise FileNotFoundError("run: python scripts/fetch_xquad.py")
        raw = json.loads(p.read_text(encoding="utf-8"))
        corpus, queries = [], []
        for art in raw["data"]:
            for pi, para in enumerate(art["paragraphs"]):
                cid = f"{art['title']}#{pi}"
                corpus.append({"id": cid, "text": para["context"]})
                for qa in para["qas"]:
                    queries.append({"id": qa["id"], "text": qa["question"], "gold": {cid}, "type": "xquad"})
    else:
        raise ValueError(name)
    return corpus, (queries[:limit] if limit else queries)


class Embedder:
    """Wraps an API embedding model or a local sentence-transformers model behind one interface."""

    def __init__(self, ctx: RunContext, spec: ModelSpec):
        self.ctx, self.spec = ctx, spec
        o = spec.options or {}
        self.q_prefix, self.d_prefix = o.get("query_prefix", ""), o.get("doc_prefix", "")
        self.batch = o.get("batch_size", 32)
        self.dimensions = o.get("dimensions")
        self.usage_tokens = 0
        self.seconds = 0.0
        self.local = None
        if spec.task == "local_embedding":
            from ..local.backends import LocalEmbedder
            self.local = LocalEmbedder(o["hf_id"], max_length=o.get("max_length", 1024),
                                       trust_remote_code=bool(o.get("trust_remote_code")), dtype=o.get("dtype", "auto"),
                                       revision=o.get("revision"), attn_implementation=o.get("attn_implementation"))

    async def embed(self, texts: list[str], kind: str) -> np.ndarray:
        o = self.spec.options or {}
        prompt_name = o.get("query_prompt_name") if kind == "query" else o.get("doc_prompt_name")
        use_prompt = self.local is not None and self.local.has_prompt(prompt_name)
        pref = "" if use_prompt else (self.q_prefix if kind == "query" else self.d_prefix)
        self.how = getattr(self, "how", {})
        self.how[kind] = f"prompt_name={prompt_name}" if use_prompt else (f"prefix={pref!r}" if pref else "none")
        texts = [pref + t for t in texts]
        t0 = time.perf_counter()
        await self.ctx.latency_gate.wait(self.spec.endpoint)       # a latency session pauses pipeline work too
        if self.local is not None:
            vecs = await asyncio.to_thread(self.local.encode, texts, self.batch, prompt_name if use_prompt else None)
        else:
            client = self.ctx.client(self.spec.endpoint)
            ep = self.ctx.reg.endpoints[self.spec.endpoint]
            out = []
            for i in range(0, len(texts), self.batch):
                chunk = texts[i:i + self.batch]
                if ep.api_style == "bedrock_invoke":
                    flavor = (self.spec.options or {}).get("bedrock_flavor", "cohere")
                    call = lambda: client.embeddings_bedrock(self.spec.api_model, chunk, flavor=flavor, kind=kind,
                                                             dimensions=self.dimensions)
                else:
                    call = lambda: client.embeddings(self.spec.api_model, chunk, dimensions=self.dimensions)
                r = await guarded(self.ctx, self.spec.endpoint, call, estimate=sum(len(t) for t in chunk) // 3 + 1,
                                  what=f"embed {self.spec.id}")
                if r.outcome != OK:
                    raise RuntimeError(f"embedding failed for {self.spec.id}: {r.outcome} {r.error}")
                out.extend(r.data["vectors"])
                self.usage_tokens += int((r.data.get("usage") or {}).get("total_tokens") or
                                         (r.data.get("usage") or {}).get("prompt_tokens") or 0)
            vecs = np.array(out, dtype=np.float32)
        self.seconds += time.perf_counter() - t0
        n = np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs / np.maximum(n, 1e-12)


async def run_retrieval(ctx: RunContext, retriever_ids: list[str], reranker_ids: list[str], *, dataset: str = "rag",
                        top_n_rerank: int = 20, limit: int | None = None, rerank_limit: int | None = None,
                        plan_meta: dict | None = None, rerank_on: list[str] | None = None) -> dict:
    corpus, queries = retrieval_dataset(dataset, limit)
    doc_ids = [d["id"] for d in corpus]
    plan_hash = sha256_obj({"dataset": dataset, "corpus": [d["text"] for d in corpus],
                            "queries": [(q["id"], q["text"], sorted(q["gold"])) for q in queries]})
    ctx.session.write_plan(f"retrieval_{dataset}", [{"query_id": q["id"], "item_id": q["id"], "text": q["text"],
                                                     "gold": sorted(q["gold"])} for q in queries],
                           meta={"plan_hash_semantic": plan_hash, "rerank_limit": rerank_limit, **(plan_meta or {})})
    out_dir = ctx.session.suite_dir(f"retrieval_{dataset}")
    rankings: dict[str, dict[str, list[str]]] = {}
    summary: dict[str, Any] = {"plan_hash": plan_hash, "n_queries": len(queries), "n_docs": len(corpus), "systems": {}}
    w = ResultWriter(out_dir / "rankings.jsonl")

    def flush(sysname: str, ranks: dict[str, list[str]], failed: set | None = None) -> None:
        """Persist one system's rankings immediately (a later failure cannot lose them)."""
        for q in queries:
            key = f"{sysname}|{q['id']}"
            if key in w.done:
                continue
            if q["id"] in (failed or set()):
                w.write_sync({"key": key, "final": False, "suite": f"retrieval_{dataset}", "session": ctx.session.id,
                              "plan_hash": plan_hash, "variant": sysname, "item_id": q["id"], "repeat": 0,
                              "error": "rerank failed for this query", "harness_version": HARNESS_VERSION})
                continue
            if q["id"] not in ranks:
                continue
            m = retrieval_metrics(ranks[q["id"]], q["gold"])
            w.write_sync({"key": key, "final": True, "suite": f"retrieval_{dataset}", "session": ctx.session.id,
                          "plan_hash": plan_hash, "variant": sysname, "item_id": q["id"], "repeat": 0,
                          "type": q["type"], "top10": ranks[q["id"]][:10], "gold": sorted(q["gold"]), "score": m,
                          "harness_version": HARNESS_VERSION})

    for rid in retriever_ids:
        t0 = time.perf_counter()
        try:
            if rid == "bm25-f5":
                bm = BM25([d["text"] for d in corpus])
                rankings[rid] = {q["id"]: [doc_ids[i] for i in bm.rank(q["text"])[:50]] for q in queries}
                info = {"seconds": round(time.perf_counter() - t0, 2), "kind": "lexical"}
            else:
                spec = ctx.reg.model(rid)
                emb = Embedder(ctx, spec)
                cache = out_dir / "embeddings" / f"{safe_name(rid)}.npz"
                o = spec.options or {}
                key = sha256_obj([spec.api_model, spec.endpoint, emb.d_prefix, o.get("doc_prompt_name"), o.get("revision"),
                                  o.get("max_length"), o.get("dtype"), emb.dimensions, [d["text"] for d in corpus]])
                dv = None
                doc_dtype = None
                if cache.exists():
                    z = np.load(cache, allow_pickle=False)
                    if str(z["key"]) == key:
                        dv = z["docs"]
                        doc_dtype = str(z["dtype"]) if "dtype" in z.files else None
                if dv is None:
                    ctx.log(f"[retrieval] embedding {len(corpus)} passages with {rid}")
                    dv = await emb.embed([d["text"] for d in corpus], "doc")
                    doc_dtype = emb.local.dtype if emb.local is not None else None
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    np.savez(cache, key=np.array(key), docs=dv, dtype=np.array(str(doc_dtype)))
                qcache = out_dir / "embeddings" / f"{safe_name(rid)}.queries.npz"
                qkey = sha256_obj([spec.api_model, spec.endpoint, emb.q_prefix, o.get("query_prompt_name"),
                                   o.get("revision"), o.get("max_length"), o.get("dtype"), emb.dimensions,
                                   [q["text"] for q in queries]])
                qv = None
                if qcache.exists():
                    z = np.load(qcache, allow_pickle=False)
                    if str(z["key"]) == qkey:
                        qv = z["queries"]
                        ctx.log(f"[retrieval] {rid}: query embeddings from the cache (nothing sent)")
                if qv is None:
                    qv = await emb.embed([q["text"] for q in queries], "query")
                    np.savez(qcache, key=np.array(qkey), queries=qv)
                sims = qv @ dv.T
                rankings[rid] = {q["id"]: [doc_ids[j] for j in np.argsort(-sims[i])[:50]] for i, q in enumerate(queries)}
                info = {"seconds": round(time.perf_counter() - t0, 2), "kind": "dense", "dim": int(dv.shape[1]),
                        "api_tokens": emb.usage_tokens, "endpoint": spec.endpoint, "inputs": getattr(emb, "how", {}),
                        **({"dtype": emb.local.dtype, "doc_dtype": doc_dtype} if emb.local is not None else {}),
                        **({"compat": emb.local.compat} if getattr(emb.local, "compat", None) else {})}
        except Exception as exc:                                   # one broken retriever must not cost the others
            ctx.log(f"[retrieval] {rid} FAILED: {type(exc).__name__}: {exc}")
            summary["systems"][rid] = {"failed": f"{type(exc).__name__}: {exc}"[:500]}
            continue
        summary["systems"][rid] = info
        flush(rid, rankings[rid])

    # second stage: rerank each first-stage ranking
    for rr in reranker_ids:
        try:
            spec = ctx.reg.model(rr)
            local = None
            if spec.task == "local_rerank":
                from ..local.backends import LocalReranker
                o = spec.options or {}
                local = LocalReranker(o["hf_id"], max_length=int(o.get("max_length", 1024)), dtype=o.get("dtype", "auto"),
                                      trust_remote_code=bool(o.get("trust_remote_code")), revision=o.get("revision"))
            client = None if local else ctx.client(spec.endpoint)
        except Exception as exc:
            ctx.log(f"[retrieval] reranker {rr} unavailable: {type(exc).__name__}: {exc}")
            summary["systems"][rr] = {"failed": f"{type(exc).__name__}: {exc}"[:500]}
            continue
        text_of = {d["id"]: d["text"] for d in corpus}
        for rid in [r for r in retriever_ids if r in rankings and (not rerank_on or r in rerank_on)]:
            name = f"{rid}+{rr}"
            t0 = time.perf_counter()
            lat: list[float] = []
            new: dict[str, list[str]] = {}
            failed: set[str] = set()
            qsub = queries[:rerank_limit] if rerank_limit else queries
            sem = asyncio.Semaphore(ctx.opts.concurrency)

            async def one(q, rid=rid, rr=rr, name=name, new=new, failed=failed, lat=lat):
                if f"{name}|{q['id']}" in w.done:                # finished in an earlier run: nothing is re-sent
                    return
                cand = rankings[rid][q["id"]][:top_n_rerank]
                docs = [text_of[c] for c in cand]
                async with sem:
                    await ctx.latency_gate.wait(spec.endpoint)
                    s0 = time.perf_counter()
                    try:
                        if local is not None:
                            scores = await asyncio.to_thread(local.score, q["text"], docs)
                        else:
                            est = (len(q["text"]) + sum(len(d) for d in docs)) // 3 + 1
                            r = await guarded(ctx, spec.endpoint,
                                              lambda: client.rerank(spec.api_model, q["text"], docs),
                                              estimate=est, what=f"rerank {rr}")
                            if r.outcome != OK:
                                failed.add(q["id"])
                                return
                            scores = [s if s is not None else -1e9 for s in r.data["scores"]]
                    except Exception:                         # incl. QuotaStop: stays non-final, re-sent on resume
                        failed.add(q["id"])
                        return
                    lat.append(time.perf_counter() - s0)
                order = np.argsort(-np.array(scores, dtype=np.float64), kind="stable")
                new[q["id"]] = [cand[i] for i in order] + rankings[rid][q["id"]][top_n_rerank:]

            already = sum(1 for q in qsub if f"{name}|{q['id']}" in w.done)
            if already:
                ctx.log(f"[retrieval] {name}: {already} of {len(qsub)} queries already done (not re-sent)")
            await asyncio.gather(*(one(q) for q in qsub))
            rankings[name] = new
            summary["systems"][name] = {"seconds": round(time.perf_counter() - t0, 2), "kind": "rerank",
                                        "rerank_top_n": top_n_rerank, "queries": len(qsub), "already_done": already,
                                        "failed_queries": len(failed),
                                        "latency_p50_s": float(np.median(lat)) if lat else None,
                                        **({"dtype": local.dtype, "max_length": (spec.options or {}).get("max_length", 1024)}
                                           if local is not None else {})}
            if failed:
                ctx.log(f"[retrieval] {name}: {len(failed)} queries failed (stored as non-final; rerun to retry)")
            flush(name, new, failed)
    return summary


def unfinished(summary: dict) -> list[str]:
    """Failed systems / non-final requests left by a pipeline suite, in words (empty = complete)."""
    out = []
    for name, info in (summary.get("systems") or {}).items():
        if isinstance(info, dict) and info.get("failed"):
            out.append(f"{name} failed ({str(info['failed'])[:120]})")
        elif isinstance(info, dict) and info.get("failed_queries"):
            out.append(f"{name}: {info['failed_queries']} queries not finished")
    for name, counts in (summary.get("models") or {}).items():
        if isinstance(counts, dict) and counts.get("failed"):
            out.append(f"{name} failed ({str(counts['failed'])[:120]})")
        elif isinstance(counts, dict):
            n = sum(v for k, v in counts.items() if isinstance(v, int) and not pipeline_final(k))
            if n:
                out.append(f"{name}: {n} requests not finished")
    return out


# ============================================================================ OCR
def ocr_manifest(limit: int | None = None) -> list[dict]:
    p = DATA_DIR / "ocr" / "manifest.jsonl"
    if not p.exists():
        raise FileNotFoundError("run: python scripts/build_ocr_pages.py")
    rows = _jsonl(p)
    return rows[:limit] if limit else rows


async def run_ocr(ctx: RunContext, model_ids: list[str], *, repeats: int = 1, limit: int | None = None,
                  plan_meta: dict | None = None) -> dict:
    pages = ocr_manifest(limit)
    base = DATA_DIR / "ocr"
    plan = [{"page_id": p["id"], "item_id": p["id"], "image_sha": p.get("image_sha256"), "category": p["category"]}
            for p in pages]
    plan_hash = ctx.session.write_plan("ocr", plan, meta={"repeats": repeats, **(plan_meta or {})})
    summary = {"plan_hash": plan_hash, "pages": len(pages), "models": {}}
    for mid in model_ids:
        spec = ctx.reg.model(mid)
        w = ResultWriter(ctx.session.suite_dir("ocr") / f"{safe_name(mid)}.jsonl")
        local = None
        if spec.task == "local_ocr":
            if all(f"{p['id']}#r{r}" in w.done for p in pages for r in range(repeats)):
                summary["models"][mid] = {"skipped": "all pages done"}    # no model load for nothing
                continue
            try:
                from ..local.backends import make_local_ocr
                if (spec.options or {}).get("backend") == "worker":
                    await ctx.latency_gate.wait(spec.endpoint)     # no model load during a latency measurement
                    if ctx.stop.is_set():
                        break
                    ctx.log(f"[ocr] {mid}: starting its worker (loads the model; the first run also downloads it)")
                local = await asyncio.to_thread(make_local_ocr, spec, ctx.session.suite_dir("ocr"))
                if getattr(local, "info", None):
                    summary.setdefault("workers", {})[mid] = local.info
                    ctx.log(f"[ocr] {mid}: worker ready — {json.dumps(local.info, ensure_ascii=False)[:400]}")
            except Exception as exc:                          # e.g. Tesseract not installed: the other models still run
                ctx.log(f"[ocr] {mid} FAILED: {type(exc).__name__}: {exc}")
                summary["models"][mid] = {"failed": f"{type(exc).__name__}: {exc}"[:500]}
                continue
        conc = int((spec.options or {}).get("concurrency", 2))
        sem = asyncio.Semaphore(conc)
        counts: dict[str, int] = {}

        async def one(page: dict, r: int, mid=mid, spec=spec, conc=conc, local=local):
            key = f"{page['id']}#r{r}"
            if key in w.done:
                return
            img = base / page["image"]
            gt = (base / page["gt"]).read_text(encoding="utf-8")
            async with sem:
                await ctx.latency_gate.wait(spec.endpoint)
                if local is not None:
                    if ctx.stop.is_set():                          # Ctrl+C: the remaining pages stay pending
                        return
                    t0 = time.perf_counter()
                    if hasattr(local, "run_detail"):               # a worker: timings, GPU memory, raw output
                        d = await asyncio.to_thread(local.run_detail, img)
                        text, err = d.get("text") or "", d.get("error")
                    else:
                        d = {}
                        text, err = await asyncio.to_thread(local.run, img)
                    # a worker that died / stopped answering is infrastructure (not final, re-sent next run); a page
                    # the model itself failed on (e.g. out of GPU memory in both modes) is a final local_error.  A
                    # worker restart before the page is not part of the page's time (restart_s, recorded).
                    res = {"outcome": OK if err is None else ("local_unavailable" if d.get("infra") else "local_error"),
                           "error": err, "text": text,
                           "latency": time.perf_counter() - t0 - float(d.get("restart_s") or 0)}
                    res.update({k: d[k] for k in ("device", "peak_vram_gb", "infer_s", "tokens_out", "tokens_per_s",
                                                  "mode", "raw", "restart_s") if d.get(k) is not None})
                else:
                    client = ctx.client(spec.endpoint)
                    durl = image_to_data_url(img)
                    opts = spec.options or {}
                    try:
                        jr = await guarded(ctx, spec.endpoint,
                                           lambda: client.ocr(spec.api_model, durl, extra=opts.get("extra")),
                                           estimate=int(opts.get("est_tokens_per_page", 3000)), what=f"ocr {mid}")
                    except QuotaStop:
                        return                                # stays pending; the next run sends it
                    res = {"outcome": jr.outcome, "error": jr.error, "http_status": jr.http_status,
                           "text": (jr.data or {}).get("text", "") if jr.outcome == OK else "",
                           "raw": (jr.data or {}).get("raw") if jr.outcome == OK else None,
                           "latency": jr.latency, "attempts": jr.attempts}
            score = score_ocr(gt, res["text"]) if res["outcome"] == OK else None
            counts[res["outcome"]] = counts.get(res["outcome"], 0) + 1
            await w.write({"key": key, "final": pipeline_final(res["outcome"]), "suite": "ocr", "session": ctx.session.id,
                           "plan_hash": plan_hash, "variant": mid, "model_id": mid, "item_id": page["id"], "repeat": r,
                           "category": page["category"], "concurrency": conc, "result": res, "score": score,
                           "recorded_at": utc_now(), "harness_version": HARNESS_VERSION})

        try:
            await asyncio.gather(*(one(p, r) for p in pages for r in range(repeats)))
        finally:
            if local is not None and hasattr(local, "close"):
                await asyncio.to_thread(local.close)
        summary["models"][mid] = counts
        ctx.log(f"[ocr] {mid}: {counts}")
    return summary


# ============================================================================ ASR
def asr_manifest(limit: int | None = None) -> list[dict]:
    p = DATA_DIR / "asr" / "manifest.jsonl"
    if not p.exists():
        raise FileNotFoundError("run: python scripts/build_asr_clips.py")
    rows = _jsonl(p)
    return rows[:limit] if limit else rows


def wav_duration(p: Path) -> float:
    with wave.open(str(p), "rb") as w:
        return w.getnframes() / float(w.getframerate())


async def run_asr(ctx: RunContext, model_ids: list[str], *, repeats: int = 1, limit: int | None = None,
                  plan_meta: dict | None = None) -> dict:
    clips = asr_manifest(limit)
    base = DATA_DIR / "asr"
    plan_hash = ctx.session.write_plan("asr", [{"clip_id": c["id"], "item_id": c["id"], "audio_sha": c.get("audio_sha256")}
                                               for c in clips], meta={"repeats": repeats, **(plan_meta or {})})
    summary = {"plan_hash": plan_hash, "clips": len(clips), "models": {}}
    for mid in model_ids:
        spec = ctx.reg.model(mid)
        w = ResultWriter(ctx.session.suite_dir("asr") / f"{safe_name(mid)}.jsonl")
        local = None
        if spec.task == "local_asr":
            try:
                from ..local.backends import LocalWhisper
                local = LocalWhisper(spec.options.get("model_size", "large-v3"), spec.options.get("device", "auto"),
                                     spec.options.get("compute_type"))
            except Exception as exc:                          # e.g. faster-whisper not installed
                ctx.log(f"[asr] {mid} FAILED: {type(exc).__name__}: {exc}")
                summary["models"][mid] = {"failed": f"{type(exc).__name__}: {exc}"[:500]}
                continue
        counts: dict[str, int] = {}
        stopped = False
        for c in clips:
            if stopped:
                break
            for r in range(repeats):
                key = f"{c['id']}#r{r}"
                if key in w.done:
                    continue
                audio = base / c["audio"]
                dur = c.get("duration_s") or wav_duration(audio)
                await ctx.latency_gate.wait(spec.endpoint)
                if local is not None:
                    t0 = time.perf_counter()
                    text, err = await asyncio.to_thread(local.transcribe, audio, "tr")
                    res = {"outcome": OK if err is None else "local_error", "error": err, "text": text,
                           "latency": time.perf_counter() - t0, "engine": dict(local.info)}
                else:
                    client = ctx.client(spec.endpoint)
                    try:
                        jr = await guarded(ctx, spec.endpoint,
                                           lambda: client.transcribe(spec.api_model, audio,  # noqa: B023 (awaited here)
                                                                     language=spec.options.get("language", "tr"),
                                                                     response_format=spec.options.get("response_format", "json")),
                                           estimate=int(30 * dur) + 200, what=f"asr {mid}")
                    except QuotaStop:
                        stopped = True                        # the rest stays pending; the next run sends it
                        break
                    res = {"outcome": jr.outcome, "error": jr.error, "http_status": jr.http_status,
                           "text": (jr.data or {}).get("text", "") if jr.outcome == OK else "",
                           "latency": jr.latency, "attempts": jr.attempts}
                res["duration_s"] = dur
                res["rtf"] = (res["latency"] / dur) if res.get("latency") and dur else None
                score = score_asr(c["ref"], res["text"]) if res["outcome"] == OK else None
                counts[res["outcome"]] = counts.get(res["outcome"], 0) + 1
                w.write_sync({"key": key, "final": pipeline_final(res["outcome"]), "suite": "asr",
                              "session": ctx.session.id, "plan_hash": plan_hash, "variant": mid, "model_id": mid,
                              "item_id": c["id"], "repeat": r, "kind": c.get("kind"), "result": res, "score": score,
                              "recorded_at": utc_now(), "harness_version": HARNESS_VERSION})
        summary["models"][mid] = counts
        ctx.log(f"[asr] {mid}: {counts}")
    return summary


# ============================================================================ typed decisions (laya / systemone)
# Every /v1/systemone model (laya, clef) writes plan_hash "laya:<hash>": the analysis reads that prefix as "typed
# decision interface" — the name is historical, the records of each model carry their own variant.
def parse_laya(ans: dict) -> dict:
    a = ans.get("answers") or {}
    out = {"parsed": bool(a)}
    b = a.get("birim") or {}
    out["birim"] = b.get("choice")
    out["birim_conf"] = b.get("answer_confidence", b.get("confidence"))     # clef calls it "confidence"
    s = (a.get("aciliyet") or {}).get("score")
    out["aciliyet_score"] = s
    out["aciliyet"] = int(min(2, max(0, round(float(s))))) if isinstance(s, (int, float)) else None
    n = (a.get("iptal_riski") or {}).get("noul")
    out["iptal_olasiligi"] = float(n) if isinstance(n, (int, float)) else None
    out["iptal_riski"] = int(n >= 0.5) if isinstance(n, (int, float)) else None
    out["truncated"] = bool(((ans.get("usage") or {}).get("truncated")))
    return out


async def run_laya(ctx: RunContext, model_id: str, *, repeats: int = 1, limit: int | None = None,
                   state_format: str = "string", plan_meta: dict | None = None) -> dict:
    from ..scoring.basic import score_decision
    spec = ctx.reg.model(model_id)
    qspec = decision_spec()
    questions = {k: v for k, v in qspec.items() if k in ("birim", "aciliyet", "iptal_riski")}
    tickets = _jsonl(DATA_DIR / "decision" / "tickets.jsonl")
    tickets = tickets[:limit] if limit else tickets
    plan_hash = ctx.session.write_plan("decision_laya", [{"id": t["id"], "item_id": t["id"], "text": t["text"]}
                                                         for t in tickets],
                                       meta={"repeats": repeats, "state_format": state_format, **(plan_meta or {})})
    client = ctx.client(spec.endpoint)
    w = ResultWriter(ctx.session.suite_dir("decision") / f"{safe_name(model_id)}__default.jsonl")
    counts: dict[str, int] = {}
    for t in tickets:
        for r in range(repeats):
            key = f"{t['id']}#r{r}"
            if key in w.done:
                continue
            state = t["text"] if state_format == "string" else {"body": t["text"]}
            await ctx.latency_gate.wait(spec.endpoint)
            jr = await guarded(ctx, spec.endpoint, lambda: client.systemone(spec.api_model, state, questions),
                               estimate=len(t["text"]) // 3 + 200, what=f"laya {model_id}")
            pred = parse_laya(jr.data) if jr.outcome == OK and isinstance(jr.data, dict) else {"parsed": False}
            score = {"pred": pred, **score_decision(t["labels"], pred)} if jr.outcome == OK else None
            counts[jr.outcome] = counts.get(jr.outcome, 0) + 1
            w.write_sync({"key": key, "final": pipeline_final(jr.outcome), "suite": "decision", "session": ctx.session.id,
                          "plan_hash": f"laya:{plan_hash}", "variant": f"{model_id}:default", "model_id": model_id,
                          "mode": "default", "item_id": t["id"], "repeat": r,
                          "result": {"outcome": jr.outcome, "latency": jr.latency, "error": jr.error, "raw": jr.data},
                          "score": score, "recorded_at": utc_now(), "harness_version": HARNESS_VERSION})
    # batch throughput (one call per ≤64 texts) — measured once, in the run that sent the tickets; a model without a
    # batch endpoint (clef: options.batch false) gets single calls only
    t0 = time.perf_counter()
    states = [t["text"] for t in tickets]
    batch_lat = []
    has_batch = (spec.options or {}).get("batch", True) is not False
    for i in (range(0, len(states), 64) if counts and has_batch else ()):
        chunk = states[i:i + 64]
        jr = await guarded(ctx, spec.endpoint, lambda: client.systemone_batch(spec.api_model, chunk, questions),  # noqa: B023
                           estimate=sum(len(x) for x in chunk) // 3 + 200, what=f"laya batch {model_id}")
        batch_lat.append({"n": len(chunk), "latency": jr.latency, "outcome": jr.outcome})
    return {"plan_hash": plan_hash, "outcomes": counts, "batch": batch_lat if has_batch else "no batch endpoint",
            "batch_total_s": round(time.perf_counter() - t0, 2)}
