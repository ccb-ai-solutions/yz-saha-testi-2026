"""Unit tests for the parts every number depends on: Turkish text handling, scorers, statistics.
Run: python -m pytest tests -q"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import stats as S  # noqa: E402
from harness.client import ApiClient, ChatResult, classify_http_error  # noqa: E402
from harness.scoring.basic import (cer_flex, extract_choice, is_refusal, parse_guard, retrieval_metrics,  # noqa: E402
                                   score_asr, score_extraction, score_rag, score_tool_first_turn)
from harness.scoring.ifeval_checks import run_checks  # noqa: E402
from harness.textnorm import casefold_tr, contains_number, norm_for_wer, tr_lower, tr_number_words, tr_upper  # noqa: E402

DATA = ROOT / "data"


def jl(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


# ------------------------------------------------------------------ Turkish text
def test_turkish_casing():
    assert tr_upper("istanbul'da ılık iğne") == "İSTANBUL'DA ILIK İĞNE"
    assert tr_lower("IŞIK İZMİR") == "ışık izmir"
    assert casefold_tr("  İSTANBUL   Işık ") == "istanbul ışık"


@pytest.mark.parametrize("n,words", [(14, "on dört"), (270, "iki yüz yetmiş"), (1000, "bin"), (1250, "bin iki yüz elli"),
                                     (1_000_000, "bir milyon"), (100, "yüz")])
def test_number_words(n, words):
    assert " ".join(tr_number_words(n)) == words


def test_contains_number_variants_and_boundaries():
    assert contains_number("Yıllık izin ondört günden az olamaz", "14")
    assert contains_number("en fazla 270 saat", "270")
    assert contains_number("toplam 1.000.000 TL", "1.000.000")
    assert contains_number("yedi buçuk saati geçemez", "7,5")
    assert not contains_number("140 gün", "14")
    assert not contains_number("konu hakkında", "10")      # 'on' inside a word must not match


def test_wer_normalisation():
    assert norm_for_wer("İstanbul'da, TOPLANTI!") == "istanbulda toplantı"
    assert norm_for_wer("3 gün", numbers_to_words=True) == "üç gün"


# ------------------------------------------------------------------ IF-TR checker
def test_ifeval_references_pass_and_empty_fails():
    tasks = jl(DATA / "ifeval_tr" / "tasks.jsonl")
    assert len(tasks) == 30
    for t in tasks:
        r = run_checks(t, t["reference_response"])
        assert all(c["pass"] is not False for c in r["checks"]), (t["id"], r)
        assert not run_checks(t, "")["strict"]


def test_ifeval_turkish_specific_negatives():
    upper = {"checks": [{"type": "uppercase_tr"}]}
    assert run_checks(upper, "İSTANBUL'DA İŞÇİ")["strict"]
    assert not run_checks(upper, "ISTANBUL'DA IŞÇI ve")["strict"]
    num = {"checks": [{"type": "tr_number_format"}]}
    assert run_checks(num, "Toplam 12.500,75 TL")["strict"]
    assert not run_checks(num, "Total 12,500.75 TL")["strict"]


# ------------------------------------------------------------------ tool calling
def test_tool_cases_ideal_and_negative():
    cases = jl(DATA / "tools_tr" / "cases.jsonl")
    for c in cases:
        e = c["expected"]
        if e["mode"] == "no_call":
            txt = " ".join(map(str, e.get("final_must_include") or [])) or "Tamam."
            if e.get("final_must_match"):
                txt += " Hangi tarih?"
            assert score_tool_first_turn(c, txt, [])["pass"], c["id"]
            assert not score_tool_first_turn(c, txt, [{"id": "x", "name": c["tools"][0], "arguments": "{}"}])["pass"]
        else:
            calls = []
            for ec in e["calls"]:
                args = {}
                for k, rule in ec.get("arguments", {}).items():
                    v = rule.get("value")
                    args[k] = " ".join(v) if rule.get("match") == "contains" and isinstance(v, list) else ("x" if rule.get("match") == "any" else v)
                calls.append({"id": "c", "name": ec["name"], "arguments": json.dumps(args, ensure_ascii=False)})
            s = score_tool_first_turn(c, "", calls)
            assert s["call_ok"], (c["id"], s)
            assert not score_tool_first_turn(c, "Tamam", [])["call_ok"]


# ------------------------------------------------------------------ RAG / MCQ / guard / extraction / OCR / ASR
def test_refusal_and_citations():
    assert is_refusal("Belgelerde bu soruya yanıt verecek bilgi bulunmuyor.") == (True, True)
    assert is_refusal("Sağlanan belgelerde bu konuda bir bilgi yer almıyor.")[0]
    assert not is_refusal("Yıllık izin 14 gündür [K2].")[0]
    s = score_rag({"answerable": True, "key_facts": ["14 gün"], "gold_labels": ["K2"], "label_map": {"K1": "a", "K2": "b"}},
                  "İzin süresi on dört gündür [K2][K1].")
    assert s["citation_precision"] == 0.5 and s["citation_recall"] == 1.0 and s["key_number_recall"] == 1.0


@pytest.mark.parametrize("text,exp", [
    ("Cevap: C", "C"), ("Düşündüm...\nCevap: (B)", "B"), ("D", "D"), ("Doğru cevap A şıkkıdır.", "A"),
    ("emin değilim", None), ("Cevap: E", None),                                    # E is not an option when n=4
    ("**Cevap:** C", "C"), ("**Cevap**: C", "C"), ("Cevap: **C**", "C"), ("Cevap — (C)", "C"), ("Cevap – C", "C"),
    ("Cevap: [C]", "C"), ("Cevap: 'C'", "C"), ("Cevap: “C”", "C"), ("Cevabım: B", "B"), ("Yanıtım: D", "D"),
    ("Sorunun yanıtı B'dir.", "B"), ("The answer is C.", "C"), ("Cevap: \\boxed{B}", "B"), ("CEVAP: D", "D"),
    ("Cevap:\nB", "B"), ("(B)", "B"), ("b.", "B"), ("B) Ankara", "B"), ("**B) Ankara**", "B"),
    ("Cevap: A ve C", None), ("Cevap: A, C", None),                                # two letters = no single choice
    ("Cevap: A. C şıkkı yanlıştır", "A"), ("Cevap: A ve açıklaması şöyle", "A"),
    ("Seçenekler:\nA) x\nB) y", None),                                             # a restated option list is not an answer
    ("C şıkkı doğrudur.", "C"), ("A şıkkı yanlış, C şıkkı doğru.", None),
    ("Cevap: B12 vitamini", None), ("Bu sorunun cevabı Ankara'dır", None),
    ("Bu seçenek A ile çelişir. Cevap: D", "D"), ("Cevap: C\n\nNot: önce B'yi düşündüm.", "C"),
])
def test_mcq_extraction(text, exp):
    assert extract_choice(text, 4) == exp


def test_guard_parse():
    assert parse_guard("Safety: Unsafe\nCategories: Violent")["label"] == "unsafe"
    assert parse_guard("Safety: Controversial\nCategories: Politically Sensitive Topics")["categories"] == ["Politically Sensitive Topics"]


def test_extraction_equivalences():
    inv = jl(DATA / "docextract" / "invoices.jsonl")[0]
    g = inv["gold"]
    v = dict(g, iban=" ".join(g["iban"][i:i + 4] for i in range(0, len(g["iban"]), 4)), ara_toplam=str(g["ara_toplam"]).replace(".", ","))
    assert score_extraction(g, json.dumps(v, ensure_ascii=False))["all_correct"]
    assert not score_extraction(g, json.dumps(dict(g, genel_toplam=g["genel_toplam"] + 1)))["all_correct"]
    assert not score_extraction(g, "not json")["parsed"]


def test_cer_flex_is_order_robust():
    gt = "Birinci satır.\nİkinci satır.\nÜçüncü satır."
    assert cer_flex(gt, "Üçüncü satır.\nBirinci satır.\nİkinci satır.") == 0.0
    assert cer_flex(gt, "Birinci satir. Ikinci satir. Ucuncu satir.") > 0.1


def test_asr_wer():
    s = score_asr("İstanbul'da toplantı yarın saat 3'te.", "istanbulda toplantı yarın saat üçte")
    assert s["wer"] > 0 and s["wer_numnorm"] <= s["wer"]


def test_retrieval_metrics():
    m = retrieval_metrics(["a", "b", "c"], {"b"})
    assert m["hit@1"] == 0 and m["hit@3"] == 1 and m["mrr@10"] == 0.5


# ------------------------------------------------------------------ client parsing / errors
def test_error_classification():
    assert classify_http_error(429, json.dumps({"error": {"message": "daily quota exceeded"}}), {})[0] == "http_429_quota"
    assert classify_http_error(429, json.dumps({"error": {"message": "Rate limit: tokens per minute"}}), {})[0] == "http_429_rate"
    assert classify_http_error(429, "{}", {"x-evren-daily-remaining-tokens": "0"})[0] == "http_429_quota"
    assert classify_http_error(403, json.dumps({"error": {"code": "terms_not_accepted"}}), {})[0] == "http_403_terms"
    assert classify_http_error(400, json.dumps({"error": {"message": "Unsupported parameter: 'seed'"}}), {})[0] == "http_400_param"
    assert classify_http_error(503, "", {"retry-after": "7"})[3] == 7.0


def test_inline_think_split_across_chunks():
    res = ChatResult()
    pieces = []
    for c in ["<thi", "nk>düşünce ", "metni</th", "ink>\nCevap ", "burada"]:
        pieces += ApiClient._split_inline_think(res, c)
    reasoning = "".join(p for k, p in pieces if k == "r")
    answer = "".join(p for k, p in pieces if k == "c")
    assert reasoning == "düşünce metni" and answer == "Cevap burada"


# ------------------------------------------------------------------ statistics
def test_bootstrap_and_paired():
    a = {i: [1.0] if i % 2 else [0.0] for i in range(100)}
    m, lo, hi, n = S.bootstrap_mean_ci(a)
    assert lo < m < hi and n == 100
    d = S.paired_diff(a, a)
    assert d["diff"] == 0 and d["p"] > 0.5
    r = S.rank_intervals({"x": a, "y": a, "z": {i: [0.0] for i in range(100)}})
    assert r["x"]["p_best"] == r["y"]["p_best"] == 1.0 and r["z"]["rank_lo"] == 3


def test_holm_monotone():
    adj = S.holm({"a": 0.01, "b": 0.02, "c": 0.5})
    assert adj["a"] <= adj["b"] <= adj["c"] <= 1.0


# ------------------------------------------------------------------ review fixes (scorers, finality, budget, gate)
from harness.client import is_final  # noqa: E402
from harness.quota import parse_quota  # noqa: E402
from harness.scoring.basic import parse_decision_llm, parse_guard_policy, refusal_info, score_ocr  # noqa: E402
from harness.textnorm import sentences  # noqa: E402


def test_refusal_must_open_the_answer():
    assert refusal_info("Belgelerde bu soruya yanıt verecek bilgi bulunmuyor.")["refused"]
    partial = refusal_info("Yıllık izin 14 gündür [K2]. Ancak kısmi süreli çalışanlar için belgelerde bu soruya yanıt "
                           "verecek bilgi bulunmuyor.")
    assert not partial["refused"] and partial["partial"]
    leak = refusal_info("Belgelerde bu soruya yanıt verecek bilgi bulunmuyor. " + "Genel olarak bilinen kural şudur. " * 8)
    assert leak["refused"] and leak["with_content"]


def test_numbers_citations_and_sentences():
    assert contains_number("Süre 14 gündür [K14].", "14")
    assert not contains_number("Ayrıntılar [K14] numaralı parçada.", "14")       # a citation label is not the number
    assert not contains_number("bir hafta içinde", "1")                          # 'bir' is not the number one
    assert norm_for_wer("1.250 TL ve 7,5 saat", numbers_to_words=True) == "bin iki yüz elli tl ve yedi virgül beş saat"
    s = sentences("İş Kanunu 15. madde uyarınca bildirim yapılır. Dr. Ayşe onayladı. Toplam 15. Sonra devam edilir.")
    assert s == ["İş Kanunu 15. madde uyarınca bildirim yapılır.", "Dr. Ayşe onayladı.", "Toplam 15.", "Sonra devam edilir."]


def test_cer_flex_exclusive_spans_and_insertions():
    assert cer_flex("Toplam\nToplam\nKDV", "Toplam KDV") > 0.3              # one OCR 'Toplam' cannot serve two lines
    gt = "Birinci satır.\nİkinci satır."
    assert cer_flex(gt, "Birinci satır. İkinci satır. UYDURULMUŞ EK METİN") > 0.3   # invented text is penalised
    d = score_ocr("Sol sütun ağaç.\nSağ sütun çiçek.", "Sağ sütun çiçek. Sol sütun ağaç.")["diacritics"]
    assert d["recall"] == 1.0                                                  # judged per aligned line, not globally


def test_guard_policy_and_decision_parsing():
    assert parse_guard_policy('{"ihlal": 1, "kategori": "siddet", "gerekce": "x"}')["label"] == "unsafe"
    assert parse_guard_policy('Önce düşündüm. {"ihlal": "0", "kategori": "yok"}')["label"] == "safe"
    assert parse_guard_policy("bilmiyorum")["label"] == "unparsed"
    labels = ["fatura", "teknik", "satis", "kargo", "iptal", "diger"]
    assert parse_decision_llm('{"birim": "Diğer", "aciliyet": 1}', labels)["birim"] == "diger"
    assert parse_decision_llm('{"birim": "SATIŞ", "aciliyet": 2}', labels)["birim"] == "satis"


def test_finality_and_quota_parsing():
    assert is_final("ok") and is_final("empty_answer") and is_final("truncated_in_reasoning")
    assert not is_final("http_404") and not is_final("http_400_param") and not is_final("timeout_total")
    q = parse_quota({"tpm": {"remaining": 400000, "limit": 500000}, "daily": {"remaining_tokens": 12_000_000, "limit": 15_000_000}})
    assert q.daily_remaining == 12_000_000 and q.daily_limit == 15_000_000


def test_kappa_undefined_and_judge_no_answer():
    import math
    from harness.judge import judge_scores
    assert math.isnan(S.cohen_kappa(["pass"] * 5, ["pass"] * 5))
    assert judge_scores({"verdict": "no_answer"}, True)["hallucination"] is None


def test_seneca_cleaned_file():
    rows = jl(DATA / "mcq" / "seneca_trbench_mcq.jsonl")
    assert len(rows) == 120
    for r in rows:
        assert 0 <= r["answer_index"] < len(r["choices"]) and len(set(r["choices"])) == len(r["choices"])
    pos = [r["answer_index"] for r in rows]
    assert max(pos.count(i) for i in range(4)) / len(rows) < 0.35              # upstream: 'A' was 49%
    g = jl(DATA / "guard" / "prompts.jsonl")
    assert len({x["id"] for x in g}) == len(g) and {x["expected"] for x in g} == {"safe", "unsafe"}


def test_budget_estimate_is_offline_and_priced():
    from harness import budget as B
    assert B.tok("x" * 300) == 100
    assert B.completion_for("rag_qa", False, "nothink") < B.completion_for("rag_qa", True, "think_min") < B.completion_for("rag_qa", None, "default")


def test_fairness_gate_ignores_limited_plan_and_intersects(tmp_path):
    import pandas as pd
    import analysis.analyze as A
    from harness.store import sha256_obj

    def session(name, items, limit):
        d = tmp_path / name
        (d / "plans").mkdir(parents=True)
        (d / "plans" / "ifeval.jsonl").write_text("".join(json.dumps(it) + "\n" for it in items), encoding="utf-8")
        (d / "plans" / "ifeval.meta.json").write_text(json.dumps({"plan_hash": sha256_obj(items), "n_requests": len(items),
                                                                  "limit": limit}), encoding="utf-8")
        return d, sha256_obj(items)
    full = [{"uid": f"t{i}#r0", "item_id": f"t{i}"} for i in range(10)]
    small = full[:3]
    d1, h1 = session("full", full, None)
    d2, h2 = session("smoke", small, 3)
    rows = []
    for v in ("a", "b"):
        for it in full:
            if v == "b" and it["item_id"] == "t9":
                continue                                                      # b misses one request (90% coverage)
            rows.append({"session": "full", "sdir": d1, "plan_hash": h1, "variant": v, "item_id": it["item_id"],
                         "key": it["uid"], "repeat": 0, "final": True, "outcome": "ok", "rescored_changed": False})
    for v in ("a", "b", "c"):                                                 # the smoke plan has MORE systems
        for it in small:
            rows.append({"session": "smoke", "sdir": d2, "plan_hash": h2, "variant": v, "item_id": it["item_id"],
                         "key": it["uid"], "repeat": 0, "final": True, "outcome": "ok", "rescored_changed": False})
    df = pd.DataFrame(rows)
    A.ARGS = None
    sub, info = A.comparable(df, "ifeval", 0.85)
    assert info["primary"] == h1 and set(info["variants"]) == {"a", "b"}
    assert "c" in info["excluded"] and "limited" in info["excluded"]["c"]
    assert info["n_dropped_requests"] == 1 and sub["key"].nunique() == 9    # restricted to requests both completed


def test_dotenv_windows_bom_export_comments_and_empty_defaults(tmp_path, monkeypatch):
    from harness import config as C
    p = tmp_path / ".env"
    p.write_text("﻿TRB_A=one\r\nexport TRB_B=\"two # not a comment\"\nTRB_C=three  # note\n# TRB_D=x\n"
                 "TRB_E=   # empty with a comment\nTRB_F=from_file\n", encoding="utf-8")
    for k in ("TRB_A", "TRB_B", "TRB_C", "TRB_D", "TRB_E"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TRB_F", "")                       # set-but-empty in the shell → the .env value wins
    C.load_dotenv(p)
    import os
    assert os.environ["TRB_A"] == "one" and os.environ["TRB_B"] == "two # not a comment"
    assert os.environ["TRB_C"] == "three" and "TRB_D" not in os.environ and os.environ["TRB_F"] == "from_file"
    # ${VAR:-default} falls back when VAR is set but empty (shell semantics)
    assert C._expand_env("m=${TRB_E:-claude-opus-5-5}") == "m=claude-opus-5-5"
    assert C._expand_env("m=${TRB_A:-x}") == "m=one"


def test_latency_lock_scope():
    from harness.locks import lock_covers
    lk = {"session": "lat1", "endpoints": ["evren"], "scope": "machine"}
    assert lock_covers(lk, "ovh", "q1") and lock_covers(lk, "evren", "q1")
    assert not lock_covers(lk, "evren", "lat1")                          # the session itself never waits
    lk_ep = dict(lk, scope="endpoint")
    assert lock_covers(lk_ep, "evren", "q1") and not lock_covers(lk_ep, "ovh", "q1")
    assert lock_covers({"session": "old", "endpoints": ["evren"]}, "ovh", "q1")   # no scope recorded → machine
    assert not lock_covers(None, "evren", "q1")


def test_manifest_merges_concurrent_writers(tmp_path, monkeypatch):
    import harness.store as ST
    monkeypatch.setattr(ST, "RESULTS_DIR", tmp_path)
    a = ST.Session("a", "evren", "shared")                  # two processes working in one session
    b = ST.Session("a", "evren", "shared")
    a.register_suite("ifeval", {"summary": {"n": 1}})
    b.register_suite("tools", {"summary": {"n": 2}})       # b loaded the manifest before a wrote — must not drop ifeval
    a.event("paused_for_latency_session", seconds=10)
    b.register_suite("tools", {"extra": True})
    m = json.loads((tmp_path / "track-a" / "evren" / "shared" / "manifest.json").read_text(encoding="utf-8"))
    assert set(m["suites"]) == {"ifeval", "tools"} and m["suites"]["tools"] == {"summary": {"n": 2}, "extra": True}
    assert [e["kind"] for e in m["events"]] == ["paused_for_latency_session"]
    assert not list((tmp_path / "track-a" / "evren" / "shared").glob("*.tmp"))


def test_batch_custom_ids_and_params():
    from harness.anthropic_batch import CUSTOM_ID_RE, custom_id_for, message_params, message_text
    keys = [f"qwen3.8-27b:think_min|rag-0{i}#r{i % 3}|{i:016x}" for i in range(500)]
    ids = [custom_id_for(k) for k in keys]
    assert all(CUSTOM_ID_RE.match(c) for c in ids) and len(set(ids)) == len(ids)
    p = message_params("claude-opus-5-5", "Soru?", 4096, {"temperature": 0.0})
    assert p == {"model": "claude-opus-5-5", "max_tokens": 4096, "temperature": 0.0,
                 "messages": [{"role": "user", "content": "Soru?"}]}
    assert message_text({"content": [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": '{"a":'},
                                     {"type": "text", "text": " 1}"}]}) == '{"a": 1}'


def test_batch_price_is_half_for_anthropic_judge():
    from harness import budget as B
    from harness.config import load_registry
    reg = load_registry()
    full, cur = B.price_line(reg, "judge-anthropic", 1_000_000, 100_000)
    half, _ = B.price_line(reg, "judge-anthropic", 1_000_000, 100_000, batch=True)
    assert cur == "USD" and abs(full - 6.0) < 1e-9 and abs(half - 3.0) < 1e-9


# ------------------------------------------------------------------ local embedders / rerankers (fake sentence-transformers)
class _FakeST:
    """Stands in for SentenceTransformer / CrossEncoder: records precision changes, can emit NaN in half precision."""
    nan_in_half = False

    def __init__(self, hf_id, device="cpu", max_length=None, **kw):
        self.hf_id, self.device, self.kw, self.calls = hf_id, device, kw, []
        self.precision = "fp32"
        self.prompts = {"query": "q: ", "SearchQuery": "task: search result | query: ", "Document": "title: none | text: "}
        self.max_length = max_length
        self.model = self                       # CrossEncoder exposes the torch module as .model

    def half(self):
        self.precision = "fp16"
        return self

    def bfloat16(self):
        self.precision = "bf16"
        return self

    def float(self):
        self.precision = "fp32"
        return self

    def encode(self, texts, **kw):
        import numpy as np
        self.calls.append(kw.get("prompt_name"))
        v = np.ones((len(texts), 4), dtype=np.float32) / 2.0
        if self.nan_in_half and self.precision != "fp32":
            v[0, 0] = np.nan
        return v

    def predict(self, pairs, **kw):
        return [float(len(d)) for _, d in pairs]


@pytest.fixture
def fake_st(monkeypatch):
    import types
    mod = types.ModuleType("sentence_transformers")
    mod.SentenceTransformer = _FakeST
    mod.CrossEncoder = _FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)
    from harness.local import backends
    _FakeST.nan_in_half = False
    return backends


def test_local_embedder_precision_and_prompt_names(fake_st, monkeypatch):
    b = fake_st
    monkeypatch.setattr(b, "_device", lambda pref="auto": "cuda")
    monkeypatch.setattr(b, "_gpu_has_bf16", lambda: False)          # Volta: no bf16
    assert b.LocalEmbedder("m", dtype="auto").dtype == "fp16"
    assert b.LocalEmbedder("m", dtype="no_fp16").dtype == "fp32"     # Gemma embedders never run in fp16
    monkeypatch.setattr(b, "_gpu_has_bf16", lambda: True)
    e = b.LocalEmbedder("m", dtype="no_fp16", trust_remote_code=True, revision="abc")
    assert e.dtype == "bf16" and e.model.kw == {"trust_remote_code": True, "revision": "abc"}
    u = b.LocalEmbedder("m", trust_remote_code=True, attn_implementation="sdpa")      # UltraBERT: SDPA, not FA2
    assert u.model.kw == {"trust_remote_code": True, "model_kwargs": {"attn_implementation": "sdpa"}}
    assert e.has_prompt("SearchQuery") and not e.has_prompt("query_does_not_exist") and not e.has_prompt(None)
    e.encode(["a"], prompt_name="SearchQuery")
    e.encode(["a"], prompt_name="nope")                             # unknown prompt -> plain encode (caller adds prefix)
    assert e.model.calls == ["SearchQuery", None]
    monkeypatch.setattr(b, "_device", lambda pref="auto": "cpu")
    assert b.LocalEmbedder("m", dtype="auto").dtype == "fp32"


def test_local_embedder_falls_back_to_fp32_on_nan(fake_st, monkeypatch):
    import numpy as np
    b = fake_st
    monkeypatch.setattr(b, "_device", lambda pref="auto": "cuda")
    _FakeST.nan_in_half = True
    e = b.LocalEmbedder("m")
    v = e.encode(["a", "b"])
    assert np.isfinite(v).all() and e.dtype == "fp32 (fp16 overflow)" and e.model.precision == "fp32"


def test_local_reranker_options(fake_st, monkeypatch):
    b = fake_st
    monkeypatch.setattr(b, "_device", lambda pref="auto": "cuda")
    r = b.LocalReranker("ytu-ce-cosmos/modernbert-tr-reranker", max_length=512, dtype="fp32")
    assert r.dtype == "fp32" and r.model.max_length == 512 and r.model.precision == "fp32"
    assert b.LocalReranker("BAAI/bge-reranker-v2-m3").dtype == "fp16"
    assert r.score("q", ["kısa", "daha uzun belge"]) == [4.0, 15.0]


def test_registry_small_embedders_are_wired():
    from harness.config import load_registry
    reg = load_registry()
    eg2 = reg.model("embeddinggemma-2").options
    mg2 = reg.model("embeddingmagibu2-text").options               # Turkish distillation of EmbeddingGemma 2 (13 Oct)
    assert mg2["dtype"] == "no_fp16" and mg2["query_prompt_name"] == "query" and mg2["doc_prompt_name"] == "document"
    assert len(mg2["revision"]) == 40 and mg2["hf_id"] == "alibayram/embeddingmagibu2-text-v1"
    assert eg2["dtype"] == "no_fp16" and eg2["query_prompt_name"] == "SearchQuery" and eg2["doc_prompt_name"] == "Document"
    assert reg.model("nomic-embed-text-v2-moe").options["trust_remote_code"] is True
    ub = reg.model("ultrabert-embed-150m").options                  # English-only control 2 (14 Oct)
    assert ub["attn_implementation"] == "sdpa" and len(ub["revision"]) == 40 and ub["query_prompt_name"] == "query"
    assert reg.model("modernbert-tr-reranker").task == "local_rerank"
    assert reg.model("mistral-small-4").endpoint == "mistral"
    small = reg.suites["retrieval"]["small_retrievers"]
    assert all(reg.model(m).task == "local_embedding" for m in small)


def test_typed_content_parts_mistral_and_anthropic_style():
    from harness.client import split_content_parts
    from harness.config import Endpoint
    parts = [{"type": "thinking", "thinking": [{"type": "text", "text": "önce "}, {"type": "text", "text": "düşün"}]},
             {"type": "text", "text": "Cevap: 30 gün [K2]"}]
    assert split_content_parts(parts) == ("önce düşün", "Cevap: 30 gün [K2]")
    assert split_content_parts([{"type": "thinking", "thinking": "kısa"}, "metin"]) == ("kısa", "metin")
    c = ApiClient(Endpoint(name="x", base_url="http://localhost:1/v1", auth="none"))
    res = ChatResult()
    c._fill_from_message(res, {"model": "m", "choices": [{"message": {"content": parts}, "finish_reason": "stop"}]}, 1.0)
    assert res.reasoning == "önce düşün" and res.content == "Cevap: 30 gün [K2]"
    assert res.reasoning_field == "content_thinking_parts" and res.t_first_reasoning == 1.0


def test_param_aliases_keep_standard_names_in_the_audit():
    from harness.capabilities import Capabilities, build_chat_payload
    from harness.config import ModelSpec
    spec = ModelSpec(id="m", endpoint="mistral", api_model="m", task="chat", options={"param_aliases": {"seed": "random_seed"}})
    caps = Capabilities({"token_param": "max_tokens", "drop_params": ["stream_options"],
                         "modes": {"default": {"available": True, "params": {}}}})
    p, eff = build_chat_payload(spec, caps, "default", [{"role": "user", "content": "x"}], max_tokens=64,
                                decoding={"temperature": 0.6, "top_p": 0.95}, seed=7)
    assert p["random_seed"] == 7 and "seed" not in p and "stream_options" not in p
    assert eff["seed"] == 7 and eff["param_aliases"] == {"seed": "random_seed"}


def _sse_client(events: list):
    """An ApiClient whose HTTP layer replays one SSE stream (no network)."""
    import httpx
    from harness.config import Endpoint
    body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events) + "data: [DONE]\n\n"
    c = ApiClient(Endpoint(name="x", base_url="http://mock.invalid/v1", auth="none"))
    c.http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())))
    return c


def _stream(events: list):
    import asyncio
    async def go():
        c = _sse_client(events)
        try:
            return await c.stream_chat({"model": "m", "messages": []})
        finally:
            await c.http.aclose()
    return asyncio.run(go())


def _ev(delta: dict | None = None, finish: str | None = None) -> dict:
    return {"model": "m", "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


def test_stream_tags_when_the_server_separates_reasoning():
    # a stray closing tag left by a reasoning parser is dropped from the answer
    r = _stream([_ev({"reasoning_content": "düşünce"}), _ev({"content": "</think>\n\nCevap 30 gün"}), _ev(finish="stop")])
    assert r.outcome == "ok" and r.reasoning == "düşünce" and r.content == "Cevap 30 gün"
    # a literal closing tag inside a separated answer stays text (nothing is moved into reasoning)
    r = _stream([_ev({"reasoning_content": "x"}), _ev({"content": "Etiket "}), _ev({"content": "[/THINK] örneği"}),
                 _ev(finish="stop")])
    assert r.content == "Etiket [/THINK] örneği" and r.reasoning == "x"
    # Mistral: thinking closes and the answer starts in one delta
    r = _stream([_ev({"content": [{"type": "thinking", "thinking": [{"type": "text", "text": "plan"}]}]}),
                 _ev({"content": [{"type": "thinking", "thinking": [], "closed": True}, {"type": "text", "text": "Ce"}]}),
                 _ev({"content": "vap"}), _ev(finish="stop")])
    assert r.reasoning == "plan" and r.content == "Cevap" and r.reasoning_field == "content_thinking_parts"
    assert r.t_first_reasoning is not None and r.t_first_reasoning <= r.t_first_content


def test_stream_flushes_a_held_back_tag_prefix():
    r = _stream([_ev({"content": "["}), _ev(finish="stop")])       # could have been the start of "[THINK]"
    assert r.content == "[" and r.outcome == "ok"


def test_transient_probe_failure_is_not_saved(tmp_path, monkeypatch):
    import asyncio
    from harness import capabilities as C
    from harness.config import ModelSpec
    monkeypatch.setattr(C, "caps_path", lambda ep, mid: tmp_path / ep / f"{mid}.json")
    spec = ModelSpec(id="m", endpoint="e", api_model="m", task="chat")

    class P(C.Prober):
        def __init__(self, outcome):
            super().__init__(None, None, spec, log=lambda *a: None)
            self.outcome = outcome

        async def _negotiate_params(self):
            r = ChatResult()
            r.outcome, r.error = self.outcome, "x"
            return r
    d = asyncio.run(P("http_503").run())
    assert d["available"] is False and d.get("transient") and not (tmp_path / "e" / "m.json").exists()
    d = asyncio.run(P("http_404").run())
    assert d["available"] is False and (tmp_path / "e" / "m.json").exists()      # a real answer from the server: kept
    # ...and a model the probe found unavailable is neither estimated nor run
    from harness.runner import unavailable_refusal
    assert "unavailable" in unavailable_refusal(C.Capabilities(d), "m:default", "rag_qa")


def test_governor_requests_per_minute_cap():
    import asyncio
    import time as _t
    from harness.quota import TokenGovernor
    g = TokenGovernor(None, rpm_limit=3)

    async def go():
        t0 = _t.monotonic()
        tickets = [await g.acquire(0) for _ in range(3)]
        assert all(t is not None for t in tickets) and _t.monotonic() - t0 < 0.5      # under the cap: no wait
        g.window[0][0] -= 59.8                                                         # pretend the first is old
        await g.acquire(0)                                                             # 4th waits for it to expire
        return _t.monotonic() - t0
    waited = asyncio.run(go())
    assert 0.1 < waited < 2.0 and g.waited_s > 0
    assert asyncio.run(TokenGovernor(None).acquire(10)) is None                       # no limits: no bookkeeping


def _riff(tag: int, ch: int, sr: int, bits: int, payload: bytes, extensible: bool = False) -> bytes:
    import struct
    if extensible:      # WAVE_FORMAT_EXTENSIBLE: cbSize 22, valid bits, channel mask, SubFormat GUID (tag first)
        fmt = struct.pack("<HHIIHHHHI", 0xFFFE, ch, sr, sr * ch * bits // 8, ch * bits // 8, bits, 22, bits, 0)
        fmt += struct.pack("<H", tag) + bytes.fromhex("000000001000800000aa00389b71")
    else:
        fmt = struct.pack("<HHIIHH", tag, ch, sr, sr * ch * bits // 8, ch * bits // 8, bits) + b"\x00\x00"
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"LIST" + struct.pack("<I", 3) + b"abc\x00"
    chunks += b"data" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def test_asr_clip_builder_reads_float_and_pcm_wavs():
    """FLEURS ships 32-bit float WAVs (format tag 3), which Python's `wave` module rejects (the phase 0 failure of
    10 Oct): the builder parses RIFF itself — float, 16/24/32-bit PCM, WAVE_FORMAT_EXTENSIBLE, odd-sized chunks."""
    import importlib.util
    import numpy as np
    spec = importlib.util.spec_from_file_location("bac", ROOT / "scripts" / "build_asr_clips.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    sig = (0.5 * np.sin(np.linspace(0, 40 * np.pi, 1600))).astype(np.float32)
    f32 = m.wav_from_bytes(_riff(3, 1, 16000, 32, sig.astype("<f4").tobytes()))
    assert f32.dtype == np.float32 and len(f32) == 1600 and np.allclose(f32, sig, atol=1e-7)
    p16 = m.wav_from_bytes(_riff(1, 1, 16000, 16, (sig * 32767).astype("<i2").tobytes()))
    assert np.allclose(p16, sig, atol=1e-4)
    i24 = (sig * ((1 << 23) - 1)).astype(np.int32)
    b24 = np.stack([(i24 >> s) & 0xFF for s in (0, 8, 16)], axis=1).astype(np.uint8).tobytes()
    assert np.allclose(m.wav_from_bytes(_riff(1, 1, 16000, 24, b24)), sig, atol=1e-4)
    ext = m.wav_from_bytes(_riff(3, 1, 16000, 32, sig.astype("<f4").tobytes(), extensible=True))
    assert np.allclose(ext, sig, atol=1e-7)
    stereo = np.stack([sig, sig], axis=1).astype("<f4").tobytes()
    assert np.allclose(m.wav_from_bytes(_riff(3, 2, 16000, 32, stereo)), sig, atol=1e-7)
    half = m.wav_from_bytes(_riff(3, 1, 8000, 32, sig.astype("<f4").tobytes()))      # resampled to 16 kHz
    assert len(half) == 3200
    with pytest.raises(ValueError):
        m.wav_from_bytes(_riff(6, 1, 16000, 8, b"\x00" * 100))                        # A-law: refused, not garbled


def test_batch_judge_params_honour_probe_drops():
    """Claude Opus 5.5 refuses `temperature`; the probe records it and the batch path must drop it like the sync path."""
    from harness.anthropic_batch import message_params
    p = message_params("claude-x", "q", 4096, {"temperature": 0.0})
    assert p["temperature"] == 0.0 and "top_p" not in p
    p = message_params("claude-x", "q", 4096, {"temperature": 0.0, "top_p": 0.9}, drop={"temperature"})
    assert "temperature" not in p and p["top_p"] == 0.9
    p = message_params("claude-x", "q", 4096, {"temperature": 0.0, "top_p": 0.9}, drop={"temperature", "top_p"})
    assert set(p) == {"model", "max_tokens", "messages"}


def test_reported_zero_reasoning_tokens_with_reasoning_text_is_estimated():
    from harness.capabilities import reasoning_tokens
    r = ChatResult()
    r.reasoning, r.content = "x" * 320, "cevap"
    r.usage = {"completion_tokens": 100, "completion_tokens_details": {"reasoning_tokens": 0}}
    n, exact = reasoning_tokens(r)
    assert not exact and n > 50                                       # OVHcloud gpt-oss-120b reports 0
    r.reasoning = ""
    assert reasoning_tokens(r) == (0, True)                           # a real 0 stays exact
    r.reasoning = "düşünce"
    r.usage = {"completion_tokens": 100, "completion_tokens_details": {"reasoning_tokens": 42}}
    assert reasoning_tokens(r) == (42, True)


def test_yaml_on_off_thinking_keys_are_not_lost():
    """`thinking: {off: ..., on: ...}` unquoted parses as {False: ..., True: ...} (YAML 1.1); the loader maps it back."""
    from harness.config import load_registry
    reg = load_registry()
    th = reg.model("nemotron-3.5-lightning").thinking
    assert th.get("off") == {"chat_template_kwargs": {"enable_thinking": False}}
    assert th.get("on") == {"chat_template_kwargs": {"enable_thinking": True}}
    assert not any(isinstance(k, bool) for m in reg.models.values() for k in (m.thinking or {}))


def test_mode_skip_reasons_are_explicit():
    from harness.capabilities import Capabilities
    from harness.runner import mode_skip_reason
    thinker = Capabilities({"default_thinks": True, "modes": {"default": {"available": True},
                                                              "nothink": {"available": False, "reason": "no candidate removed reasoning"}}})
    assert "already thinks" in mode_skip_reason(thinker, "think")
    assert "no candidate removed reasoning" in mode_skip_reason(thinker, "nothink")
    assert "budget_min" in mode_skip_reason(thinker, "think_min")
    plain = Capabilities({"default_thinks": False, "modes": {"default": {"available": True}}})
    assert "does not think" in mode_skip_reason(plain, "nothink")



# FLEURS tr_tr dev: the 338 utterance durations in seconds, in dev.tsv order (num_samples / 16 kHz)
FLEURS_TR_DEV_SECONDS = [
    8.16,7.5,10.74,12.06,18.78,11.58,9.06,11.4,8.7,9.96,9.24,6.72,8.22,8.58,8.28,10.86,14.4,15.18,15.9,19.02,10.98,
    11.34,16.5,13.08,12.6,12.78,12.72,7.5,11.22,11.28,15.9,15.12,12.36,12.96,10.92,10.5,11.64,12.42,15.42,15.36,14.16,
    10.8,13.98,13.08,8.7,12,10.68,12.36,13.56,9.66,8.04,10.02,20.28,20.76,15.72,17.16,12.72,13.8,5.34,9,5.46,9.6,
    12.54,9.96,9.06,13.5,12.96,13.74,12.48,11.52,11.64,10.2,16.44,15.9,14.88,17.16,15.96,9.36,7.26,5.88,11.1,8.88,
    6.24,12.12,12.12,13.08,11.94,10.68,11.4,13.62,9.72,9.24,9.66,12.48,11.22,9.96,9.18,4.68,4.92,12.24,10.56,11.52,
    11.52,12.84,7.44,7.98,6.6,11.16,10.56,7.5,16.08,13.02,15.36,16.02,17.52,11.88,11.28,15.12,14.82,18.3,18.54,10.2,
    10.98,10.2,11.46,12.6,16.02,14.58,11.28,10.32,12.9,7.08,9.42,13.62,14.04,9.24,6.06,15.36,5.88,6.54,6.66,12,9.12,
    8.64,16.8,15.24,16.98,8.28,8.22,9.6,10.62,13.62,6.24,10.14,6.36,11.4,7.56,11.34,15.78,9.48,14.94,16.44,11.28,9.48,
    11.22,12,12.84,14.16,9.6,7.2,7.44,18.9,11.16,15.54,12.24,16.5,15.36,10.62,9.72,8.1,13.32,11.22,12.18,11.4,8.7,
    16.38,11.7,11.76,11.22,9.6,11.88,8.46,7.74,7.8,9.72,9.6,6.48,5.64,16.68,15.6,15,12.36,12.66,12.36,12.66,17.64,
    15.36,11.58,6.6,7.02,9.18,4.98,6.36,9.12,13.32,16.44,19.38,15.24,10.38,11.16,12.18,32.04,14.76,14.4,15.66,16.08,
    11.34,14.34,9.48,14.52,11.34,6.3,10.56,9.48,6.96,8.16,13.32,14.64,9.84,16.56,17.46,13.14,9.06,11.04,24.96,20.04,
    19.62,12.66,14.52,9,11.28,8.76,16.02,15.24,10.44,5.34,8.94,15.18,11.58,10.38,12.96,9,8.04,9.54,12.6,14.1,12,18.12,
    18.48,14.28,24.36,10.26,19.86,9.36,11.88,11.76,10.5,11.34,13.14,10.2,11.52,13.56,14.64,12.36,13.2,11.1,15.36,15,
    13.14,15.72,11.34,10.26,11.88,14.16,12.54,11.04,13.08,13.14,15.3,14.46,13.74,5.34,5.52,8.4,16.8,12.18,5.7,10.44,
    18.18,15.48,17.94,16.02,16.92,14.28,17.64,17.22,11.04,11.16,10.8,9.36,10.92,6,10.44,10.56,13.62,13.5,13.02,12.9,
    9.6,5.58,8.7,9.96,6.18,10.68,10.02,10.62,4.38,5.1
]


def test_asr_clip_packing_keeps_long_utterances_for_later_clips(tmp_path):
    """Only 136 FLEURS tr_tr dev utterances last 2.5-11 s.  Dropping every utterance that did not fit the room left in
    one clip used them up after 17 of the 20 meeting clips (device test, 11 Oct); now they wait for a later clip."""
    import importlib.util
    import numpy as np
    spec = importlib.util.spec_from_file_location("bac2", ROOT / "scripts" / "build_asr_clips.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.SR, m.OUT = 100, tmp_path                     # 100 Hz keeps the synthetic audio tiny
    assert len(FLEURS_TR_DEV_SECONDS) == 338
    rows = [{"sid": str(i), "file": f"{i}.wav", "raw": f"cümle {i}", "gender": "MALE"}
            for i in range(len(FLEURS_TR_DEV_SECONDS))]
    audio = {f"{i}.wav": np.full(int(round(d * m.SR)), 0.1, dtype=np.float32)
             for i, d in enumerate(FLEURS_TR_DEV_SECONDS)}
    used: set = set()
    clips = m.build(rows, audio, 20261009, 25.0, 20, (25.0, 29.0), "meeting_sim", used, 1)
    assert len(clips) == 20 and all(25.0 <= c["duration_s"] <= 29.5 for c in clips)
    longs = m.build(rows, audio, 20261010, 25.0, 2, (55.0, 62.0), "long_probe", used, 21)
    assert len(longs) == 2 and all(55.0 <= c["duration_s"] <= 62.5 for c in longs)
    assert not ({s["fleurs_id"] for c in longs for s in c["sources"]} & {s["fleurs_id"] for c in clips for s in c["sources"]})


# ------------------------------------------------------------------ audit of the smoke follow-up (11 Oct)
@pytest.mark.parametrize("answer,refused", [
    # unanswerable rag-004 on 10-11 Oct: answers that never answered but were scored as answers
    (("Belgelerde VERBİS'e kayıt zorunluluğu ile ilgili çalışan sayısı veya mali bilanço eşiği belirtilmemiştir. "
      "Bu bilgi K3 veya K5 maddelerinde yer almamaktadır."), True),                                  # Qwen3-VL
    (("Belgelerde VERBİS'e kayıt zorunluluğunun çalışan sayısı eşiğiyle ilgili bir bilgi bulunmuyor. [K3] ve [K5] "
      "maddelerine göre kayıt zorunluluğu var."), True),                                             # EVREN auto
    ("Belgelere göre VERBİS’e kayıt zorunluluğu için çalışan sayısı ile ilgili bir bilgi bulunmamaktadır.", True),
    ("Verilen belge parçalarında çalışan sayısı eşiğine ilişkin herhangi bir bilgi bulunmamaktadır.", True),
    ("VERBİS'e kayıt için çalışan sayısı eşiği bilgisi belgelerde yer almamaktadır. [K3] genel kuralı verir.", True),
    ("Belgelerde bu konuda bir düzenleme bulunmuyor.", True),                                         # uncited
    # answers, not refusals
    ("Belgelere göre deneme süresi içinde ihbar süresi bulunmuyor [K3].", False),                     # negative fact
    ("Belgelere göre deneme süresinde ihbar süresi bulunmuyor.", False),                              # no info noun
    ("Belgelerde 72 saatlik bir süre belirtilmemiştir. Kanun 'en kısa sürede' bildirimi öngörür [K4].", False),
    ("Belgelerde 72 saatlik bir düzenleme bulunmuyor. Kanun 'en kısa sürede' bildirimi öngörür [K4].", False),
    ("Belgelerde yıllık izin süresi 14 gün olarak belirtilmiştir [K2].", False),
    ("Belgelerde kıdeme göre ayrı bir süre belirtilmemiş, ancak kıdemli çalışanlar için 20 gündür [K2].", False),
])
def test_no_information_openings(answer, refused):
    assert is_refusal(answer)[0] is refused


def test_reference_answers_classify_as_their_answerability():
    for r in jl(DATA / "rag" / "questions.jsonl"):
        assert is_refusal(r["answer"])[0] is (not r["answerable"]), r["id"]


def test_quota_keeps_the_daily_reset_not_the_window_reset():
    q = parse_quota({"used_tokens": 12006, "cap": 2_500_000, "window_minutes": 5, "reset_at": "2026-10-11T06:05:00Z",
                     "daily_limit_tokens": 15_000_000, "daily_remaining_tokens": 14_987_994,
                     "daily_reset_at": "2026-10-12T00:00:00+03:00"})                              # EVREN, 11 Oct
    assert (q.daily_remaining, q.daily_limit) == (14_987_994, 15_000_000)
    assert q.reset_at == "2026-10-12T00:00:00+03:00"


def test_think_min_needs_every_run_below_the_shortest_reference():
    from harness.capabilities import BUDGET_RULE, budget_verdict, recheck_think_min
    assert budget_verdict([277, 399], [103, 62], 0.7) is None                      # GLM-5.3 reasoning_effort=low
    assert budget_verdict([732, 2436], [691, 692], 0.5)                            # Nemotron: one long reference run
    assert budget_verdict([30, 40], [5, 5], 0.7)                                   # reference too short to measure
    assert budget_verdict([], [5], 0.7) and budget_verdict([300], [], 0.7)

    def caps(cand, ref, documented):
        return {"modes": {"default": {"available": True}, "think_min": {
            "available": True, "params": {"x": 1}, "documented": documented,
            "evidence": {"reasoning_tokens": cand}, "reference": {"reasoning_tokens": ref}}}}
    nemo = recheck_think_min(caps([691, 692], [732, 2436], False))["modes"]["think_min"]
    assert nemo["available"] is False and "re-checked" in nemo["reason"] and nemo["superseded"]["params"] == {"x": 1}
    for cand, ref in (([103, 62], [277, 399]), ([124, 145], [244, 236]), ([141, 113], [377, 495]),
                      ([263, 241], [475, 396])):                                   # GLM, gpt-oss 120b/20b, Glimmer
        kept = recheck_think_min(caps(cand, ref, True))["modes"]["think_min"]
        assert kept["available"] and kept["rule"] == BUDGET_RULE
    old = {"modes": {"think_min": {"available": True, "params": {}}}}              # no per-run numbers: as probed
    assert recheck_think_min(old) == old


def test_nothink_must_not_move_the_reasoning_into_the_answer():
    from harness.capabilities import nothink_leak
    base = {"content_chars": [60, 70], "content_turkish_ratio": [1.0, 1.0]}
    assert nothink_leak(base, {"content_chars": [65, 72], "content_turkish_ratio": [1.0, 1.0]}) is None
    assert "stopped being Turkish" in nothink_leak(base, {"content_chars": [150, 170],
                                                          "content_turkish_ratio": [0.2, 0.3]})
    assert "grew" in nothink_leak(base, {"content_chars": [900, 1100], "content_turkish_ratio": [1.0, 1.0]})
    assert nothink_leak({}, {"content_chars": [900]}) is None                       # evidence of an older probe


def test_prompt_tokens_follow_the_tokenizer_family():
    from harness import budget as B
    from harness.config import load_registry
    reg = load_registry()
    assert B.prompt_factor(reg, "judge-anthropic") == pytest.approx(3.0 / 2.1)    # Claude: 2.08 chars per token
    assert B.prompt_factor(reg, "deepseek-v4-flash") == pytest.approx(3.0 / 2.35)
    assert B.prompt_factor(reg, "glm-5.3") == 1.0


def test_mode_effect_flags_switches_that_did_not_do_their_job():
    import pandas as pd

    import analysis.analyze as A

    def rec(model, mode, rc, tr):
        return {"model_id": model, "mode": mode, "final": True, "outcome": "ok", "score": {"turkish_ratio": tr},
                "rec": {"result": {"reasoning_chars": rc, "usage": {"completion_tokens": 100 + rc // 3}}}}
    rows = []
    for _ in range(3):
        rows += [rec("glm", "default", 420, 1.0), rec("glm", "nothink", 0, 0.27), rec("glm", "think_min", 0, 1.0),
                 rec("nemo", "default", 2654, 1.0), rec("nemo", "think_min", 3365, 1.0),
                 rec("oss", "default", 780, 1.0), rec("oss", "think_min", 125, 1.0)]
    me = A.mode_effect(pd.DataFrame(rows)).set_index(["model", "mode"])["flags"]
    assert "no longer Turkish" in me[("glm", "nothink")]
    assert "did not reason" in me[("glm", "think_min")]
    assert "no ≥20% cut" in me[("nemo", "think_min")]
    assert me[("oss", "think_min")] == "ok" and me[("oss", "default")] == "ok"


def test_vendor_system_prompt_as_a_mode_parameter():
    """Magistral reasons only under Mistral's reasoning system prompt: the `think` mode adds it, default does not."""
    from harness.capabilities import SYSTEM_PREFIX_KEY, Capabilities, build_chat_payload
    from harness.config import load_registry
    spec = load_registry().model("magistral-small-1.2")
    vendor = "prompts/vendor/magistral_small_2509_system.txt"
    caps = Capabilities({"token_param": "max_tokens", "drop_params": [], "modes": {
        "default": {"available": True, "params": {}},
        "think": {"available": True, "params": {SYSTEM_PREFIX_KEY: vendor}}}})
    msgs = [{"role": "system", "content": "Sadece belgelere dayan."}, {"role": "user", "content": "Soru?"}]
    p, eff = build_chat_payload(spec, caps, "think", msgs, max_tokens=8192, decoding={"temperature": 0.7})
    assert p["messages"][0]["content"].endswith("Sadece belgelere dayan.") and len(p["messages"]) == 2
    assert len(p["messages"][0]["content"]) > 200 and SYSTEM_PREFIX_KEY not in p
    assert eff.get("system_prefix_file") == vendor and msgs[0]["content"] == "Sadece belgelere dayan."
    p0, eff0 = build_chat_payload(spec, caps, "default", msgs, max_tokens=8192, decoding={"temperature": 0.7})
    assert p0["messages"] == msgs and "system_prefix_file" not in eff0


