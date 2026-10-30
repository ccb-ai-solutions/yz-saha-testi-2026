"""End-to-end: mock gateway -> probe -> every suite -> judge (+ calibration judge) -> human check -> analysis.

Runs on a TRIMMED copy of data/ (so full plans stay small and no --limit is needed) and checks the properties
that make results trustworthy:
  * nothing is sent without --yes (dry run prints the plan and the token table only);
  * keep-alive lines are skipped, two TTFTs are recorded for thinking models, stalls/drops/quota are classified;
  * runs are resumable; a latency session holds the latency lock, carries on where it stopped when started again in
    its window (only what is missing is sent), waits out a network outage and sends the requests it hit again;
  * the fairness gate drops a system with partial coverage AND ignores a --limit (smoke) plan;
  * docextract over OCR output, both guard formats, Bedrock-style embeddings, judge signatures, calibration.
Run: python -m pytest tests/test_e2e_mock.py -q   (≈2–4 minutes)
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def jl(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def wjl(p: Path, rows) -> None:
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def trimmed_data(dst: Path) -> Path:
    """A small but complete copy of data/: full (non --limit) plans stay fast on the mock."""
    shutil.copytree(ROOT / "data", dst, ignore=shutil.ignore_patterns("_cache", "*.wav"))
    qs = jl(dst / "rag" / "questions.jsonl")
    wjl(dst / "rag" / "questions.jsonl", [q for q in qs if q["answerable"]][:8] + [q for q in qs if not q["answerable"]][:2])
    wjl(dst / "ifeval_tr" / "tasks.jsonl", jl(dst / "ifeval_tr" / "tasks.jsonl")[:6])
    cases = jl(dst / "tools_tr" / "cases.jsonl")
    wjl(dst / "tools_tr" / "cases.jsonl", cases[:6] + [c for c in cases if c.get("followup")][:2])
    wjl(dst / "decision" / "tickets.jsonl", jl(dst / "decision" / "tickets.jsonl")[:8])
    inv = jl(dst / "docextract" / "invoices.jsonl")[:4]
    wjl(dst / "docextract" / "invoices.jsonl", inv)
    keep_inv = {i["id"] for i in inv}
    man = jl(dst / "ocr" / "manifest.jsonl")
    wjl(dst / "ocr" / "manifest.jsonl", man[:2] + [m for m in man if m.get("invoice_id") in keep_inv])
    g = jl(dst / "guard" / "prompts.jsonl")
    wjl(dst / "guard" / "prompts.jsonl", [x for x in g if x.get("source", "own") == "own"][:10]
        + [x for x in g if x.get("source") == "seneca-trbench"][:6])
    wjl(dst / "mcq" / "seneca_trbench_mcq.jsonl", jl(dst / "mcq" / "seneca_trbench_mcq.jsonl")[:12])
    return dst


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("trbench")
    data = trimmed_data(tmp / "data")
    port = free_port()
    e = dict(os.environ, TRBENCH_CONFIG_DIR=str(ROOT / "tests" / "configs"), TRBENCH_RESULTS_DIR=str(tmp / "results"),
             TRBENCH_DATA_DIR=str(data), MOCK_API_KEY="test", MOCK_BASE_URL=f"http://127.0.0.1:{port}/v1",
             MOCK_ROOT_URL=f"http://127.0.0.1:{port}", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_evren.py"), "--port", str(port)], env=e,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield e, tmp
    proc.terminate()


def cli(e, *args, check=True) -> str:
    p = subprocess.run([sys.executable, str(ROOT / "harness" / "run.py"), *args], env=e, capture_output=True, text=True,
                       encoding="utf-8", timeout=900)
    if check and p.returncode != 0:
        raise AssertionError(p.stdout + p.stderr)
    return p.stdout + p.stderr


def analyze(e, tmp, *extra) -> str:
    p = subprocess.run([sys.executable, str(ROOT / "analysis" / "analyze.py"), "--out", str(tmp / "report"),
                        "--kb", str(tmp / "kb.csv"), *extra], env=e, capture_output=True, text=True, encoding="utf-8",
                       timeout=900)
    assert p.returncode == 0, p.stdout + p.stderr
    return (tmp / "report" / "report.md").read_text(encoding="utf-8")


def test_dry_run_sends_nothing(env):
    e, tmp = env
    out = cli(e, "run", "--suite", "rag_qa", "--endpoint", "mock")
    assert "DRY RUN" in out and "tokens in" in out
    out = cli(e, "probe", "--endpoint", "mock")
    assert "DRY RUN" in out
    assert not list((tmp / "results").glob("track-*/*/*")), "a dry run must not create sessions"
    camp = tmp / "campaign.yaml"
    camp.write_text("phases:\n  - name: test\n    commands:\n      - run --suite rag_qa --endpoint mock --modes default\n"
                    "      - judge --suite rag_qa --assume-variants 3 --judge-repeats 0\n", encoding="utf-8")
    out = cli(e, "budget", "--campaign", str(camp))
    assert "grand total" in out and "EUR" in out


def test_probe_and_streaming_edge_cases(env):
    e, tmp = env
    out = cli(e, "probe", "--endpoint", "mock", "--models",
              "mock-think,mock-ds,mock-inline,mock-strict,mock-plain,mock-leak,mock-magistral", "--yes")
    # an off-switch that only moves the reasoning into the answer (GLM-5.3 on EVREN, 11 Oct) is not nothink
    leak = json.loads((tmp / "results" / "capabilities" / "mock" / "mock-leak.json").read_text(encoding="utf-8"))
    assert leak["modes"]["nothink"]["params"] == {"thinking": {"type": "disabled"}}
    assert "moved into the answer" in out
    # a vendor system prompt as the 'think' switch (Magistral): the prompt is prepended, [THINK] is split off
    mg = json.loads((tmp / "results" / "capabilities" / "mock" / "mock-magistral.json").read_text(encoding="utf-8"))
    assert mg["default_thinks"] is False and mg["modes"]["think"]["available"]
    cli(e, "run", "--suite", "rag_qa", "--models", "mock-magistral", "--modes", "default,think", "--limit", "3",
        "--repeats", "1", "--session", "vendor", "--yes")
    recs = jl(next((tmp / "results").glob("track-test/mock/vendor/rag_qa/mock-magistral__think.jsonl")))
    assert recs and all(r["effective_params"].get("system_prefix_file", "").endswith("magistral_small_2509_system.txt")
                        and "_system_prefix_file" not in r["effective_params"] for r in recs)
    assert all(r["result"]["reasoning_chars"] > 0 and "[THINK]" not in r["result"]["content"]
               and "[/THINK]" not in r["result"]["content"] for r in recs if r["result"]["outcome"] == "ok")
    plain = jl(next((tmp / "results").glob("track-test/mock/vendor/rag_qa/mock-magistral__default.jsonl")))
    assert all(not r["effective_params"].get("system_prefix_file") and not r["result"]["reasoning_chars"] for r in plain)
    caps = json.loads((tmp / "results" / "capabilities" / "mock" / "mock-strict.json").read_text(encoding="utf-8"))
    assert caps["token_param"] == "max_completion_tokens" and "seed" in caps["drop_params"]
    assert "temperature" not in caps["drop_params"] and "top_p" not in caps["drop_params"]
    ds = json.loads((tmp / "results" / "capabilities" / "mock" / "mock-ds.json").read_text(encoding="utf-8"))
    assert ds["modes"]["nothink"]["available"] and ds["modes"]["think_min"]["available"]
    assert ds["reasoning_format"] == ["reasoning"]

    from harness.client import ApiClient
    from harness.config import Endpoint
    ep = Endpoint(name="mock", base_url=e["MOCK_BASE_URL"], api_key_env="MOCK_API_KEY", auth="x-api-key")
    os.environ["MOCK_API_KEY"] = "test"

    async def go():
        async with ApiClient(ep) as c:
            msgs = [{"role": "user", "content": "Merhaba"}]
            stall = await c.stream_chat({"model": "mock-stall", "messages": msgs, "max_tokens": 64}, ttft_timeout=2, idle_timeout=5)
            drop = await c.stream_chat({"model": "mock-drop", "messages": [{"role": "user", "content": "En fazla 80 kelime"}], "max_tokens": 64})
            quota = await c.stream_chat({"model": "mock-quota", "messages": msgs, "max_tokens": 64})
            think = await c.stream_chat({"model": "mock-think", "messages": msgs, "max_tokens": 4096,
                                         "stream_options": {"include_usage": True}})
            return stall, drop, quota, think
    stall, drop, quota, think = asyncio.run(go())
    assert stall.outcome == "timeout_ttft" and stall.n_keepalive > 2
    assert drop.outcome in ("stream_incomplete", "stream_broken")
    assert quota.outcome == "http_429_quota"
    assert think.outcome == "ok" and think.t_first_reasoning < think.t_first_content and think.usage
    assert think.n_keepalive >= 1 and think.reasoning and think.content


def test_all_suites_judge_and_report(env):
    e, tmp = env
    cli(e, "probe", "--endpoint", "mock", "--models", "mock-think,mock-ds,mock-plain,mock-strict,mock-guard,mock-safeguard,"
        "mock-judge,mock-judge-b,auto,mock-flaky,mock-capped", "--yes")
    # an unprobed model is refused unless explicitly allowed
    out = cli(e, "run", "--suite", "ifeval", "--models", "mock-unprobed", "--endpoint", "mock", "--session", "refuse", "--yes")
    assert "no capability probe" in out
    common = ["--endpoint", "mock", "--session", "e2e", "--repeats", "2", "--yes"]
    cli(e, "run", "--suite", "rag_qa", "--models", "mock-think,mock-ds,mock-plain,mock-flaky", "--modes", "default,nothink,think_min", *common)
    for suite in ("ifeval", "tools", "decision", "docextract"):
        cli(e, "run", "--suite", suite, "--models", "mock-think,mock-plain", *common)
    # a second typed-decision model (clef on EVREN: no batch endpoint) on the same tickets, single calls only
    out = cli(e, "run", "--suite", "decision", "--systemone", "mock-clef", "--session", "e2e")
    assert "mock-clef:default" in out and "no batch endpoint" in out and "mock-think" not in out
    cli(e, "run", "--suite", "decision", "--systemone", "mock-clef", "--session", "e2e", "--yes")
    sd = next((tmp / "results").glob("track-test/mock/e2e"))
    clef = jl(sd / "decision" / "mock-clef__default.jsonl")
    assert len(clef) == 8 and all(r["result"]["outcome"] == "ok" and r["plan_hash"].startswith("laya:") for r in clef)
    man = json.loads((sd / "manifest.json").read_text(encoding="utf-8"))
    assert man["suites"]["decision_mock-clef"]["summary"]["batch"] == "no batch endpoint"
    assert isinstance(man["suites"]["decision_laya"]["summary"]["batch"], list)       # laya's summary is kept
    cli(e, "run", "--suite", "mcq", "--models", "mock-think,mock-plain", "--mcq-source", "seneca_trbench", *common)
    cli(e, "run", "--suite", "guard", "--endpoint", "mock", "--session", "e2e", "--yes")
    cli(e, "run", "--suite", "retrieval", "--endpoint", "mock", "--session", "e2e", "--yes")
    # a rerun sends nothing for finished work: query embeddings come from the cache, finished reranks are skipped
    out = cli(e, "run", "--suite", "retrieval", "--endpoint", "mock", "--session", "e2e", "--yes")
    assert "query embeddings from the cache" in out and "queries already done (not re-sent)" in out
    # a run of in-process models only (BM25 here; local Whisper on 12 Oct) joins the named session, not a new
    # track-*/local/<name> folder
    out = cli(e, "run", "--suite", "retrieval", "--retrievers", "bm25-f5", "--rerankers=", "--session", "e2e", "--yes")
    assert "joining the existing session" in out
    assert not list((tmp / "results").glob("track-*/local/e2e")), out
    cli(e, "run", "--suite", "ocr", "--endpoint", "mock", "--session", "e2e", "--yes")
    # an OCR model in its own Python environment (Unlimited-OCR): the worker protocol, with a fake model
    ef = dict(e, TRBENCH_OCR_WORKER_FAKE="1")
    out = cli(ef, "run", "--suite", "ocr", "--models", "mock-uocr", "--session", "e2e")
    assert "own environment .venv-ocr" in out and "DRY RUN" in out
    out = cli(ef, "run", "--suite", "ocr", "--models", "mock-uocr", "--session", "e2e", "--yes")
    assert "worker ready" in out and "joining the existing session" in out
    sd = next((tmp / "results").glob("track-test/mock/e2e"))
    uo = jl(sd / "ocr" / "mock-uocr.jsonl")
    n_pages = len(jl(sd / "plans" / "ocr.jsonl"))
    assert len(uo) == n_pages and all(r["result"]["outcome"] == "ok" and r["result"]["device"] == "fake"
                                      and r["concurrency"] == 1 and r["score"]["cer_flex"] < 0.1 for r in uo)
    assert "directly resize" in (sd / "ocr" / "mock-uocr.worker.log").read_text(encoding="utf-8")
    out = cli(ef, "run", "--suite", "ocr", "--models", "mock-uocr", "--session", "e2e", "--yes")
    assert "worker ready" not in out                                   # every page done: the model is not loaded
    cli(e, "run", "--suite", "docextract", "--models", "mock-think,mock-plain", "--doc-source", "ocr:mock-ocr-a",
        "--ocr-session", "e2e", *common)
    # provider concurrency cap (max_concurrency: 1) still completes
    cli(e, "run", "--suite", "ifeval", "--models", "mock-capped", "--endpoint", "mock-free", "--session", "capped",
        "--repeats", "1", "--yes")
    # latency: one contiguous session; a resume of the same session is refused
    out = cli(e, "run", "--suite", "latency", "--endpoint", "mock", "--models", "mock-think,mock-ds,mock-plain", "--session", "lat",
              "--prompts", "3", "--concurrency-levels", "2,4", "--ocr-probe", "mock-ocr-a")
    assert "latency (OCR timing)" in out and "2 pages one at a time + 4 with 2 in flight" in out and "time budget" in out
    assert "stage order: OCR timing, warm-up, chat → rag → long → sweep" in out     # no early sweep configured here
    cli(e, "run", "--suite", "latency", "--endpoint", "mock", "--models", "mock-think,mock-ds,mock-plain", "--session", "lat",
        "--prompts", "3", "--concurrency-levels", "2,4", "--ocr-probe", "mock-ocr-a", "--yes")
    lo = jl(next((tmp / "results").glob("track-test/mock/lat/latency_ocr/mock-ocr-a.jsonl")))
    assert sorted(r["concurrency"] for r in lo) == [1, 1, 2, 2, 2, 2]
    assert all(r["result"]["outcome"] == "ok" and r["score"]["cer_flex"] < 0.2 for r in lo)    # the comment is harmless
    assert len({r["image_bytes"] for r in lo}) >= 1 and all(r["result"]["latency"] > 0 for r in lo)
    lman = json.loads(next((tmp / "results").glob("track-test/mock/lat/manifest.json")).read_text(encoding="utf-8"))
    lsum = lman["suites"]["latency"]["summary"]
    assert lsum["budget_stops"] == [] and [c["concurrency"] for c in lsum["ocr_probe"]] == [1, 2]
    assert lsum["completed"] and lsum["end_reason"] == "complete" and len(lsum["parts"]) == 1
    # a finished session sends nothing more (the scheduled task starts it again every 10 minutes until its end)
    # read_jsonl skips a line cut by a kill, like the harness does
    from harness.store import read_jsonl
    n_lat = lambda name: sum(len(read_jsonl(f)) for f in (tmp / "results").glob(f"track-test/mock/{name}/latency/*.jsonl"))
    n0 = n_lat("lat")
    out = cli(e, "run", "--suite", "latency", "--endpoint", "mock", "--models", "mock-plain", "--session", "lat",
              "--prompts", "3", "--concurrency-levels", "2", "--yes")
    assert "is complete — nothing to send" in out and n_lat("lat") == n0
    assert not (tmp / "results" / ".latency.lock").exists()
    # ended from outside mid-session (14 Oct: the 15:00 / 21:00 sessions): the next start carries on — only what is
    # missing is sent; a sweep cell that did not finish is measured again from its start; a line cut by the kill is
    # skipped
    lat = ["run", "--suite", "latency", "--endpoint", "mock", "--models", "mock-think,mock-ds,mock-plain", "--prompts",
           "3", "--concurrency-levels", "2,4", "--no-ocr-probe", "--yes"]
    units = lambda rs: {(r["workload"], r["variant"], r["prompt_id"]) for r in rs if r["stage"] == "c1" and not r.get("outage")}
    for name in ("latk", "latw"):
        cli(e, *lat, "--session", name)
        sd = next((tmp / "results").glob(f"track-test/mock/{name}"))
        for f in (sd / "latency").glob("*.jsonl"):           # killed in the 'long' stage: mock-ds's long, the sweep
            keep = [r for r in jl(f) if r["stage"] != "sweep" and not (r["workload"] == "long" and r["model_id"] == "mock-ds")]
            f.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
        with open(sd / "latency" / "mock-plain__default.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"key": "c1|cut", "final": tr')
        prog = jl(sd / "latency_progress.jsonl")
        wjl(sd / "latency_progress.jsonl", [x for x in prog if x["event"] in ("part_start", "rtt", "quota")][:3])
        man = json.loads((sd / "manifest.json").read_text(encoding="utf-8"))
        man["suites"]["latency"].pop("summary")
        (sd / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    sd = next((tmp / "results").glob("track-test/mock/latk"))
    out = cli(e, *lat, "--session", "latk")
    assert "resuming session latk: part 2" in out and "3 variant(s) with requests left" in out   # warm-up: 3 of 6
    assert "single-stream 'long'" in out and "3 still to send" in out and "single-stream 'chat'" not in out
    after = [r for f in (sd / "latency").glob("*.jsonl") for r in read_jsonl(f)]
    new = [r for r in after if r.get("part") == 2]
    assert {(r["workload"], r["model_id"]) for r in new if r["stage"] == "c1"} == {("long", "mock-ds")}
    assert len(units(after)) == 36 and len([r for r in after if r["stage"] == "c1" and not r.get("outage")]) == 36
    assert {r["cell"] for r in new if r["stage"] == "sweep"} == {r["cell"] for r in after if r["stage"] == "sweep"}
    s2 = json.loads((sd / "manifest.json").read_text(encoding="utf-8"))["suites"]["latency"]["summary"]
    assert s2["completed"] and [p["part"] for p in s2["parts"]] == [1, 2] and s2["parts"][0]["end"].startswith("ended from outside")
    assert s2["coverage"]["c1"]["long"] == {"done": 9, "planned": 9} and s2["coverage"]["sweep_cells"]["measured"] == 6
    # ... unless its window is over: then nothing is sent, the summary is written from what it has (partial)
    sw = next((tmp / "results").glob("track-test/mock/latw"))
    nw = n_lat("latw")
    out = cli(e, *lat, "--session", "latw", "--until", "00:00")
    assert "its window ended at 00:00" in out and "partial (window_over)" in out and n_lat("latw") == nw
    sws = json.loads((sw / "manifest.json").read_text(encoding="utf-8"))["suites"]["latency"]["summary"]
    assert sws["partial"] and sws["end_reason"] == "window_over" and sws["coverage"]["c1"]["long"]["done"] == 6
    out = cli(e, *lat, "--session", "latnew", "--until", "00:00")      # a slot whose window is over: not started
    assert "has passed — nothing to send" in out and not list((tmp / "results").glob("track-test/mock/latnew"))
    # a network outage on this machine: from its 25th chat request on, the mock cuts every connection for 1.5 s —
    # the streams wait for it, the requests it hit are sent again, the records it hit are labelled and left out
    req = urllib.request.Request(e["MOCK_ROOT_URL"] + "/__offline", method="POST",
                                 data=json.dumps({"after_chat_requests": 25, "seconds": 1.5}).encode())
    urllib.request.urlopen(req, timeout=10).read()
    out = cli(e, *lat, "--session", "latnet")
    assert "cannot be reached from this machine" in out and "answers again after" in out
    rn = [r for f in next((tmp / "results").glob("track-test/mock/latnet")).glob("latency/*.jsonl") for r in jl(f)]
    hit = [r for r in rn if r.get("outage")]
    assert hit and len(units(rn)) == 36 and any(r["try"] == 1 for r in rn if r["stage"] == "c1")
    sn = json.loads(next((tmp / "results").glob("track-test/mock/latnet/manifest.json")).read_text(encoding="utf-8"))
    sn = sn["suites"]["latency"]["summary"]
    assert sn["completed"] and len(sn["outages"]) == 1 and sn["outages"][0]["seconds"] >= 1.0
    assert sn["coverage"]["requests_hit_by_outages"] == len(hit)
    # a model that is slow right now (15 Oct afternoon / evening: every stage waited for one stream at its time
    # budget): mock-ds uses up its 'chat' budget -> it is left out of rag, the sweep and long, and the session still
    # completes; a short window (--until in an hour) runs the sweep before 'long'
    from datetime import datetime, timedelta

    from harness.quota import TSI
    cfgdir = tmp / "configs-slow"
    shutil.copytree(ROOT / "tests" / "configs", cfgdir)
    sy = (cfgdir / "suites.yaml").read_text(encoding="utf-8")
    assert "time_budget_s: {warmup: 600, chat: 1200," in sy and "early_sweep_below_minutes" not in sy
    assert "  warmup_requests: 3 " in sy                 # the fast models need ~1 s for their 3 chat prompts
    (cfgdir / "suites.yaml").write_text(sy.replace("time_budget_s: {warmup: 600, chat: 1200,", "time_budget_s: {warmup: 600, chat: 4,")
                                        .replace("  warmup_requests: 3 ", "  warmup_requests: 1 ")
                                        .replace("  max_parts: 12\n", "  max_parts: 12\n  early_sweep_below_minutes: 150\n"),
                                        encoding="utf-8")

    def post(path, body):
        urllib.request.urlopen(urllib.request.Request(e["MOCK_ROOT_URL"] + path, method="POST",
                                                      data=json.dumps(body).encode()), timeout=10).read()
    post("/__slow", {"model": "mock-ds", "extra_s": 2.5})          # 2 of its 3 chat prompts, then its budget is used up
    try:
        until = (datetime.now(TSI) + timedelta(minutes=60)).isoformat(timespec="minutes")
        out = cli(dict(e, TRBENCH_CONFIG_DIR=str(cfgdir)), *lat, "--session", "latslow", "--until", until)
    finally:
        post("/__slow", {"model": "mock-ds", "extra_s": 0})
    assert "stage order: chat → rag → sweep → long" in out and "time budget: mock-ds stopped in c1 'chat'" in out
    assert "mock-ds: left out of 'rag'" in out and "mock-ds: left out of 'long'" in out
    assert "time budget used up: mock-ds in 'chat' (left out of the later stages)" in out
    sl = next((tmp / "results").glob("track-test/mock/latslow"))
    rs = [r for f in (sl / "latency").glob("*.jsonl") for r in jl(f)]
    ds = [r for r in rs if r["model_id"] == "mock-ds" and r["stage"] in ("c1", "sweep")]
    assert {(r["stage"], r["workload"]) for r in ds} == {("c1", "chat")} and 1 <= len(ds) < 3
    sweep_at = max(r["recorded_at"] for r in rs if r["stage"] == "sweep")
    assert sweep_at < min(r["recorded_at"] for r in rs if r["stage"] == "c1" and r["workload"] == "long")
    assert sweep_at > max(r["recorded_at"] for r in rs if r["stage"] == "c1" and r["workload"] == "rag")
    ss = json.loads((sl / "manifest.json").read_text(encoding="utf-8"))["suites"]["latency"]["summary"]
    assert ss["completed"] and ss["slow_models"] == {"mock-ds": "chat"}
    assert ss["stage_order"] == ["chat", "rag", "sweep", "long"]
    assert [(x["workload"], x["reason"]) for x in ss["budget_stops"]] == [("chat", "time_budget"), ("rag", "slow"),
                                                                           ("long", "slow")]
    assert sorted(c["concurrency"] for c in ss["sweeps"] if c.get("slow_after") == "chat") == [2, 4]
    # resumability: re-running the same session must not repeat finished requests
    out = cli(e, "run", "--suite", "ifeval", "--models", "mock-think", *common)
    assert "0 to run" in out
    # extending a session with another model (RUN_PLAN: "DeepSeek first, then the rest in the same session")
    out = cli(e, "run", "--suite", "ifeval", "--models", "mock-think,mock-ds", *common)
    assert "mock-think:default: 0 to run" in out and "mock-ds:default: 12 to run (0 already done)" in out
    # fairness gate 1: same plan, partial coverage -> excluded with the coverage as reason
    cli(e, "run", "--suite", "ifeval", "--models", "mock-strict", "--endpoint", "mock", "--session", "partial",
        "--repeats", "2", "--yes")
    f = next((tmp / "results").glob("track-test/mock/partial/ifeval/mock-strict__default.jsonl"))
    lines = f.read_text(encoding="utf-8").splitlines()
    f.write_text("\n".join(lines[: len(lines) // 2]) + "\n", encoding="utf-8")
    # fairness gate 2: a --limit (smoke) plan with an extra system must not become the comparison plan
    cli(e, "run", "--suite", "tools", "--models", "mock-think,mock-plain,mock-ds", "--endpoint", "mock", "--session", "smoke",
        "--limit", "3", "--repeats", "1", "--yes")
    # judge: main judge on everything + a calibration judge on a deterministic 50% sample
    cli(e, "judge", "--session", "e2e", "--suite", "rag_qa", "--yes")
    cli(e, "judge", "--session", "e2e", "--suite", "rag_qa", "--judge-model", "mock-judge-b", "--sample", "0.5", "--yes")
    cli(e, "judge", "--session", "e2e", "--suite", "ifeval", "--yes")
    out = cli(e, "judge", "--session", "e2e", "--suite", "rag_qa", "--yes")
    assert "cached:" in out and "0 answers to judge" in out           # verdicts are cached per signature + answer
    # judge transport "batch" (Anthropic Message Batches on the mock): submit without waiting, collect later,
    # an errored result is re-submitted, a non-JSON result gets the repair turn, nothing is sent twice
    cli(e, "run", "--suite", "rag_qa", "--models", "mock-plain,mock-think", "--endpoint", "mock", "--session", "batchj",
        "--repeats", "1", "--modes", "default", "--yes")
    bj = ["judge", "--session", "batchj", "--suite", "rag_qa", "--judge-model", "mock-judge-b", "--transport", "batch",
          "--poll-seconds", "0.2", "--yes"]
    out = cli(e, *bj, "--no-wait")
    assert "batch price" in out and "[batch] submitted" in out and "still processing" in out
    assert "spend cap" in out and "1.5 × this estimate" in out                  # shown in the table before sending
    out = cli(e, *bj, check=False)                       # one errored result -> the step reports a failure
    assert "already in open batches" in out and "[batch] collected" in out and "[batch] submitted" not in out
    assert "1 verdict(s) failed" in out and "mock overload" in out
    jf = next((tmp / "results").glob("track-test/mock/batchj/rag_qa/judge/*.jsonl"))
    recs = jl(jf)
    assert sum(1 for r in recs if not r["final"]) == 1                          # the errored one
    assert any((r.get("judge_meta") or {}).get("repair") for r in recs if r["final"])
    assert all((r.get("judge_meta") or {}).get("transport") == "batch" for r in recs if r["final"]
               and (r.get("judge_meta") or {}).get("outcome") != "skipped_no_answer")
    out = cli(e, *bj)
    assert "1 answers to judge" in out and "submitted msgbatch_mock" in out and "with 1 requests" in out
    out = cli(e, *bj)
    assert "0 answers to judge" in out
    # mock-judge-b refuses `temperature` like Claude Opus 5.5: the probe dropped it, so batch requests must not carry
    # it (10 Oct: 40 of 40 errored).  With the drop removed from the probe record every request errors and the step
    # fails loudly; restored, the same command re-sends only those answers.
    capf = tmp / "results" / "capabilities" / "mock" / "mock-judge-b.json"
    caps = json.loads(capf.read_text(encoding="utf-8"))
    assert "temperature" in caps["drop_params"]
    cli(e, "run", "--suite", "rag_qa", "--models", "mock-plain", "--endpoint", "mock", "--session", "batchk",
        "--repeats", "1", "--modes", "default", "--limit", "3", "--yes")
    bk = ["judge", "--session", "batchk", "--suite", "rag_qa", "--judge-model", "mock-judge-b", "--transport", "batch",
          "--poll-seconds", "0.2", "--yes"]
    # spend cap: a cap below the estimate stops the judge before anything is submitted (non-zero exit)
    p = subprocess.run([sys.executable, str(ROOT / "harness" / "run.py"), *bk, "--max-cost", "0.000001"], env=e,
                       capture_output=True, text=True, encoding="utf-8", timeout=900, check=False)
    out = p.stdout + p.stderr
    assert p.returncode != 0 and "stopped by its spend cap" in out and "[batch] submitted" not in out
    assert "To accept up to" in out and "--max-cost" in out
    ledger = next((tmp / "results").glob("track-test/mock/batchk/rag_qa/judge/spend/*.jsonl"))
    ev = jl(ledger)
    assert [e["event"] for e in ev] == ["start", "cap_stop", "end"] and ev[-1]["status"] == "stopped_by_spend_cap"
    assert ev[0]["answers_to_judge"] == ev[-1]["answers_left"] and ev[-1]["next"] == ev[1]["next_command"]
    capf.write_text(json.dumps({**caps, "drop_params": [d for d in caps["drop_params"] if d != "temperature"]}),
                    encoding="utf-8")
    p = subprocess.run([sys.executable, str(ROOT / "harness" / "run.py"), *bk], env=e, capture_output=True, text=True,
                       encoding="utf-8", timeout=900)
    out = p.stdout + p.stderr
    assert p.returncode != 0 and "verdict(s) failed" in out and "`temperature` is deprecated" in out
    capf.write_text(json.dumps(caps), encoding="utf-8")
    out = cli(e, *bk, check=False)              # (the mock overloads the first request of a batch once per answer)
    assert "[batch] submitted" in out and "is deprecated" not in out
    out = cli(e, *bk)
    assert '"judge_failed": 0' in out
    stems = sorted(p.stem for p in next((tmp / "results").glob("track-test/mock/e2e/rag_qa/judge")).glob("*.jsonl"))
    assert len(stems) == 2 and all("__" in s for s in stems)
    main_stem = next(s for s in stems if "mock-judge-b" not in s)
    out = cli(e, "human-export", "--session", "e2e", check=False)
    assert "choose one judge file" in out                                 # two judges → must say which
    cli(e, "human-export", "--session", "e2e", "--judge-model", "mock-judge")
    assert main_stem.startswith("mock-mock-judge-default__")
    # simulate annotators: agree with the judge on every labelled row
    hc = next((tmp / "results").glob("track-test/mock/e2e/rag_qa/human_check"))
    key = {r["sample_id"]: r for r in csv.DictReader(open(hc / "KEY_do_not_share_with_annotators.csv", encoding="utf-8-sig"))}
    rows = list(csv.DictReader(open(hc / "sheet.csv", encoding="utf-8-sig")))
    for r in rows:
        r["human_verdict"] = key[r["sample_id"]]["judge_verdict"]
    with open(hc / "sheet.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    md = analyze(e, tmp)
    assert "Capability matrix" in md and "mock-think:default" in md
    assert "stable_share" in md                                           # free answer-stability check of repeats
    if_sec = md.split("## Instruction following", 1)[1].split("\n## ", 1)[0]
    assert "mock-strict:default" in if_sec and "coverage" in if_sec        # excluded, with the reason
    tools_sec = md.split("## Tool calling", 1)[1].split("\n## ", 1)[0]
    assert "limited" in tools_sec                                         # the smoke plan is listed as not used
    assert "mock-ds:default" not in tools_sec.split("Not comparable", 1)[0]
    assert "Turkish language competence" in md                            # seneca MCQ
    assert "Safety classification" in md and "mock-safeguard:default" in md and "mock-guard:default" in md
    assert "Document extraction over OCR" in md and "clean text vs OCR" in md
    dec_sec = md.split("## Decision / routing", 1)[1].split("\n## ", 1)[0]
    assert "mock-clef:default" in dec_sec and "mock-laya:default" in dec_sec              # both typed models
    ocr_sec = md.split("## OCR (Turkish scanned pages)", 1)[1].split("\n## ", 1)[0]
    assert "### ocr speed" in ocr_sec and "mock (hosted)" in ocr_sec and "latency_p90_s" in ocr_sec
    assert "mock-uocr" in ocr_sec and "this PC (fake)" in ocr_sec
    assert "## OCR timing by time of day (latency sessions)" in md and "pages_per_min" in md
    lat_sec = md.split("## Latency as experienced", 1)[1].split("\n## ", 1)[0]
    assert "| latk | ran in 2 parts" in lat_sec and "| latw | ended from outside" in lat_sec       # session table
    assert "| latnet | endpoint unreachable (the network of this PC or the provider" in lat_sec   # the mock cut the
    assert "left out" in lat_sec and "same_sessions" in lat_sec                                 # connections: no DNS
    assert "mock-ds left out of rag, long and the sweep" in lat_sec and "the sweep ran before long" in lat_sec
    assert "mock-cohere" in md and "mock-titan" in md
    assert "judge calibration" in md and "judge vs human agreement" in md
    assert "Method notes (generated from the runs)" in md and "temperature" in md
    lat = jl(next((tmp / "results").glob("track-test/mock/lat/latency/mock-ds__default.jsonl")))
    c1 = [r for r in lat if r["stage"] == "c1"]
    assert c1 and all(r["result"]["t_first_reasoning"] is not None for r in c1 if r["result"]["outcome"] == "ok")
    assert all(r.get("config_hash") for r in lat) and all("in_flight_at_start" in r for r in lat)
    meta = json.loads(next((tmp / "results").glob("track-test/mock/smoke/plans/tools.meta.json")).read_text(encoding="utf-8"))
    assert meta["limit"] == 3
    plan = jl(next((tmp / "results").glob("track-test/mock/e2e/plans/rag_qa.jsonl")))
    assert all(it["decoding"] == {"temperature": 0.6, "top_p": 0.95} for it in plan)
    kb = (tmp / "kb.csv").read_text(encoding="utf-8")
    assert "Grounded QA" in kb
    # --include-limited analyses but never writes the knowledge base
    n_before = kb.count("\n")
    analyze(e, tmp, "--include-limited")
    assert (tmp / "kb.csv").read_text(encoding="utf-8").count("\n") == n_before


def test_mistral_style_api(env):
    """Mistral's API: 422 for unknown fields, random_seed, reasoning_effort none|high, thinking as typed content chunks."""
    e, tmp = env
    cli(e, "probe", "--models", "mock-mistral", "--yes")
    caps = json.loads((tmp / "results" / "capabilities" / "mock" / "mock-mistral.json").read_text(encoding="utf-8"))
    assert caps["available"] and caps["default_thinks"] is False
    assert caps["drop_params"] == ["stream_options"]                     # seed is kept: sent as random_seed
    assert caps["modes"]["think"]["available"] and caps["modes"]["think"]["params"] == {"reasoning_effort": "high"}
    assert caps["modes"]["think"]["evidence"]["reasoning_fields"] == ["content_thinking_parts"]
    assert caps["usage_in_stream"]
    cli(e, "run", "--suite", "rag_qa", "--models", "mock-mistral", "--endpoint", "mock", "--session", "mistral",
        "--repeats", "1", "--yes")
    d = next((tmp / "results").glob("track-test/mock/mistral/rag_qa"))
    assert sorted(p.name for p in d.glob("mock-mistral__*.jsonl")) == ["mock-mistral__default.jsonl", "mock-mistral__think.jsonl"]
    think = [r for r in jl(d / "mock-mistral__think.jsonl") if r["result"]["outcome"] == "ok"]
    plain = [r for r in jl(d / "mock-mistral__default.jsonl") if r["result"]["outcome"] == "ok"]
    assert think and plain
    assert all(r["result"]["t_first_reasoning"] is not None and r["result"]["t_first_reasoning"] < r["result"]["t_first_content"]
               for r in think)
    assert all(r["result"]["t_first_reasoning"] is None for r in plain)
    assert all("[" not in r["result"]["content"][:1] and "thinking" not in r["result"]["content"] for r in think)
    eff = think[0]["effective_params"]
    assert eff["seed"] is not None and eff["param_aliases"] == {"seed": "random_seed"}