# ------------------------------------------------------------------ phase 3 day 2 (12 Oct): EVREN's per-minute limit
def test_evren_per_minute_429_is_a_rate_limit_not_the_daily_quota():
    from harness.client import classify_http_error
    per_min = json.dumps({"error": {"message": "Dakikalık token limitiniz aşıldı.", "evren": {"remaining_tokens": 0}}})
    daily = json.dumps({"error": {"message": "Günlük token limitiniz aşıldı (tüm API anahtarlarınızın toplamı)."}})
    assert classify_http_error(429, per_min, {})[0] == "http_429_rate"          # was taken for the daily quota
    assert classify_http_error(429, daily, {})[0] == "http_429_quota"
    assert classify_http_error(429, per_min, {"x-evren-daily-remaining-tokens": "0"})[0] == "http_429_quota"
    assert classify_http_error(429, daily, {"x-evren-daily-remaining-tokens": "9000000"})[0] == "http_429_rate"


def test_pipeline_requests_confirm_the_daily_quota_before_stopping(monkeypatch):
    import asyncio

    import harness.runner as R
    from harness.client import RetryPolicy
    from harness.quota import TokenGovernor

    class Res:
        def __init__(self, outcome):
            self.outcome, self.error, self.data, self.http_status, self.retry_after = outcome, "x", {}, 429, None

    class Ctx:
        def __init__(self, exhausted):
            self.stop, self.logs, self.exhausted = asyncio.Event(), [], exhausted
            self.opts = type("O", (), {"retry": RetryPolicy(max_attempts=1)})()
            self.session = type("S", (), {"id": "t", "event": lambda *a, **k: None})()

        def governor(self, ep):
            return TokenGovernor(None)

        async def quota_really_exhausted(self, ep):
            return self.exhausted

        def log(self, m):
            self.logs.append(m)

    async def no_sleep(*_):
        return None
    monkeypatch.setattr(R.asyncio, "sleep", no_sleep)
    calls = []

    async def fn():
        calls.append(1)
        return Res("http_429_quota" if len(calls) == 1 else "ok")
    ctx = Ctx(exhausted=False)                                   # /quota shows tokens left: wait, then retry
    assert asyncio.run(R.guarded(ctx, "evren", fn, estimate=10)).outcome == "ok"
    assert len(calls) == 2 and "treating it as a rate limit" in ctx.logs[0] and not ctx.stop.is_set()
    calls.clear()
    ctx = Ctx(exhausted=True)                                    # really gone: a clean stop with the usual line
    with pytest.raises(R.QuotaStop):
        asyncio.run(R.guarded(ctx, "evren", fn, estimate=10))
    assert ctx.stop.is_set() and ctx.logs[0].startswith("!! daily quota exhausted")


def test_pipeline_steps_report_what_is_left():
    from harness.suites.pipeline import unfinished
    s = {"systems": {"bge-m3": {"failed": "RuntimeError: local embeddings need ..."}, "a+b": {"failed_queries": 3},
                     "bm25-f5": {"kind": "lexical"}, "x+y": {"failed_queries": 0}}}
    assert len(unfinished(s)) == 2
    s = {"models": {"dots-ocr": {"ok": 29, "http_429_rate": 1}, "tesseract-tur": {"failed": "RuntimeError: tesseract"}}}
    assert len(unfinished(s)) == 2
    assert unfinished({"models": {"m": {"ok": 3, "http_400_content_filter": 1}}}) == []


def test_local_whisper_falls_back_to_cpu_when_cuda_libraries_are_missing(monkeypatch):
    import types

    import harness.local.backends as LB

    class FakeModel:
        def __init__(self, size, device, compute_type):
            self.device = device

        def transcribe(self, path, **kw):
            if self.device == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            return [types.SimpleNamespace(text=" merhaba ")], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    w = LB.LocalWhisper("large-v3", device="cuda")
    assert w.transcribe(Path("x.wav")) == ("merhaba", None)
    assert w.info["device"] == "cpu" and "cublas64_12" in w.info["fallback_from_cuda"]


# ------------------------------------------------------------------ phase 3, 12 Oct afternoon: local Whisper, retries, OCR extraction
def test_retry_outcomes_resend_only_the_named_final_failures(tmp_path):
    from harness.store import ResultWriter
    p = tmp_path / "w.jsonl"
    recs = [{"key": "a#r0", "final": True, "result": {"outcome": "local_error"}},
            {"key": "b#r0", "final": True, "result": {"outcome": "ok"}},
            {"key": "c#r0", "final": False, "result": {"outcome": "timeout_read"}},
            {"key": "d#r0", "final": True, "result": {"outcome": "local_error"}},
            {"key": "d#r0", "final": True, "result": {"outcome": "ok"}}]          # already retried: latest is ok
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    assert ResultWriter(p).done == {"a#r0", "b#r0", "d#r0"}
    try:
        ResultWriter.retry_outcomes = frozenset({"local_error"})
        assert ResultWriter(p).done == {"b#r0", "d#r0"}
    finally:
        ResultWriter.retry_outcomes = frozenset()


def test_local_whisper_gets_the_decoded_clip_not_a_path(monkeypatch, tmp_path):
    import types
    import wave

    import numpy as np

    import harness.local.backends as LB
    clip = tmp_path / "c.wav"
    with wave.open(str(clip), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.sin(np.arange(1600) / 5) * 16000).astype("<i2").tobytes())
    seen = {}

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, audio, **kw):
            seen["audio"] = audio
            return [types.SimpleNamespace(text="tamam")], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    assert LB.LocalWhisper("large-v3", device="cpu").transcribe(clip) == ("tamam", None)
    a = seen["audio"]
    assert isinstance(a, np.ndarray) and a.dtype == np.float32 and len(a) == 1600 and float(np.abs(a).max()) <= 1.0


def test_pipeline_analysis_keeps_the_latest_record_per_key(tmp_path):
    import analysis.analyze as A
    d = tmp_path / "sess" / "asr"
    d.mkdir(parents=True)
    rows = [{"key": "asr-01#r0", "final": True, "plan_hash": "p", "variant": "whisper", "item_id": "asr-01",
             "result": {"outcome": "local_error"}, "score": None},
            {"key": "asr-01#r0", "final": True, "plan_hash": "p", "variant": "whisper", "item_id": "asr-01",
             "result": {"outcome": "ok"}, "score": {"wer": 0.1, "wer_numnorm": 0.1, "cer": 0.05}}]
    (d / "whisper.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    A.ARGS = None
    df, _ = A._pipeline_records([tmp_path / "sess"], "asr", {"wer": 1.0, "wer_numnorm": 1.0, "cer": 1.0})
    assert len(df) == 1 and not bool(df["failed"].iloc[0]) and df["wer"].iloc[0] == 0.1


def _cap_fixtures(tmp_path, monkeypatch, limit):
    import asyncio
    import types

    import harness.judge as J
    from harness.store import ResultWriter
    price = {"in": 2.0, "out": 10.0, "currency": "USD", "batch": 0.5}
    reg = types.SimpleNamespace(model=lambda _id: types.SimpleNamespace(price=price))
    cap = J.SpendCap(reg, "judge", limit, 0.002)
    w = ResultWriter(tmp_path / "verdicts.jsonl")
    sent: list[int] = []

    async def fake_run_batches(client, state, chunk, handle, *, wait, poll_s, log):
        sent.append(len(chunk))
        for k, _ in chunk:              # each verdict: 1,000 tokens in, 500 out → (2,000 + 5,000) / 1e6 × 0.5 = 0.0035
            await handle(k, {"content": []}, {"transport": "batch", "usage": {"prompt_tokens": 1000,
                                                                               "completion_tokens": 500}})
        return {"submitted": len(chunk)}

    async def handle(key, message, meta):
        cap.add(meta)
        await w.write({"key": key, "final": True, "judge_meta": meta})

    monkeypatch.setattr(J, "run_batches", fake_run_batches)
    ctx = types.SimpleNamespace(client=lambda ep: None, log=lambda *a: None)
    judge = types.SimpleNamespace(spec=types.SimpleNamespace(endpoint="anthropic"))
    reqs = [(f"k{i}", {}) for i in range(100)]

    def run():
        return asyncio.run(J.capped_batches(ctx, judge, tmp_path / "verdicts.jsonl", w, reqs, handle, cap,
                                            wait=True, poll_s=0))
    return cap, sent, w, run


def test_judge_spend_cap_prices_a_pilot_batch_and_stops_before_the_rest(tmp_path, monkeypatch):
    # estimate 100 × 0.002 = 0.20, cap 0.30; the pilot (20) costs 0.0035 each → 0.07 + 80 × 0.0035 = 0.35 > 0.30
    cap, sent, w, run = _cap_fixtures(tmp_path, monkeypatch, 0.30)
    run()
    assert sent == [20] and len(w.done) == 20 and cap.stopped and "spend cap" in cap.stopped
    assert abs(cap.spent - 0.07) < 1e-9


def test_judge_spend_cap_stop_is_written_down_with_the_command_to_continue(tmp_path, monkeypatch):
    import harness.judge as J
    cap, _sent, _w, run = _cap_fixtures(tmp_path, monkeypatch, 0.30)
    monkeypatch.setattr(sys, "argv", ["harness/run.py", "judge", "--session", "evren-a", "--max-cost", "0.30", "--yes"])
    cap.ledger = J.SpendLedger(tmp_path / "verdicts.spend.jsonl", {"session": "evren-a"})
    run()
    ev = [json.loads(line) for line in (tmp_path / "verdicts.spend.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [e["event"] for e in ev]
    assert kinds == ["pilot_submitting", "pilot_submitted", "pilot_priced", "cap_stop"]
    stop = ev[-1]
    assert stop["answers_left"] == 80 and abs(stop["projected_total"] - 0.35) < 1e-9 and stop["cap"] == 0.30
    # the continue command keeps the call and replaces --max-cost by the projection + 10 % (0.385 -> 0.39)
    assert stop["next_command"].endswith(" harness/run.py judge --session evren-a --yes --max-cost 0.39")
    assert "To accept up to 0.39 USD, run: " in cap.stopped and "Record:" in cap.stopped


def test_judge_spend_cap_sends_the_rest_when_the_projection_fits(tmp_path, monkeypatch):
    cap, sent, w, run = _cap_fixtures(tmp_path, monkeypatch, 0.40)
    run()
    assert sent == [20, 80] and len(w.done) == 100 and cap.stopped is None and abs(cap.spent - 0.35) < 1e-9


def test_judge_spend_cap_counts_a_repair_turn_at_full_price():
    import types

    import harness.judge as J
    reg = types.SimpleNamespace(model=lambda _id: types.SimpleNamespace(
        price={"in": 2.0, "out": 10.0, "currency": "USD", "batch": 0.5}))
    cap = J.SpendCap(reg, "judge", 1.0, 0.002)
    u = {"prompt_tokens": 1000, "completion_tokens": 500}
    assert abs(cap.cost_of({"transport": "batch", "usage": u}) - 0.0035) < 1e-12
    assert abs(cap.cost_of({"transport": "batch", "usage": u, "repair": {"transport": "batch+repair", "usage": u}})
               - (0.0035 + 0.007)) < 1e-12
    assert cap.cost_of({"outcome": "skipped_no_answer"}) == 0


def test_mistral_family_gets_nine_character_tool_call_ids_others_unchanged():
    import types

    from harness.capabilities import Capabilities, build_chat_payload, mistral_tool_ids
    msgs = [{"role": "user", "content": "P-20417 izin?"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                                                                   "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "aB3dE6gH9", "type": "function",
                                                                   "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "aB3dE6gH9", "content": "{}"}]
    out, changed = mistral_tool_ids(msgs)
    new = out[1]["tool_calls"][0]["id"]
    assert changed and len(new) == 9 and new.isalnum() and out[2]["tool_call_id"] == new
    assert out[3]["tool_calls"][0]["id"] == "aB3dE6gH9" and msgs[1]["tool_calls"][0]["id"] == "call_1"   # plan untouched
    caps = Capabilities({"modes": {"default": {"available": True, "params": {}}}, "drop_params": []})
    spec = types.SimpleNamespace(id="m", api_model="m", options={}, extra_body={}, family="mistral")
    p, eff = build_chat_payload(spec, caps, "default", msgs, max_tokens=64, decoding={})
    assert p["messages"][2]["tool_call_id"] == new and eff.get("tool_call_ids")
    p, eff = build_chat_payload(types.SimpleNamespace(**{**spec.__dict__, "family": "qwen"}), caps, "default", msgs,
                                max_tokens=64, decoding={})
    assert p["messages"][2]["tool_call_id"] == "call_1" and "tool_call_ids" not in eff


def test_legacy_attention_mask_shim_restores_the_transformers4_encoder_mask():
    torch = pytest.importorskip("torch")
    from harness.local.backends import legacy_attention_mask_shim

    class Cfg:
        is_decoder = False

    remote = type("NomicBertModel", (torch.nn.Module,),
                  {"__module__": "transformers_modules.nomic.modeling_hf_nomic_bert",
                   "dtype": property(lambda self: torch.float32)})
    m = remote()
    m.config = Cfg()
    assert legacy_attention_mask_shim(torch.nn.Sequential(m)) == ["NomicBertModel"]
    mask = torch.tensor([[1, 1, 0]])
    out = m.get_extended_attention_mask(mask, mask.shape)
    assert tuple(out.shape) == (1, 1, 1, 3) and float(out[0, 0, 0, 0]) == 0.0
    assert float(out[0, 0, 0, 2]) == torch.finfo(torch.float32).min
    assert legacy_attention_mask_shim(torch.nn.Sequential(torch.nn.Linear(2, 2))) == []     # native classes untouched


def test_existing_session_dir_finds_one_session_and_refuses_a_name_under_two_endpoints(tmp_path, monkeypatch):
    import harness.run as R
    monkeypatch.setattr(R, "RESULTS_DIR", tmp_path)
    a = tmp_path / "track-a" / "evren" / "evren-a"
    a.mkdir(parents=True)
    (a / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "track-b" / "local" / "evren-a").mkdir(parents=True)           # no manifest: not a session
    assert R.existing_session_dir("evren-a") == a and R.existing_session_dir("other") is None
    (tmp_path / "track-b" / "local" / "evren-a" / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="several endpoints"):
        R.existing_session_dir("evren-a")


def test_judged_metrics_use_the_judged_repeats_only(tmp_path):
    import pandas as pd

    import analysis.analyze as A
    s = tmp_path / "sess"
    (s / "rag_qa" / "judge").mkdir(parents=True)
    rows, verdicts = [], []
    for v in ("a:default", "b:default"):
        for i in range(3):
            for r in range(3):
                key = f"q{i}#r{r}"
                rows.append({"session": "sess", "variant": v, "key": key, "content_sha": "h", "repeat": r,
                             "plan_hash": "p", "item_id": f"q{i}"})
                if r == 0:                                    # phase 5 judges repeat 0 only
                    verdicts.append({"final": True, "signature": "sig", "variant": v, "uid": key, "content_sha": "h",
                                     "key": f"{v}|{key}|h", "scores": {"correctness": 1.0}})
    (s / "rag_qa" / "judge" / "j.jsonl").write_text("".join(json.dumps(x) + "\n" for x in verdicts), encoding="utf-8")
    A.ARGS = None
    jd, info = A.select_judged([s], pd.DataFrame(rows), "rag_qa", 0.98)
    assert info["judged_repeats"] == [0] and info["coverage"] == {"a:default": 1.0, "b:default": 1.0}
    assert not info["excluded"] and len(jd) == 6 and set(jd["repeat"]) == {0}


def test_retrieval_rerank_subset_is_paired_with_its_first_stage_not_dropped(tmp_path):
    import analysis.analyze as A
    s = tmp_path / "sess"
    (s / "plans").mkdir(parents=True)
    items = [{"query_id": f"q{i}", "item_id": f"q{i}", "text": "?", "gold": [f"art{'AB'[i // 2]}#{i % 2}"]} for i in range(4)]
    (s / "plans" / "retrieval_xquad_tr.jsonl").write_text("".join(json.dumps(x) + "\n" for x in items), encoding="utf-8")
    (s / "plans" / "retrieval_xquad_tr.meta.json").write_text(json.dumps({"rerank_limit": 2}), encoding="utf-8")

    def rec(v, i, x):
        return {"key": f"{v}|q{i}", "final": True, "plan_hash": "p", "variant": v, "item_id": f"q{i}", "repeat": 0,
                "score": {k: x for k in ("ndcg@10", "hit@1", "hit@5", "recall@10", "full@10", "mrr@10")}}
    rows = [rec("e5", i, 0.5) for i in range(4)] + [rec("e5+rr", i, 1.0) for i in range(2)]
    (s / "retrieval_xquad_tr").mkdir()
    (s / "retrieval_xquad_tr" / "rankings.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    A.ARGS = None
    A._PLAN_META.clear()
    A._PLAN_ITEMS.clear()
    r = A.analyze_retrieval([s], "xquad_tr")
    assert list(r["table"]["system"]) == ["e5"] and r["n_queries"] == 4         # the ranked table keeps all 4 queries
    row = r["deltas"].iloc[0]
    assert row["reranked"] == "e5+rr" and row["queries"] == 2 and row["delta_ndcg@10"] == 0.5
    assert row["first_stage_ndcg@10"] == 0.5 and row["reranked_ndcg@10"] == 1.0
    note = r["info"]["partial"]["e5+rr"]
    assert "first 2 of 4 queries" in note and "1 of 2 source documents" in note and not r["info"]["excluded"]


def test_ocr_extraction_is_compared_on_the_scanned_invoices_only():
    import pandas as pd

    import analysis.analyze as A
    assert len(A.scanned_invoices()) == 12                                   # inv-01 … inv-12 have a scan
    ok, bad = {"all_correct": True}, {"all_correct": False}
    base = pd.DataFrame({"variant": ["m"] * 4, "item_id": ["inv-01", "inv-01", "inv-13", "inv-13"],
                         "score": [ok, bad, bad, bad]})
    ocr = pd.DataFrame({"variant": ["m"] * 2, "item_id": ["inv-01", "inv-01"], "score": [ok, ok]})
    t = A.docextract_text_vs_ocr({"docextract": {"df": base}, "docextract_ocr_x": {"df": ocr}})
    row = t.iloc[0]
    assert row["invoices"] == 1 and row["all_correct_text"] == 0.5 and row["all_correct_ocr"] == 1.0


# ------------------------------------------------------------------ 14 Oct: latency time budgets, OCR timing, clef
def _latency_runner(budgets: dict, models=("slow", "fast")):
    import asyncio
    import collections
    import types

    from harness.config import Variant
    from harness.suites.latency import LatencyRunner
    ctx = types.SimpleNamespace(session=types.SimpleNamespace(id="s"), stop=asyncio.Event(), log=lambda m: None,
                                reg=types.SimpleNamespace(model=lambda m: types.SimpleNamespace(endpoint="x")))
    r = LatencyRunner(ctx, [Variant(m, "default") for m in models], {"time_budget_s": budgets, "warmup_max_tokens": 8})
    sent = collections.Counter()

    async def one(v, stage, workload, conc, cell, prompt, max_tokens, **kw):
        sent[(v.model_id, stage)] += 1
        await asyncio.sleep(0.06 if v.model_id == "slow" else 0.001)
        return types.SimpleNamespace(outcome="ok")
    r.one = one
    return r, sent


@pytest.mark.parametrize("schedule", ["parallel", "interleaved"])
def test_latency_time_budget_stops_only_the_slow_stream(schedule):
    import asyncio
    r, sent = _latency_runner({"chat": 0.15})
    asyncio.run(r.single_stream("chat", 6, 64, schedule))
    assert sent[("fast", "c1")] == 6 and 2 <= sent[("slow", "c1")] < 6
    (stop,) = r.budget_stops
    assert stop["model"] == "slow" and stop["stage"] == "c1" and stop["workload"] == "chat"
    assert stop["sent"] == sent[("slow", "c1")] and stop["planned"] == 6 and stop["budget_s"] == 0.15


def test_latency_time_budget_in_warmup_and_sweep_cells():
    import asyncio

    from harness.config import Variant
    r, sent = _latency_runner({"warmup": 0.15, "cell": 0.15})
    asyncio.run(r.warmup(6))
    assert sent[("fast", "warmup")] == 6 and sent[("slow", "warmup")] < 6
    out = asyncio.run(r.concurrency_cell(Variant("slow", "default"), "chat", 2, 3, 64, 20))
    assert out.get("budget_stop") and sum(out["outcomes"].values()) < 20
    r2, _ = _latency_runner({})                                       # no budget: everything is sent
    out = asyncio.run(r2.concurrency_cell(Variant("slow", "default"), "chat", 4, 3, 64, 8))
    assert sum(out["outcomes"].values()) == 8 and "budget_stop" not in out and not r2.budget_stops


def test_ocr_probe_cache_buster_keeps_the_image_valid(tmp_path):
    from harness.suites.latency import cache_busted_image
    PIL = pytest.importorskip("PIL.Image")
    for fmt, ext in (("JPEG", "jpg"), ("PNG", "png")):
        p = tmp_path / f"x.{ext}"
        PIL.new("RGB", (40, 30), (200, 10, 10)).save(p, fmt)
        a, b = cache_busted_image(p, "s1-0"), cache_busted_image(p, "s1-1")
        assert a != b != p.read_bytes() and b"trbench s1-0" in a
        import io
        ia, ib = PIL.open(io.BytesIO(a)), PIL.open(io.BytesIO(p.read_bytes()))
        ia.load()
        assert ia.size == (40, 30) and ia.tobytes() == ib.tobytes()               # same pixels, different bytes
    other = tmp_path / "x.gif"
    other.write_bytes(b"GIF89a....")
    assert cache_busted_image(other, "n") == b"GIF89a...."


def test_ocr_probe_models_follow_the_session_choice():
    import types

    import harness.run as R
    from harness.config import load_registry
    reg = load_registry()
    cfg = reg.suite("latency")

    def a(**kw):
        base = {"endpoint": "evren", "models": None, "variants": None, "model": None, "ocr_probe": None, "no_ocr_probe": False}
        return types.SimpleNamespace(**{**base, **kw})
    assert R.ocr_probe_models(reg, a(), cfg) == ["dots-ocr"]                        # the scheduled sessions
    assert R.ocr_probe_models(reg, a(endpoint="ovh"), cfg) == []
    assert R.ocr_probe_models(reg, a(models="glm-5.3"), cfg) == []                  # a hand-picked session: none
    assert R.ocr_probe_models(reg, a(models="glm-5.3", ocr_probe="dots-ocr,deepseek-ocr-2"), cfg) == ["dots-ocr", "deepseek-ocr-2"]
    assert R.ocr_probe_models(reg, a(no_ocr_probe=True), cfg) == []
    with pytest.raises(SystemExit, match="not a hosted OCR model"):
        R.ocr_probe_models(reg, a(ocr_probe="glm-5.3"), cfg)


def test_systemone_estimate_clef_has_no_batch_pass():
    import harness.run as R
    from harness.config import load_registry
    reg = load_registry()
    n = len(R.read_jsonl(R.DATA_DIR / "decision" / "tickets.jsonl"))
    clef, laya = R.systemone_line(reg, "clef", None), R.systemone_line(reg, "laya", None)
    assert clef.requests == n and laya.requests == n + -(-n // 64)
    assert laya.tokens_in == 2 * clef.tokens_in and "no batch endpoint" in clef.note and clef.tokens_out == 0
    assert R.systemone_line(reg, "clef", 3).requests == 3
    with pytest.raises(SystemExit, match="not a typed-decision model"):
        R.systemone_line(reg, "glm-5.3", None)


def test_latency_session_end_follows_the_laptop_hours():
    from datetime import datetime

    from harness.quota import TSI
    from harness.suites.latency import session_deadline
    cfg = {"session_window": {"day_end_tsi": "18:00", "margin_minutes": 10, "evening_max_minutes": 110}}

    def d(h, m=0):
        return datetime(2026, 10, 14, h, m, tzinfo=TSI)
    assert session_deadline(cfg, d(15)) == d(17, 50) and session_deadline(cfg, d(10)) == d(17, 50)
    assert session_deadline(cfg, d(21)) == d(22, 50)                      # evening: 110 min
    assert session_deadline(cfg, d(15), max_minutes=60) == d(16)           # --max-minutes when earlier
    assert session_deadline({}, d(15)) is None and session_deadline({}, d(15), 30) == d(15, 30)
    cfg["session_window"].update(slots_tsi=["10:00", "15:00", "21:00"], before_next_slot_minutes=20)
    assert session_deadline(cfg, d(10)) == d(14, 40) and session_deadline(cfg, d(12)) == d(14, 40)   # clear of 15:00
    assert session_deadline(cfg, d(15)) == d(17, 50) and session_deadline(cfg, d(14, 50)) == d(17, 50)
    assert session_deadline(cfg, d(21)) == d(22, 50)


def test_mistral_tool_calls_left_in_the_text_are_parsed():
    """Bedrock returned Magistral Small 1.2's tool calls as text on every tool case (23 Oct); the report reads them
    to compare the model's tool use on equal terms with the self-hosted twin."""
    from harness.scoring.basic import parse_mistral_text_calls, parse_tool_calls
    t, c = parse_mistral_text_calls('[TOOL_CALLS]harcirah_hesapla{"sehir_kategorisi": "A", "gun_sayisi": 3, '
                                    '"konaklama_dahil": true}')
    assert t == "" and c == [{"name": "harcirah_hesapla",
                              "arguments": '{"sehir_kategorisi": "A", "gun_sayisi": 3, "konaklama_dahil": true}'}]
    t, c = parse_mistral_text_calls('Bakıyorum.[TOOL_CALLS]izin_bakiyesi_sorgula{"personel_no": "P-1"}'
                                    '[TOOL_CALLS]izin_talebi_olustur[ARGS]{"gun": 2}')
    assert t == "Bakıyorum." and [x["name"] for x in c] == ["izin_bakiyesi_sorgula", "izin_talebi_olustur"]
    assert json.loads(c[1]["arguments"]) == {"gun": 2}
    _, c = parse_mistral_text_calls('[TOOL_CALLS][{"name": "doviz_kuru_getir", "arguments": {"para_birimi": "EUR"}}]')
    assert c == [{"name": "doviz_kuru_getir", "arguments": '{"para_birimi": "EUR"}'}]
    _, c = parse_mistral_text_calls('[TOOL_CALLS]f[CALL_ID]abc123def[ARGS]{"a": 1')      # broken JSON -> invalid args
    assert c[0]["name"] == "f" and parse_tool_calls(c)[0]["args_valid"] is False
    assert parse_mistral_text_calls("Merhaba, nasıl yardımcı olabilirim?") == ("Merhaba, nasıl yardımcı olabilirim?", [])


def test_deployment_parity_pairs_a_self_hosted_system_with_its_twin():
    """Phase 6: options.twin pairs the self-deployed system with the hosted one; paired difference on the common
    items and the share of word-for-word identical answers."""
    from types import SimpleNamespace

    import pandas as pd

    import analysis.analyze as A
    reg = SimpleNamespace(models={"dep": SimpleNamespace(id="dep", options={"twin": "host"}, family="deploy"),
                                  "host": SimpleNamespace(id="host", options={}, family="mistral")})
    rows = []
    for i in range(30):
        for v, ok, sha in (("dep", i % 3 != 0, f"s{i}"),
                           ("host", (i % 3 != 0) and i != 4, f"s{i}" if i < 21 else f"x{i}")):
            rows.append({"variant": f"{v}:default", "model_id": v, "mode": "default", "item_id": f"q{i}", "repeat": 0,
                         "accuracy": float(ok), "content_sha": sha})
    t = A.deployment_parity({"mcq_global_mmlu_tr": {"df": pd.DataFrame(rows)}}, reg)
    assert len(t) == 1
    r = t.iloc[0]
    assert (r["self-hosted"], r["hosted twin"], r["n_items"]) == ("dep:default", "host:default", 30)
    assert abs(r["difference"] - 1 / 30) < 1e-9 and abs(r["identical answers"] - 0.7) < 1e-9
    assert r["ci_lo"] <= r["difference"] <= r["ci_hi"]


def test_deployment_capacity_splits_the_first_burst_from_steady_load():
    """Phase 6 capacity table: the first round of a level (everyone presses Enter at once) apart from the steady
    requests; a runaway answer at the token limit is left out of the timings and named."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import pandas as pd

    import analysis.analyze as A
    reg = SimpleNamespace(models={"dep": SimpleNamespace(id="dep", options={"twin": "host"}, family="deploy")})
    t0 = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
    rows = []

    def add(stage, c, start, ttft, e2e, outcome="ok", toks=100):
        rows.append({"session": "deploy-lat", "variant": "dep:default", "model_id": "dep", "workload": "rag",
                     "stage": stage, "concurrency": c, "outcome": outcome,
                     "wall_start": (t0 + timedelta(seconds=start)).isoformat(), "ttft_answer": ttft, "e2e": e2e,
                     "decode_tps": 20.0, "completion_tokens": toks, "prompt_tokens": 2000})
    for k in range(4):
        add("c1", 1, k * 10, 1.0, 5.0)
    add("sweep", 2, 100, 3.0, 8.0)                     # the burst: both start together
    add("sweep", 2, 100.01, 4.0, 9.0)
    for start, ttft in ((108, 1.2), (109, 1.4), (114, 1.3)):
        add("sweep", 2, start, ttft, 6.0)
    add("sweep", 2, 115, 1.1, 6.0, outcome="truncated", toks=4096)
    cap, notes = A.deployment_capacity({"df": pd.DataFrame(rows)}, reg)
    c1 = cap[cap["in flight"] == 1].iloc[0]
    c2 = cap[cap["in flight"] == 2].iloc[0]
    assert c1["first token p50 s"] == 1.0 and pd.isna(c1["burst: first token p50 s"])
    assert (c2["burst: first token p50 s"], c2["burst: first token max s"]) == (3.5, 4.0)
    assert c2["steady answers"] == 3 and abs(c2["first token p50 s"] - 1.3) < 1e-9
    assert abs(c2["answers per min"] - 15.0) < 1e-9          # 5 complete answers in the 20 s up to the last one
    assert any("token limit" in n and "2 in flight" in n for n in notes)
    assert any(n.startswith("dep:default, rag: comfortable") and "up to 2 requests" in n for n in notes)


def test_latency_capacity_overrides_for_a_self_hosted_server():
    """Phase 6 capacity sweep (23 Oct): more requests per stream, longer cells and no laptop-hours window, set on the
    command line for that session only — the suites.yaml defaults stay, and the config hash records the change."""
    from types import SimpleNamespace

    from harness.config import load_registry
    from harness.run import latency_cfg
    from harness.suites.latency import latency_config_hash, session_deadline
    reg = load_registry()
    base = {"workload": "rag", "concurrency_levels": None, "prompts": None, "schedule": None, "max_minutes": None,
            "until": None, "sweep_requests_per_stream": None, "cell_minutes": None, "no_session_window": False}
    default = latency_cfg(reg, SimpleNamespace(**base))
    budgets = dict(default["time_budget_s"])
    cap = latency_cfg(reg, SimpleNamespace(**{**base, "concurrency_levels": "2,4,8,12,16,20,24,32,48",
                                               "sweep_requests_per_stream": 3, "cell_minutes": 15,
                                               "no_session_window": True, "max_minutes": 120}))
    assert cap["concurrency_levels"] == [2, 4, 8, 12, 16, 20, 24, 32, 48] and cap["sweep_requests_per_stream"] == 3
    assert cap["time_budget_s"] == {**budgets, "cell": 900} and cap["session_window"] == {}
    assert session_deadline(cap) is None                     # only --max-minutes bounds it
    again = latency_cfg(reg, SimpleNamespace(**base))         # the suites.yaml defaults are untouched
    assert again["time_budget_s"] == budgets and again["session_window"] and again["sweep_requests_per_stream"] == 2
    h_cap, _ = latency_config_hash(reg, cap, "standard", ["rag"], 20)
    h_def, _ = latency_config_hash(reg, again, "standard", ["rag"], 20)
    assert h_cap != h_def


def test_latency_session_end_stops_every_stream():
    import asyncio
    import time
    r, sent = _latency_runner({})
    r.deadline = time.time() + 0.1                                        # wall clock: a PC asleep does not stretch it
    asyncio.run(r.single_stream("chat", 6, 64, "parallel"))
    assert r.deadline_hit and sent[("slow", "c1")] < 6
    assert {x["reason"] for x in r.budget_stops} == {"session_end"}
    before = sum(sent.values())
    asyncio.run(r.warmup(3))                                              # past the end: nothing more is sent
    assert sum(sent.values()) == before


def test_unlimited_ocr_post_processing_follows_the_model_card():
    from harness.local.ocr_worker import parse_document
    raw = ("Önsöz satırı\n<|det|>title [10, 10, 900, 40]<|/det|>Başlık\n\n<|det|>text [1,2,3,4]<|/det|>Birinci satır\n"
           "ikinci satır   \n<|det|>image [0, 0, 5, 5]<|/det|>\nresimden sonra\n<|det|>table<|/det|>\n| a | b |\n")
    assert parse_document(raw) == ("Önsöz satırı\n\nBaşlık\n\nBirinci satır\nikinci satır\nresimden sonra"
                                   "\n\n| a | b |")
    assert parse_document("") == "" and parse_document("<|det|>image [1]<|/det|>") == ""


def test_ocr_worker_protocol_isolation_and_restart(tmp_path, monkeypatch):
    import types

    from harness.local.backends import WorkerOCR
    monkeypatch.setenv("TRBENCH_OCR_WORKER_FAKE", "1")
    page = tmp_path / "p-01.jpg"
    page.write_bytes(b"x")
    (tmp_path / "p-01.gt.txt").write_text("Birinci satır\n\nİkinci satır\n", encoding="utf-8")
    spec = types.SimpleNamespace(id="uocr", api_model="baidu/Unlimited-OCR",
                                 options={"backend": "worker", "revision": "r1", "crop_mode": True})
    w = WorkerOCR.from_spec(spec, tmp_path / "logs")
    try:
        assert w.info["device"] == "fake" and "--crop-mode" in w.cmd and w.cmd[w.cmd.index("--crop-mode") + 1] == "1"
        d = w.run_detail(page)
        assert d["error"] is None and d["text"] == "Birinci satır\n\nİkinci satır" and d["raw"].startswith("<|det|>")
        w.proc.kill()                                                    # a dead worker is restarted for the next page
        w.proc.wait(timeout=10)
        d2 = w.run_detail(page)                                          # ... and its start time is reported apart
        assert d2["error"] is None and d2["text"].startswith("Birinci") and w.restarts == 1 and d2["restart_s"] >= 0
        assert "restart_s" not in w.run_detail(page)
    finally:
        w.close()
    log = (tmp_path / "logs" / "uocr.worker.log").read_text(encoding="utf-8")
    assert log.count("directly resize") == 3 and log.count("start: ") == 2   # model prints never reach the protocol


def test_ocr_worker_without_its_environment_says_how_to_create_it(monkeypatch):
    from harness.local.backends import worker_python
    monkeypatch.delenv("TRBENCH_OCR_WORKER_FAKE", raising=False)
    with pytest.raises(RuntimeError, match="setup_unlimited_ocr.ps1"):
        worker_python("no/such/python.exe")


def test_latency_lock_waits_for_a_finishing_session_then_gives_up(tmp_path):
    import asyncio
    import json as _json
    import time as _time

    from harness.locks import LatencyLock
    lock = tmp_path / ".latency.lock"

    def other():
        lock.write_text(_json.dumps({"session": "earlier", "endpoints": ["evren"], "scope": "machine",
                                     "heartbeat_epoch": _time.time()}), encoding="utf-8")

    async def acquire(wait_s, release_after=None):
        other()
        if release_after is not None:
            asyncio.get_running_loop().call_later(release_after, lock.unlink)
        lk = LatencyLock("next", ["evren"], path=lock, wait_s=wait_s, log=lambda m: None)
        async with lk:
            assert _json.loads(lock.read_text(encoding="utf-8"))["session"] == "next"
        return lk.waited_s
    assert asyncio.run(acquire(5.0, release_after=0.3)) > 0.2            # the earlier one finished: this one starts
    with pytest.raises(RuntimeError, match="must not overlap"):
        asyncio.run(acquire(0.3))                                         # still running after the wait: give up


def test_latency_sweep_skips_the_higher_levels_of_a_model_that_hit_its_cell_budget():
    import asyncio
    import types
    r, _sent = _latency_runner({"cell": 0.15})
    r.cfg.update({"concurrency_levels": [2, 4], "sweep_workloads": ["chat"], "sweep_modes": ["default"],
                  "sweep_min_requests": 8, "sweep_requests_per_stream": 2, "cooldown_s": 0})
    r.ctx.reg.endpoints = {"x": types.SimpleNamespace(limits={}, name="x")}
    out = asyncio.run(r.sweeps(["chat"], 3, {"chat": 64}))
    by = {(x["variant"], x["concurrency"]): x for x in out}
    assert by[("slow:default", 2)].get("budget_stop") and "higher" not in str(by[("slow:default", 2)])
    assert "time budget" in by[("slow:default", 4)]["skipped"]                 # 14 Oct: GLM-5.3 at 1.7 tokens/s
    assert "skipped" not in by[("fast:default", 4)] and sum(by[("fast:default", 4)]["outcomes"].values()) == 8


def test_latency_lock_mirrors_heartbeats_into_the_session(tmp_path):
    import asyncio
    import json as _json

    from harness.locks import LatencyLock

    async def go():
        async with LatencyLock("s1", ["evren"], path=tmp_path / ".latency.lock", mirror=tmp_path / "hb.json"):
            pass
    asyncio.run(go())
    hb = _json.loads((tmp_path / "hb.json").read_text(encoding="utf-8"))
    assert hb["session"] == "s1" and "power" in hb and not (tmp_path / ".latency.lock").exists()   # lock released
    assert hb["released"] is True                       # ended normally: the analysis does not take it for running


def test_analysis_names_how_an_unfinished_latency_session_ended(tmp_path):
    import json as _json
    import time as _time

    import analysis.analyze as A
    s = tmp_path / "20261014-1500-afternoon-x"
    (s / "latency").mkdir(parents=True)
    (s / "manifest.json").write_text(_json.dumps({"endpoint": "evren", "suites": {}}), encoding="utf-8")
    (s / "latency" / "m__default.jsonl").write_text("", encoding="utf-8")
    hb = s / "latency_heartbeat.json"
    A.ARGS = None
    A._MAN.clear()
    hb.write_text(_json.dumps({"heartbeat_epoch": _time.time(), "power": {"ac_power": False}}), encoding="utf-8")
    _df, excluded, notes = A.latency_records([s])
    assert "still running" in excluded[s.name] and s.name not in notes            # a fresh heartbeat: running
    hb.write_text(_json.dumps({"heartbeat_epoch": _time.time() - 600, "power": {"ac_power": False}}), encoding="utf-8")
    _df, excluded, notes = A.latency_records([s])                                  # killed 10 min ago: it counts
    assert not excluded and "stopped without finishing" in notes[s.name] and "on battery" in notes[s.name]
    hb.write_text(_json.dumps({"heartbeat_epoch": _time.time(), "released": True}), encoding="utf-8")
    _df, excluded, notes = A.latency_records([s])                                  # ended normally a moment ago
    assert not excluded


# ------------------------------------------------------------------ 15 Oct: resumable sessions, network outages
def test_net_guard_tells_an_outage_on_this_side_from_a_server_failure():
    import asyncio
    import time as _time

    from harness.suites.latency import NetGuard
    state = {"up": True, "probes": 0}

    async def probe(ep):
        state["probes"] += 1
        return (True, "HTTP 200") if state["up"] else (False, "conn_error: ConnectError: [Errno 11001] getaddrinfo failed")

    async def go():
        g = NetGuard(probe, interval_s=0.05, log=lambda m: None)
        t = _time.perf_counter()
        assert await g.check("stream_broken", "ReadError: ", t, t, "e") is None       # it answers: the server's failure
        assert await g.check("http_503", None, t, t, "e") is None and state["probes"] == 1   # an HTTP answer: no check
        dns = "ConnectError: [Errno 11001] getaddrinfo failed"
        assert await g.check("conn_error", dns, t, t, "e") == "client"            # a failed name lookup: this side
        assert not g.offline and state["probes"] == 2
        state["up"] = False                                                       # 14 Oct 16:15: the Wi-Fi drops
        t0 = _time.perf_counter()
        sides = await asyncio.gather(*(g.check("stream_broken", "ReadError: ", t0, _time.perf_counter(), "e")
                                       for _ in range(5)))
        assert sides == ["client"] * 5 and g.offline and state["probes"] == 3     # five streams, one check
        asyncio.get_running_loop().call_later(0.25, lambda: state.update(up=True))
        await g.ready()                                                            # every stream waits for it
        assert not g.offline and len(g.outages) == 1 and g.outages[0]["seconds"] >= 0.2
        assert g.outages[0]["side"] == "client" and g.outages[0]["ended"] == "endpoint answers again"
        assert await g.check("ok", None, t0, _time.perf_counter(), "e") == "client"   # ran while offline: labelled
        assert 0.2 <= g.downtime(t0) < 5                                          # not charged to time budgets
        t1 = _time.perf_counter()
        assert await g.check("ok", None, t1, t1, "e") is None and g.downtime(t1) == 0
    asyncio.run(go())


def test_net_guard_stops_waiting_at_the_session_end():
    import asyncio
    import time as _time

    from harness.suites.latency import NetGuard
    end = {"now": False}

    async def down(ep):
        return False, "timeout_connect: ConnectTimeout"

    async def go():
        g = NetGuard(down, interval_s=0.05, log=lambda m: None, give_up=lambda: end["now"])
        t = _time.perf_counter()
        assert await g.check("timeout_connect", "ConnectTimeout", t, t, "e") == "unknown"   # no DNS evidence
        asyncio.get_running_loop().call_later(0.2, lambda: end.update(now=True))
        await asyncio.wait_for(g.ready(), 5)
        assert g.outages[0]["ended"] == "session end"
    asyncio.run(go())


def test_latency_request_hit_by_an_outage_is_sent_again_with_a_fresh_tag():
    import asyncio
    import types
    r, _sent = _latency_runner({})
    calls = []

    async def one(v, stage, workload, conc, cell, prompt, max_tokens, **kw):
        calls.append((v.model_id, kw["try_"], prompt["messages"][-1]["content"], kw["key"]))
        hit = v.model_id == "fast" and kw["try_"] == 0 and sum(c[0] == "fast" for c in calls) == 2
        return types.SimpleNamespace(outcome="conn_error" if hit else "ok", outage="client" if hit else None)
    r.one = one
    asyncio.run(r.single_stream("chat", 3, 64, "parallel"))
    fast = [c for c in calls if c[0] == "fast"]
    assert len(fast) == 4 and [c[1] for c in fast] == [0, 0, 1, 0]                # the 2nd prompt again, try 1
    assert fast[2][2] != fast[1][2] and fast[2][3].endswith("p1t1")               # a new cache-busting tag and key
    assert fast[2][2].split("\n", 1)[1] == fast[1][2].split("\n", 1)[1]           # ... the same question
    assert len(r.c1_done) == 6


def test_latency_resume_sends_only_what_is_missing():
    import asyncio

    from harness.suites.latency import prompt_ids
    pids = prompt_ids("chat", 4)
    r, sent = _latency_runner({"chat": 5})
    r.part = 2
    r.c1_done = {("chat", "fast:default", p) for p in pids[:3]} | {("chat", "slow:default", pids[0])}
    asyncio.run(r.single_stream("chat", 4, 64, "parallel"))
    assert sent[("fast", "c1")] == 1 and sent[("slow", "c1")] == 3 and len(r.c1_done) == 8
    assert r.salt("v", 0) == "v|p2t0" and r.salt("v", 1) == "v|p2t1"             # part 2: never the part-1 tags
    r.part = 1
    assert r.salt("v", 0) == "v"                                                   # part 1: tags as before 15 Oct
    # a model stopped on its time budget in an earlier part stays stopped; the time earlier parts used counts
    r2, sent2 = _latency_runner({"chat": 5})
    r2.carried_stops = {("c1", "chat", "slow")}
    r2.c1_spent = {("chat", "fast"): 10.0}
    asyncio.run(r2.single_stream("chat", 4, 64, "parallel"))
    assert sent2[("slow", "c1")] == 0 and sent2[("fast", "c1")] == 0
    (stop,) = r2.budget_stops
    assert stop["model"] == "fast" and stop["elapsed_s"] >= 10 and stop["reason"] == "time_budget"
    assert r2.progress.of("budget_stop")[0]["model"] == "fast"                     # kept for the next part


def test_latency_sweep_cell_hit_by_an_outage_is_measured_again_and_finished_cells_are_skipped():
    import asyncio
    import collections
    import types
    r, _ = _latency_runner({})
    r.cfg.update({"concurrency_levels": [2], "sweep_workloads": ["chat"], "sweep_modes": ["default"],
                  "sweep_min_requests": 4, "sweep_requests_per_stream": 2, "cooldown_s": 0})
    r.ctx.reg.endpoints = {"x": types.SimpleNamespace(limits={}, name="x")}
    n = collections.Counter()

    async def one(v, stage, workload, conc, cell, prompt, max_tokens, **kw):
        n[(v.model_id, kw["try_"])] += 1
        hit = v.model_id == "slow" and kw["try_"] == 0 and n[("slow", 0)] == 2
        await asyncio.sleep(0.001)
        return types.SimpleNamespace(outcome="stream_broken" if hit else "ok", outage="unknown" if hit else None)
    r.one = one
    r.cells_done = {("sweep", "chat", 2, "fast:default")}                         # finished by an earlier part
    out = asyncio.run(r.sweeps(["chat"], 3, {"chat": 64}))
    assert [x["variant"] for x in out] == ["slow:default"] and n[("fast", 0)] == 0
    assert n[("slow", 1)] == 4 and out[0]["attempt"] == 1 and out[0]["outcomes"] == {"ok": 4}
    assert not out[0].get("outage")
    (ev,) = r.progress.of("cell_done")
    assert ev["key"] == ["chat", 2, "slow:default"] and ev["result"]["attempt"] == 1


def test_records_from_before_the_network_check_get_their_outage_found():
    from datetime import datetime, timedelta, timezone

    from harness.suites.latency import label_legacy_outages, latest_attempts, record_outage
    t = datetime(2026, 10, 14, 13, 15, 26, tzinfo=timezone.utc)                   # 14 Oct, 16:15:26 TSİ
    dns = "ConnectError: [Errno 11001] getaddrinfo failed"

    def rec(outcome, start, dur, err=None, **kw):
        return {"stage": "c1", "result": {"outcome": outcome, "wall_start": (t + timedelta(seconds=start)).isoformat(),
                                          "t_end": dur, "error": err}, **kw}
    recs = [rec("stream_broken", -227, 227, "ReadError: "),       # the five streams that broke in the same second
            rec("stream_broken", -26, 26.1, "ReadError: "),
            rec("conn_error", 0.1, 0.0, dns), rec("conn_error", 1.0, 0.0, dns, stage="sweep"),
            rec("stream_broken", -3600, 60, "ReadError: "),       # an hour earlier: the server's
            rec("ok", -10, 5), rec("http_503", 0.5, 0.1)]
    assert label_legacy_outages(recs) == 4
    assert [r.get("outage") for r in recs] == ["client"] * 4 + [None] * 3
    assert record_outage({"part": 1, "result": {"outcome": "conn_error", "error": dns}}) == "client"
    c1 = lambda p, a, **kw: {"stage": "c1", "workload": "chat", "variant": "a", "prompt_id": p, "part": a[0],
                             "try": a[1], **kw}
    keep = latest_attempts([c1("p1", (1, 0), outage="client"), c1("p1", (1, 1)), c1("p2", (1, 0)),
                            {"stage": "sweep", "cell": "c2-x", "part": 1, "try": 0},
                            {"stage": "sweep", "cell": "c2-x", "part": 2, "try": 0, "n": 1},
                            {"stage": "sweep", "cell": "c2-x", "part": 2, "try": 0, "n": 2},
                            {"stage": "warmup"}])
    assert sorted((r["stage"], r.get("prompt_id"), r.get("try"), r.get("n")) for r in keep) == [
        ("c1", "p1", 1, None), ("c1", "p2", 0, None), ("sweep", None, 0, 1), ("sweep", None, 0, 2),
        ("warmup", None, None, None)]


def _session_state(progress=(), summary=None, has_records=True, meta=None):
    from datetime import datetime
    from pathlib import Path

    from harness.quota import TSI
    from harness.suites.latency import SessionState
    return SessionState(sdir=Path("x"), session_id="s", created=datetime(2026, 10, 15, 10, 0, tzinfo=TSI),
                        summary=summary or {}, progress=list(progress), meta=meta or {"config_hash": "h1"},
                        has_records=has_records)


def test_latency_session_status_window_quota_parts_configuration():
    from datetime import datetime

    from harness.quota import TSI
    from harness.suites.latency import session_status
    cfg = {"session_window": {"day_end_tsi": "18:00", "margin_minutes": 10, "evening_max_minutes": 110,
                              "slots_tsi": ["10:00", "15:00", "21:00"], "before_next_slot_minutes": 20}, "max_parts": 3}

    def at(h, m=0):
        return datetime(2026, 10, 15, h, m, tzinfo=TSI)
    st = _session_state
    assert session_status(st(), cfg, at(12))[0] is None                            # begun before 15 Oct, 10:00 slot
    assert session_status(st(), cfg, at(14, 41))[0] == "window_over"              # its window: until 14:40
    p1 = {"event": "part_start", "part": 1, "window_end_tsi": "2026-10-15T13:00+03:00"}
    assert session_status(st([p1]), cfg, at(12, 59))[0] is None                    # part 1 fixed the window
    assert session_status(st([p1]), cfg, at(13, 0))[0] == "window_over"
    assert session_status(st([p1]), dict(cfg, until_tsi="2026-10-15T12:30+03:00"), at(12, 45))[0] == "window_over"
    assert session_status(st([p1, {"event": "part_end", "part": 1, "reason": "complete"}]), cfg, at(12))[0] == "complete"
    assert session_status(st([p1, {"event": "part_end", "part": 1, "reason": "quota"}]), cfg, at(12))[0] == "quota"
    many = [p1, {"event": "part_start", "part": 2}, {"event": "part_start", "part": 3}]
    assert session_status(st(many), cfg, at(12))[0] == "max_parts"
    assert session_status(st([p1]), cfg, at(12), config_hash="h2")[0] == "config_changed"
    reason, msg = session_status(st([p1, {"event": "part_end", "part": 1, "reason": "interrupted"}]), cfg, at(12),
                                 config_hash="h1")
    assert reason is None and "part 2" in msg and "13:00" in msg                   # Ctrl+C'd: resumed
    assert session_status(st(has_records=False), cfg, at(12)) == (None, "")       # a new session
    # (a full date: run.py turns --until HH:MM into one when it parses it; a bare "11:00" here would mean 11:00 on the
    # day the test runs — 16 Oct: the test failed the day after it was written)
    assert session_status(st(has_records=False), dict(cfg, until_tsi="2026-10-15T11:00+03:00"), at(12))[0] == "window_over"
    assert session_status(st(summary={"completed": True}), cfg, at(23))[0] == "complete"


def test_latency_summary_over_parts_and_closing_a_killed_session(tmp_path):
    import json as _json
    import types

    from harness.suites.latency import build_summary, close_session, load_state
    s = tmp_path / "20261015-1000-morning-slot"
    (s / "latency").mkdir(parents=True)
    (s / "plans").mkdir()
    (s / "plans" / "latency.meta.json").write_text(_json.dumps({"config_hash": "h1", "config": {
        "workloads": ["chat"], "n": 2, "mode_workloads": {"nothink": ["rag"]}}, "variants": ["a:default", "a:nothink"]}),
        encoding="utf-8")
    man = {"session_id": s.name, "created_tsi": "2026-10-15T10:00:02+03:00", "suites": {}}
    (s / "manifest.json").write_text(_json.dumps(man), encoding="utf-8")

    def rec(pid, part, outage=None, at="2026-10-15T07:10:00.000+00:00"):
        r = {"stage": "c1", "workload": "chat", "variant": "a:default", "model_id": "a", "prompt_id": pid, "part": part,
             "try": 0, "recorded_at": at, "result": {"outcome": "ok", "t_end": 1.0, "wall_start": at}}
        return {**r, "outage": outage} if outage else r
    (s / "latency" / "a__default.jsonl").write_text("".join(_json.dumps(x) + "\n" for x in (
        rec("q1", 1), rec("q2", 1, outage="client", at="2026-10-15T07:20:00.000+00:00"))), encoding="utf-8")
    ev = [{"event": "part_start", "part": 1, "started_tsi": "2026-10-15T10:00:02+03:00", "variants": ["a:default", "a:nothink"],
           "window_end_tsi": "2026-10-15T14:40+03:00", "deadline_tsi": "2026-10-15T14:40+03:00", "config_hash": "h1",
           "plan_hash": "ph", "power_start": {"ac_power": True}},
          {"event": "budget_stop", "part": 1, "stage": "c1", "workload": "chat", "model": "a", "sent": 1, "planned": 2,
           "reason": "time_budget"},
          {"event": "outage", "part": 1, "start_tsi": "2026-10-15T10:19:55+03:00", "seconds": 40.0, "side": "client"},
          {"event": "part_start", "part": 2, "started_tsi": "2026-10-15T10:30:00+03:00", "power_start": {"ac_power": False}},
          {"event": "cell_done", "kind": "sweep", "key": ["chat", 2, "a:default"], "part": 2,
           "result": {"cell": "c2-chat-a_default", "outcomes": {"ok": 8}}},
          {"event": "part_end", "part": 2, "ended_tsi": "2026-10-15T11:00:00+03:00", "reason": "deadline",
           "client_loop_lag": {"p99_ms": 3.0}, "kept_awake": True}]
    (s / "latency_progress.jsonl").write_text("".join(_json.dumps(e) + "\n" for e in ev), encoding="utf-8")
    summ = build_summary(load_state(s), {}, "deadline")
    p1, p2 = summ["parts"]
    assert p1["end"].startswith("ended from outside") and p1["ended_tsi"] == "2026-10-15T10:20:00+03:00"
    assert p2["end"] == "deadline" and summ["deadline_hit"] and summ["partial"] and not summ["interrupted"]
    assert summ["coverage"]["c1"] == {"chat": {"done": 1, "planned": 2}}          # nothink is rag-only
    assert summ["coverage"]["requests_hit_by_outages"] == 1 and summ["outages"][0]["seconds"] == 40.0
    assert summ["budget_stops"][0]["model"] == "a" and summ["sweeps"][0]["outcomes"] == {"ok": 8}
    assert summ["plan_hash"] == "ph" and summ["resumed"] and summ["window_end_tsi"] == "2026-10-15T14:40+03:00"
    assert summ["power_start"] == {"ac_power": True} and summ["client_loop_lag"]["p99_ms"] == 3.0
    written = []
    sess = types.SimpleNamespace(dir=s, manifest=man, register_suite=lambda suite, info: written.append(info))
    # its last part ended normally (deadline): the summary says so, also when it is closed after the window
    assert close_session(sess, {}, "window_over")["end_reason"] == "deadline" and len(written) == 1
    man["suites"]["latency"] = written[-1]                                         # a later start after the window:
    assert close_session(sess, {}, "window_over")["end_reason"] == "deadline" and len(written) == 1   # unchanged
    man["suites"]["latency"] = {"summary": {"end_reason": "error: OSError: x"}}    # a stale summary is repaired
    assert close_session(sess, {}, "window_over")["end_reason"] == "deadline" and len(written) == 2
    prog = (s / "latency_progress.jsonl").read_text(encoding="utf-8").splitlines()  # killed in part 2 instead:
    (s / "latency_progress.jsonl").write_text("\n".join(prog[:-1]) + "\n", encoding="utf-8")
    man["suites"]["latency"] = written[-1]
    summ = close_session(sess, {}, "window_over")
    assert summ["end_reason"] == "window_over" and not summ["deadline_hit"] and len(written) == 3
    assert summ["parts"][1]["end"].startswith("ended from outside")


def test_latency_lock_same_session_in_another_process(tmp_path, monkeypatch):
    import asyncio
    import json as _json
    import time as _time

    import harness.locks as Lk
    monkeypatch.setattr(Lk, "STALE_S", 0.4)
    monkeypatch.setattr(Lk, "HEARTBEAT_S", 0.1)
    lock = tmp_path / ".latency.lock"

    def other():
        lock.write_text(_json.dumps({"session": "s1", "pid": 999999, "heartbeat_epoch": _time.time()}), encoding="utf-8")

    async def acquire():
        async with Lk.LatencyLock("s1", ["e"], path=lock, log=lambda m: None):
            assert _json.loads(lock.read_text(encoding="utf-8"))["pid"] != 999999
    other()                                                       # a part killed a moment ago: its lock goes stale
    asyncio.run(acquire())

    async def busy():
        async def beat():
            while True:
                other()
                await asyncio.sleep(0.05)
        t = asyncio.create_task(beat())                           # ... still alive in another process: not twice
        await asyncio.sleep(0.02)
        try:
            with pytest.raises(Lk.SessionBusy, match="already running"):
                await acquire()
        finally:
            t.cancel()
    asyncio.run(busy())


def test_result_writer_repairs_a_line_cut_by_a_kill(tmp_path):
    import asyncio

    from harness.store import ResultWriter, read_jsonl
    p = tmp_path / "r.jsonl"
    p.write_text('{"key": "a", "final": true}\n{"key": "b", "fin', encoding="utf-8")
    w = ResultWriter(p)
    asyncio.run(w.write({"key": "c", "final": True}))
    assert w.done == {"a", "c"} and [r["key"] for r in read_jsonl(p)] == ["a", "c"]


def test_latency_window_gives_each_slot_a_fixed_session_and_end(capsys):
    import json as _json
    import types

    import harness.run as R
    from harness.config import load_registry
    args = types.SimpleNamespace(at=["2026-10-15 10:00", "2026-10-15 15:00", "2026-10-15 21:00", "now"], max_minutes=None)
    R.cmd_latency_window(load_registry(), args)
    out = [_json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [(o["session"], o["end_tsi"], o["minutes"]) for o in out[:3]] == [
        ("20261015-1000-morning-slot", "2026-10-15T14:40+03:00", 280),
        ("20261015-1500-afternoon-slot", "2026-10-15T17:50+03:00", 170),
        ("20261015-2100-evening-slot", "2026-10-15T22:50+03:00", 110)]
    assert len(out[3]["session"].rsplit("-", 1)[1]) == 4 and not out[3]["session"].endswith("slot")


def test_analysis_compares_systems_on_the_same_sessions(tmp_path):
    import json as _json

    import analysis.analyze as A

    def session(name, cells):
        s = tmp_path / name
        (s / "latency").mkdir(parents=True)
        (s / "manifest.json").write_text(_json.dumps({"session_id": name, "endpoint": "e", "track": "a",
                                                      "time_slot_tsi": "morning", "created_tsi": "2026-10-15T10:00:00+03:00",
                                                      "suites": {"latency": {"summary": {"completed": True}}}}), encoding="utf-8")
        rows = {}
        for (v, w), ttft in cells.items():
            rr = [{"variant": v, "model_id": v.split(":")[0], "mode": "default", "workload": w, "stage": "c1",
                   "concurrency": 1, "cell": "c1", "config_hash": "h", "prompt_id": f"p{i}", "part": 1, "try": 0,
                   "result": {"outcome": "ok", "t_first_content": ttft, "t_last_token": ttft + 1, "t_end": ttft + 1,
                              "wall_start": "2026-10-15T07:00:00+00:00", "content": "bir iki", "usage": {"completion_tokens": 9}}}
                  for i in range(3)]
            rr.append({**rr[0], "prompt_id": "p9", "outage": "client",              # hit by an outage: left out
                       "result": {**rr[0]["result"], "outcome": "conn_error", "t_first_content": None}})
            rows.setdefault(v, []).extend(rr)
        for v, rr in rows.items():
            (s / "latency" / f"{v.split(':')[0]}__default.jsonl").write_text(
                "".join(_json.dumps(r) + "\n" for r in rr), encoding="utf-8")
        return s
    s1 = session("s1", {("a:default", "chat"): 1.0, ("b:default", "chat"): 2.0, ("a:default", "rag"): 1.0,
                        ("b:default", "rag"): 2.0})
    s2 = session("s2", {("a:default", "chat"): 9.0, ("a:default", "rag"): 3.0,   # stopped part-way: no chat for b
                        ("b:default", "rag"): 4.0})
    A.ARGS = None
    A._MAN.clear()
    r = A.analyze_latency([s1, s2])
    t = r["summary"].set_index(["workload", "variant"])
    assert t.loc[("chat", "a:default"), "sessions"] == 1 and t.loc[("chat", "a:default"), "ttft_answer_p50"] == 1.0
    assert t.loc[("rag", "a:default"), "sessions"] == 2 and t.loc[("rag", "b:default"), "ttft_answer_p50"] == 3.0
    assert bool(t.loc[("chat", "b:default"), "same_sessions"]) and t.loc[("chat", "a:default"), "ok_rate"] == 1.0


def test_net_guard_never_leaves_the_streams_waiting():
    """15 Oct review: a failing log line / progress write inside the watcher must not keep every stream waiting."""
    import asyncio
    import time as _time

    from harness.suites.latency import NetGuard

    async def case(**kw):
        state = {"up": False}

        async def probe(ep):
            return (True, "HTTP 200") if state["up"] else (False, "conn_error: ConnectError")
        g = NetGuard(probe, interval_s=0.05, **kw)
        t = _time.perf_counter()
        assert await g.check("stream_broken", "ReadError", t, t, "e") == "unknown" and g.offline
        asyncio.get_running_loop().call_later(0.15, lambda: state.update(up=True))
        await asyncio.wait_for(g.ready(), 3)
        assert not g.offline and len(g.outages) == 1

    def boom(*a):
        raise OSError(22, "Invalid argument")              # print() to a closed pipe on Windows, a locked file
    asyncio.run(case(log=boom))
    asyncio.run(case(log=lambda m: None, on_outage=boom, on_back=boom))


def test_latency_cell_cut_by_the_session_end_after_an_outage_is_not_done():
    import asyncio
    import time as _time
    import types
    r, _ = _latency_runner({})
    r.cfg.update({"concurrency_levels": [2], "sweep_workloads": ["chat"], "sweep_modes": ["default"],
                  "sweep_min_requests": 4, "sweep_requests_per_stream": 2, "cooldown_s": 0})
    r.ctx.reg.endpoints = {"x": types.SimpleNamespace(limits={}, name="x")}

    async def one(v, stage, workload, conc, cell, prompt, max_tokens, **kw):
        if v.model_id == "slow":
            r.deadline = _time.time() - 1                     # the window ends while the cell is hit
            return types.SimpleNamespace(outcome="stream_broken", outage="client")
        return types.SimpleNamespace(outcome="ok", outage=None)
    r.one = one
    out = asyncio.run(r.sweeps(["chat"], 3, {"chat": 64}))
    assert not r.progress.of("cell_done") or all(e["key"][2] != "slow:default" for e in r.progress.of("cell_done"))
    assert ("sweep", "chat", 2, "slow:default") not in r.cells_done and any(x.get("outage") for x in out)
    assert r.end_reason() in ("deadline", "part_limit") and not r.left_out


def test_latency_requests_given_up_after_outages_keep_the_session_open():
    import asyncio
    import types
    r, _ = _latency_runner({})

    async def one(v, stage, workload, conc, cell, prompt, max_tokens, **kw):
        hit = v.model_id == "fast" and kw["key"].split("|")[3] == r._hit
        return types.SimpleNamespace(outcome="conn_error" if hit else "ok", outage="client" if hit else None)
    from harness.suites.latency import prompt_ids
    r._hit = prompt_ids("chat", 3)[1]
    r.one = one
    asyncio.run(r.single_stream("chat", 3, 64, "parallel"))
    assert r.left_out == 1 and r.end_reason() == "incomplete" and r.c1_left("chat", 3) == [("fast:default", r._hit)]


def test_latency_more_prompts_than_the_subset_has():
    import asyncio

    from harness.suites.latency import prompt_ids
    r, sent = _latency_runner({})
    n = len(prompt_ids("chat", 10_000))
    asyncio.run(r.single_stream("chat", n + 5, 64, "parallel"))            # each prompt of the subset once
    assert sent[("fast", "c1")] == n and len(r.c1_done) == 2 * n


# ------------------------------------------------------------------ 16 Oct: a slow model's later stages, stage order
@pytest.mark.parametrize("schedule", ["parallel", "interleaved"])
def test_latency_model_that_used_up_its_budget_is_left_out_of_the_later_stages(schedule):
    """15 Oct afternoon: chat and rag waited 16 and 18 min for one stream at its time budget (GLM-5.3), and the
    sweep ran out of time.  Now that model is left out of the session's later stages and of its sweep cells."""
    import asyncio
    import types

    from harness.config import Variant
    from harness.suites.latency import prompt_ids
    r, sent = _latency_runner({"chat": 0.15})
    asyncio.run(r.single_stream("chat", 6, 64, schedule))
    assert r.slow_models == {"slow": "chat"} and 2 <= sent[("slow", "c1")] < 6
    n_slow = sent[("slow", "c1")]
    asyncio.run(r.single_stream("rag", 3, 64, schedule))
    asyncio.run(r.single_stream("rag", 3, 64, schedule))                          # noted once per session
    assert sent[("slow", "c1")] == n_slow and sent[("fast", "c1")] == 6 + 3
    stop, skip = r.budget_stops
    assert stop["reason"] == "time_budget" and stop["workload"] == "chat"
    assert skip == {"stage": "c1", "workload": "rag", "model": "slow", "sent": 0, "planned": 3, "elapsed_s": None,
                    "budget_s": None, "reason": "slow", "budget_used_in": "chat"}
    assert [e["reason"] for e in r.progress.of("budget_stop")] == ["time_budget", "slow"]
    assert r.c1_left("long", 3) == [("fast:default", p) for p in prompt_ids("long", 3)]
    assert not r.has_work(Variant("slow", "default"), ["chat", "rag", "long"], 3)    # no warm-up in a later part
    assert r.has_work(Variant("fast", "default"), ["chat", "rag", "long"], 3)
    r.cfg.update({"concurrency_levels": [2], "sweep_workloads": ["rag"], "sweep_modes": ["default"],
                  "sweep_min_requests": 4, "sweep_requests_per_stream": 2, "cooldown_s": 0})
    r.ctx.reg.endpoints = {"x": types.SimpleNamespace(limits={}, name="x")}
    out = {x["variant"]: x for x in asyncio.run(r.sweeps(["chat", "rag", "long"], 3, {"rag": 64}))}
    assert out["slow:default"]["slow_after"] == "chat" and "time budget" in out["slow:default"]["skipped"]
    assert sum(out["fast:default"]["outcomes"].values()) == 4 and sent[("slow", "sweep")] == 0
    assert r.end_reason() == "complete"                                            # left out on purpose: not missing


def test_latency_slow_model_stays_left_out_when_the_session_is_resumed():
    import asyncio

    from harness.suites.latency import prompt_ids
    r, sent = _latency_runner({"chat": 5})
    r.adopt(_session_state([
        {"event": "part_start", "part": 1},
        {"event": "budget_stop", "part": 1, "stage": "c1", "workload": "chat", "model": "slow", "sent": 3, "planned": 6,
         "reason": "time_budget"},
        {"event": "budget_stop", "part": 1, "stage": "c1", "workload": "rag", "model": "slow", "sent": 0, "planned": 3,
         "reason": "slow", "budget_used_in": "chat"},
        {"event": "budget_stop", "part": 1, "stage": "c1", "workload": "rag", "model": "fast", "sent": 1, "planned": 3,
         "reason": "session_end"}]))
    assert r.slow_models == {"slow": "chat"} and ("c1", "chat", "slow") in r.carried_stops
    r.part = 2
    asyncio.run(r.single_stream("rag", 3, 64, "parallel"))
    asyncio.run(r.single_stream("long", 3, 64, "parallel"))
    assert sent[("slow", "c1")] == 0 and sent[("fast", "c1")] == 6
    assert [(e["workload"], e["reason"]) for e in r.progress.of("budget_stop")] == [("long", "slow")]   # rag: noted before
    # a model whose stage the session's END stopped is not slow: it carries on in the next part
    assert r.c1_left("rag", 3) == [] and "fast" not in r.slow_models
    r2, _ = _latency_runner({"chat": 5})
    r2.c1_done = {("long", "slow:default", p) for p in prompt_ids("long", 3)}       # nothing left there: no note
    r2.slow_models = {"slow": "chat"}
    r2.skip_slow("long", 3)
    assert not r2.budget_stops


def test_latency_stage_order_runs_the_sweep_before_long_in_a_short_window():
    from datetime import datetime

    from harness.quota import TSI
    from harness.suites.latency import stage_order
    w = ["chat", "rag", "long"]
    assert stage_order(w, ["rag"]) == ["chat", "rag", "long", "sweep"]
    assert stage_order(w, ["rag"], early=True) == ["chat", "rag", "sweep", "long"]
    assert stage_order(w, ["long"], early=True) == ["chat", "rag", "long", "sweep"]     # its own speed first
    assert stage_order(w, ["chat", "rag"], early=True) == ["chat", "rag", "sweep", "long"]
    assert stage_order(w, [], early=True) == ["chat", "rag", "long", "sweep"]
    r, _ = _latency_runner({})
    r.cfg.update({"early_sweep_below_minutes": 150, "sweep_workloads": ["rag"]})

    def at(h, m=0):
        return datetime(2026, 10, 16, h, m, tzinfo=TSI)
    new = _session_state(has_records=False)
    assert r.order_for(new, w, at(21), at(22, 50)) == ["chat", "rag", "sweep", "long"]   # the 110-min evening
    assert r.order_for(new, w, at(15), at(17, 50)) == ["chat", "rag", "long", "sweep"]   # the 170-min afternoon
    assert r.order_for(new, w, at(10), at(14, 40)) == ["chat", "rag", "long", "sweep"]
    assert r.order_for(new, w, at(16, 30), at(17, 50)) == ["chat", "rag", "sweep", "long"]   # started late by hand
    assert r.order_for(new, w, at(21), None) == ["chat", "rag", "long", "sweep"]         # no window
    early = [{"event": "part_start", "part": 1, "stage_order": ["chat", "rag", "sweep", "long"]}]
    assert r.order_for(_session_state(early), w, at(15), at(17, 50)) == ["chat", "rag", "sweep", "long"]   # kept
    assert r.order_for(_session_state([{"event": "part_start", "part": 1}]), w, at(21), at(22, 50)) == \
        ["chat", "rag", "long", "sweep"]                                  # a session begun before 16 Oct: as it began
    odd = [{"event": "part_start", "part": 1, "stage_order": ["chat", "sweep"]}]
    assert r.order_for(_session_state(odd), w, at(21), at(22, 50)) == ["chat", "rag", "long", "sweep"]
    r.cfg.pop("early_sweep_below_minutes")
    assert r.order_for(new, w, at(21), at(22, 50)) == ["chat", "rag", "long", "sweep"]   # not configured: sweep last


def test_latency_measure_follows_the_stage_order_and_skips_the_slow_model():
    import asyncio

    from harness.suites.latency import LoopLag
    r, _ = _latency_runner({})
    calls = []

    async def nothing(*a, **kw):
        return {}

    async def ss(w, n, maxt, schedule):
        calls.append(w)

    async def sw(workloads, n, maxt):
        calls.append("sweep")
    r.guard.ensure_online = nothing
    r.rtt_baseline, r._quota, r.warmup, r.single_stream, r.sweeps = nothing, nothing, nothing, ss, sw
    r.endpoints, r.stage_order = ["x"], ["chat", "rag", "sweep", "long"]
    r.slow_models = {"slow": "chat"}
    reason = asyncio.run(r._measure(r.ctx, {"schedule": "parallel"}, ["chat", "rag", "long"], 3,
                                    {"chat": 8, "rag": 8, "long": 8}, LoopLag(), {}))
    assert calls == ["chat", "rag", "sweep", "long"] and reason == "complete"
    assert [e["workload"] for e in r.progress.of("budget_stop")] == ["chat", "rag", "long"]   # the slow model's skips
    assert all(e["reason"] == "slow" and e["model"] == "slow" for e in r.progress.of("budget_stop"))


def test_latency_summary_lists_the_slow_model_and_the_stage_order():
    from harness.suites.latency import build_summary, describe_summary
    ev = [{"event": "part_start", "part": 1, "started_tsi": "2026-10-16T21:00:01+03:00",
           "window_end_tsi": "2026-10-16T22:50+03:00", "variants": ["slow:default", "fast:default"],
           "stage_order": ["chat", "rag", "sweep", "long"]},
          {"event": "budget_stop", "part": 1, "stage": "c1", "workload": "chat", "model": "slow", "sent": 12,
           "planned": 20, "reason": "time_budget"},
          {"event": "budget_stop", "part": 1, "stage": "c1", "workload": "rag", "model": "slow", "sent": 0,
           "planned": 20, "reason": "slow", "budget_used_in": "chat"},
          {"event": "cell_done", "kind": "sweep", "key": ["rag", 2, "slow:default"], "part": 1,
           "result": {"variant": "slow:default", "workload": "rag", "concurrency": 2, "slow_after": "chat",
                      "skipped": "its single-stream stage 'chat' used up its time budget in this session"}},
          {"event": "part_end", "part": 1, "reason": "complete"}]
    s = build_summary(_session_state(ev), {}, "complete")
    assert s["slow_models"] == {"slow": "chat"} and s["stage_order"] == ["chat", "rag", "sweep", "long"]
    assert s["completed"] and s["coverage"]["sweep_cells"] == {"measured": 0, "skipped": 1}
    assert "time budget used up: slow in 'chat'" in describe_summary(s)
    old = build_summary(_session_state(ev[1:2]), {}, "deadline")                 # begun before 16 Oct
    assert old["stage_order"] == ["chat", "rag", "long", "sweep"]


def test_analysis_notes_the_slow_model_and_the_early_sweep(tmp_path):
    import json as _json

    import analysis.analyze as A
    s = tmp_path / "20261016-2100-evening-slot"
    (s / "latency").mkdir(parents=True)
    summ = {"completed": True, "end_reason": "complete", "partial": False, "parts": [{"part": 1}],
            "stage_order": ["chat", "rag", "sweep", "long"], "slow_models": {"glm-5.3": "chat"},
            "budget_stops": [{"stage": "c1", "workload": "chat", "model": "glm-5.3", "sent": 12, "planned": 20,
                              "reason": "time_budget"},
                             {"stage": "c1", "workload": "rag", "model": "glm-5.3", "sent": 0, "planned": 60,
                              "reason": "slow", "budget_used_in": "chat"},
                             {"stage": "c1", "workload": "long", "model": "glm-5.3", "sent": 0, "planned": 20,
                              "reason": "slow", "budget_used_in": "chat"}],
            "sweeps": [{"variant": "glm-5.3:default", "concurrency": c, "slow_after": "chat", "skipped": "x"}
                       for c in (2, 4)]}
    (s / "manifest.json").write_text(_json.dumps({"suites": {"latency": {"summary": summ}}}), encoding="utf-8")
    A.ARGS = None
    A._MAN.clear()
    note = A.latency_session_stops([s])[s.name]
    assert "glm-5.3 c1 'chat' after 12/20" in note
    assert "glm-5.3 left out of rag, long and the sweep (its time budget was used up earlier" in note
    assert "the sweep ran before long" in note


def test_latency_configuration_hash_is_unchanged_by_the_stage_order_setting():
    """The sessions since 14 Oct share the configuration hash 7f0673bf494cab5d (the fingerprint that groups comparable
    sessions); the 16 Oct rules change the order and what a slow model is sent, not what is measured."""
    from harness.config import load_registry
    from harness.suites.latency import latency_config_hash
    reg = load_registry()
    cfg = reg.suite("latency")
    w, n = cfg.get("workloads"), cfg.get("prompts_per_workload")
    h, _ = latency_config_hash(reg, cfg, "standard", w, n)
    assert cfg.get("early_sweep_below_minutes") == 300
    assert latency_config_hash(reg, {k: v for k, v in cfg.items() if k != "early_sweep_below_minutes"}, "standard", w, n)[0] == h
    assert latency_config_hash(reg, dict(cfg, early_sweep_below_minutes=0), "standard", w, n)[0] == h
    assert h == "7f0673bf494cab5d", "the latency configuration changed: new sessions are not comparable with earlier ones"


def test_every_slot_runs_the_sweep_right_after_rag():
    """16 Oct (author's decision): every slot runs chat → rag → sweep → long — on 15 Oct afternoon DeepSeek-V4.1-Flash
    used up its 'long' budget (15 of 20 prompts in 65 min); with the sweep last, the slow-model rule would have cost it
    its sweep cells."""
    from datetime import datetime

    from harness.config import load_registry
    from harness.quota import TSI
    from harness.suites.latency import session_deadline
    cfg = load_registry().suite("latency")
    r, _ = _latency_runner({})
    r.cfg.update(cfg)
    new = _session_state(has_records=False)
    for hh in (10, 15, 21):
        now = datetime(2026, 10, 16, hh, 0, 30, tzinfo=TSI)          # the scheduled task starts a few seconds late
        window = session_deadline(cfg, now)
        assert (window - now).total_seconds() / 60 <= cfg["early_sweep_below_minutes"]
        assert r.order_for(new, cfg["workloads"], now, window) == ["chat", "rag", "sweep", "long"]


def _hcw():
    import importlib.util
    spec = importlib.util.spec_from_file_location("human_check_workbook", ROOT / "scripts" / "human_check_workbook.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _human_sheets(results, sessions):
    """Blind sheets as `run.py human-export` writes them (shuffled) plus their KEY files."""
    import csv

    from harness.judge import HUMAN_COLUMNS
    out = {}
    for session, n_yes, n_no in sessions:
        d = results / "track-a" / "evren" / session / "rag_qa" / "human_check"
        d.mkdir(parents=True)
        rows = [[f"H{i + 1:04d}", f"Soru {session} {i}: süre kaç gün?", "yes" if i < n_yes else "no",
                 f"[K1] Belge {i}, satır 1\n\n[K2] \"tırnaklı\"; noktalı virgül, virgül", f"Referans {i}",
                 "30 gün | yazılı bildirim", f"Model cevabı {session} {i}\nikinci satır", "", "", ""]
                for i in range(n_yes + n_no)]
        rows = rows[::2] + rows[1::2]
        with open(d / "sheet.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(HUMAN_COLUMNS)
            w.writerows(rows)
        with open(d / "KEY_do_not_share_with_annotators.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sample_id", "variant", "uid", "judge_verdict", "judge_signature", "content_sha"])
            for r in rows:
                w.writerow([r[0], "m:default", f"q{r[0]}", "correct" if r[2] == "yes" else "refused_correctly", "sig1", "x"])
        out[session] = d / "sheet.csv"
    return out


def test_human_check_workbook_sample_and_layout(tmp_path):
    """17 Oct: one annotator checks ~20 % of every blind sheet in ONE workbook — answerable and unanswerable
    questions sampled separately, nothing that names the system, the session or the judge; per-row verdict lists."""
    openpyxl = pytest.importorskip("openpyxl")
    H = _hcw()
    results = tmp_path / "results"
    sheets = _human_sheets(results, [("evren-x", 18, 2), ("ovh-x", 9, 1)])
    wbp = results / "human_check" / "human_check_sample.xlsx"
    out, mapf, n = H.build(results, wbp, 0.20)
    assert n == (4 + 1) + (2 + 1) and mapf.name == "human_check_sample_map_do_not_open.csv"
    assert not list(wbp.parent.glob("*.partial*"))
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Check", "How to fill"]
    ws = wb["Check"]
    head = [c.value for c in ws[1]]
    assert head == H.COLUMNS and not {"sample_id", "judge_verdict", "variant", "session"} & set(head)
    assert ws.max_row == n + 1 and ws.freeze_panes == "E2"
    shown = [dict(zip(head, [c.value for c in row])) for row in ws.iter_rows(min_row=2)]
    assert [s["row"] for s in shown] == list(range(1, n + 1))
    assert sum(s["answerable"] == "no" for s in shown) == 2
    assert all("evren-x" in s["question"] or "ovh-x" in s["question"] for s in shown)
    assert shown[0]["key_facts"] == "- 30 gün\n- yazılı bildirim" and "\n" in shown[0]["model_answer"]
    assert all(s["human_verdict"] is None for s in shown)
    # the first rows of each answerable group of each sheet (sheet order is the export's random order)
    import csv
    want = set()
    for session, p in sheets.items():
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        k_yes = 4 if session == "evren-x" else 2
        want |= {r["question"] for r in [r for r in rows if r["answerable"] == "yes"][:k_yes]}
        want |= {r["question"] for r in [r for r in rows if r["answerable"] == "no"][:1]}
    assert {s["question"] for s in shown} == want
    # per-row dropdowns: four verdicts for answerable rows, two for the others, every verdict cell covered once
    col = openpyxl.utils.get_column_letter(H.COLUMNS.index("human_verdict") + 1)
    lists = {}
    for dv in ws.data_validations.dataValidation:
        for rng in dv.sqref.ranges:
            assert rng.min_col == rng.max_col == 2
            for r in range(rng.min_row, rng.max_row + 1):
                assert r not in lists
                lists[r] = dv.formula1.strip('"').split(",")
    assert sorted(lists) == list(range(2, n + 2))
    for s in shown:
        assert lists[s["row"] + 1] == list(H.VERDICTS_ANSWERABLE if s["answerable"] == "yes" else H.VERDICTS_UNANSWERABLE)
    assert all(len(dv.prompt or "") <= 255 and len(dv.promptTitle or "") <= 32 for dv in ws.data_validations.dataValidation)
    assert all(len(dv.formula1) <= 255 for dv in ws.data_validations.dataValidation)
    assert col == "B"
    # never overwritten; a smaller fraction still takes one of each group
    with pytest.raises(SystemExit):
        H.build(results, wbp, 0.20)
    _, _, n_small = H.build(results, results / "human_check" / "small.xlsx", 0.01)
    assert n_small == 4


def test_human_check_workbook_merge_round_trip(tmp_path):
    """merge copies valid verdicts into the session sheets (nothing else changes), refuses verdicts that do not fit the
    question, skips rows whose sheet was exported again, and analysis/analyze.py counts exactly what was copied."""
    openpyxl = pytest.importorskip("openpyxl")
    import csv

    import analysis.analyze as A
    H = _hcw()
    results = tmp_path / "results"
    sheets = _human_sheets(results, [("evren-x", 18, 2), ("ovh-x", 9, 1)])
    before = {s: p.read_bytes() for s, p in sheets.items()}
    wbp = results / "human_check" / "human_check_sample.xlsx"
    _, mapf, n = H.build(results, wbp, 0.20)
    r0 = H.merge(results, wbp)                                    # nothing filled yet: nothing changes
    assert (r0["filled"], r0["copied"], r0["problems"]) == (0, 0, []) and r0["lines"] == n
    assert {s: p.read_bytes() for s, p in sheets.items()} == before
    wb = openpyxl.load_workbook(wbp)
    ws = wb["Check"]
    head = [c.value for c in ws[1]]
    iv, ia, inote = (head.index(c) + 1 for c in ("human_verdict", "answerable", "human_notes"))
    yes_rows = [r for r in range(2, n + 2) if ws.cell(r, ia).value == "yes"]
    no_rows = [r for r in range(2, n + 2) if ws.cell(r, ia).value == "no"]
    ws.cell(yes_rows[0], iv).value = "correct"
    ws.cell(yes_rows[1], iv).value = "Partially correct"            # typed by hand: normalised
    ws.cell(yes_rows[1], inote).value = "unsure"
    ws.cell(yes_rows[2], iv).value = "hallucinated"                 # does not fit an answerable question
    ws.cell(no_rows[0], iv).value = "refused_correctly"
    ws.cell(no_rows[1], iv).value = "nonsense"
    wb.save(wbp)
    r1 = H.merge(results, wbp)
    assert r1["filled"] == 5 and r1["copied"] == 3 and len(r1["problems"]) == 2
    assert any("does not fit" in p for p in r1["problems"]) and any("not one of the six" in p for p in r1["problems"])
    with open(mapf, encoding="utf-8-sig", newline="") as f:
        where = {int(m["row"]): m for m in csv.DictReader(f)}
    got = {}
    for s, p in sheets.items():
        with open(p, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row["human_verdict"]:
                    got[(s, row["sample_id"])] = (row["human_verdict"], row["human_notes"])
    wrow = lambda r: where[ws.cell(r, 1).value]
    want = {(wrow(r)["sheet"].split("/")[2], wrow(r)["sample_id"]): v for r, v in
            ((yes_rows[0], ("correct", "")), (yes_rows[1], ("partially_correct", "unsure")),
             (no_rows[0], ("refused_correctly", "")))}
    assert got == want
    for s, p in sheets.items():                                    # every other field byte-for-byte the same
        with open(p, encoding="utf-8-sig", newline="") as f:
            new = list(csv.DictReader(f))
        old = list(csv.DictReader(before[s].decode("utf-8-sig").splitlines(keepends=True)))
        strip = lambda rows: [{k: v for k, v in r.items() if k not in ("human_verdict", "human_notes")} for r in rows]
        assert strip(new) == strip(old)
        assert p.read_bytes().startswith(b"\xef\xbb\xbf") and b"\r\n" in p.read_bytes()
    agree = A.human_agreement([results / "track-a" / "evren" / s for s in sheets], "sig1")
    assert int(agree.iloc[0]["n_annotated"]) == 3
    # emptied in the workbook -> removed from the sheet; merge is idempotent
    ws.cell(yes_rows[0], iv).value = None
    wb.save(wbp)
    r2 = H.merge(results, wbp)
    assert r2["copied"] == 2 and r2["cleared"] == 1
    assert H.merge(results, wbp)["cleared"] == 0
    # a session exported again after the build: its rows are not touched
    p = sheets["ovh-x"]
    with open(p, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        fields, rows = rd.fieldnames, list(rd)
    for row in rows:
        row["model_answer"] += " (yeni)"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    ovh = [r for r in range(2, n + 2) if wrow(r)["sheet"].split("/")[2] == "ovh-x"]
    ws.cell(ovh[0], iv).value = "refused_correctly" if ws.cell(ovh[0], ia).value == "no" else "correct"
    wb.save(wbp)
    snapshot = p.read_bytes()
    r3 = H.merge(results, wbp)
    assert any("exported again" in x for x in r3["problems"]) and p.read_bytes() == snapshot
    # a sheet saved again by a Turkish Excel (semicolons) is read, and written back with commas
    p2 = sheets["evren-x"]
    with open(p2, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        fields, rows = rd.fieldnames, list(rd)
    with open(p2, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    evx = [r for r in yes_rows if wrow(r)["sheet"].split("/")[2] == "evren-x"]
    ws.cell(evx[0], iv).value = "incorrect"
    wb.save(wbp)
    H.merge(results, wbp)
    assert p2.read_text(encoding="utf-8-sig").split("\n", 1)[0].count(",") == 9
    with open(p2, encoding="utf-8-sig", newline="") as f:
        n_filled = sum(1 for row in csv.DictReader(f) if row["human_verdict"])
    assert n_filled >= 1
    assert int(A.human_agreement([results / "track-a" / "evren" / "evren-x"], "sig1").iloc[0]["n_annotated"]) == n_filled


def test_human_check_workbook_keeps_odd_text_as_text(tmp_path):
    """An answer that starts with '=' stays text (not an Excel formula); control characters Excel refuses are dropped;
    a cell over Excel's limit is cut with a note — the fingerprint still uses the sheet's own text."""
    openpyxl = pytest.importorskip("openpyxl")
    import csv

    from harness.judge import HUMAN_COLUMNS
    H = _hcw()
    results = tmp_path / "results"
    d = results / "track-h" / "ovh" / "ovh-x" / "rag_qa" / "human_check"
    d.mkdir(parents=True)
    with open(d / "sheet.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HUMAN_COLUMNS)
        w.writerow(["H0001", "Soru?", "yes", "x" * 40_000, "Ref", "a | b", "=SUM(A1:A3) 30 gün\x0b", "", "", ""])
        w.writerow(["H0002", "Soru 2?", "no", "K1", "Ref", "c", "Belgelerde yok.", "", "", ""])
    out, _, n = H.build(results, results / "human_check" / "w.xlsx", 0.2)
    ws = openpyxl.load_workbook(out)["Check"]
    head = [c.value for c in ws[1]]
    cells = {ws.cell(r, 1).value: {h: ws.cell(r, j + 1) for j, h in enumerate(head)} for r in range(2, n + 2)}
    ans = next(c for c in cells.values() if c["answerable"].value == "yes")
    assert ans["model_answer"].data_type == "s" and ans["model_answer"].value == "=SUM(A1:A3) 30 gün"
    assert len(ans["context"].value) < 32_767 and ans["context"].value.endswith("[cut: Excel's cell limit]")
    assert H.merge(results, out)["problems"] == []


def test_kappa_ci():
    import math
    k, lo, hi = S.kappa_ci(["a", "b", "a", "b"] * 5, ["a", "b", "a", "b"] * 5)
    assert (k, lo, hi) == (1.0, 1.0, 1.0)
    a = ["c"] * 40 + ["p"] * 8 + ["i"] * 12
    b = ["c"] * 36 + ["p"] * 4 + ["c"] * 4 + ["p"] * 4 + ["i"] * 10 + ["c"] * 2
    k, lo, hi = S.kappa_ci(a, b)
    assert k == pytest.approx(S.cohen_kappa(a, b)) and lo < k < hi and hi - lo < 0.6
    assert all(math.isnan(x) for x in S.kappa_ci([], []))


def test_human_check_calibration_lines_reported_apart(tmp_path):
    """19 Oct: lines discussed with Claude while they were filled (<workbook>_calibration_rows.txt) are marked
    'calibration' by merge; the report shows the agreement with and without them, and human × judge counts."""
    openpyxl = pytest.importorskip("openpyxl")
    import csv

    import analysis.analyze as A
    H = _hcw()
    results = tmp_path / "results"
    sheets = _human_sheets(results, [("evren-x", 18, 2), ("ovh-x", 9, 1)])
    wbp = results / "human_check" / "human_check_sample.xlsx"
    _, _, n = H.build(results, wbp, 0.20)
    wb = openpyxl.load_workbook(wbp)
    ws = wb["Check"]
    head = [c.value for c in ws[1]]
    iv, ia, inote = (head.index(c) + 1 for c in ("human_verdict", "answerable", "human_notes"))
    for r in range(2, n + 2):
        yes = ws.cell(r, ia).value == "yes"
        ws.cell(r, iv).value = ("incorrect" if r == 3 else "correct") if yes else "refused_correctly"
    ws.cell(2, inote).value = "form missing"
    ws.cell(4, 1).value = 3.0                                            # Excel may keep a row number as 3.0
    wb.save(wbp)
    H.calibration_path(wbp).write_text("# discussed with Claude\n1, 2\n3\n", encoding="utf-8")
    r = H.merge(results, wbp)
    assert (r["copied"], r["calibration"], r["problems"]) == (n, 3, [])
    notes = []
    for p in sheets.values():
        with open(p, encoding="utf-8-sig", newline="") as f:
            notes += [row["human_notes"] for row in csv.DictReader(f) if row["human_verdict"]]
    assert sorted(x for x in notes if x) == ["calibration", "calibration", "form missing [calibration]"]
    snap = {p: p.read_bytes() for p in sheets.values()}
    assert H.merge(results, wbp)["calibration"] == 3 and {p: p.read_bytes() for p in sheets.values()} == snap
    sess = [results / "track-a" / "evren" / s for s in sheets]
    agree = A.human_agreement(sess, "sig1")
    assert list(agree["lines"]) == ["all", "without calibration lines"]
    assert list(agree["n_annotated"]) == [n, n - 3]
    assert (agree["kappa_ci_low"] <= agree["cohen_kappa"]).all() and (agree["cohen_kappa"] <= agree["kappa_ci_high"]).all()
    conf = A.human_judge_confusion(sess, "sig1").set_index("human")
    assert int(conf["total"].sum()) == n
    n_no = sum(ws.cell(r, ia).value == "no" for r in range(2, n + 2))
    assert int(conf.loc["incorrect", "judge: correct"]) == 1 and int(conf.loc["correct", "judge: correct"]) == n - n_no - 1
    assert int(conf.loc["refused_correctly", "judge: refused_correctly"]) == n_no == 2
    assert list(conf.columns)[:2] == ["judge: correct", "judge: incorrect"]
    H.calibration_path(wbp).write_text("1 999", encoding="utf-8")
    assert any("999" in p for p in H.merge(results, wbp)["problems"])


def test_judge_calibration_needs_enough_human_labels(tmp_path):
    """A judge that saw only 2 human-labelled answers gets no kappa against the humans (the report showed 0.000 for
    Sonnet's 2 overlapping answers on 19 Oct); the main judge, with all of them, does."""
    import csv
    import json

    import analysis.analyze as A
    results = tmp_path / "results"
    sheets = _human_sheets(results, [("evren-x", 30, 5)])
    sdir = results / "track-a" / "evren" / "evren-x"
    with open(sheets["evren-x"], encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        fields, rows = rd.fieldnames, list(rd)
    for i, r in enumerate(rows):
        r["human_verdict"] = ("incorrect" if i % 4 == 0 else "correct") if r["answerable"] == "yes" else "refused_correctly"
    with open(sheets["evren-x"], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    (sdir / "rag_qa" / "judge").mkdir(parents=True)
    with open(sdir / "rag_qa" / "judge" / "j.jsonl", "w", encoding="utf-8") as f:
        for i, r in enumerate(rows):
            for sig, model in (("sigA", "opus"), ("sigB", "sonnet")):
                if sig == "sigB" and i >= 2:
                    continue
                v = "correct" if r["answerable"] == "yes" else "refused_correctly"
                f.write(json.dumps({"final": True, "signature": sig, "judge_model": model, "variant": "m:default",
                                    "uid": f"q{r['sample_id']}", "content_sha": "x", "judge": {"verdict": v}}) + "\n")
    t = A.judge_calibration([sdir]).set_index("judge")
    assert int(t.loc["opus", "human_labelled_overlap"]) == 35 and not pd_isna(t.loc["opus", "kappa_vs_human"])
    assert int(t.loc["sonnet", "human_labelled_overlap"]) == 2 and pd_isna(t.loc["sonnet", "kappa_vs_human"])


def pd_isna(x) -> bool:
    import pandas as pd
    return bool(pd.isna(x))


def test_service_change_leaves_out_results_recorded_before_it(tmp_path, monkeypatch):
    """16 Oct: a change on EVREN's side to /v1/ocr.  OCR readings, OCR timing and the extraction over OCR output
    recorded before it are left out; later ones and other models stay; a note says what was left out.  The real
    config names exactly this change."""
    import yaml

    import analysis.analyze as A
    doc = yaml.safe_load((ROOT / "configs" / "service_changes.yaml").read_text(encoding="utf-8"))
    changes = A.parse_service_changes(doc)
    assert [(c["id"], c["models"], c["since_dt"].isoformat()) for c in changes] == [
        ("evren-ocr-change", ["dots-ocr", "deepseek-ocr-2"], "2026-10-16T00:43:00+03:00")]
    monkeypatch.setattr(A, "_CHANGES", changes)
    monkeypatch.setattr(A, "_SUPERSEDED", set())
    before, after = "2026-10-15T21:00:00+00:00", "2026-10-16T07:00:00+00:00"        # 16 Oct 00:00 / 10:00 TSİ
    ocr = lambda m, t, i="p1": {"variant": m, "model_id": m, "session": "s", "key": f"{i}|0", "recorded_at": t}
    assert A.superseded_by("ocr", ocr("dots-ocr", before))["id"] == "evren-ocr-change"
    assert A.superseded_by("ocr", ocr("dots-ocr", after)) is None
    assert A.superseded_by("ocr", ocr("tesseract-tur", before)) is None
    assert A.superseded_by("ocr", {"variant": "dots-ocr", "model_id": "dots-ocr"}) is None          # no time: kept
    assert A.superseded_by("latency_ocr", ocr("dots-ocr", "2026-10-15T18:00:01Z")) is not None
    chat = lambda t: {"variant": "deepseek-v4-flash:default", "model_id": "deepseek-v4-flash", "recorded_at": t}
    assert A.superseded_by("docextract_ocr_dots-ocr", chat(before)) is not None
    assert A.superseded_by("docextract_ocr_deepseek-ocr-2", chat(before)) is not None
    assert A.superseded_by("docextract_ocr_dots-ocr", chat(after)) is None
    assert A.superseded_by("docextract", chat(before)) is None
    assert A.superseded_by("rag_qa", chat(before)) is None
    # through the OCR analysis: the old readings of dots-ocr are left out, its new ones and Tesseract's count
    sdir = tmp_path / "track-a" / "evren" / "evren-a"
    (sdir / "ocr").mkdir(parents=True)
    rows = {"dots-ocr": [ocr("dots-ocr", before, i) for i in ("p1", "p2")] + [ocr("dots-ocr", after, i) for i in ("p1", "p2")],
            "tesseract-tur": [ocr("tesseract-tur", before, i) for i in ("p1", "p2")]}
    for m, rs in rows.items():
        with open(sdir / "ocr" / f"{m}.jsonl", "w", encoding="utf-8") as f:
            for r in rs:
                cer = 0.9 if r["recorded_at"] == before and m == "dots-ocr" else 0.01
                f.write(json.dumps({**r, "item_id": r["key"].split("|")[0], "plan_hash": "P", "final": True,
                                    "result": {"outcome": "ok", "latency": 2.0}, "score": {"cer_flex": cer}}) + "\n")
    df, _ = A._pipeline_records([sdir], "ocr", {"cer_flex": 1.0})
    assert sorted(df.groupby("variant").size().items()) == [("dots-ocr", 2), ("tesseract-tur", 2)]
    assert df["cer_flex"].max() == 0.01
    notes = A.superseded_notes()
    assert len(notes) == 1 and "ocr: 2 (dots-ocr 2)" in notes[0] and "2026-10-16 00:43 TSİ" in notes[0]


def test_check_ocr_session_waits_for_every_page(tmp_path):
    """Phase 3e: the extraction over OCR output builds its plan from the OCR text, so it must not start while the OCR
    step is still running or after it ended with pages missing."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("cos", ROOT / "scripts" / "check_ocr_session.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    pages = ["ocr-01", "ocr-02", "ocr-03"]
    assert m.ocr_progress(tmp_path, "evren-ocr", ["dots-ocr"], pages) is None
    d = tmp_path / "track-a" / "evren" / "evren-ocr" / "ocr"
    d.mkdir(parents=True)
    rec = lambda i, outcome="ok", final=True: json.dumps({"item_id": i, "final": final, "result": {"outcome": outcome}})
    (d / "dots-ocr.jsonl").write_text("\n".join([rec("ocr-01"), rec("ocr-02"), rec("ocr-03"), rec("ocr-01")]) + "\n",
                                      encoding="utf-8")
    (d / "deepseek-ocr-2.jsonl").write_text("\n".join([rec("ocr-01"), rec("ocr-02", "http_error"),
                                                        rec("ocr-03", "ok", False)]) + "\n", encoding="utf-8")
    got = m.ocr_progress(tmp_path, "evren-ocr", ["dots-ocr", "deepseek-ocr-2", "missing-model"], pages)
    assert got == {"dots-ocr": 3, "deepseek-ocr-2": 1, "missing-model": 0}


def test_ocr_words_ignore_html_table_markup():
    """dots-ocr writes tables as HTML (13 of the 30 pages): its tags are not words — WER and bag-of-words F1 strip the
    markup like the CER does (until 19 Oct they did not: dots-ocr's median WER read 10 % instead of 1 %)."""
    plain = "Ürün Miktar Tutar\nKalem A 2 1.200,00 TL\nKalem B 1 350,50 TL"
    html = ("<table><tr><th>Ürün</th><th>Miktar</th><th>Tutar</th></tr>"
            "<tr><td>Kalem A</td><td>2</td><td>1.200,00 TL</td></tr><tr><td>Kalem B</td><td>1</td><td>350,50 TL</td></tr></table>")
    s_html, s_plain = score_ocr(plain, html), score_ocr(plain, plain)
    assert s_plain["wer"] == 0.0 and s_html["wer"] == 0.0 and s_html["bow_f1"] == 1.0
    assert s_html["cer_flex"] == pytest.approx(0.0, abs=0.02)
    md = "# Başlık\n\n**Kalem A** 2 1.200,00 TL"
    assert score_ocr("Başlık\nKalem A 2 1.200,00 TL", md)["wer"] == 0.0


def test_ocr_analysis_rescores_with_the_current_scorer(tmp_path, monkeypatch):
    """The OCR table is computed from the stored text and the ground truth (not from scores stored at run time),
    like every chat suite — a scorer fix reaches old records without sending anything again."""
    import analysis.analyze as A
    data = tmp_path / "data"
    (data / "ocr" / "pages").mkdir(parents=True)
    (data / "ocr" / "pages" / "p1.gt.txt").write_text("Kalem A 2 1.200,00 TL", encoding="utf-8")
    (data / "ocr" / "manifest.jsonl").write_text(json.dumps({"id": "p1", "gt": "pages/p1.gt.txt", "category": "invoice"}) + "\n",
                                                encoding="utf-8")
    monkeypatch.setattr(A, "DATA_DIR", data)
    monkeypatch.setattr(A, "_CHANGES", [])
    sdir = tmp_path / "track-a" / "evren" / "evren-ocr"
    (sdir / "ocr").mkdir(parents=True)
    rec = {"variant": "dots-ocr", "model_id": "dots-ocr", "item_id": "p1", "key": "p1|0", "plan_hash": "P", "final": True,
           "result": {"outcome": "ok", "text": "<table><tr><td>Kalem A</td><td>2</td><td>1.200,00 TL</td></tr></table>"},
           "score": {"cer_flex": 0.0, "wer": 0.9, "bow_f1": 0.4}}                   # as stored by the old scorer
    (sdir / "ocr" / "dots-ocr.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    df, _ = A._pipeline_records([sdir], "ocr", {"wer": 1.0}, rescore=A.ocr_rescorer())
    assert df["wer"].tolist() == [0.0] and df["bow_f1"].tolist() == [1.0]
    df_stored, _ = A._pipeline_records([sdir], "ocr", {"wer": 1.0})
    assert df_stored["wer"].tolist() == [0.9]


def test_glm_nothink_is_quality_only():
    """19 Oct: GLM-5.3's off-switch is EVREN's documented thinking:false (phase 3f).  glm-5.3:nothink may join the
    quality suites but never the latency sessions — it would change GLM-5.3's single stream and the set of systems every
    session has; nothing else leaves the latency sessions, and their configuration hash is unchanged."""
    import argparse

    from harness.config import load_registry
    from harness.run import select_variants
    from harness.suites.latency import latency_config_hash
    reg = load_registry()
    assert reg.model("glm-5.3").thinking["off"] == {"thinking": False}
    ns = lambda **k: argparse.Namespace(**{"endpoint": None, "models": None, "model": None, "modes": None,
                                           "exclude_models": None, "variants": None, **k})
    lat = {v.key for v in select_variants(reg, ns(endpoint="evren"), "latency")}
    assert "glm-5.3:nothink" not in lat and {"glm-5.3:default", "glm-5.3:think_min", "deepseek-v4-flash:nothink"} <= lat
    lat_all = {v.key for v in select_variants(reg, ns(endpoint="evren", modes="default,nothink,think_min"), "latency")}
    assert lat_all == lat
    rag = {v.key for v in select_variants(reg, ns(models="glm-5.3", modes="nothink"), "rag_qa")}
    assert rag == {"glm-5.3:nothink"}
    cfg = reg.suite("latency")
    w, n = cfg.get("workloads"), cfg.get("prompts_per_workload")
    assert latency_config_hash(reg, cfg, "standard", w, n)[0] == "7f0673bf494cab5d"


def test_ocr_timing_flags_spread_within_one_time_slot():
    """20 Oct: dots-ocr read a page in 1.7 s on the morning of 16 Oct and 5.4 s on the morning of 20 Oct (as in every
    session since the afternoon of 16 Oct) — the by-slot median must not pass for a time-of-day effect."""
    import pandas as pd

    import analysis.analyze as A
    per = pd.DataFrame([
        {"session": "20261016-1000-morning-slot", "slot": "morning", "variant": "dots-ocr", "concurrency": 1, "s_per_page_p50": 1.74},
        {"session": "20261020-1000-morning-slot", "slot": "morning", "variant": "dots-ocr", "concurrency": 1, "s_per_page_p50": 5.43},
        {"session": "20261016-1500-afternoon-slot", "slot": "afternoon", "variant": "dots-ocr", "concurrency": 1, "s_per_page_p50": 5.39},
        {"session": "20261016-1000-morning-slot", "slot": "morning", "variant": "dots-ocr", "concurrency": 4, "s_per_page_p50": 2.54},
        {"session": "20261020-1000-morning-slot", "slot": "morning", "variant": "dots-ocr", "concurrency": 4, "s_per_page_p50": 4.0},
    ])
    notes = A.slot_spread_notes(per)
    assert len(notes) == 1 and "1 in flight" in notes[0] and "1.7 s (20261016-1000-morning-slot)" in notes[0]
    assert "5.4 s (20261020-1000-morning-slot)" in notes[0]
    assert A.slot_spread_notes(per.iloc[0:0]) == []


# ------------------------------------------------------------------------ local classifier guard (phase 7, 30 Oct)
def test_guard_classifier_scoring_and_plan_name():
    from harness.scoring.basic import score_guard
    from harness.suites.quality import guard_plan_name
    assert guard_plan_name("classifier") == "guard_classifier"
    assert guard_plan_name("qwen3guard") == "guard" and guard_plan_name("policy_json") == "guard_policy"
    s = score_guard("unsafe", json.dumps({"unsafe": 0.91, "threshold": 0.5, "categories": ["PRIVACY_VIOLATION"]}),
                    fmt="classifier")
    assert s["pred_strict"] == s["pred_loose"] == "unsafe" and s["correct_strict"] and s["p_unsafe"] == 0.91
    assert s["categories"] == ["PRIVACY_VIOLATION"]
    at = score_guard("safe", json.dumps({"unsafe": 0.5, "threshold": 0.5}), fmt="classifier")
    assert at["pred_strict"] == "safe" and at["correct_strict"]                 # unsafe only ABOVE the threshold
    bad = score_guard("safe", "not json", fmt="classifier")
    assert bad["label"] == "unparsed" and bad["pred_strict"] == "unparsed" and bad["p_unsafe"] is None


def test_guard_label_rule_and_answer():
    from harness.local.backends import guard_answer, guard_label_rule
    overall, cats = guard_label_rule(["VIOLENT_CRIMES", "PRIVACY_VIOLATION", "SAFE", "unsafe"])
    assert overall == "unsafe" and cats == ["VIOLENT_CRIMES", "PRIVACY_VIOLATION"]
    a = guard_answer({"VIOLENT_CRIMES": 0.2, "PRIVACY_VIOLATION": 0.7, "SAFE": 0.3, "unsafe": 0.4}, overall, cats, 0.5)
    assert a["unsafe"] == 0.4 and a["categories"] == ["PRIVACY_VIOLATION"]      # the overall output decides
    overall2, cats2 = guard_label_rule(["VIOLENT_CRIMES", "PRIVACY_VIOLATION", "SAFE"])
    assert overall2 is None and cats2 == ["VIOLENT_CRIMES", "PRIVACY_VIOLATION"]
    b = guard_answer({"VIOLENT_CRIMES": 0.2, "PRIVACY_VIOLATION": 0.7, "SAFE": 0.3}, overall2, cats2, 0.5)
    assert b["unsafe"] == 0.7 and b["rule"] == "max harm category > 0.5"


def test_guard_classifier_auroc():
    import analysis.analyze as A
    assert A._auroc([0.9, 0.8], [0.1, 0.2]) == 1.0 and A._auroc([0.1], [0.9]) == 0.0
    assert A._auroc([0.5], [0.5]) == 0.5 and A._auroc([], [0.1]) != A._auroc([], [0.1])      # nan


def test_local_classifier_guard_runs_without_probe(tmp_path, monkeypatch):
    """The whole path of a local classifier guard through the runner, with a stand-in for the model: no probe,
    no HTTP client, one final scored record per prompt in guard_classifier/."""
    import asyncio

    import harness.store as ST
    from harness.config import Variant, load_registry
    from harness.runner import RunContext, RunOptions, run_chat_suite
    from harness.suites import quality as Q
    monkeypatch.setattr(ST, "RESULTS_DIR", tmp_path)
    reg = load_registry()
    spec = reg.model("modernbert-tr-guardrail")
    assert Q.guard_format(spec) == "classifier"

    class Stub:
        hf_id, revision = spec.api_model, "stub"

        def classify(self, text):
            p = 0.9 if "bomba" in text.lower() else 0.1
            return {"unsafe": p, "threshold": 0.5, "categories": ["VIOLENT_CRIMES"] if p > 0.5 else [], "scores": {}}

        def params(self):
            return {"local": "stub", "threshold": 0.5}

        def describe(self):
            return "stub"

    monkeypatch.setitem(Q._LOCAL_GUARDS, spec.id, Stub())
    monkeypatch.setattr(Q, "_LOCAL_GUARD_LOCK", [])
    plan = Q.plan_guard("t", 1, 128, 5, fmt="classifier")
    session = ST.Session("b", "local", "t-cosmos")
    logs = []

    async def go():
        ctx = RunContext(reg, session, RunOptions(repeats=1, concurrency=4), log=logs.append)
        try:
            return await run_chat_suite(ctx, "guard_classifier", plan, "h", [Variant(spec.id, "default")],
                                        score_fn=Q.score_guard_item, custom_item_fn=Q.run_local_guard_item)
        finally:
            await ctx.close()

    summary = asyncio.run(go())
    recs = [json.loads(x) for x in (session.suite_dir("guard_classifier") / f"{spec.id}__default.jsonl")
            .read_text(encoding="utf-8").splitlines() if x.strip()]
    assert summary[f"{spec.id}:default"]["ran"] == len(plan) == len(recs) == 5
    assert all(r["final"] and r["endpoint"] == "local" and r["effective_params"]["local"] == "stub" for r in recs)
    assert all(r["score"]["pred_strict"] in ("safe", "unsafe") and r["score"]["p_unsafe"] in (0.1, 0.9) for r in recs)
    assert not any("probe" in str(x) for x in logs)
