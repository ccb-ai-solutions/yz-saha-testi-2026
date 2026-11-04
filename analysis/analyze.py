"""Regenerate every table, chart and the report from raw results — no manual steps.

    python analysis/analyze.py                      # all sessions under results/
    python analysis/analyze.py --track a --endpoint evren --since 2026-10-10
    python analysis/analyze.py --min-coverage 0.98 --out report/2026-10-12
    python analysis/analyze.py --include-limited    # also smoke/--limit plans (report marked, no KB write)

Fairness gate (the rule that keeps unequal runs out of the same table):
  * quality suites — a "plan" is the hashed list of identical requests.  Plans built with --limit (smoke
    tests) are excluded unless --include-limited.  Among the remaining plans the one with the most
    comparable evidence (complete systems × requests) is chosen; a system is compared only if it
    completed >= --min-coverage of that plan; plans with the same items/data that the same systems
    completed are pooled as extra replicates; finally every system is restricted to the requests ALL
    compared systems completed.  The same plan run twice for a system: the latest complete session wins.
    Everything else is listed as "not comparable" with the reason, and dropped plans are listed too.
  * scores are recomputed from the stored answers with the current scorers (a scorer fix applies to
    old runs); the report says how many records changed.
  * judge — one judge signature (template + model + decoding) per table; a system needs judge coverage
    >= --min-coverage; judged metrics use only answers judged for every compared system.
  * latency — sessions are grouped by endpoint + latency config hash; sessions interrupted on purpose (Ctrl+C,
    quota) and sessions still running are excluded, a session that stopped part-way counts with what it has;
    requests hit by a network outage on this machine are left out, a cell measured again counts once (its latest
    attempt); cross-session summaries use only the sessions in which every compared system has that workload and
    level; timing statistics use usable responses only; TTF-answer is also reported censoring-aware.
  * OCR / ASR / retrieval — one plan, a common item set, final service failures scored as errors (1.0).
Outputs: report/<stamp>/report.md, report.html (self-contained), tables/*.csv, charts/*.png and
knowledge_base/capability_matrix.csv (appended, de-duplicated, with provenance; never from limited plans).
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import stats as S  # noqa: E402
from harness.client import FINAL_OUTCOMES, USABLE, ChatResult, is_final  # noqa: E402
from harness.config import CONFIG_DIR, DATA_DIR, RESULTS_DIR, load_registry  # noqa: E402
from harness.judge import content_sha  # noqa: E402
from harness.locks import STALE_S  # noqa: E402
from harness.store import plan_meta as read_plan_meta  # noqa: E402
from harness.suites.latency import label_legacy_outages, latest_attempts, record_outage  # noqa: E402
from harness.store import read_jsonl, safe_name, sha256_obj  # noqa: E402
from harness.scoring.basic import key_numbers  # noqa: E402
from harness.textnorm import word_count  # noqa: E402

TASK_PRIMARY = {   # suite dir -> (task type label, primary metric column, higher is better)
    "rag_qa": ("Grounded QA (RAG)", "correctness", True),
    "mcq_trmmlu": ("Knowledge (TR-MMLU, research-only licence)", "accuracy", True),
    "mcq_global_mmlu_tr": ("Knowledge (Global-MMLU-tr)", "accuracy", True),
    "mcq_seneca_trbench": ("Turkish language competence (seneca-trbench MCQ, cleaned)", "accuracy", True),
    "ifeval": ("Instruction following", "strict", True),
    "tools": ("Tool calling", "pass", True),
    "decision": ("Decision / routing", "birim_ok", True),
    "docextract": ("Document extraction (clean text)", "all_correct", True),
    "guard_all": ("Safety classification (guard)", "accuracy_strict", True),
}
QUANT_RE = re.compile(r"(nvfp4|mxfp4|fp8|fp16|bf16|int8|int4|awq|gptq|w4a16|w8a8|q4_k_m|q8_0)", re.I)
ARGS = None
_MAN: dict = {}
_PLAN_ITEMS: dict = {}
_PLAN_META: dict = {}
_CHANGES: list | None = None          # configs/service_changes.yaml, parsed once
_SUPERSEDED: set = set()               # records left out: (change id, suite dir, model, session, key, recorded_at)


# =============================================================================== provider-side service changes
def _ts(s) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def parse_service_changes(doc: dict) -> list[dict]:
    out = []
    for c in (doc or {}).get("changes") or []:
        since = _ts(c.get("since"))
        if since is None:
            raise SystemExit(f"configs/service_changes.yaml: change '{c.get('id')}' needs a valid 'since' (ISO time)")
        out.append({**c, "since_dt": since, "models": [str(m) for m in c.get("models") or []],
                    "suites": [str(s) for s in c.get("suites") or []]})
    return out


def service_changes() -> list[dict]:
    """Changes a provider made to its service during the campaign (configs/service_changes.yaml, METHODOLOGY §9)."""
    global _CHANGES
    if _CHANGES is None:
        import yaml
        p = CONFIG_DIR / "service_changes.yaml"
        _CHANGES = parse_service_changes(yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {})
    return _CHANGES


def _model_of(rec: dict) -> str:
    return str(rec.get("model_id") or str(rec.get("variant", "")).split(":", 1)[0])


def superseded_by(suite_dir: str, rec: dict) -> dict | None:
    """The service change that made this record obsolete: recorded before it, in a result folder it affects —
    a model's own records (ocr, latency_ocr) or every record of the extraction over its output (docextract_ocr_<model>)."""
    t = _ts(rec.get("recorded_at"))
    if t is None:
        return None
    for c in service_changes():
        if t >= c["since_dt"]:
            continue
        if suite_dir.startswith("docextract_ocr_"):
            if "docextract_ocr" in c["suites"] and suite_dir[len("docextract_ocr_"):] in {safe_name(m) for m in c["models"]}:
                return c
        elif suite_dir in c["suites"] and _model_of(rec) in c["models"]:
            return c
    return None


def drop_superseded(suite_dir: str, rec: dict) -> bool:
    c = superseded_by(suite_dir, rec)
    if c is not None:
        _SUPERSEDED.add((c["id"], suite_dir, _model_of(rec), rec.get("session"), rec.get("key"), rec.get("recorded_at")))
    return c is not None


def superseded_notes() -> list[str]:
    """One method note per service change: what changed and which results recorded before it were left out."""
    notes = []
    for c in service_changes():
        n = Counter((s, m) for cid, s, m, *_ in _SUPERSEDED if cid == c["id"])
        if not n:
            continue
        since = c["since_dt"].astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M TSİ")
        parts = []
        for s in sorted({s for s, _ in n}):
            per = {m: k for (s2, m), k in n.items() if s2 == s}
            who = ", ".join(f"{m} {k}" for m, k in sorted(per.items())) if len(per) <= 3 else f"{len(per)} models"
            parts.append(f"{s}: {sum(per.values())} ({who})")
        left = "; ".join(parts)
        notes.append(f"Service change on {c.get('endpoint')} ({since}): {c.get('what')} — results recorded before it "
                     f"are left out (kept on disk): {left}. Source: {c.get('source', '')}.")
    return notes


# =============================================================================== loading
def manifest(sdir: Path) -> dict:
    if sdir not in _MAN:
        _MAN[sdir] = json.loads((sdir / "manifest.json").read_text(encoding="utf-8"))
    return _MAN[sdir]


def find_sessions(results: Path, track=None, endpoint=None, since=None, pattern=None) -> list[Path]:
    out = []
    for m in sorted(results.glob("track-*/*/*/manifest.json")):
        sdir = m.parent
        man = manifest(sdir)
        if track and man.get("track") != track:
            continue
        if endpoint and man.get("endpoint") != endpoint:
            continue
        if since and man.get("created_utc", "") < since:
            continue
        if pattern and not re.search(pattern, sdir.name):
            continue
        out.append(sdir)
    return out


def plan_meta_of(sdir: Path, plan_name: str) -> dict:
    if sdir not in _PLAN_META:
        _PLAN_META[sdir] = read_plan_meta(sdir)
    return _PLAN_META[sdir].get(plan_name, {})


def plan_items(sdir: Path, plan_name: str) -> dict:
    k = (sdir, plan_name)
    if k not in _PLAN_ITEMS:
        _PLAN_ITEMS[k] = {it.get("uid", it.get("item_id")): it for it in read_jsonl(sdir / "plans" / f"{plan_name}.jsonl")}
    return _PLAN_ITEMS[k]


def _chat(res: dict) -> ChatResult:
    return ChatResult(**{k: v for k, v in res.items() if k in ChatResult.__dataclass_fields__})


def rescore_fn(suite_dir: str):
    """The current scorer for a suite, as f(plan_item, record) -> score (None = keep the stored score)."""
    from harness.suites import quality as Q
    if suite_dir == "rag_qa":
        return lambda it, r: Q.score_rag_item(it, _chat(r["result"]))
    if suite_dir.startswith("mcq_"):
        return lambda it, r: Q.score_mcq_item(it, _chat(r["result"]))
    if suite_dir == "ifeval":
        return lambda it, r: Q.score_ifeval_item(it, _chat(r["result"]))
    if suite_dir in ("guard", "guard_policy", "guard_classifier"):
        return lambda it, r: Q.score_guard_item(it, _chat(r["result"]))
    if suite_dir.startswith("docextract"):
        return lambda it, r: it.get("skip_score") if it.get("skip_request") else Q.score_docextract_item(it, _chat(r["result"]))
    if suite_dir == "tools":
        from harness.suites.tools import score_tool_record
        return lambda it, r: score_tool_record(it, r["result"], r.get("followup_result"))
    if suite_dir == "decision":
        def f(it, r):
            if str(r.get("plan_hash", "")).startswith("laya:"):
                from harness.scoring.basic import score_decision
                from harness.suites.pipeline import parse_laya
                raw = (r.get("result") or {}).get("raw")
                gold = _gold_ticket(r["item_id"])
                if gold is None:
                    return None
                if (r.get("result") or {}).get("outcome") != "ok" or not isinstance(raw, dict):
                    return {"pred": {"parsed": False}, "parsed": False, "birim_ok": False, "aciliyet_ok": False,
                            "iptal_ok": False}
                pred = parse_laya(raw)
                return {"pred": pred, **score_decision(gold, pred)}
            return Q.score_decision_item(it, _chat(r["result"]))
        return f
    return None


def load_quality(sessions: list[Path], suite_dir: str) -> pd.DataFrame:
    """All records of a suite (final and non-final), scores recomputed with the current scorer."""
    rows = []
    fn = rescore_fn(suite_dir) if not (ARGS and ARGS.no_rescore) else None
    for sdir in sessions:
        d = sdir / suite_dir
        if not d.exists():
            continue
        man = manifest(sdir)
        items = plan_items(sdir, suite_dir)
        for f in sorted(d.glob("*.jsonl")):
            for r in read_jsonl(f):
                if "variant" not in r or drop_superseded(suite_dir, r):
                    continue
                res = r.get("result") or {}
                outcome = res.get("outcome")
                final = bool(r.get("final", True)) and (outcome in FINAL_OUTCOMES or str(r.get("plan_hash", "")).startswith("laya:"))
                stored = r.get("score") or {}
                score, changed = stored, False
                if final and fn is not None:
                    it = items.get(r["key"])
                    if it is None and str(r.get("plan_hash", "")).startswith("laya:"):
                        it = {}
                    if it is not None:
                        try:
                            new = fn(it, r)
                        except Exception:              # a scorer bug must not silently drop rows
                            new = None
                        if new is not None:
                            changed = _score_sig(new) != _score_sig(stored)
                            score = new
                eff = r.get("effective_params") or {}
                rows.append({"session": sdir.name, "sdir": sdir, "slot": man.get("time_slot_tsi"),
                             "plan_hash": r.get("plan_hash"), "variant": r["variant"], "model_id": r.get("model_id"),
                             "mode": r.get("mode", "default"), "endpoint": r.get("endpoint"), "item_id": r["item_id"],
                             "repeat": r.get("repeat", 0), "key": r["key"], "final": final, "outcome": outcome,
                             "served_model": res.get("served_model"), "usage": res.get("usage"), "t_end": res.get("t_end"),
                             "latency": res.get("latency"), "eff": eff, "score": score or {}, "rescored_changed": changed,
                             "content_sha": content_sha(res.get("content") or ""), "rec": r})
    return pd.DataFrame(rows)


def _score_sig(s: dict) -> str:
    """Comparable part of a score: verdict-like fields only (timings such as t_tool_call are rounded on storage)."""
    keep = {k: v for k, v in (s or {}).items()
            if (isinstance(v, (bool, int, float, str)) or v is None) and not k.startswith("t_")}
    return sha256_obj(keep)


# =============================================================================== fairness gate
def comparable(df: pd.DataFrame, plan_name: str, min_cov: float) -> tuple[pd.DataFrame, dict]:
    """Rows of the compared systems on the chosen plan(s), restricted to requests all of them completed."""
    info = {"variants": [], "plans": [], "excluded": {}, "dropped_plans": {}, "coverage": {}, "limited": False,
            "superseded_sessions": {}, "n_requests_common": 0, "n_dropped_requests": 0}
    if df.empty:
        return df, info
    include_limited = bool(ARGS and ARGS.include_limited)
    force = [p for p in (ARGS.plan if ARGS and ARGS.plan else [])]
    plans: dict[str, dict] = {}
    for (ph, sess), g in df.groupby(["plan_hash", "session"]):
        sdir = g["sdir"].iloc[0]
        meta = plan_meta_of(sdir, plan_name)
        if meta.get("plan_hash") not in (None, ph):
            meta = {}
        p = plans.setdefault(ph, {"size": 0, "limited": False, "limit": None, "items": set(), "data": set(),
                                  "sessions": set()})
        p["sessions"].add(sess)
        p["size"] = max(p["size"], int(meta.get("n_requests") or 0))
        if meta.get("limit"):
            p["limited"], p["limit"] = True, meta.get("limit")
        p["data"].add(meta.get("data_sha256"))
        p["items"] |= {it.get("item_id") for it in plan_items(sdir, plan_name).values()}
    fin = df[df["final"]]
    cov: dict[str, dict[str, dict]] = defaultdict(dict)       # plan -> variant -> {session: coverage}
    for (ph, var, sess), g in fin.groupby(["plan_hash", "variant", "session"]):
        size = plans[ph]["size"] or fin[fin.plan_hash == ph]["key"].nunique()
        cov[ph].setdefault(var, {})[sess] = g["key"].nunique() / max(1, size)
    chosen: dict[tuple, str] = {}
    complete: dict[str, set] = {}
    for ph, vs in cov.items():
        complete[ph] = set()
        for var, by_sess in vs.items():
            ok = sorted(s for s, c in by_sess.items() if c >= min_cov)
            sess = ok[-1] if ok else max(by_sess, key=by_sess.get)
            chosen[(ph, var)] = sess
            if ok:
                complete[ph].add(var)
                if len(by_sess) > 1:
                    info["superseded_sessions"][f"{var} @ {ph[:10]}"] = sorted(s for s in by_sess if s != sess)
    cands = [ph for ph in plans if include_limited or not plans[ph]["limited"]]
    if force:
        cands = [ph for ph in plans if any(ph.startswith(f) for f in force)]
    if not cands:
        for ph, p in plans.items():
            info["dropped_plans"][ph[:12]] = f"limited plan (--limit {p['limit']}); add --include-limited to analyse"
        for var in sorted(df["variant"].unique()):
            info["excluded"][var] = "only limited (smoke) plans"
        return df.iloc[0:0], info
    def evidence(ph):
        return (len(complete.get(ph, ())) * plans[ph]["size"], plans[ph]["size"], max(plans[ph]["sessions"]))
    primary = max(cands, key=evidence)
    V = set(complete.get(primary, set()))
    used = [primary]
    for ph in cands:
        if ph == primary:
            continue
        same = plans[ph]["items"] == plans[primary]["items"] and plans[ph]["data"] == plans[primary]["data"]
        if same and V and V <= complete.get(ph, set()):
            used.append(ph)
            continue
        p = plans[ph]
        why = ("limited plan (--limit %s)" % p["limit"]) if p["limited"] else (
            "different items or data version" if not same else
            f"fewer comparable observations ({len(complete.get(ph, ()))} complete systems × {p['size']} requests)")
        info["dropped_plans"][ph[:12]] = why
    for ph in plans:
        if ph not in cands and ph[:12] not in info["dropped_plans"]:
            info["dropped_plans"][ph[:12]] = f"limited plan (--limit {plans[ph]['limit']})"
    for var in sorted(set(df["variant"]) - V):
        c = cov.get(primary, {}).get(var)
        if c:
            best = max(c.values())
            bad = df[(df.plan_hash == primary) & (df.variant == var) & ~df["final"]]["outcome"].value_counts().head(3)
            why = f"coverage {best:.0%} on plan {primary[:10]}"
            if len(bad):
                why += " (non-final: " + ", ".join(f"{o}×{n}" for o, n in bad.items()) + ")"
        else:
            other = [ph for ph in cov if var in cov[ph]]
            lim = [ph for ph in other if plans[ph]["limited"]]
            why = ("only ran limited (smoke) plans" if lim and len(lim) == len(other)
                   else f"ran on a different plan ({', '.join(ph[:10] for ph in other)})")
        info["excluded"][var] = why
    keep = []
    for ph in used:
        for var in V:
            sess = chosen.get((ph, var))
            if sess:
                keep.append(fin[(fin.plan_hash == ph) & (fin.variant == var) & (fin.session == sess)])
    sub = pd.concat(keep) if keep else fin.iloc[0:0]
    sub = sub.drop_duplicates(subset=["plan_hash", "variant", "key"], keep="last")
    if len(V) > 1 and not sub.empty:
        sub = sub.assign(_k=list(zip(sub.plan_hash, sub.key)))
        common = set.intersection(*(set(g["_k"]) for _, g in sub.groupby("variant")))
        before = len(sub)
        sub = sub[sub["_k"].isin(common)].drop(columns=["_k"])
        info["n_dropped_requests"] = before - len(sub)
        info["n_requests_common"] = len(common)
    elif not sub.empty:
        info["n_requests_common"] = sub["key"].nunique()
    info.update({"variants": sorted(V), "plans": used, "primary": primary, "limited": plans[primary]["limited"],
                 "coverage": {v: round(max(cov[primary][v].values()), 4) for v in V},
                 "rescored_changed": int(sub["rescored_changed"].sum()) if not sub.empty else 0})
    return sub, info


def by_item(df: pd.DataFrame, col: str) -> dict:
    out: dict = defaultdict(lambda: defaultdict(list))
    for v, i, x in zip(df["variant"], df["item_id"], df[col]):
        if x is None or (isinstance(x, float) and math.isnan(x)):
            continue
        out[v][i].append(float(x))
    return out


def summarize(df: pd.DataFrame, col: str, higher=True) -> pd.DataFrame:
    """Mean ± 95% cluster-bootstrap CI per variant, plus bootstrap rank interval (ties share the better rank)."""
    if df.empty or col not in df:
        return pd.DataFrame()
    bi = by_item(df, col)
    ranks = S.rank_intervals(bi, higher_is_better=higher) if len(bi) > 1 else {}
    rows = []
    for v, items in bi.items():
        m, lo, hi, n = S.bootstrap_mean_ci(items)
        rk = ranks.get(v, {})
        rows.append({"variant": v, col: m, "ci_lo": lo, "ci_hi": hi, "n_items": n,
                     "n_obs": sum(len(x) for x in items.values()), "p_best_or_tied": rk.get("p_best"),
                     "rank_95": f"{rk.get('rank_lo')}–{rk.get('rank_hi')}" if rk else None})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    # ties broken by name (stable sort): the top row is the reference of every pairwise-vs-best table, so it must
    # not depend on the machine or the pandas version
    return out.sort_values([col, "variant"], ascending=[not higher, True], kind="mergesort").reset_index(drop=True)


def pairwise(df: pd.DataFrame, col: str, ref: str | None = None) -> pd.DataFrame:
    bi = by_item(df, col)
    vs = sorted(bi)
    pairs, rows = {}, []
    for i, a in enumerate(vs):
        for b in vs[i + 1:]:
            if ref and ref not in (a, b):
                continue
            d = S.paired_diff(bi[a], bi[b])
            if d.get("n"):
                pairs[(a, b)] = d["p"]
                rows.append({"a": a, "b": b, "diff_a_minus_b": d["diff"], "ci_lo": d["lo"], "ci_hi": d["hi"], "p": d["p"],
                             "n_items": d["n"]})
    adj = S.holm(pairs)
    for r in rows:
        r["p_holm"] = adj[(r["a"], r["b"])]
        r["significant"] = r["p_holm"] < 0.05
    return pd.DataFrame(rows)


def flatten_scores(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    for k in keys:
        df[k] = df["score"].map(lambda s: _num(s.get(k)) if isinstance(s, dict) else None)
    return df


def _num(v):
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    return None


# =============================================================================== decoding / parameter audit
def params_audit(df: pd.DataFrame, suite: str) -> pd.DataFrame:
    """What was actually sent per system (after capability fallbacks) and where it deviates from the plan."""
    if df.empty:
        return pd.DataFrame()
    rows = []
    for v, g in df.groupby("variant"):
        sigs = Counter()
        flags = set()
        example = {}
        for eff, rec in zip(g["eff"], g["rec"]):
            e = {k: val for k, val in (eff or {}).items() if k not in ("seed", "stream_options", "param_aliases")}
            sigs[json.dumps(e, sort_keys=True, ensure_ascii=False)] += 1
            example = e
            plan_dec = None
            sd = rec.get("session")
            items = plan_items(g["sdir"].iloc[0], suite) if suite else {}
            it = items.get(rec["key"])
            if it:
                plan_dec = it.get("decoding")
                mt = it.get("max_tokens")
                sent_mt = e.get("max_tokens", e.get("max_completion_tokens"))
                if mt and sent_mt and sent_mt != mt:
                    flags.add(f"max_tokens {mt}→{sent_mt}")
            if plan_dec:
                for k, val in plan_dec.items():
                    if k not in e:
                        flags.add(f"{k} not sent (dropped by probe)")
                    elif e[k] != val:
                        flags.add(f"{k} {val}→{e[k]}")
            if "seed" not in (eff or {}):
                flags.add("seed not sent")
            _ = sd
        thinking = {k: val for k, val in example.items() if k not in ("max_tokens", "max_completion_tokens", "temperature",
                                                                       "top_p", "top_k", "mode", "tool_choice")}
        rows.append({"variant": v, "token_param": "max_completion_tokens" if "max_completion_tokens" in example else "max_tokens",
                     "max_tokens": example.get("max_tokens", example.get("max_completion_tokens")),
                     "temperature": example.get("temperature"), "top_p": example.get("top_p"),
                     "mode_params": json.dumps(thinking, ensure_ascii=False) if thinking else "",
                     "distinct_param_sets": len(sigs), "flags": "; ".join(sorted(flags)) or "ok"})
    return pd.DataFrame(rows)


# =============================================================================== suite analyses
def analyze_rag(sessions, min_cov) -> dict:
    df = load_quality(sessions, "rag_qa")
    if df.empty:
        return {}
    df, info = comparable(df, "rag_qa", min_cov)
    if df.empty:
        return {"info": info, "tables": {}}
    meta = {}
    for sdir in sessions:
        for it in plan_items(sdir, "rag_qa").values():
            meta[it["item_id"]] = it["meta"]
    df["answerable"] = df["item_id"].map(lambda i: meta.get(i, {}).get("answerable"))
    df["qtype"] = df["item_id"].map(lambda i: meta.get(i, {}).get("type"))
    df["tags"] = df["item_id"].map(lambda i: meta.get(i, {}).get("tags", []))
    df = flatten_scores(df, ["refused", "false_refusal", "correct_refusal", "partial_refusal", "citation_precision",
                             "citation_recall", "key_number_recall"])
    tables, res = {}, {"info": info}
    jd, jinfo = select_judged(sessions, df, "rag_qa", min_cov)
    res["judge"] = jinfo
    res["jd"] = jd
    if jd is not None and not jd.empty:
        tables["rag_correctness"] = summarize(jd, "correctness")
        tables["rag_faithfulness"] = summarize(jd[jd["answerable"] == True], "faithfulness")  # noqa: E712
        tables["rag_hallucination"] = summarize(jd, "hallucination", higher=False)
        best = tables["rag_correctness"]["variant"].iloc[0] if not tables["rag_correctness"].empty else None
        tables["rag_pairwise_vs_best"] = pairwise(jd, "correctness", ref=best)
        res["pairwise_full"] = pairwise(jd, "correctness")
        t = jd.groupby(["variant", "qtype"])["correctness"].mean().unstack()
        tables["rag_by_type"] = t.reset_index()
        kc = jd[jd["tags"].map(lambda t: "knowledge_conflict" in (t or []))]
        if not kc.empty:
            tables["rag_knowledge_conflict"] = summarize(kc, "correctness")
        res["primary"] = tables["rag_correctness"]
    tables["rag_refusal_on_unanswerable"] = summarize(df[df["answerable"] == False], "correct_refusal")  # noqa: E712
    tables["rag_false_refusal"] = summarize(df[df["answerable"] == True], "false_refusal", higher=False)  # noqa: E712
    tables["rag_citation_precision"] = summarize(df[df["answerable"] == True], "citation_precision")  # noqa: E712
    tables["rag_key_numbers"] = summarize(df[df["answerable"] == True], "key_number_recall")  # noqa: E712
    agree = human_agreement(sessions, jinfo.get("signature"))
    if agree is not None:
        tables["rag_judge_vs_human_agreement"] = agree
        tables["rag_judge_vs_human_verdicts"] = human_judge_confusion(sessions, jinfo.get("signature"))
    calib = judge_calibration(sessions)
    if calib is not None and not calib.empty:
        tables["rag_judge_calibration"] = calib
    tables["rag_request_parameters"] = params_audit(df, "rag_qa")
    me = mode_effect(df)
    if not me.empty:
        tables["rag_mode_effect"] = me
    stab = answer_stability(df)
    if not stab.empty:
        tables["rag_answer_stability"] = stab
    res["tables"] = tables
    res["df"] = df
    return res


def mode_effect(df: pd.DataFrame) -> pd.DataFrame:
    """Did each reasoning switch do what its name says, on these requests?  Median reasoning characters, completion
    tokens and Turkish share of the answer per model and mode.  Flags: nothink that still reasons; nothink / think_min
    whose answer stopped being Turkish (reasoning moved into the answer, as GLM-5.3's enable_thinking=false did on
    11 Oct); think_min without a ≥20 % cut in reasoning against default (Nemotron 3.5 Lightning on NVIDIA, 11 Oct);
    think_min that did not reason at all (GLM-5.3 at reasoning_effort=low on the 5 RAG smoke questions, 11 Oct: a
    low effort the model may spend as zero on easy questions — reported as measured, not a failure)."""
    if df.empty or "mode" not in df or df["mode"].nunique() < 2:
        return pd.DataFrame()
    ok = df[df["final"] & df["outcome"].isin(USABLE)]
    rows = []
    for mid, g in ok.groupby("model_id"):
        if g["mode"].nunique() < 2:
            continue
        stats = {}
        for mode, gm in g.groupby("mode"):
            res = [r.get("result") or {} for r in gm["rec"]]
            rc = [x.get("reasoning_chars") or 0 for x in res]
            ct = [(x.get("usage") or {}).get("completion_tokens") or 0 for x in res]
            tr = [s.get("turkish_ratio") for s in gm["score"] if isinstance(s, dict) and s.get("turkish_ratio") is not None]
            stats[mode] = (float(np.median(rc)), float(np.median(ct)), float(np.median(tr)) if tr else None, len(res))
        d_rc, _, d_tr, _ = stats.get("default", (None, None, None, 0))
        for mode, (rc, ct, tr, n) in sorted(stats.items()):
            flags = []
            if mode == "nothink" and rc > 0:
                flags.append("still reasons")
            if mode in ("nothink", "think_min") and d_tr is not None and tr is not None and d_tr >= 0.6 and tr < 0.6:
                flags.append("answer no longer Turkish (reasoning in the answer?)")
            if mode == "think_min" and d_rc and rc == 0:
                flags.append("did not reason on these questions (acts as nothink here)")
            elif mode == "think_min" and d_rc and rc > 0.8 * d_rc:
                flags.append("no ≥20% cut in reasoning vs default")
            if mode == "think" and rc == 0:
                flags.append("does not reason")
            rows.append({"model": mid, "mode": mode, "n": n, "median_reasoning_chars": rc,
                         "median_completion_tokens": ct, "median_turkish_share": tr,
                         "reasoning_vs_default": (rc / d_rc) if d_rc else None, "flags": "; ".join(flags) or "ok"})
    return pd.DataFrame(rows)


def answer_stability(df: pd.DataFrame) -> pd.DataFrame:
    """Free consistency check of repeats 1+ against repeat 0 of the same system and question (METHODOLOGY §3.8):
    the same refusal status, the same cited chunks (each repeat's labels mapped through its own context order) and the
    same key numbers present.  `stable_share` = questions whose every repeat matches repeat 0 on all three."""
    if df.empty or "repeat" not in df or df["repeat"].nunique() < 2:
        return pd.DataFrame()
    items: dict = {}
    for sdir in df["sdir"].unique():
        items.update(plan_items(sdir, "rag_qa"))
    ok = df[df["final"] & df["outcome"].isin(USABLE)]
    rows = []
    for v, g in ok.groupby("variant"):
        n = stable = 0
        flips = Counter()
        for _, gi in g.groupby("item_id"):
            if 0 not in set(gi["repeat"]) or gi["repeat"].nunique() < 2:
                continue
            sig = {}
            for _, r in gi.iterrows():
                m = (items.get(r["key"]) or {}).get("meta") or {}
                sc = r["score"] or {}
                lm = m.get("label_map") or {}
                chunks = frozenset(lm.get(c, c) for c in sc.get("cited") or [])
                nums = set(key_numbers(m.get("key_facts") or [])) - set(sc.get("key_numbers_missing") or [])
                sig[int(r["repeat"])] = (bool(sc.get("refused")), chunks, frozenset(nums))
            base = sig[0]
            n += 1
            same = True
            for k, sg in sig.items():
                if k == 0:
                    continue
                for name, x, y in (("refusal", sg[0], base[0]), ("citations", sg[1], base[1]), ("key_numbers", sg[2], base[2])):
                    if x != y:
                        flips[name] += 1
                        same = False
            stable += int(same)
        if n:
            rows.append({"variant": v, "questions": n, "stable_share": stable / n,
                         "refusal_flips": flips["refusal"], "citation_changes": flips["citations"],
                         "key_number_changes": flips["key_numbers"]})
    return pd.DataFrame(rows).sort_values("stable_share", ascending=False) if rows else pd.DataFrame()


def select_judged(sessions, df: pd.DataFrame, suite_dir: str, min_cov: float) -> tuple[pd.DataFrame | None, dict]:
    """One judge signature for the whole table; per-system coverage gate; common judged answers only."""
    by_sig: dict[str, dict] = defaultdict(dict)
    sig_info: dict[str, dict] = {}
    for sdir in sessions:
        for jf in sorted((sdir / suite_dir / "judge").glob("*.jsonl")):
            for j in read_jsonl(jf):
                if not (j.get("final") and j.get("scores")):
                    continue
                sig = j.get("signature") or f"legacy:{jf.stem}"
                uid = j.get("uid") or j["key"].split("|")[1]
                csha = j.get("content_sha")
                by_sig[sig][(sdir.name, j["variant"], uid, csha)] = j["scores"]
                sig_info.setdefault(sig, {"model": j.get("judge_model") or jf.stem, "file": jf.name})
    if not by_sig:
        return None, {"signature": None, "note": "not run yet — python harness/run.py judge ... --yes"}
    keys = list(zip(df["session"], df["variant"], df["key"], df["content_sha"]))

    def lookup(sig):
        d = by_sig[sig]
        if sig.startswith("legacy:"):                       # pre-signature files: no content hash available
            d2 = {(a, b, c): v for (a, b, c, _), v in d.items()}
            return [d2.get((a, b, c)) for a, b, c, _ in keys]
        return [d.get(k) for k in keys]
    wanted = ARGS.judge_signature if ARGS and ARGS.judge_signature else None
    sigs = [s for s in by_sig if not wanted or s.startswith(wanted)]
    if not sigs:
        return None, {"signature": None, "note": f"no judge results with signature {wanted}"}
    best = max(sigs, key=lambda s: sum(x is not None for x in lookup(s)))
    found = lookup(best)
    jd = df.copy()
    for c in ("correctness", "faithfulness", "hallucination", "key_fact_recall"):
        jd[c] = [_num((x or {}).get(c)) for x in found]
    jd["judged"] = [x is not None for x in found]
    # the judge may grade only some repeats (phase 5: repeat 0, RUN_PLAN decision 2): coverage and the comparison
    # use the repeats every judged system has verdicts for (13 Oct: over all three repeats every system showed 33 %)
    reps = [set(g.loc[g["judged"], "repeat"]) for _, g in jd.groupby("variant")]
    reps = [s for s in reps if s]
    judged_reps = set.intersection(*reps) if reps else set()
    if reps and not judged_reps:
        judged_reps = set().union(*reps)
    if judged_reps:
        jd = jd[jd["repeat"].isin(judged_reps)]
    covg = jd.groupby("variant")["judged"].mean()
    ok_vars = sorted(v for v, c in covg.items() if c >= min_cov)
    excluded = {v: f"judge coverage {c:.0%}" for v, c in covg.items() if c < min_cov}
    jd = jd[jd["variant"].isin(ok_vars) & jd["judged"]]
    if len(ok_vars) > 1 and not jd.empty:
        common = set.intersection(*(set(zip(g.plan_hash, g.key)) for _, g in jd.groupby("variant")))
        jd = jd[[k in common for k in zip(jd.plan_hash, jd.key)]]
    other = sorted(set(by_sig) - {best})
    return jd, {"signature": best, "model": sig_info[best]["model"], "excluded": excluded,
                "judged_repeats": sorted(int(r) for r in judged_reps),
                "coverage": {k: round(float(v), 4) for k, v in covg.items()}, "other_signatures_ignored": other}


RAG_VERDICTS = ["correct", "partially_correct", "incorrect", "refused_incorrectly", "refused_correctly", "hallucinated"]
MIN_HUMAN_OVERLAP = 20      # fewer human-labelled answers than this: no kappa for that judge (2 answers say nothing)


def _coarse(v: str) -> str:
    return "pass" if v in ("correct", "refused_correctly") else ("partial" if v == "partially_correct" else "fail")


def _human_labels(sessions, signature: str | None) -> pd.DataFrame | None:
    """The filled lines of the blind human check next to the judge's verdict (from the KEY file).  A line whose note
    says 'calibration' was discussed with Claude — the judge's model family — while it was being filled."""
    import csv
    rows = []
    for sdir in sessions:
        sheet = sdir / "rag_qa" / "human_check" / "sheet.csv"
        keyf = sdir / "rag_qa" / "human_check" / "KEY_do_not_share_with_annotators.csv"
        if not (sheet.exists() and keyf.exists()):
            continue
        with open(keyf, encoding="utf-8-sig") as f:
            key = {r["sample_id"]: r for r in csv.DictReader(f)}
        with open(sheet, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                hv = (r.get("human_verdict") or "").strip().lower()
                k = key.get(r["sample_id"])
                if hv and k and (not signature or not k.get("judge_signature") or k["judge_signature"] == signature):
                    rows.append({"session": sdir.name, "human": hv, "judge": k["judge_verdict"], "variant": k["variant"],
                                 "calibration": "calibration" in (r.get("human_notes") or "").lower()})
    return pd.DataFrame(rows) if rows else None


def human_agreement(sessions, signature: str | None) -> pd.DataFrame | None:
    """Agreement between the LLM judge and the blind human check (sheet filled by annotators): on all filled lines
    and, when some were discussed with Claude while filling (note 'calibration'), also without those."""
    d = _human_labels(sessions, signature)
    if d is None:
        return None

    def row(x: pd.DataFrame, lines: str) -> dict:
        k, lo, hi = S.kappa_ci(x.human.tolist(), x.judge.tolist())
        return {"lines": lines, "n_annotated": len(x), "exact_agreement": float((x.human == x.judge).mean()),
                "cohen_kappa": k, "kappa_ci_low": lo, "kappa_ci_high": hi,
                "kappa_pass_partial_fail": S.cohen_kappa([_coarse(v) for v in x.human], [_coarse(v) for v in x.judge]),
                "judge_more_lenient": float(((x.judge.map(_coarse) == "pass") & (x.human.map(_coarse) != "pass")).mean()),
                "judge_more_strict": float(((x.judge.map(_coarse) != "pass") & (x.human.map(_coarse) == "pass")).mean())}

    out = [row(d, "all")]
    if d["calibration"].any() and not d["calibration"].all():
        out.append(row(d[~d["calibration"]], "without calibration lines"))
    return pd.DataFrame(out)


def human_judge_confusion(sessions, signature: str | None) -> pd.DataFrame | None:
    """Human verdict (rows) × judge verdict (columns) on the filled lines of the blind check."""
    d = _human_labels(sessions, signature)
    if d is None:
        return None
    seen = set(d.human) | set(d.judge)
    labels = [v for v in RAG_VERDICTS if v in seen] + sorted(seen - set(RAG_VERDICTS))
    t = pd.crosstab(d.human, d.judge).reindex(index=labels, columns=labels, fill_value=0)
    t.columns = [f"judge: {c}" for c in t.columns]
    t["total"] = t.sum(axis=1)
    return t.rename_axis("human").reset_index()


def judge_calibration(sessions) -> pd.DataFrame | None:
    """Every judge signature vs the blind human labels (and vs each other) on the answers both saw —
    the evidence for choosing a cheaper judge (e.g. Sonnet vs Opus) on YOUR data.  The agreement with the human
    labels is left out for a judge that saw fewer than MIN_HUMAN_OVERLAP of them."""
    import csv
    human = {}
    for sdir in sessions:
        sheet = sdir / "rag_qa" / "human_check" / "sheet.csv"
        keyf = sdir / "rag_qa" / "human_check" / "KEY_do_not_share_with_annotators.csv"
        if not (sheet.exists() and keyf.exists()):
            continue
        with open(keyf, encoding="utf-8-sig") as f:
            key = {r["sample_id"]: r for r in csv.DictReader(f)}
        with open(sheet, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                hv = (r.get("human_verdict") or "").strip().lower()
                k = key.get(r["sample_id"])
                if hv and k:
                    human[(sdir.name, k["variant"], k["uid"], k.get("content_sha", ""))] = hv
    verdicts: dict[str, dict] = defaultdict(dict)
    model_of = {}
    for sdir in sessions:
        for jf in (sdir / "rag_qa" / "judge").glob("*.jsonl"):
            for j in read_jsonl(jf):
                if j.get("final") and j.get("judge") and j.get("signature"):
                    v = (j["judge"] or {}).get("verdict")
                    verdicts[j["signature"]][(sdir.name, j["variant"], j.get("uid"), j.get("content_sha"))] = v
                    model_of[j["signature"]] = j.get("judge_model")
    if len(verdicts) < 2 and not human:
        return None
    coarse = lambda v: "pass" if v in ("correct", "refused_correctly") else ("partial" if v == "partially_correct" else "fail")
    rows = []
    sigs = sorted(verdicts)
    for s in sigs:
        hk = [k for k in verdicts[s] if k in human]
        row = {"judge": model_of.get(s), "signature": s, "answers_judged": len(verdicts[s]), "human_labelled_overlap": len(hk)}
        if len(hk) >= MIN_HUMAN_OVERLAP:
            a, b = [human[k] for k in hk], [verdicts[s][k] for k in hk]
            row.update({"kappa_vs_human": S.cohen_kappa(a, b),
                        "kappa_vs_human_pass_partial_fail": S.cohen_kappa([coarse(x) for x in a], [coarse(x) for x in b]),
                        "exact_agreement_vs_human": float(np.mean([x == y for x, y in zip(a, b)]))})
        for t in sigs:
            if t != s:
                ck = [k for k in verdicts[s] if k in verdicts[t]]
                if ck:
                    row[f"agreement_with_{t[:8]}"] = float(np.mean([coarse(verdicts[s][k]) == coarse(verdicts[t][k]) for k in ck]))
        rows.append(row)
    return pd.DataFrame(rows)


def analyze_simple(sessions, min_cov, suite_dir: str, keys: list[str], primary: str, extra=None,
                   keep_items: set | None = None, keep_note: str = "") -> dict:
    df = load_quality(sessions, suite_dir)
    if df.empty:
        return {}
    llm = df[~df["plan_hash"].astype(str).str.startswith("laya:")]
    laya = df[df["plan_hash"].astype(str).str.startswith("laya:") & df["final"]]
    df2, info = comparable(llm, suite_dir, min_cov)
    if keep_items is not None and not df2.empty:          # after the gate: coverage is judged on the full plan
        left_out = sorted(set(df2["item_id"]) - set(keep_items))
        df2 = df2[df2["item_id"].isin(keep_items)]
        if left_out:
            info["note"] = (info.get("note", "") + " " + keep_note.format(n=len(left_out))).strip()
    if not laya.empty:
        laya = laya.sort_values("session").drop_duplicates(subset=["variant", "key"], keep="last")
        if not df2.empty:
            laya = laya[laya["item_id"].isin(set(df2["item_id"]))]
        df2 = pd.concat([df2, laya])           # laya uses /systemone — same tickets & gold, different interface
        info["variants"] = sorted(set(info["variants"]) | set(laya["variant"]))
        info["note"] = "laya answers the same tickets through /v1/systemone (typed decisions), LLMs through chat JSON"
    if df2.empty:
        return {"info": info, "tables": {}}
    df2 = flatten_scores(df2, keys)
    res = {"info": info, "tables": {}, "df": df2, "suite": suite_dir}
    if extra:
        extra(df2, res)
    if primary in df2.columns:
        res["primary"] = summarize(df2, primary)
        res["tables"][f"{suite_dir}_{primary}"] = res["primary"]
        if df2["variant"].nunique() > 1:
            best = res["primary"]["variant"].iloc[0]
            res["tables"][f"{suite_dir}_pairwise_vs_best"] = pairwise(df2, primary, ref=best)
            res["pairwise_full"] = pairwise(df2, primary)
    res["tables"][f"{suite_dir}_request_parameters"] = params_audit(df2[~df2["plan_hash"].astype(str).str.startswith("laya:")],
                                                                     suite_dir)
    return res


def _mcq_extra(df, res):
    n = res["suite"]
    df["accuracy"] = df["score"].map(lambda s: float(bool(s.get("correct", False))))
    res["tables"][f"{n}_accuracy_rotation0"] = summarize(df[df["repeat"] == 0], "accuracy")
    cons = df.groupby(["variant", "item_id"])["accuracy"].min().reset_index().rename(columns={"accuracy": "consistent"})
    cons["repeat"] = 0
    res["tables"][f"{n}_consistent_across_rotations"] = summarize(cons, "consistent")
    df["invalid"] = df["score"].map(lambda s: float(bool(s.get("invalid", False))))
    res["tables"][f"{n}_invalid_answer_rate"] = summarize(df, "invalid", higher=False)


def _ifeval_extra(df, res):
    cat = {t["id"]: t["category"] for t in read_jsonl(DATA_DIR / "ifeval_tr" / "tasks.jsonl")}
    df["check_rate"] = df["score"].map(lambda s: s.get("check_rate"))
    df["category"] = df["item_id"].map(cat)
    df["strict_f"] = df["score"].map(lambda s: float(bool(s.get("strict"))))
    res["tables"]["ifeval_check_rate"] = summarize(df, "check_rate")
    res["tables"]["ifeval_strict_by_category"] = df.groupby(["variant", "category"])["strict_f"].mean().unstack().reset_index()
    judged = []
    shas = dict(zip(zip(df["session"], df["variant"], df["key"]), df["content_sha"]))
    for sdir in {s for s in df["sdir"]}:
        for jf in (sdir / "ifeval" / "judge").glob("*.jsonl"):
            for j in read_jsonl(jf):
                if j.get("judge") is None or not j.get("final", True):
                    continue
                uid = j.get("uid") or j["key"].split("|")[1]
                if j.get("content_sha") and shas.get((sdir.name, j["variant"], uid)) != j["content_sha"]:
                    continue                           # stale verdict for an answer that was re-generated
                judged.append({"variant": j["variant"], "item_id": j["item_id"], "repeat": j["repeat"],
                               "tone_pass": float(bool(j["judge"].get("pass")))})
    if judged:
        res["tables"]["ifeval_tone_checks_llm_judged"] = summarize(pd.DataFrame(judged), "tone_pass")


def _tools_extra(df, res):
    df["category"] = df["score"].map(lambda s: s.get("category"))
    res["tables"]["tools_by_category"] = df.groupby(["variant", "category"])["pass"].mean().unstack().reset_index()
    res["tables"]["tools_textual_calls"] = df.groupby("variant")["score"].apply(
        lambda s: float(np.mean([bool(x.get("textual_call")) for x in s]))).reset_index(name="textual_call_rate")


def _decision_extra(df, res):
    labels = ["fatura", "teknik", "satis", "kargo", "iptal", "diger"]
    rows = []
    for v, g in df.groupby("variant"):
        gold_b, pred_b, gold_a, pred_a, gy, gp = [], [], [], [], [], []
        for _, r in g.iterrows():
            pred = (r["score"] or {}).get("pred") or {}
            gl = _gold_ticket(r["item_id"])
            if gl is None:
                continue
            gold_b.append(gl["birim"])
            pred_b.append(pred.get("birim"))
            gold_a.append(gl["aciliyet"])
            pred_a.append(pred.get("aciliyet") if pred.get("aciliyet") is not None else -1)
            if pred.get("iptal_olasiligi") is not None:
                gy.append(gl["iptal_riski"])
                gp.append(pred["iptal_olasiligi"])
        ok_a = [(a, b) for a, b in zip(gold_a, pred_a) if b in (0, 1, 2)]
        is_laya = str(g["plan_hash"].iloc[0]).startswith("laya:")
        lat = [x for x in (g["latency"] if is_laya else g["t_end"]).tolist()
               if x is not None and not (isinstance(x, float) and math.isnan(x))]
        rows.append({"variant": v, "interface": "systemone (typed)" if is_laya else "chat (JSON)", "n": len(gold_b),
                     "birim_acc": float(np.mean([a == b for a, b in zip(gold_b, pred_b)])) if gold_b else None,
                     "birim_macro_f1": S.macro_f1(gold_b, pred_b, labels) if gold_b else None,
                     "aciliyet_acc": float(np.mean([a == b for a, b in zip(gold_a, pred_a)])) if gold_a else None,
                     "aciliyet_qwk": S.quadratic_kappa([a for a, _ in ok_a], [b for _, b in ok_a], 3) if ok_a else None,
                     "iptal_auroc": S.auroc(gy, gp) if gy else None, "iptal_brier": S.brier(gy, gp) if gy else None,
                     "latency_p50_s (not comparable across interfaces)": float(np.median(lat)) if lat else None})
    res["tables"]["decision_detail"] = pd.DataFrame(rows)


_TICKETS = None


def _gold_ticket(tid):
    global _TICKETS
    if _TICKETS is None:
        _TICKETS = {t["id"]: t["labels"] for t in read_jsonl(DATA_DIR / "decision" / "tickets.jsonl")}
    return _TICKETS.get(tid)


def _docextract_extra(df, res):
    n = res["suite"]
    df["field_acc"] = df["score"].map(lambda s: s.get("field_acc"))
    res["tables"][f"{n}_field_acc"] = summarize(df, "field_acc")
    fields = defaultdict(lambda: defaultdict(list))
    for _, r in df.iterrows():
        for k, ok in (r["score"].get("fields") or {}).items():
            fields[r["variant"]][k].append(ok)
    res["tables"][f"{n}_by_field"] = pd.DataFrame({v: {k: np.mean(x) for k, x in d.items()} for v, d in fields.items()}).reset_index().rename(columns={"index": "field"})


def _guard_extra(df, res):
    df["accuracy_strict"] = [float(bool(s.get("correct_strict", False))) for s in df["score"]]


def _auroc(pos: list, neg: list) -> float:
    """P(an unsafe prompt scores above a safe one), ties count half: the threshold-free view of a score."""
    if not pos or not neg:
        return float("nan")
    return sum((a > b) + 0.5 * (a == b) for a in pos for b in neg) / (len(pos) * len(neg))


def analyze_guard(sessions, min_cov) -> dict:
    """Dedicated classifiers (raw prompt) and policy-prompted guards answer the SAME prompts; each format is
    gated on its own plan, then the per-prompt results are merged into one table."""
    parts, infos, audits = [], {}, {}
    for d in ("guard", "guard_policy", "guard_classifier"):
        r = analyze_simple(sessions, min_cov, d, [], "accuracy_strict", _guard_extra)
        if r.get("df") is not None and not r["df"].empty:
            parts.append(r["df"].assign(format=d))
            infos[d] = r["info"]
            audits[f"{d}_request_parameters"] = r["tables"].get(f"{d}_request_parameters")
    if not parts:
        return {"info": infos.get("guard") or {}, "tables": {}} if infos else {}
    df = pd.concat(parts)
    cat = {g["id"]: (g["category"], g.get("source", "own")) for g in read_jsonl(DATA_DIR / "guard" / "prompts.jsonl")}
    rows = []
    for v, g in df.groupby("variant"):
        sc = g["score"].tolist()
        for mapping in ("strict", "loose"):
            unsafe = [s for s in sc if s.get("expected") == "unsafe"]
            safe = [s for s in sc if s.get("expected") == "safe"]
            rec = np.mean([s[f"pred_{mapping}"] == "unsafe" for s in unsafe]) if unsafe else float("nan")
            fpr = np.mean([s[f"pred_{mapping}"] == "unsafe" for s in safe]) if safe else float("nan")
            rows.append({"variant": v, "format": g["format"].iloc[0], "mapping": mapping, "unsafe_recall": rec,
                         "safe_false_positive_rate": fpr, "balanced_acc": (rec + (1 - fpr)) / 2,
                         "unparsed": np.mean([s.get("label") == "unparsed" for s in sc]), "n": len(sc)})
    bycat = []
    for (v, c), g in df.assign(category=df["item_id"].map(lambda i: cat.get(i, ("?",))[0])).groupby(["variant", "category"]):
        bycat.append({"variant": v, "category": c, "flagged_unsafe": float(np.mean([s.get("pred_strict") == "unsafe" for s in g["score"]])),
                      "n": len(g)})
    info = {"variants": sorted(df["variant"].unique()), "per_format": infos,
            "excluded": {k: v for i in infos.values() for k, v in i.get("excluded", {}).items()},
            "limited": any(i.get("limited") for i in infos.values()),
            "note": "guard = dedicated classifiers (prompt as-is); guard_policy = models given prompts/tr/guard_policy.md; "
                    "guard_classifier = local classifiers scored in-process (unsafe when the unsafe probability is "
                    "above the threshold fixed before the run; AUROC needs no threshold). "
                    "Strict mapping counts Qwen3Guard 'Controversial' as unsafe."}
    roc = []
    for v, g in df.groupby("variant"):
        ps = [(s.get("p_unsafe"), s.get("expected")) for s in g["score"]]
        if ps and all(p is not None for p, _ in ps):
            pos, neg = [p for p, e in ps if e == "unsafe"], [p for p, e in ps if e == "safe"]
            roc.append({"variant": v, "auroc": _auroc(pos, neg), "n_unsafe": len(pos), "n_safe": len(neg)})
    primary = summarize(df, "accuracy_strict")
    t = {"guard_accuracy_strict": primary, "guard_summary": pd.DataFrame(rows),
         "guard_flag_rate_by_category": pd.DataFrame(bycat).pivot_table(index="variant", columns="category",
                                                                          values="flagged_unsafe").reset_index() if bycat else pd.DataFrame()}
    if df["variant"].nunique() > 1:
        t["guard_pairwise_vs_best"] = pairwise(df, "accuracy_strict", ref=primary["variant"].iloc[0])
    if roc:
        t["guard_classifier_auroc"] = pd.DataFrame(roc)
    t.update({k: v for k, v in audits.items() if v is not None})
    return {"info": info, "tables": t, "primary": primary, "df": df}


def scanned_invoices() -> set:
    """Invoices that have a scanned page in the OCR set (data/ocr/manifest.jsonl): the only ones an
    extraction-over-OCR run can be judged on (12 of the 30 invoices)."""
    p = DATA_DIR / "ocr" / "manifest.jsonl"
    return {m["invoice_id"] for m in read_jsonl(p) if m.get("invoice_id")} if p.exists() else set()


def docextract_text_vs_ocr(results: dict) -> pd.DataFrame:
    base = results.get("docextract", {}).get("df")
    rows = []
    for k, r in results.items():
        if not k.startswith("docextract_ocr_") or not r.get("df") is not None:
            continue
        o = r["df"]
        for v in sorted(set(o["variant"]) & (set(base["variant"]) if base is not None else set())):
            a: dict = {}
            b: dict = {}
            for i, s_ in zip(o[o.variant == v]["item_id"], o[o.variant == v]["score"]):
                a.setdefault(i, []).append(float(bool(s_.get("all_correct"))))
            for i, s_ in zip(base[base.variant == v]["item_id"], base[base.variant == v]["score"]):
                if i in a:                                  # the same invoices as the OCR side
                    b.setdefault(i, []).append(float(bool(s_.get("all_correct"))))
            d = S.paired_diff(a, b)
            rows.append({"variant": v, "ocr_source": k.replace("docextract_ocr_", ""), "invoices": len(a),
                         "all_correct_text": float(np.mean([np.mean(x) for x in b.values()])) if b else None,
                         "all_correct_ocr": float(np.mean([np.mean(x) for x in a.values()])) if a else None,
                         "delta_ocr_minus_text": d.get("diff"), "ci_lo": d.get("lo"), "ci_hi": d.get("hi"),
                         "ocr_pages_missing": int(sum(bool(s.get("ocr_missing")) for s in o[o.variant == v]["score"]))})
    return pd.DataFrame(rows)


# =============================================================================== pipeline gates
def _pipeline_records(sessions, suite_dir: str, fail_value: dict, rescore=None) -> tuple[pd.DataFrame, dict]:
    """Records of a pipeline suite on one plan; latest complete session per (plan, system); final failures
    become worst-case scores; restricted to items every compared system has."""
    rows = []
    for sdir in sessions:
        d = sdir / suite_dir
        for f in (d.glob("*.jsonl") if d.exists() else []):
            for r in read_jsonl(f):
                if "variant" not in r or not r.get("final", True) or drop_superseded(suite_dir, r):
                    continue
                s = dict(r.get("score") or {})
                res = r.get("result") or {}
                failed = bool(res) and res.get("outcome") != "ok"
                if failed:
                    s = {**fail_value}
                elif rescore is not None:                 # the current scorer, like the chat suites
                    new = rescore(r)
                    if new is not None:
                        s = new
                rows.append({"session": sdir.name, "sdir": sdir, "plan_hash": r.get("plan_hash"), "variant": r["variant"],
                             "key": r.get("key"), "item_id": r["item_id"], "repeat": r.get("repeat", 0), "failed": failed,
                             "category": r.get("category"), "kind": r.get("kind"),
                             "latency": res.get("latency"), "rtf": res.get("rtf"), "device": res.get("device"),
                             "in_flight": r.get("concurrency"), "peak_vram_gb": res.get("peak_vram_gb"), **s})
    info = {"excluded": {}, "dropped_plans": {}}
    if not rows:
        return pd.DataFrame(), info
    df = pd.DataFrame(rows)
    # a request sent again (run --retry-outcomes) appends a newer final record: the latest one counts
    df = df.drop_duplicates(subset=["session", "plan_hash", "variant", "key"], keep="last")
    plan_name = suite_dir
    lim = {}
    for (ph, sess), g in df.groupby(["plan_hash", "session"]):
        m = plan_meta_of(g["sdir"].iloc[0], plan_name)
        lim[ph] = lim.get(ph, False) or bool(m.get("limit"))
    cands = [ph for ph in df["plan_hash"].unique() if not lim.get(ph) or (ARGS and ARGS.include_limited)]
    if not cands:
        info["dropped_plans"] = {ph[:12]: "limited plan" for ph in df["plan_hash"].unique()}
        return pd.DataFrame(), info
    plan = max(cands, key=lambda ph: (df[df.plan_hash == ph]["variant"].nunique(), df[df.plan_hash == ph]["item_id"].nunique()))
    for ph in df["plan_hash"].unique():
        if ph != plan:
            info["dropped_plans"][ph[:12]] = "limited plan" if lim.get(ph) else "fewer systems / items"
    df = df[df.plan_hash == plan]
    keep, partial = [], {}
    n_items = df["item_id"].nunique()
    min_cov = ARGS.min_coverage if ARGS else 0.98
    # retrieval run with --rerank-limit N: the reranked systems ("first+reranker") cover the first N queries by design
    rl = 0
    if plan_name.startswith("retrieval_"):
        rl = max((int(plan_meta_of(s, plan_name).get("rerank_limit") or 0) for s in df["sdir"].unique()), default=0)
    for v, g in df.groupby("variant"):
        by_sess = {s: gg for s, gg in g.groupby("session")}
        cnt = {s: gg["item_id"].nunique() for s, gg in by_sess.items()}
        ok = sorted(s for s, c in cnt.items() if c >= min_cov * n_items)
        if ok:
            keep.append(by_sess[ok[-1]])
            continue
        if rl and "+" in v:
            part = sorted(s for s, c in cnt.items() if c >= min_cov * min(rl, n_items))
            if part:
                partial[v] = by_sess[part[-1]]
                continue
        info["excluded"][v] = f"covers {max(cnt.values())}/{n_items} items"
    if not keep:
        return pd.DataFrame(), info
    df = pd.concat(keep)
    common = set.intersection(*(set(zip(g.item_id, g.repeat)) for _, g in df.groupby("variant")))
    df = df[[k in common for k in zip(df.item_id, df.repeat)]]
    for v, g in partial.items():        # kept on their own queries; analyze_retrieval pairs them with the first stage
        df = pd.concat([df, g[[k in common for k in zip(g.item_id, g.repeat)]]])
        info.setdefault("partial", {})[v] = f"reranked on the first {min(rl, n_items)} of {n_items} queries (--rerank-limit)"
    info.update({"plan": plan, "n_items": len({i for i, _ in common}), "variants": sorted(df["variant"].unique()),
                 "limited": bool(lim.get(plan))})
    return df, info


def analyze_retrieval(sessions, dataset: str) -> dict:
    plan_name = f"retrieval_{dataset}"
    df, info = _pipeline_records(sessions, plan_name, {})
    if df.empty:
        return {}
    partial = info.get("partial") or {}
    for v in partial:                   # which source documents the subset comes from (XQuAD: articles, in order)
        g = df[df.variant == v]
        items = plan_items(g["sdir"].iloc[0], plan_name)

        def docs(ids, items=items):
            return {str(x).split("#")[0] for i in ids for x in (items.get(i) or {}).get("gold", [])}
        partial[v] += f", from {len(docs(g['item_id']))} of {len(docs(items))} source documents"
    out = []
    for v, g in df[~df["variant"].isin(list(partial))].groupby("variant"):   # the ranked table: full query set only
        row = {"system": v, "queries": g["item_id"].nunique()}
        for m in ("ndcg@10", "hit@1", "hit@5", "recall@10", "full@10", "mrr@10"):
            items = {i: [x] for i, x in zip(g["item_id"], g[m])}
            mean, lo, hi, _ = S.bootstrap_mean_ci(items)
            row[m] = mean
            if m == "ndcg@10":
                row["ndcg_ci"] = f"[{lo:.3f}, {hi:.3f}]"
        out.append(row)
    table = pd.DataFrame(out).sort_values("ndcg@10", ascending=False)
    deltas = []
    for v in df["variant"].unique():
        if "+" in v:
            base = v.split("+")[0]
            a = {i: [x] for i, x in zip(df[df.variant == v]["item_id"], df[df.variant == v]["ndcg@10"])}
            b = {i: [x] for i, x in zip(df[df.variant == base]["item_id"], df[df.variant == base]["ndcg@10"])}
            d = S.paired_diff(a, b)
            if d.get("n"):
                both = set(a) & set(b)
                deltas.append({"reranked": v, "first_stage": base, "queries": d["n"],
                               "first_stage_ndcg@10": float(np.mean([b[i][0] for i in both])),
                               "reranked_ndcg@10": float(np.mean([a[i][0] for i in both])),
                               "delta_ndcg@10": d["diff"], "ci_lo": d["lo"], "ci_hi": d["hi"], "p": d["p"]})
    return {"table": table, "deltas": pd.DataFrame(deltas), "plan": info["plan"], "n_queries": info["n_items"],
            "info": info}


_REG_CACHE: dict = {}


def _model_spec(mid: str):
    if "reg" not in _REG_CACHE:
        try:
            _REG_CACHE["reg"] = load_registry()
        except (OSError, KeyError, ValueError, SystemExit):      # analysis of copied results without configs
            _REG_CACHE["reg"] = None
    reg = _REG_CACHE["reg"]
    try:
        return reg.model(mid) if reg is not None else None
    except KeyError:                                             # a model id no longer in the registry
        return None


def ocr_where(variant: str, devices) -> tuple[str, int | None]:
    """Where an OCR model ran and how many pages were in flight: a hosted endpoint (network + the provider's
    servers) or this PC (its GPU / CPU) — the two latencies are not comparable with each other."""
    spec = _model_spec(variant)
    if spec is None:
        return "?", None
    conc = int((spec.options or {}).get("concurrency", 2))
    if spec.task.startswith("local_"):
        dev = ", ".join(sorted({str(d) for d in devices if d and str(d) != "nan"}))
        return (f"this PC ({dev})" if dev else "this PC"), conc
    return f"{spec.endpoint} (hosted)", conc


def ocr_rescorer():
    """OCR scores recomputed from the stored text and the ground truth with the current scorer (METHODOLOGY §9);
    None with --no-rescore or without the OCR data."""
    if ARGS and ARGS.no_rescore:
        return None
    from harness.scoring.basic import score_ocr
    p = DATA_DIR / "ocr" / "manifest.jsonl"
    if not p.exists():
        return None
    gt = {m["id"]: (DATA_DIR / "ocr" / m["gt"]).read_text(encoding="utf-8")
          for m in read_jsonl(p) if m.get("gt") and (DATA_DIR / "ocr" / m["gt"]).exists()}

    def f(r: dict) -> dict | None:
        res = r.get("result") or {}
        if res.get("outcome") != "ok" or r.get("item_id") not in gt:
            return None
        return score_ocr(gt[r["item_id"]], res.get("text") or "")
    return f


def analyze_ocr(sessions) -> dict:
    fail = {"cer_flex": 1.0, "cer": 1.0, "wer": 1.0, "bow_f1": 0.0, "diacritics": {"recall": 0.0}}
    df, info = _pipeline_records(sessions, "ocr", fail, rescore=ocr_rescorer())
    if df.empty:
        return {}
    df["diacritic_recall"] = df["diacritics"].map(lambda d: (d or {}).get("recall") if isinstance(d, dict) else None)
    df["confusions"] = df["diacritics"].map(lambda d: (d or {}).get("top_confusions") if isinstance(d, dict) else None)
    rows, speed = [], []
    for v, g in df.groupby("variant"):
        items = {i: [x] for i, x in zip(g["item_id"], g["cer_flex"])}
        m, lo, hi, n = S.bootstrap_mean_ci(items)
        conf = Counter()
        for c in g["confusions"]:
            for ch, k in (c or []):
                conf[ch] += k
        lat = pd.to_numeric(g["latency"], errors="coerce").dropna()
        where, conc = ocr_where(v, g["device"].tolist())
        inflight = pd.to_numeric(g["in_flight"], errors="coerce").dropna()
        rows.append({"variant": v, "pages": n, "failed_pages_scored_1": int(g["failed"].sum()),
                     "cer_flex_median": float(g["cer_flex"].median()), "cer_flex_mean": m, "cer_flex_ci": f"[{lo:.3f}, {hi:.3f}]",
                     "cer_strict_median": float(g["cer"].median()), "wer_median": float(g["wer"].median()),
                     "bow_f1_mean": float(g["bow_f1"].mean()), "diacritic_recall_mean": float(g["diacritic_recall"].mean()),
                     "latency_p50_s": float(lat.median()) if len(lat) else None,
                     "latency_p90_s": float(lat.quantile(0.9)) if len(lat) else None,
                     "ran_on": where,
                     "top_diacritic_confusions": ", ".join(f"{c}×{k}" for c, k in conf.most_common(6))})
        vram = pd.to_numeric(g["peak_vram_gb"], errors="coerce").dropna()
        speed.append({"variant": v, "ran_on": where,
                      "pages_in_flight": int(inflight.max()) if len(inflight) else conc, "pages": len(lat),
                      "s_per_page_p50": float(lat.median()) if len(lat) else None,
                      "s_per_page_p90": float(lat.quantile(0.9)) if len(lat) else None,
                      "s_per_page_max": float(lat.max()) if len(lat) else None,
                      "pages_per_min_one_stream": 60.0 / float(lat.mean()) if len(lat) and lat.mean() > 0 else None,
                      "peak_vram_gb": float(vram.max()) if len(vram) else None})
    summ = pd.DataFrame(rows).sort_values("cer_flex_median")
    bycat = df.pivot_table(index="variant", columns="category", values="cer_flex", aggfunc="median").reset_index()
    speed_df = pd.DataFrame(speed).sort_values("s_per_page_p50") if speed else pd.DataFrame()
    if not speed_df.empty:
        speed_df = speed_df.dropna(axis=1, how="all")          # e.g. peak GPU memory when nothing ran locally
    return {"summary": summ, "by_category": bycat, "speed": speed_df, "df": df, "info": info}


def analyze_latency_ocr(sessions) -> dict:
    """The OCR timing of the latency sessions (latency_ocr/): seconds per page one at a time and pages per minute
    with several in flight, per time slot; a reading with CER > 0.5 is counted as garbage."""
    rows = []
    for sdir in sessions:
        d = sdir / "latency_ocr"
        if not d.exists() or latency_heartbeat(sdir)[2]:             # still running: its cells may be unfinished
            continue
        man = manifest(sdir)
        for f in d.glob("*.jsonl"):
            recs = read_jsonl(f)
            label_legacy_outages(recs)
            for r in latest_attempts(recs):                          # outage-hit requests out, latest cell attempt
                if drop_superseded("latency_ocr", r):
                    continue
                res = r.get("result") or {}
                rows.append({"session": sdir.name, "slot": man.get("time_slot_tsi"), "variant": r["variant"],
                             "endpoint": r.get("endpoint"), "concurrency": r.get("concurrency"), "cell": r.get("cell"),
                             "ok": res.get("outcome") == "ok", "outcome": res.get("outcome"),
                             "latency": res.get("latency"), "wall_start": res.get("wall_start"),
                             "cer_flex": (r.get("score") or {}).get("cer_flex")})
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    per = []
    for (s, v, c), g in df.groupby(["session", "variant", "concurrency"]):
        ok = g[g["ok"]]
        lat = pd.to_numeric(ok["latency"], errors="coerce").dropna()
        t0 = pd.to_datetime(ok["wall_start"], utc=True, format="mixed")
        end = t0 + pd.to_timedelta(pd.to_numeric(ok["latency"], errors="coerce").fillna(0), unit="s")
        secs = (end.max() - t0.min()).total_seconds() if len(ok) else 0
        cer = pd.to_numeric(ok["cer_flex"], errors="coerce").dropna()
        per.append({"session": s, "slot": g["slot"].iloc[0], "variant": v, "concurrency": int(c), "n": len(g),
                    "ok_rate": float(g["ok"].mean()), "s_per_page_p50": float(lat.median()) if len(lat) else None,
                    "s_per_page_p90": float(lat.quantile(0.9)) if len(lat) else None,
                    "pages_per_min": (60.0 / float(lat.mean()) if int(c) == 1 and len(lat) and lat.mean() > 0
                                      else (len(ok) * 60.0 / secs if secs > 0 else None)),
                    "garbage_share": float((cer > 0.5).mean()) if len(cer) else None})
    per_df = pd.DataFrame(per)
    summ = []
    for (v, c), g in per_df.groupby(["variant", "concurrency"]):
        summ.append({"variant": v, "pages_in_flight": c, "sessions": g["session"].nunique(), "requests": int(g["n"].sum()),
                     "ok_rate": float(g["ok_rate"].mean()), "s_per_page_p50": g["s_per_page_p50"].median(),
                     "s_per_page_p50_min": g["s_per_page_p50"].min(), "s_per_page_p50_max": g["s_per_page_p50"].max(),
                     "s_per_page_p90": g["s_per_page_p90"].median(), "pages_per_min": g["pages_per_min"].median(),
                     "garbage_share": g["garbage_share"].mean()})
    by_slot = per_df.pivot_table(index=["variant", "concurrency"], columns="slot", values="s_per_page_p50",
                                 aggfunc="median").reset_index() if per_df["slot"].notna().any() else pd.DataFrame()
    return {"summary": pd.DataFrame(summ), "by_slot": by_slot, "per_session": per_df,
            "notes": slot_spread_notes(per_df)}


def slot_spread_notes(per_df: pd.DataFrame, ratio: float = 2.0) -> list[str]:
    """Where sessions of the SAME time slot differ by more than `ratio` in seconds per page, the by-slot medians
    do not show a time-of-day effect (20 Oct: dots-ocr 1.7 s on the morning of 16 Oct, 5.4 s on the morning of
    20 Oct, as in every session since the afternoon of 16 Oct)."""
    notes = []
    d = per_df.dropna(subset=["s_per_page_p50", "slot"]) if not per_df.empty else per_df
    for (v, c, slot), g in (d.groupby(["variant", "concurrency", "slot"]) if not d.empty else []):
        lo, hi = float(g["s_per_page_p50"].min()), float(g["s_per_page_p50"].max())
        if len(g) >= 2 and lo > 0 and hi / lo > ratio:
            fast = g.loc[g["s_per_page_p50"].idxmin(), "session"]
            slow = g.loc[g["s_per_page_p50"].idxmax(), "session"]
            notes.append(f"{v}, {int(c)} in flight: within the {slot} slot alone the sessions read a page in "
                         f"{lo:.1f} s ({fast}) to {hi:.1f} s ({slow}) — a difference between times of day in the "
                         "table above is not reliably the time of day (see the per-session table).")
    return notes


def analyze_asr(sessions) -> dict:
    fail = {"wer": 1.0, "wer_numnorm": 1.0, "cer": 1.0}
    df, info = _pipeline_records(sessions, "asr", fail)
    if df.empty:
        return {}
    out = []
    for (v, kind), g in df.groupby(["variant", df["kind"].fillna("clip")]):
        items = {i: [x] for i, x in zip(g["item_id"], g["wer"])}
        m, lo, hi, n = S.bootstrap_mean_ci(items)
        out.append({"variant": v, "clips": kind, "n": n, "failed_scored_1": int(g["failed"].sum()), "wer_mean": m,
                    "wer_ci": f"[{lo:.3f}, {hi:.3f}]", "wer_numnorm_mean": g["wer_numnorm"].mean(),
                    "cer_mean": g["cer"].mean(), "rtf_p50": g["rtf"].median(), "latency_p50_s": g["latency"].median()})
    return {"summary": pd.DataFrame(out).sort_values(["clips", "wer_mean"]), "df": df, "info": info}


# =============================================================================== latency
def latency_heartbeat(sdir: Path) -> tuple[datetime | None, bool | None, bool]:
    """(last heartbeat of the session's latency lock in TSİ, on AC power?, still running = a heartbeat < 75 s old
    that was not the release written when a part ended normally)."""
    hb = sdir / "latency_heartbeat.json"
    if not hb.exists():
        return None, None, False
    try:
        h = json.loads(hb.read_text(encoding="utf-8"))
        epoch = float(h.get("heartbeat_epoch", 0))
    except (ValueError, OSError):
        return None, None, False
    return (datetime.fromtimestamp(epoch, timezone(timedelta(hours=3))), (h.get("power") or {}).get("ac_power"),
            not h.get("released") and datetime.now(timezone.utc).timestamp() - epoch < STALE_S)


def latency_records(sessions) -> tuple[pd.DataFrame, dict, dict]:
    """(rows of the usable latency records, sessions left out with the reason, notes on sessions that are in).

    A session that stopped without finishing counts with the records it has (each request is its own measurement):
    killed before its summary was written, closed after its window as partial, or resumed in several parts.  Left out:
    a session still running (fresh heartbeat) and one interrupted on purpose (Ctrl+C / quota; --include-interrupted
    keeps it).  Records hit by a network outage on this machine are dropped (label_legacy_outages finds them in
    sessions from before the network check: failed name lookups and what broke with them), and of a sweep / OCR cell
    measured more than once only its latest attempt is used."""
    rows, excluded, notes = [], {}, {}
    for sdir in sessions:
        d = sdir / "latency"
        if not d.exists():
            continue
        man = manifest(sdir)
        summ = ((man.get("suites") or {}).get("latency") or {}).get("summary") or {}
        when, ac, running = latency_heartbeat(sdir)
        if running:
            excluded[sdir.name] = f"still running (heartbeat {when:%d %b %H:%M} TSİ)"
            continue
        if not summ:
            notes[sdir.name] = ("stopped without finishing, not closed yet"
                                + (f" — last heartbeat {when:%d %b %H:%M} TSİ" if when else "")
                                + ("" if ac is None else (" on AC power" if ac else " on battery"))
                                + " (its records count; running the same session again closes it)")
        elif summ.get("interrupted"):
            excluded[sdir.name] = "interrupted (quota, Ctrl+C or fatal error)"
            if not (ARGS and ARGS.include_interrupted):
                continue
        recs = []
        for f in d.glob("*.jsonl"):
            recs += read_jsonl(f)
        label_legacy_outages(recs)
        hit = sum(1 for r in recs if record_outage(r))
        if hit:
            said = f"{hit} request(s) hit by a network outage on this machine left out"
            notes[sdir.name] = f"{notes[sdir.name]}; {said}" if notes.get(sdir.name) else said
        for r in latest_attempts(recs):
            res = r["result"]
            u = res.get("usage") or {}
            ct = u.get("completion_tokens")
            det = u.get("completion_tokens_details") or {}
            rt = det.get("reasoning_tokens") if isinstance(det, dict) else None
            if rt == 0 and ((res.get("reasoning_chars") or 0) > 0 or (res.get("reasoning") or "").strip()):
                rt = None              # a reported 0 next to reasoning text is wrong (OVHcloud gpt-oss-120b)
            first_any = min([x for x in (res.get("t_first_reasoning"), res.get("t_first_content"), res.get("t_first_tool")) if x is not None], default=None)
            ttfa = res.get("t_first_content") if res.get("t_first_content") is not None else res.get("t_first_tool")
            last = res.get("t_last_token")
            dec = ((ct - 1) / (last - first_any)) if (ct and first_any is not None and last and last > first_any) else None
            words = word_count(res.get("content") or "")
            pt = u.get("prompt_tokens")
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") if isinstance(u.get("prompt_tokens_details"), dict) else None
            rows.append({"session": sdir.name, "endpoint": man.get("endpoint"), "track": man.get("track"),
                         "slot": man.get("time_slot_tsi"), "created": man.get("created_tsi"),
                         "config_hash": r.get("config_hash") or "legacy", "variant": r["variant"],
                         "model_id": r["model_id"], "mode": r["mode"], "workload": r["workload"], "stage": r["stage"],
                         "concurrency": r["concurrency"], "cell": r["cell"], "in_flight_at_start": r.get("in_flight_at_start"),
                         "outcome": res.get("outcome"), "ttft_answer": ttfa, "ttft_reasoning": res.get("t_first_reasoning"),
                         "ttft_any": first_any, "e2e": res.get("t_end"), "completion_tokens": ct, "reasoning_tokens": rt,
                         "prompt_tokens": pt, "cached_tokens": cached,
                         "cached_share": (cached / pt) if (cached is not None and pt) else None,
                         "prompt_id": r.get("prompt_id"), "prompt_words": r.get("prompt_words"), "decode_tps": dec, "answer_words": words,
                         "words_per_s": (words / (last - ttfa)) if (words and ttfa is not None and last and last > ttfa) else None,
                         "served_model": res.get("served_model"), "wall_start": res.get("wall_start"), "t_last": last,
                         "keepalive": res.get("n_keepalive"), "part": int(r.get("part") or 1)})
    return pd.DataFrame(rows), excluded, notes


def _steady(g: pd.DataFrame, conc: int) -> pd.Series:
    """Time-weighted number of requests in flight during each request of a sweep cell (>=75% of c = steady)."""
    t0 = pd.to_datetime(g["wall_start"], utc=True, format="mixed")
    start = (t0 - t0.min()).dt.total_seconds().to_numpy()
    end = start + g["e2e"].fillna(0).to_numpy()
    avg = []
    for s, e in zip(start, end):
        if e <= s:
            avg.append(0.0)
            continue
        ov = np.clip(np.minimum(end, e) - np.maximum(start, s), 0, None).sum()
        avg.append(ov / (e - s))
    return pd.Series(np.array(avg) >= 0.75 * conc, index=g.index)


def _p(s: pd.Series, q: float):
    s = s.dropna()
    return float(s.quantile(q)) if len(s) else None


END_NOTES = {"max_parts": "started too many times — not started again",
             "config_changed": "not resumed: the latency configuration changed",
             "quota": "stopped on the daily quota",
             "incomplete": "requests given up after repeated network outages were not sent again inside its window"}


def _and(xs: list[str]) -> str:
    xs = [str(x) for x in xs]
    return ", ".join(xs[:-1]) + " and " + xs[-1] if len(xs) > 1 else "".join(xs)


def latency_session_stops(sessions, extra: dict | None = None) -> dict:
    """Sessions that sent less than planned, ran in several parts or in another stage order: their end time
    (session_window / --max-minutes / --until) or a model's time budget stopped a stage (that model is then left out
    of the later stages and the sweep), the process was ended from outside and the window ran out before it was
    started again, a network outage, the sweep run before 'long' (since 16 Oct) — the records they have are
    valid, there are just fewer of them."""
    out = {}
    for sdir in sessions:
        if not (sdir / "latency").exists():
            continue
        summ = ((manifest(sdir).get("suites") or {}).get("latency") or {}).get("summary") or {}
        er = str(summ.get("end_reason") or "")
        sp = summ.get("parts") or []
        parts = [f"stopped at its end time {str(summ.get('deadline_tsi') or '')[11:16]}"] if summ.get("deadline_hit") else []
        if er == "window_over" and sp:
            parts.append(f"ended from outside at {str(sp[-1].get('ended_tsi') or '')[11:16]} (part {len(sp)}), not "
                         "restarted before its window ended")
        elif er.startswith("error"):
            parts.append(f"its last part stopped with an error ({er[7:90]})")
        elif er in END_NOTES:
            parts.append(END_NOTES[er])
        if len(sp) > 1:
            parts.append(f"ran in {len(sp)} parts (" + ", ".join(
                f"{str(p.get('started_tsi') or '')[11:16]}–{str(p.get('ended_tsi') or '')[11:16]}" for p in sp) + ")")
        cov = summ.get("coverage") or {}
        if summ.get("partial") and cov:
            short = [f"{w} {x.get('done')}/{x.get('planned')}" for w, x in (cov.get("c1") or {}).items()
                     if (x.get("done") or 0) < (x.get("planned") or 0)]
            if short:
                parts.append("single-stream " + ", ".join(short))
            if not (cov.get("sweep_cells") or {}).get("measured"):
                parts.append("no concurrency sweep")
        for o in summ.get("outages") or []:
            # client: this machine could not even start a connection (name lookup, no route); unknown: the endpoint
            # could not be reached and the check cannot tell whether the PC's network or the provider was down
            what = ("network outage on this machine" if o.get("side") == "client" else
                    "endpoint unreachable (the network of this PC or the provider — the check cannot tell)")
            parts.append(f"{what}{' (found afterwards)' if o.get('found_afterwards') else ''} at "
                         f"{str(o.get('start_tsi') or '')[11:16]}"
                         + (f" for {float(o['seconds']):.0f} s" if o.get("seconds") is not None else ""))
        powers = [summ.get("power_start"), summ.get("power_end")] + [p.get(k) for p in sp for k in ("power_start", "power_end")]
        if any((x or {}).get("ac_power") is False for x in powers):
            parts.append("ran (partly) on battery")
        stops = summ.get("budget_stops") or []
        parts += [f"{x['model']} {x['stage']} '{x['workload']}' after {x['sent']}/{x['planned']}"
                  for x in stops if x.get("reason", "time_budget") == "time_budget"]
        # since 16 Oct a model that used up a single-stream budget is left out of the later stages and the sweep
        left: dict[str, list[str]] = {}
        for x in stops:
            if x.get("reason") == "slow":
                left.setdefault(str(x.get("model")), []).append(str(x.get("workload")))
        for c in summ.get("sweeps") or []:
            if c and c.get("slow_after"):
                m = str(c.get("variant") or "").rsplit(":", 1)[0]
                if "the sweep" not in left.setdefault(m, []):
                    left[m].append("the sweep")
        parts += [f"{m} left out of {_and(w)} (its time budget was used up earlier in the session)"
                  for m, w in left.items()]
        order = summ.get("stage_order") or []
        if "sweep" in order and order[-1] != "sweep":
            parts.append(f"the sweep ran before {_and(order[order.index('sweep') + 1:])}")
        if extra and extra.get(sdir.name):
            parts.append(extra[sdir.name])
        if parts:
            out[sdir.name] = "; ".join(parts)
    return out


def tokenizer_fertility(df: pd.DataFrame, n_boot: int = 2000, seed: int = 20261104) -> tuple[pd.DataFrame, str | None]:
    """Prompt tokens per Turkish word, compared on identical text (2026-11-04; METHODOLOGY §12).

    Every model gets the same fixed RAG prompts in the latency workload (system prompt, document passages and
    question; only the passage order changes between cycles). A prompt's tokens are the API-reported prompt_tokens,
    the median over its requests. Only prompts that every model has, with the same word count, are used, so every row
    counts the same text. tokens_per_word = Σ tokens / Σ words (system prompt and chat template included);
    vs_lowest = the model's Σ tokens over those of the model with the fewest, with a 95 % bootstrap interval over
    prompts, and the range over single prompts. v1.0.0 fitted the slope of tokens on words instead, which prompts of
    similar length cannot pin down (DeepSeek's intercept came out negative)."""
    d = df[(df.workload == "rag") & df.prompt_tokens.notna() & df.prompt_words.notna() & df.prompt_id.notna()]
    if d.empty:
        return pd.DataFrame(), None
    d = d.assign(prompt_tokens=pd.to_numeric(d.prompt_tokens), prompt_words=pd.to_numeric(d.prompt_words))
    per = (d.groupby(["model_id", "prompt_id"])
             .agg(tokens=("prompt_tokens", "median"), words=("prompt_words", "median"), requests=("prompt_tokens", "size"))
             .reset_index())
    tok = per.pivot(index="prompt_id", columns="model_id", values="tokens").dropna()
    wrd = per.pivot(index="prompt_id", columns="model_id", values="words").loc[tok.index]
    same = wrd.nunique(axis=1) == 1                     # the identical text for every model
    tok, words = tok[same], wrd[same].iloc[:, 0]
    if len(tok) < 3:
        return pd.DataFrame(), None
    tpw = tok.sum() / words.sum()
    ref = tpw.idxmin()
    t = tok.to_numpy(dtype=float)
    boot = t[np.random.default_rng(seed).integers(0, len(t), size=(n_boot, len(t)))].sum(axis=1)
    j = list(tok.columns).index(ref)
    lo, hi = np.percentile(boot / boot[:, [j]], [2.5, 97.5], axis=0)
    single = tok.div(tok[ref], axis=0)
    reqs = per[per.prompt_id.isin(tok.index)].groupby("model_id")["requests"].sum()
    out = pd.DataFrame({"model_id": list(tok.columns), "tokens_per_word": tpw.to_numpy(),
                        "vs_lowest": (tok.sum() / tok[ref].sum()).to_numpy(), "vs_lowest_ci_lo": lo,
                        "vs_lowest_ci_hi": hi, "single_prompt_min": single.min().to_numpy(),
                        "single_prompt_max": single.max().to_numpy(), "prompts": len(tok), "words": int(words.sum()),
                        "requests": reqs.reindex(tok.columns).to_numpy()})
    out = out.sort_values("tokens_per_word", kind="stable").reset_index(drop=True)
    note = "\n".join([
        f"The same {len(tok)} RAG prompts for every model ({int(words.min())}–{int(words.max())} words each: system "
        "prompt, document passages, question); prompt tokens as reported by the API, median over a prompt's requests.",
        "tokens_per_word = Σ prompt tokens / Σ words over these prompts (system prompt and chat template included).",
        f"vs_lowest = the model's prompt tokens over {ref}'s on the identical prompts, with a 95 % bootstrap interval "
        "over prompts (they share some passages, so it is on the narrow side); single_prompt_min / max = the range over "
        "single prompts.",
        "Corrected on 2026-11-04 (METHODOLOGY §12): v1.0.0 showed the regression slope of tokens on words here; over "
        "prompts of similar length the slope was not stable."])
    return out, note


def analyze_latency(sessions, slo_ttft=1.0, slo_tpot_ms=50.0) -> dict:
    df, excluded, notes = latency_records(sessions)
    if df.empty:
        return {"excluded_sessions": excluded} if excluded else {}
    df["usable"] = df["outcome"].isin(list(USABLE))
    df["model_attempt"] = df["outcome"].isin(list(FINAL_OUTCOMES))
    df["tpot_ms"] = 1000.0 / df["decode_tps"]
    df["steady"] = True
    for (s, cell), g in df[df.stage == "sweep"].groupby(["session", "cell"]):
        df.loc[g.index, "steady"] = _steady(g, int(g["concurrency"].iloc[0]))
    meas = df[df.stage.isin(["c1", "sweep"])]
    rows = []
    for (grp, s, v, w, c), g in meas.groupby([meas.endpoint + "|" + meas.config_hash, "session", "variant", "workload",
                                               "concurrency"]):
        steady = g[g["steady"]] if (g.stage.iloc[0] == "sweep" and g["steady"].sum() >= 3) else g
        u = steady[steady["usable"]]
        att = steady[steady["model_attempt"]]
        cens = att["ttft_answer"].where(att["ttft_answer"].notna(), np.inf)
        cens_p50 = float(np.median(cens)) if len(cens) else None
        rows.append({"group": grp, "session": s, "variant": v, "workload": w, "concurrency": c,
                     "n": len(g), "n_steady": int(g["steady"].sum()), "n_ok": len(u), "ok_rate": float(g["usable"].mean()),
                     "rate_429": float(g["outcome"].astype(str).str.startswith("http_429").mean()),
                     "rate_503": float((g["outcome"] == "http_503").mean()),
                     "rate_timeout": float(g["outcome"].astype(str).str.startswith("timeout").mean()),
                     "no_answer_rate": float(att["ttft_answer"].isna().mean()) if len(att) else None,
                     "ttft_answer_p50": _p(u["ttft_answer"], 0.5), "ttft_answer_p90": _p(u["ttft_answer"], 0.9),
                     "ttft_answer_p50_censored": None if cens_p50 is None or math.isinf(cens_p50) else cens_p50,
                     "ttft_reasoning_p50": _p(u["ttft_reasoning"], 0.5), "e2e_p50": _p(u["e2e"], 0.5), "e2e_p90": _p(u["e2e"], 0.9),
                     "decode_tps_p50": _p(u["decode_tps"], 0.5), "words_per_s_p50": _p(u["words_per_s"], 0.5),
                     "reasoning_tokens_p50": _p(u["reasoning_tokens"], 0.5), "completion_tokens_p50": _p(u["completion_tokens"], 0.5),
                     "cached_share_p50": _p(u["cached_share"], 0.5),
                     "goodput_answer": float((u["ttft_answer"].le(slo_ttft) & u["tpot_ms"].le(slo_tpot_ms)).sum() / max(1, len(g))),
                     "aggregate_tok_s": None})
    agg = pd.DataFrame(rows)
    for i, r in agg.iterrows():
        if r["concurrency"] > 1:
            g = meas[(meas.session == r["session"]) & (meas.variant == r["variant"]) & (meas.concurrency == r["concurrency"])
                     & (meas.workload == r["workload"])]
            t0 = pd.to_datetime(g["wall_start"], utc=True, format="mixed")
            t1 = max(ts + pd.Timedelta(seconds=0 if (e is None or (isinstance(e, float) and math.isnan(e))) else e)
                     for ts, e in zip(t0, g["e2e"]))
            secs = (t1 - t0.min()).total_seconds()
            agg.at[i, "aggregate_tok_s"] = g["completion_tokens"].fillna(0).sum() / secs if secs > 0 else None
    summ_rows, not_comparable = [], {}
    for grp, gg in agg.groupby("group"):
        c1 = gg[gg.concurrency == 1]
        sess_of = c1.groupby("variant")["session"].apply(set).to_dict()
        best, best_score = None, (-1, -1)
        cands = {frozenset(x) for x in sess_of.values()} | {frozenset([s]) for s in c1["session"].unique()}
        for cand_sessions in sorted(cands, key=sorted):           # sorted: the same choice on every run
            for s0 in sorted(cand_sessions):
                vs = {v for v, ss in sess_of.items() if s0 in ss}
                common = set.intersection(*(sess_of[v] for v in vs)) if vs else set()
                score = (len(vs) * len(common), len(vs))        # a tie goes to more systems compared
                if score > best_score:
                    best, best_score = (frozenset(vs), frozenset(common)), score
        if not best:
            continue
        V, common = best
        for v in set(sess_of) - set(V):
            not_comparable[f"{v} [{grp}]"] = f"ran in {len(sess_of[v])} session(s) not shared with the other systems"
        for (w, c), g in gg.groupby(["workload", "concurrency"]):
            # the sessions in which EVERY compared system has this workload / level — a session that stopped
            # part-way (or skipped a cell) must not leave one system's median from other times of day than the rest
            has = [set(gv.session) & common for v, gv in g.groupby("variant") if v in V]
            same = set.intersection(*has) if has else set()
            use = same or common
            for v, gv in g.groupby("variant"):
                if v not in V:
                    continue
                gc = gv[gv.session.isin(use)]
                if gc.empty:
                    continue
                summ_rows.append({"endpoint|config": grp, "workload": w, "concurrency": c, "variant": v,
                                  "same_sessions": bool(same), "sessions": gc["session"].nunique(),
                                  "n_ok": int(gc["n_ok"].sum()),
                                  "ttft_answer_p50": gc["ttft_answer_p50"].median(), "ttft_answer_p50_min": gc["ttft_answer_p50"].min(),
                                  "ttft_answer_p50_max": gc["ttft_answer_p50"].max(),
                                  "ttft_answer_p50_censored": gc["ttft_answer_p50_censored"].median(),
                                  "no_answer_rate": gc["no_answer_rate"].mean(), "ttft_answer_p90": gc["ttft_answer_p90"].median(),
                                  "ttft_reasoning_p50": gc["ttft_reasoning_p50"].median(), "e2e_p50": gc["e2e_p50"].median(),
                                  "decode_tps_p50": gc["decode_tps_p50"].median(), "words_per_s_p50": gc["words_per_s_p50"].median(),
                                  "reasoning_tokens_p50": gc["reasoning_tokens_p50"].median(), "ok_rate": gc["ok_rate"].mean(),
                                  "rate_429": gc["rate_429"].mean(), "rate_503": gc["rate_503"].mean(),
                                  "cached_share_p50": gc["cached_share_p50"].median(),
                                  "goodput_answer": gc["goodput_answer"].mean(), "aggregate_tok_s": pd.to_numeric(gc["aggregate_tok_s"]).median()})
    summary = pd.DataFrame(summ_rows)
    c1u = df[(df.stage == "c1") & df["usable"]]
    by_slot = c1u.groupby(["variant", "workload", "slot"])["ttft_answer"].median().unstack().reset_index() if not c1u.empty else pd.DataFrame()
    fertility, fertility_note = tokenizer_fertility(df)
    warm = df[df.stage == "warmup"].sort_values("wall_start")
    cold = warm.groupby(["session", "variant"]).head(1)[["session", "variant", "outcome", "ttft_any", "e2e"]].rename(
        columns={"ttft_any": "cold_first_token_s", "e2e": "cold_e2e_s"})
    served = df.groupby("variant")["served_model"].agg(          # NaN = a request without an answer (e.g. a 503)
        lambda s: ", ".join(sorted({x for x in s if isinstance(x, str) and x}))).reset_index()
    return {"per_session": agg, "summary": summary, "by_slot": by_slot, "fertility": fertility, "fertility_note": fertility_note, "served": served,
            "cold_start": cold, "excluded_sessions": excluded, "not_comparable": not_comparable,
            "stopped_early": latency_session_stops(sessions, notes), "df": df}


# =============================================================================== self-hosted deployment (phase 6)
def deployment_twins(reg) -> dict:
    """Self-deployed model id -> its hosted twin (models.yaml options.twin): the same weights served by a provider."""
    return {m.id: m.options["twin"] for m in reg.models.values() if (m.options or {}).get("twin")}


def first_call_ok_parsed(df: pd.DataFrame) -> pd.Series:
    """Tools: is the FIRST turn's call right when tool calls written as text are parsed too (Mistral's
    '[TOOL_CALLS]name{…}')?  The model's tool use on equal terms when a host returns the calls as text instead of
    tool_calls (Bedrock's Magistral, 23 Oct).  The first turn only: a call left as text never got a tool result."""
    from harness.scoring.basic import parse_mistral_text_calls, score_tool_first_turn
    cache: dict = {}
    out = []
    for sdir, key, rec in zip(df["sdir"], df["key"], df["rec"]):
        if sdir not in cache:
            cache[sdir] = plan_items(sdir, "tools")
        it = cache[sdir].get(key)
        if it is None:
            out.append(None)
            continue
        res = rec.get("result") or {}
        content, calls = res.get("content") or "", res.get("tool_calls") or []
        if not calls:
            content, calls = parse_mistral_text_calls(content)
        out.append(float(bool(score_tool_first_turn(it["meta"]["case"], content, calls).get("call_ok"))))
    return pd.Series(out, index=df.index, dtype=float)


# (results key, column, label, rows used) — the same rows the main tables use
PARITY_METRICS = [
    ("rag_qa:judged", "correctness", "RAG correctness (judge: 1 / ½ / 0)", None),
    ("rag_qa:judged", "hallucination", "RAG answers with a hallucination (judge; lower is better)", None),
    ("rag_qa", "correct_refusal", "RAG correct refusals (unanswerable questions)", "unanswerable"),
    ("rag_qa", "citation_precision", "RAG citation precision", "answerable"),
    ("rag_qa", "key_number_recall", "RAG key numbers in the answer", "answerable"),
    ("mcq_global_mmlu_tr", "accuracy", "Global-MMLU-tr accuracy", None),
    ("mcq_seneca_trbench", "accuracy", "seneca-trbench accuracy", None),
    ("ifeval", "strict", "IFEval: all checks pass", None),
    ("docextract", "all_correct", "invoice extraction: all fields right", None),
    ("decision", "birim_ok", "routing: unit", None),
    ("decision", "aciliyet_ok", "routing: urgency", None),
    ("tools", "pass", "tools: pass (as each API returned the calls)", None),
    ("tools", "first_call_ok_parsed", "tools: first call right (calls written as text parsed too)", None),
]


def deployment_parity(results: dict, reg) -> pd.DataFrame:
    """Each self-deployed system next to its hosted twin on the same plans, paired by question (and repeat):
    means on the common items, paired difference with a 95 % bootstrap CI and a sign-flip p-value, and the share of
    answers that are identical word for word (not for tools: a parsed call and a text call are never the same)."""
    rows = []
    for dep, twin in deployment_twins(reg).items():
        for key, col, label, subset in PARITY_METRICS:
            r = results.get(key.split(":")[0]) or {}
            df = r.get("jd") if key.endswith(":judged") else r.get("df")
            if df is None or df.empty:
                continue
            if col == "first_call_ok_parsed" and col not in df:
                df[col] = first_call_ok_parsed(df)
            if col not in df:
                continue
            if subset is not None and "answerable" in df:
                df = df[df["answerable"] == (subset == "answerable")]
            for mode in sorted(set(df.loc[df["model_id"] == dep, "mode"]) & set(df.loc[df["model_id"] == twin, "mode"])):
                a, b = f"{dep}:{mode}", f"{twin}:{mode}"
                sub = df[df["variant"].isin([a, b])]
                bi = by_item(sub, col)
                if a not in bi or b not in bi:
                    continue
                d = S.paired_diff(bi[a], bi[b])
                if not d.get("n"):
                    continue
                common = set(bi[a]) & set(bi[b])
                ident = None
                if key != "tools":
                    sa = sub[sub["variant"] == a].set_index(["item_id", "repeat"])["content_sha"]
                    sb = sub[sub["variant"] == b].set_index(["item_id", "repeat"])["content_sha"]
                    both = sa.index.intersection(sb.index)
                    ident = float((sa.loc[both] == sb.loc[both]).mean()) if len(both) else None
                rows.append({"self-hosted": a, "hosted twin": b, "metric": label,
                             "self-hosted value": float(np.mean([np.mean(bi[a][i]) for i in common])),
                             "twin value": float(np.mean([np.mean(bi[b][i]) for i in common])),
                             "difference": d["diff"], "ci_lo": d["lo"], "ci_hi": d["hi"], "p": d["p"],
                             "n_items": d["n"], "identical answers": ident})
    return pd.DataFrame(rows)


def deployment_capacity(latency: dict, reg) -> tuple[pd.DataFrame, list[str]]:
    """Capacity of each self-deployed system from its latency session(s), per level of requests in flight.

    steady = the requests after the first round of a sweep cell with >= 75 % of the level in flight on average
    (closed loop: each simulated user asks again as soon as an answer is complete); burst = the first round, when
    every user presses Enter at once.  Throughput counts up to the end of the last complete answer of the cell; an
    answer that hit the token limit is left out of the timings and named in the notes."""
    df = latency.get("df") if latency else None
    deployed = set(deployment_twins(reg)) | {m.id for m in reg.models.values() if m.family == "deploy"}
    if df is None or df.empty:
        return pd.DataFrame(), []
    df = df[df["model_id"].isin(deployed) & df["stage"].isin(["c1", "sweep"])]
    rows, notes = [], []
    for (s, v, w, c), g in df.groupby(["session", "variant", "workload", "concurrency"]):
        c = int(c)
        g = g.assign(t0=pd.to_datetime(g["wall_start"], utc=True, format="mixed")).sort_values("t0")
        ok = g["outcome"] == "ok"
        if c > 1:
            flag = _steady(g, c)
            later = g.iloc[c:]
            steady = later[flag.loc[later.index] & ok.loc[later.index]]
            burst = g.iloc[:c][ok.iloc[:c]]
        else:
            steady, burst = g[ok], g.iloc[0:0]
        done = g[ok]
        span = ((done["t0"] + pd.to_timedelta(done["e2e"].fillna(0), unit="s")).max() - g["t0"].min()).total_seconds() \
            if not done.empty else 0.0
        trunc = int((g["outcome"] == "truncated").sum())
        if trunc:
            notes.append(f"{v}, {w}, {c} in flight (session {s}): {trunc} answer(s) ran into the token limit (text "
                         "repeating itself) — left out of the timings; such an answer holds its slot for minutes, so cap "
                         "max_tokens in production")
        rows.append({"system": v, "session": s, "workload": w, "in flight": c, "answers": int(ok.sum()),
                     "steady answers": len(steady),
                     "first token p50 s": _p(steady["ttft_answer"], .5), "first token p90 s": _p(steady["ttft_answer"], .9),
                     "burst: first token p50 s": _p(burst["ttft_answer"], .5) if c > 1 else None,
                     "burst: first token max s": float(burst["ttft_answer"].max()) if c > 1 and not burst.empty else None,
                     "tokens/s per user p50": _p(steady["decode_tps"], .5),
                     "full answer p50 s": _p(steady["e2e"], .5), "full answer p90 s": _p(steady["e2e"], .9),
                     "answers per min": len(done) / span * 60 if span > 0 else None,
                     "output tokens/s": done["completion_tokens"].fillna(0).sum() / span if span > 0 else None,
                     "prompt tokens p50": _p(done["prompt_tokens"], .5), "answer tokens p50": _p(done["completion_tokens"], .5)})
    out = pd.DataFrame(rows)
    if out.empty:
        return out, notes
    out = out.sort_values(["system", "workload", "in flight"]).reset_index(drop=True)
    for (v, w), g in out.groupby(["system", "workload"]):
        for name, t_max, tps_min in (("comfortable", 2.0, 10.0), ("usable", 5.0, 5.0)):
            fit = g[(g["first token p90 s"] <= t_max) & (g["tokens/s per user p50"] >= tps_min)]["in flight"]
            notes.append(f"{v}, {w}: {name} (first token within {t_max:g} s for 90 % of the steady requests and at "
                         f"least {tps_min:g} tokens/s per user) up to {int(fit.max()) if not fit.empty else '—'} "
                         "requests in flight")
        top = g["answers per min"].max()
        if pd.notna(top):
            notes.append(f"{v}, {w}: at most {top:.0f} answers per minute (higher levels only make every answer slower)")
    return out, notes


# =============================================================================== charts
def chart_footer(fig, text):
    fig.text(0.01, -0.2 / fig.get_figheight(), text, fontsize=7, color="#555")


def save_fig(fig, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


# Categorical palette validated for colour-blind separation (8 slots, fixed order): the colour follows the model,
# never its rank; a ninth model and later fold into grey.  Reasoning mode is a second channel (marker / line style).
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
GREY, INK, QUIET = "#8a8984", "#1d2433", "#52514e"
MODE_ORDER = ["default", "think", "think_min", "nothink"]
MODE_MARKER = {"default": "o", "think": "D", "think_min": "^", "nothink": "s"}
MODE_STYLE = {"default": "-", "think": "-.", "think_min": ":", "nothink": "--"}
MODE_LABEL = {"default": "default mode (reasoning on where the model has it)", "think": "reasoning on",
              "think_min": "minimal reasoning", "nothink": "reasoning off"}


def _vmodel(v) -> str:
    return str(v).split(":")[0]


def _vmode(v) -> str:
    parts = str(v).split(":")
    return parts[1] if len(parts) > 1 else "default"


def _modes(variants) -> list[str]:
    ms = {_vmode(v) for v in variants}
    return [m for m in MODE_ORDER if m in ms] + sorted(ms - set(MODE_ORDER))


def _display(model: str) -> str:
    """Chart name of a model id; a self-deployed copy is named after the weights it serves."""
    spec = _model_spec(model)
    twin = (getattr(spec, "options", None) or {}).get("twin") if spec is not None else None
    return f"{twin} (own server)" if twin else model


def _colours(models) -> dict:
    ms = sorted(set(models))
    return {m: (PALETTE[i] if i < len(PALETTE) else GREY) for i, m in enumerate(ms)}


def _place_labels(ax, anchors, labels, obstacles=(), fontsize=8.5) -> None:
    """Write each label beside its anchor point, clear of the other labels, the points and the obstacle boxes
    (display coordinates, e.g. error bars); a label set away from its point gets a thin leader line."""
    fig = ax.figure
    fig.canvas.draw()
    rend = fig.canvas.get_renderer()
    axbb = ax.get_window_extent(rend)
    pts = [tuple(ax.transData.transform(a)) for a in anchors]
    dots = [(x - 6, y - 6, x + 6, y + 6) for x, y in pts]
    taken = list(dots) + list(obstacles)
    cand = [(7, 3), (7, -13), (-7, 3), (-7, -13), (9, 15), (9, -25), (-9, 15), (-9, -25),
            (12, 27), (12, -37), (-12, 27), (-12, -37), (16, 40), (16, -50), (-16, 40), (-16, -50)]

    def hit(a, b):
        return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])

    for i, (anchor, lab) in enumerate(zip(anchors, labels)):
        best = None
        for dx, dy in cand:
            tx = ax.annotate(lab, anchor, xytext=(dx, dy), textcoords="offset points", fontsize=fontsize,
                             ha="left" if dx >= 0 else "right", va="bottom")
            bb = tx.get_window_extent(rend)
            tx.remove()
            box = (bb.x0 - 2, bb.y0 - 1, bb.x1 + 2, bb.y1 + 1)
            inside = axbb.x0 <= box[0] and box[2] <= axbb.x1 and axbb.y0 <= box[1] and box[3] <= axbb.y1
            clash = sum(hit(box, o) for j, o in enumerate(taken) if j != i)
            score = clash * 10 + (0 if inside else 25) + (abs(dx) + abs(dy)) / 100
            if best is None or score < best[0]:
                best = (score, dx, dy, box)
            if score < 1:
                break
        _, dx, dy, box = best
        far = abs(dy) > 13 or abs(dx) > 9
        ax.annotate(lab, anchor, xytext=(dx, dy), textcoords="offset points", fontsize=fontsize, color=INK,
                    ha="left" if dx >= 0 else "right", va="bottom", zorder=5,
                    arrowprops=dict(arrowstyle="-", lw=0.6, color=GREY, shrinkA=1, shrinkB=5) if far else None)
        taken.append(box)


def fertility_chart(fert: pd.DataFrame, footer: str):
    """Tokens per Turkish word on identical prompts; each bar labelled with its excess over the lowest."""
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 3.6))
    ax.barh(fert["model_id"], fert["tokens_per_word"], color="#4a7ab5")
    for i, (v, r) in enumerate(zip(fert["tokens_per_word"], fert["vs_lowest"])):
        ax.text(v + 0.03, i, f"{v:.2f}" + (f"  (+{100 * (r - 1):.0f} %)" if r > 1.005 else ""), va="center", fontsize=8)
    ax.set_xlim(0, float(fert["tokens_per_word"].max()) * 1.25)
    ax.set_xlabel(f"prompt tokens per Turkish word (the same {int(fert['prompts'].iloc[0])} RAG prompts for every model)")
    ax.set_title("Turkish token tax by tokenizer (API-reported prompt tokens)")
    chart_footer(fig, footer)
    return fig


def make_charts(out: Path, results: dict, footer: str) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    imgs = {}
    lat = results.get("latency") or {}
    rag = results.get("rag_qa") or {}
    s_all = lat.get("summary")
    if s_all is not None and not s_all.empty and rag.get("primary") is not None and not rag["primary"].empty:
        s = s_all[(s_all.workload == "rag") & (s_all.concurrency == 1)].drop_duplicates("variant").set_index("variant")
        p = rag["primary"].set_index("variant")
        common = [v for v in p.index if v in s.index and pd.notna(s.loc[v, "ttft_answer_p50"])]
        if common:
            from matplotlib.lines import Line2D
            fig, ax = plt.subplots(figsize=(9.6, 6.4))
            col = _colours(_vmodel(v) for v in common)
            by_model = defaultdict(list)
            for v in common:
                by_model[_vmodel(v)].append((s.loc[v, "ttft_answer_p50"], p.loc[v, "correctness"], v))
            for m, pts in by_model.items():      # the modes of one model, joined
                if len(pts) > 1:
                    pts = sorted(pts)
                    ax.plot([a for a, _, _ in pts], [b for _, b, _ in pts], ls="--", lw=0.9, color=col[m], alpha=0.5,
                            zorder=1)
            bars = []
            for v in common:
                x, y, lo, hi = s.loc[v, "ttft_answer_p50"], p.loc[v, "correctness"], p.loc[v, "ci_lo"], p.loc[v, "ci_hi"]
                c = col[_vmodel(v)]
                ax.errorbar(x, y, yerr=[[y - lo], [hi - y]], fmt="none", ecolor=c, elinewidth=1.3, capsize=3,
                            alpha=0.7, zorder=2)
                ax.plot(x, y, MODE_MARKER.get(_vmode(v), "o"), ms=8, color=c, mec="white", mew=1.3, zorder=3)
                bars.append((x, lo, hi))
            ax.set_xscale("log")
            ax.set_xlabel("time to first answer token, s (median of usable responses, RAG workload, 1 stream; log scale)")
            ax.set_ylabel("RAG answer correctness (judge; mean and 95% CI)")
            ax.set_title("Turkish RAG: quality vs. time to first answer")
            ax.grid(alpha=0.3)
            leg = ax.legend(handles=[Line2D([], [], ls="", marker=MODE_MARKER.get(mo, "o"), ms=7, color=QUIET,
                                            label=MODE_LABEL.get(mo, mo)) for mo in _modes(common)],
                            loc="best", fontsize=8, frameon=False, title="marker = reasoning mode; colour = model",
                            title_fontsize=8)
            fig.canvas.draw()
            lb = leg.get_window_extent(fig.canvas.get_renderer())
            obst = [(lb.x0, lb.y0, lb.x1, lb.y1)]       # the legend, every error bar and every point: no label on them
            for x, lo, hi in bars:
                (bx, y0), (_, y1) = ax.transData.transform((x, lo)), ax.transData.transform((x, hi))
                obst.append((bx - 3, y0, bx + 3, y1))
            for v in common:
                px, py = ax.transData.transform((s.loc[v, "ttft_answer_p50"], p.loc[v, "correctness"]))
                obst.append((px - 6, py - 6, px + 6, py + 6))
            anchors, labels = [], []
            for m in sorted(by_model):            # one label per model, at its default-mode point
                pts = by_model[m]
                d = [q for q in pts if _vmode(q[2]) == "default"] or pts
                anchors.append((d[0][0], d[0][1]))
                labels.append(_display(m))
            _place_labels(ax, anchors, labels, obst)
            chart_footer(fig, footer + " | workload rag, c=1")
            imgs["pareto"] = save_fig(fig, out / "charts" / "rag_quality_vs_ttft.png")
    if s_all is not None and not s_all.empty:
        rag_s = s_all[(s_all.workload == "rag")]
        if rag_s["concurrency"].nunique() > 1:
            from matplotlib.lines import Line2D
            from matplotlib.ticker import FixedLocator, NullLocator, ScalarFormatter
            fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.4))
            col = _colours(_vmodel(v) for v in rag_s["variant"].unique())
            for v, g in rag_s.groupby("variant"):
                g = g.sort_values("concurrency")
                kw = dict(color=col[_vmodel(v)], ls=MODE_STYLE.get(_vmode(v), "-"), marker="o", ms=4, lw=1.6)
                axes[0].plot(g["concurrency"], g["ttft_answer_p50"], **kw)
                axes[1].plot(g["concurrency"], g["decode_tps_p50"], **kw)
            lv = sorted(rag_s["concurrency"].unique())
            nice = [x for x in (1, 2, 4, 8, 16, 32, 48, 64, 128, 256) if lv[0] <= x <= lv[-1]]
            for a in axes:
                a.set_xscale("log", base=2)
                a.xaxis.set_major_locator(FixedLocator(nice))
                a.xaxis.set_major_formatter(ScalarFormatter())
                a.xaxis.set_minor_locator(NullLocator())
                a.set_xlabel("requests in flight (log scale)")
                a.grid(alpha=0.3)
            axes[0].set_title("waiting time", fontsize=10)
            axes[1].set_title("generation speed per request", fontsize=10)
            axes[0].set_ylabel("time to first answer token, p50 (s)")
            axes[1].set_ylabel("tokens per second per request, p50")
            handles = [Line2D([], [], color=col[m], lw=2.2, label=_display(m)) for m in sorted(col)]
            handles += [Line2D([], [], color=QUIET, ls=MODE_STYLE.get(mo, "-"), lw=1.6, label=MODE_LABEL.get(mo, mo))
                        for mo in _modes(rag_s["variant"].unique())]
            fig.legend(handles=handles, loc="lower center", ncol=3, fontsize=8, frameon=False, bbox_to_anchor=(0.5, 0.0))
            fig.subplots_adjust(bottom=0.34, wspace=0.28)
            fig.suptitle("Latency under concurrency (RAG workload, steady-state requests)")
            chart_footer(fig, footer + " | workload rag")
            imgs["concurrency"] = save_fig(fig, out / "charts" / "latency_vs_concurrency.png")
        fert = lat.get("fertility")
        if fert is not None and not fert.empty:
            imgs["fertility"] = save_fig(fertility_chart(fert, footer), out / "charts" / "tokenizer_fertility.png")
        modes = s_all[(s_all.workload == "rag") & (s_all.concurrency == 1)]
        multi = modes.assign(model=modes.variant.str.split(":").str[0]).groupby("model")["variant"].nunique()
        mm = multi[multi > 1].index.tolist()
        if mm:
            fig, ax = plt.subplots(figsize=(8, 4))
            sub = modes.assign(model=modes.variant.str.split(":").str[0], mode=modes.variant.str.split(":").str[1])
            sub = sub[sub.model.isin(mm)]
            piv = sub.pivot_table(index="model", columns="mode", values="ttft_answer_p50")
            piv = piv[[m for m in MODE_ORDER if m in piv.columns] + sorted(set(piv.columns) - set(MODE_ORDER))]
            piv.plot.bar(ax=ax, color=[PALETTE[MODE_ORDER.index(m)] if m in MODE_ORDER else GREY for m in piv.columns],
                         rot=0)
            ax.set_ylabel("TTF-answer p50 (s)")
            ax.set_title("Cost of thinking: time to first answer token by reasoning mode (RAG, c=1)")
            ax.grid(alpha=0.3, axis="y")
            chart_footer(fig, footer + " | workload rag, c=1")
            imgs["thinking"] = save_fig(fig, out / "charts" / "thinking_cost.png")
    ret = results.get("retrieval_rag")
    if ret:
        from matplotlib.patches import Patch
        t = ret["table"].sort_values(["ndcg@10", "system"], ascending=[True, False], kind="mergesort")
        fig, ax = plt.subplots(figsize=(8.6, max(3.8, 0.22 * len(t) + 1.4)))
        ax.barh(t["system"], t["ndcg@10"], height=0.72,
                color=[PALETTE[0] if "+" in x else "#b9b8b2" for x in t["system"]])
        ax.tick_params(axis="y", labelsize=8)
        ax.set_xlim(0, 1)
        ax.margins(y=0.004)
        ax.grid(alpha=0.3, axis="x")
        ax.set_xlabel("nDCG@10 (RAG corpus, answerable questions; 1 = the right chunks ranked first)")
        ax.set_title("Retrieval on the RAG corpus: first stage vs + reranker")
        ax.legend(handles=[Patch(color="#b9b8b2", label="first stage only"),
                           Patch(color=PALETTE[0], label="first stage + reranker")],
                  loc="lower right", fontsize=8, frameon=False)
        chart_footer(fig, footer)
        imgs["retrieval"] = save_fig(fig, out / "charts" / "retrieval_ndcg.png")
    ocr = results.get("ocr")
    if ocr:
        b = ocr["by_category"].set_index("variant")
        fig, ax = plt.subplots(figsize=(8, 3.8))
        b.plot.bar(ax=ax)
        ax.set_ylabel("CER (order-robust, median; failed pages = 1.0)")
        ax.set_title("Turkish OCR error by page type")
        ax.grid(alpha=0.3, axis="y")
        chart_footer(fig, footer)
        imgs["ocr"] = save_fig(fig, out / "charts" / "ocr_cer_by_category.png")
    return imgs


# =============================================================================== capability matrix + KB
def capability_matrix(results: dict, reg, probes: dict) -> pd.DataFrame:
    rows = defaultdict(dict)
    for suite, (label, col, _) in task_primary(results).items():
        r = results.get(suite) or {}
        t = r.get("primary")
        if t is None or t.empty:
            continue
        for _, x in t.iterrows():
            rows[x["variant"]][label] = f"{x[col]:.3f} [{x['ci_lo']:.2f}–{x['ci_hi']:.2f}]"
    lat = (results.get("latency") or {}).get("summary")
    if lat is not None and not lat.empty:
        for _, x in lat[(lat.workload == "rag") & (lat.concurrency == 1)].iterrows():
            rows[x["variant"]]["RAG TTF-answer p50 (s)"] = f"{x['ttft_answer_p50']:.2f}" if pd.notna(x["ttft_answer_p50"]) else "–"
            rows[x["variant"]]["decode tok/s p50"] = f"{x['decode_tps_p50']:.0f}" if pd.notna(x["decode_tps_p50"]) else "–"
    out = []
    for v, cols in rows.items():
        mid = v.split(":")[0]
        spec = reg.models.get(mid)
        served = (probes.get(mid) or {}).get("served_model") or ""
        q = QUANT_RE.search(served or "")
        fmt = lambda x: f"{x:g}" if isinstance(x, (int, float)) else "–"
        out.append({"system": v, "endpoint": spec.endpoint if spec else "", "role": spec.tier if spec else "",
                    "params_B (total/active)": f"{fmt(spec.params_total_b)}/{fmt(spec.params_active_b)}" if spec else "–",
                    "served_as": served, "served_quant": q.group(1).lower() if q else "unknown",
                    "on_prem_fit": (spec.deploy or {}).get("fits") if spec and spec.deploy else _tier(spec.params_total_b if spec else None),
                    **cols})
    m = pd.DataFrame(out)
    return m.fillna("–") if not m.empty else m


def task_primary(results: dict) -> dict:
    tp = dict(TASK_PRIMARY)
    for k in results:
        if k.startswith("docextract_ocr_"):
            tp[k] = (f"Document extraction over OCR ({k.replace('docextract_ocr_', '')})", "all_correct", True)
    return tp


def _tier(b):
    from harness.run import tier_of
    return tier_of(b)


def update_kb(kb_path: Path, results: dict, provenance: dict) -> int:
    rows = []
    for suite, (label, col, _) in task_primary(results).items():
        r = results.get(suite) or {}
        t = r.get("primary")
        if t is None or t.empty or (r.get("info") or {}).get("limited"):
            continue
        info = r.get("info", {})
        for _, x in t.iterrows():
            rows.append({**provenance, "task_type": label, "suite": suite, "system": x["variant"], "metric": col,
                         "value": x[col], "ci_lo": x["ci_lo"], "ci_hi": x["ci_hi"], "n_items": x["n_items"],
                         "plans": ";".join(p[:12] for p in info.get("plans", [])),
                         "judge": ((r.get("judge") or {}).get("signature") or "") if suite == "rag_qa" else ""})
    lat = (results.get("latency") or {}).get("summary")
    if lat is not None and not lat.empty:
        for _, x in lat[(lat.workload == "rag") & (lat.concurrency == 1)].iterrows():
            for metric in ("ttft_answer_p50", "ttft_answer_p50_censored", "ttft_reasoning_p50", "e2e_p50", "decode_tps_p50"):
                if pd.notna(x[metric]):
                    rows.append({**provenance, "task_type": "Latency (RAG, 1 stream)", "suite": "latency", "system": x["variant"],
                                 "metric": metric, "value": x[metric], "ci_lo": None, "ci_hi": None,
                                 "n_items": x["sessions"], "plans": x["endpoint|config"], "judge": ""})
    for key, col, label in (("ocr", "cer_flex_median", "OCR"), ("asr", "wer_mean", "ASR")):
        r = results.get(key) or {}
        t = r.get("summary")
        if t is not None and not t.empty and not (r.get("info") or {}).get("limited"):
            for _, x in t.iterrows():
                rows.append({**provenance, "task_type": label, "suite": key, "system": x["variant"], "metric": col,
                             "value": x[col], "ci_lo": None, "ci_hi": None, "n_items": x.get("pages", x.get("n")),
                             "plans": (r.get("info") or {}).get("plan", "")[:12], "judge": ""})
    r = results.get("retrieval_rag")
    if r and not (r.get("info") or {}).get("limited"):
        for _, x in r["table"].iterrows():
            rows.append({**provenance, "task_type": "Retrieval (RAG corpus)", "suite": "retrieval", "system": x["system"],
                         "metric": "ndcg@10", "value": x["ndcg@10"], "ci_lo": None, "ci_hi": None, "n_items": x["queries"],
                         "plans": r["plan"][:12], "judge": ""})
    if not rows:
        return 0
    new = pd.DataFrame(rows)
    if kb_path.exists():
        old = pd.read_csv(kb_path)
        new = pd.concat([old, new]).drop_duplicates(subset=["suite", "system", "metric", "plans", "endpoint"], keep="last")
    kb_path.parent.mkdir(parents=True, exist_ok=True)
    new.to_csv(kb_path, index=False, encoding="utf-8")
    return len(rows)


# =============================================================================== report
def df_html(df: pd.DataFrame, digits: int = 3) -> str:
    if df is None or df.empty:
        return "<p><em>no data</em></p>"
    return df.to_html(index=False, float_format=lambda x: f"{x:.{digits}f}", na_rep="–", border=0, classes="t")


def df_md(df: pd.DataFrame, digits: int = 3) -> str:
    if df is None or df.empty:
        return "_no data_\n"
    d = df.copy()
    for c in d.columns:
        if d[c].dtype.kind == "f":
            d[c] = d[c].map(lambda x: "–" if pd.isna(x) else f"{x:.{digits}f}")
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in d.iterrows():
        lines.append("| " + " | ".join(str(v) for v in r.values) + " |")
    return "\n".join(lines) + "\n"


CSS = """body{font-family:Inter,Segoe UI,Arial,sans-serif;max-width:1180px;margin:24px auto;padding:0 16px;color:#1d2433;background:#fff}
h1{font-size:26px}h2{margin-top:36px;border-bottom:2px solid #e3e7ef;padding-bottom:4px}h3{margin-top:22px}
table.t{border-collapse:collapse;font-size:12.5px;margin:8px 0 14px}table.t th,table.t td{border-bottom:1px solid #e3e7ef;padding:4px 8px;text-align:right}
table.t th{background:#f4f6fa}table.t td:first-child,table.t th:first-child{text-align:left}
.note{background:#f7f8fb;border-left:3px solid #4a7ab5;padding:8px 12px;font-size:13px;line-height:1.5;margin:8px 0}
.note ul{margin:0;padding-left:18px}.note li{margin:2px 0}.warn{border-left-color:#c0504d}
img{max-width:100%;border:1px solid #eee;margin:8px 0}code{background:#f4f6fa;padding:1px 4px}"""


def _readable(v) -> str:
    """A dict, list or value in a note, written for people instead of as a Python repr."""
    if v is None or (hasattr(v, "__len__") and not isinstance(v, str) and len(v) == 0) or v == "":
        return "none"
    if isinstance(v, dict):
        vals = list(v.values())
        if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vals):
            low = {k: x for k, x in v.items() if x < 1}
            if not low:
                return f"100% for all {len(v)}"
            return "; ".join(f"{k} {x:.0%}" for k, x in low.items()) + f"; the other {len(v) - len(low)} at 100%"
        return "; ".join(f"{k}: {_readable(x) if isinstance(x, (dict, list, tuple, set)) else x}" for k, x in v.items())
    if isinstance(v, (list, tuple, set)):
        return ", ".join(str(x) for x in v)
    return str(v)


def _plans_note(d: dict) -> str:
    """{plan hash: why} -> 'limited plan (--limit 5) × 2'"""
    return "; ".join(f"{why} × {n}" if n > 1 else str(why) for why, n in Counter(d.values()).items())


def note_html(note: str, cls: str = "note") -> str:
    """A note given as lines (one per point) becomes a bullet list in the note box."""
    lines = [x for x in str(note).split("\n") if x.strip()]
    if len(lines) == 1:
        return f"<div class='{cls}'>{lines[0]}</div>"
    return f"<div class='{cls}'><ul>" + "".join(f"<li>{x}</li>" for x in lines) + "</ul></div>"


def note_md(note: str) -> str:
    lines = [x for x in str(note).split("\n") if x.strip()]
    return (lines[0] + "\n") if len(lines) == 1 else "".join(f"- {x}\n" for x in lines)


def gate_note(info: dict) -> str:
    """The fairness-gate facts of one table, one line each."""
    parts = []
    if info.get("limited"):
        parts.append("<b>LIMITED (smoke) PLAN — not for conclusions; the knowledge base was not updated.</b>")
    parts.append(f"Not comparable: {_readable(info.get('excluded'))}.")
    if info.get("dropped_plans"):
        parts.append(f"Plans not used: {_plans_note(info['dropped_plans'])}.")
    if info.get("n_dropped_requests"):
        parts.append(f"{info['n_dropped_requests']} records outside the request set common to all systems were dropped.")
    if info.get("rescored_changed"):
        parts.append(f"{info['rescored_changed']} records were re-scored differently by the current scorer than at run time.")
    if info.get("superseded_sessions"):
        parts.append(f"Re-runs (latest complete session used): {_readable(info['superseded_sessions'])}.")
    if info.get("note"):
        parts.append(info["note"])
    return "\n".join(parts)


def method_notes(results: dict, sessions: list[Path], reg) -> list[str]:
    """Method notes generated from what the runs actually used (plan metadata, judge, latency config)."""
    dec, mts, reps, seeds, lims = Counter(), Counter(), Counter(), Counter(), 0
    for sdir in sessions:
        for name, m in read_plan_meta(sdir).items():
            for prof, vals in (m.get("decoding") or {}).items():
                dec[f"{prof} {json.dumps(vals)}"] += 1
            if m.get("max_tokens"):
                mts[str(m["max_tokens"])] += 1
            if m.get("repeats"):
                reps[str(m["repeats"])] += 1
            if m.get("plan_seed"):
                seeds[str(m["plan_seed"])] += 1
            lims += bool(m.get("limit"))
    notes = []
    if dec:
        notes.append("Decoding (values stored in every plan item): " + "; ".join(k for k, _ in dec.most_common()))
    if mts:
        notes.append(f"max_tokens shared by thinking and answer: {', '.join(mts)}; repeats per item: {', '.join(reps)}; "
                     f"plan seeds: {', '.join(seeds)}.")
    if lims:
        notes.append(f"{lims} plan(s) were built with --limit (smoke tests); they are excluded unless --include-limited.")
    j = (results.get("rag_qa") or {}).get("judge") or {}
    if j.get("signature"):
        notes.append(f"RAG judge: {j.get('model')} (signature {j['signature']}); prompt published in prompts/tr/judge_rag.md; "
                     "answers judged blind and shuffled.")
    lat = results.get("latency") or {}
    if lat.get("summary") is not None and not lat["summary"].empty:
        notes.append("Latency: one stream per model at c=1 (modes take turns), cache-busting per session and per variant; "
                     "timings from usable responses; TTF-answer also reported censoring-aware (no answer = infinite); "
                     "sweep statistics from steady-state requests (≥75% of the nominal concurrency in flight).")
        if lat.get("excluded_sessions"):
            notes.append(f"Latency sessions excluded: {lat['excluded_sessions']}.")
    notes.append("Items, not requests, are the bootstrap unit; repeats change context order / option order / seed.")
    notes.append("Hosted latency reflects shared multi-tenant load at measurement time and does not transfer to on-prem "
                 "hardware; quality transfers only for the same weights, quantisation and chat template.")
    notes += superseded_notes()
    return notes


def write_report(out: Path, results: dict, sessions: list[Path], imgs: dict, matrix: pd.DataFrame, args, reg) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    H, M = [], []

    def sec(title, html="", md=""):
        H.append(f"<h2>{title}</h2>{html}")
        M.append(f"\n## {title}\n\n{md}")

    def sub(title, df, digits=3, note=None):
        n = note_html(note) if note else ""
        H.append(f"<h3>{title}</h3>{n}{df_html(df, digits)}")
        M.append(f"\n### {title}\n\n{(note_md(note) + chr(10)) if note else ''}{df_md(df, digits)}")
        if df is not None and not df.empty:
            df.to_csv(out / "tables" / f"{re.sub(r'[^a-z0-9]+', '_', title.lower()).strip('_')}.csv", index=False, encoding="utf-8")

    (out / "tables").mkdir(parents=True, exist_ok=True)
    man = [manifest(s) for s in sessions]
    sess_df = pd.DataFrame([{"session": m["session_id"], "track": m["track"], "endpoint": m["endpoint"],
                             "created_tsi": m.get("created_tsi"), "slot": m.get("time_slot_tsi"),
                             "suites": ", ".join(sorted(m.get("suites", {}))),
                             "client_loop_lag_p99_ms": ((m.get("suites", {}).get("latency", {}).get("summary") or {}).get("client_loop_lag") or {}).get("p99_ms")}
                            for m in man])
    limited_any = any((r or {}).get("info", {}).get("limited") for r in results.values() if isinstance(r, dict))
    banner = ("<p class='note warn'><b>This report includes limited (smoke) plans — do not draw conclusions.</b></p>"
              if limited_any else "")
    H.append(f"<h1>Turkish LLM &amp; pipeline benchmark — results</h1>{banner}<p>Generated {now} from {len(sessions)} session(s). "
             f"Fairness gate: systems are compared only on identical request plans they completed (coverage ≥ {args.min_coverage:.0%}) "
             "and only on the requests all of them completed. Intervals are 95% cluster-bootstrap CIs over items; ranks are "
             "bootstrap rank intervals (ties share the better rank, so P(best or tied) can sum above 1).</p>")
    M.append(f"# Turkish LLM & pipeline benchmark — results\n\n{'**Includes limited (smoke) plans — do not draw conclusions.**' if limited_any else ''}\n\n"
             f"Generated {now} from {len(sessions)} session(s). Systems are compared only on identical request plans they "
             f"completed (coverage ≥ {args.min_coverage:.0%}).\n")
    sec("Capability matrix (primary metric per task type)", df_html(matrix), df_md(matrix))
    if not matrix.empty:
        matrix.to_csv(out / "tables" / "capability_matrix.csv", index=False, encoding="utf-8")
    rag = results.get("rag_qa") or {}
    if rag:
        j = rag.get("judge") or {}
        reps = j.get("judged_repeats")
        note = "\n".join([
            f"Judge: {j.get('model') or '(not run yet — run harness/run.py judge ... --yes)'}"
            f"{' (signature ' + j['signature'] + ')' if j.get('signature') else ''}.",
            "Repeats judged: " + ("the first of each system's repeats (repeat 0)." if reps == [0] else f"{_readable(reps)}."),
            f"Judge coverage: {_readable(j.get('coverage'))} systems.",
            f"Excluded by judge coverage: {_readable(j.get('excluded'))}.",
            gate_note(rag.get("info", {}))])
        sec("Grounded QA (RAG) — answer quality", note_html(note), note_md(note))
        if imgs.get("pareto"):
            H.append(f"<img src='data:image/png;base64,{imgs['pareto']}'/>")
            M.append("\n![quality vs latency](charts/rag_quality_vs_ttft.png)\n")
        for name, t in rag.get("tables", {}).items():
            sub(name.replace("_", " "), t)
        if rag.get("pairwise_full") is not None:
            rag["pairwise_full"].to_csv(out / "tables" / "rag_pairwise_full.csv", index=False, encoding="utf-8")
    for suite in [k for k in task_primary(results) if k not in ("rag_qa",)]:
        r = results.get(suite) or {}
        if not r:
            continue
        label = task_primary(results)[suite][0]
        note = gate_note(r.get("info", {}))
        sec(label, note_html(note), note_md(note))
        for name, t in r.get("tables", {}).items():
            sub(name.replace("_", " "), t)
        if r.get("pairwise_full") is not None:
            r["pairwise_full"].to_csv(out / "tables" / f"{suite}_pairwise_full.csv", index=False, encoding="utf-8")
    tvo = results.get("docextract_text_vs_ocr")
    if tvo is not None and not tvo.empty:
        sec("Document extraction: clean text vs OCR pipeline", "", "")
        sub("extraction all-correct, text vs OCR (paired)", tvo)
    lat = results.get("latency") or {}
    if lat.get("summary") is not None:
        stopped = lat.get("stopped_early") or {}
        note = "\n".join([
            "TTF-answer = time to the first visible answer token; TTF-reasoning = time to the first thinking token.",
            "Timings come from usable responses; '…_censored' counts a response without any answer token as "
            "infinitely slow.",
            "Cross-session values are medians of per-session p50s over the sessions in which every compared system has "
            "that workload and level (same_sessions = False: no such session, each system's own sessions are used); "
            "min/max show the spread across times of day.",
            f"Excluded sessions: {_readable(lat.get('excluded_sessions'))}.",
            f"Not comparable: {_readable(lat.get('not_comparable'))}.",
            (f"{len(stopped)} session(s) sent less than planned, ran in several parts or in another stage order — "
             "what happened in each is in the table 'latency sessions that ran short or in parts' below."
             if stopped else "Every session ran as planned.")])
        sec("Latency as experienced (streaming, client in Türkiye)", note_html(note), note_md(note))
        for k in ("concurrency", "thinking", "fertility"):
            if imgs.get(k):
                H.append(f"<img src='data:image/png;base64,{imgs[k]}'/>")
        sub("latency summary", lat["summary"], 3)
        sub("latency by time slot (TTF-answer p50, c=1)", lat["by_slot"], 3)
        sub("tokenizer fertility (prompt tokens per word)", lat["fertility"], 3, note=lat.get("fertility_note"))
        sub("cold start (first warm-up request per session)", lat["cold_start"], 3)
        sub("served models (from responses)", lat["served"])
        sub("latency per session (raw cells)", lat["per_session"], 3)
        if stopped:
            sub("latency sessions that ran short or in parts",
                pd.DataFrame([{"session": k, "what happened": v} for k, v in stopped.items()]),
                note="End time, a model's time budget (that model is then left out of the session's later stages and "
                     "sweep), ended from outside, a network outage on this machine (requests hit by it are left out; "
                     "their other records count), or the sweep run before 'long', as every session does since 16 Oct.")
    dep = results.get("deployment") or {}
    par, cap = dep.get("parity"), dep.get("capacity")
    if (par is not None and not par.empty) or (cap is not None and not cap.empty):
        hws = []
        for d in sorted(deployment_twins(reg)):
            hw = reg.endpoints[reg.model(d).endpoint].hardware or ""
            hws.append(f"{d}: {hw if hw and not hw.startswith('describe it') else 'hardware not recorded (OVH_DEPLOY_HARDWARE in .env)'}")
        note = ("The same weights served by us (Track B, practice deployment) next to the provider that hosts them, on the "
                f"same request plans, paired by question and repeat — {'; '.join(hws)}. 'identical answers' = word for "
                "word the same answer (both sides get the same sampling seed). Tool calls: 'as each API returned the "
                "calls' scores what the API delivered; 'calls written as text parsed too' also reads Mistral's "
                "[TOOL_CALLS]name{…} left in the answer text (Bedrock returns Magistral's calls that way) — first call "
                "only, because a call left as text never got a tool result.")
        note = note.replace(" — ", "\n", 1).replace(". 'identical answers'", ".\n'identical answers'", 1) \
                   .replace(". Tool calls:", ".\nTool calls:", 1)
        sec("Self-hosted deployment: the same weights on our server vs the provider", note_html(note), note_md(note))
        if par is not None and not par.empty:
            sub("self-hosted vs hosted twin (paired)", par, 3)
        if cap is not None and not cap.empty:
            cap_note = "\n".join(
                ["Practice hardware — Volta GPUs (no FlashAttention, no bf16 / FP8, PCIe between the GPUs), not "
                 "representative of RTX PRO 6000 / H200: read it as a lower bound.",
                 "RAG requests (8 chunks, shuffled per request so the server's prefix cache cannot help).",
                 "'steady' = the later requests of a level, with that many in flight (each simulated user asks again as "
                 "soon as an answer is complete); 'burst' = the first round, everyone pressing Enter at once."]
                + [f"{n}." for n in dep.get("notes") or []])
            sub("self-hosted capacity: requests in flight vs latency", cap, 2, note=cap_note)
    for ds in ("rag", "xquad_tr"):
        r = results.get(f"retrieval_{ds}")
        if r:
            ri = r.get("info") or {}
            lines = [f"{r.get('n_queries')} queries; every system in the ranked table on the same queries."]
            if ri.get("partial"):
                lines.append("Reranked on a subset — delta table only, paired with its first stage on the same queries: "
                             + "; ".join(f"{k}: {x}" for k, x in ri["partial"].items()) + ".")
            lines.append(f"Not comparable: {_readable(ri.get('excluded'))}.")
            if ri.get("dropped_plans"):
                lines.append(f"Plans not used: {_plans_note(ri['dropped_plans'])}.")
            note = "\n".join(lines)
            sec(f"Retrieval — {ds}", note_html(note), note_md(note))
            if ds == "rag" and imgs.get("retrieval"):
                H.append(f"<img src='data:image/png;base64,{imgs['retrieval']}'/>")
            sub(f"retrieval {ds}", r["table"])
            sub(f"reranker delta {ds} (paired, nDCG@10)", r["deltas"])
    if results.get("ocr"):
        ocr_note = f"Failed pages count as CER 1.0.\n{_readable(results['ocr'].get('info'))}"
        sec("OCR (Turkish scanned pages)", note_html(ocr_note), note_md(ocr_note))
        if imgs.get("ocr"):
            H.append(f"<img src='data:image/png;base64,{imgs['ocr']}'/>")
        sub("ocr summary", results["ocr"]["summary"])
        sub("ocr CER by page type", results["ocr"]["by_category"])
        if results["ocr"].get("speed") is not None and not results["ocr"]["speed"].empty:
            sub("ocr speed", results["ocr"]["speed"], 3,
                note="Seconds per page from the OCR run (upload + reading + answer, as the client measured it). Hosted "
                     "rows include the network and the provider's queue; 'this PC' rows are this machine's GPU or CPU "
                     "— compare within a group, not across. Pages per minute for one stream = 60 / mean seconds per "
                     "page; pages_in_flight = how many pages the run sent at once.")
    lo = results.get("latency_ocr") or {}
    if lo.get("summary") is not None and not lo["summary"].empty:
        lo_note = ("The same 10 pages in every latency session, one at a time, then several in flight; each request is "
                   "a byte-unique copy (a JPEG comment), so no cache can answer it. Garbage share = readings with CER "
                   "above 0.5. Pages per minute: one stream = 60 / mean seconds per page; several in flight = pages "
                   "finished / wall time of the cell.")
        if lo.get("notes"):
            lo_note += " " + " ".join(lo["notes"])
        sec("OCR timing by time of day (latency sessions)", note_html(lo_note), note_md(lo_note))
        sub("ocr timing summary", lo["summary"], 3)
        sub("ocr seconds per page by time slot (p50)", lo["by_slot"], 3)
        sub("ocr timing per session", lo["per_session"], 3)
    if results.get("asr"):
        asr_note = f"Failed clips count as WER 1.0.\n{_readable(results['asr'].get('info'))}"
        sec("Speech-to-text (Turkish)", note_html(asr_note), note_md(asr_note))
        sub("asr summary", results["asr"]["summary"])
    sec("Sessions", df_html(sess_df), df_md(sess_df))
    notes = method_notes(results, sessions, reg)
    sec("Method notes (generated from the runs)", "<ul>" + "".join(f"<li>{n}</li>" for n in notes) + "</ul>",
        "".join(f"- {n}\n" for n in notes))
    html = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Turkish LLM benchmark</title><style>{CSS}</style></head><body>{''.join(H)}</body></html>")
    (out / "report.html").write_text(html, encoding="utf-8")
    (out / "report.md").write_text("".join(M), encoding="utf-8")


def main() -> None:
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(RESULTS_DIR))
    ap.add_argument("--track"); ap.add_argument("--endpoint"); ap.add_argument("--since"); ap.add_argument("--sessions")
    ap.add_argument("--min-coverage", type=float, default=0.98)
    ap.add_argument("--include-limited", action="store_true", help="also analyse --limit (smoke) plans; no KB update")
    ap.add_argument("--include-interrupted", action="store_true", help="keep interrupted latency sessions")
    ap.add_argument("--plan", action="append", help="force a plan (hash prefix) for the suite that has it; repeatable")
    ap.add_argument("--judge-signature", help="use this judge signature (prefix) instead of the best-covered one")
    ap.add_argument("--no-rescore", action="store_true", help="use the scores stored at run time")
    ap.add_argument("--out")
    ap.add_argument("--kb", default=str(ROOT / "knowledge_base" / "capability_matrix.csv"))
    args = ap.parse_args()
    ARGS = args
    reg = load_registry()
    sessions = find_sessions(Path(args.results), args.track, args.endpoint, args.since, args.sessions)
    if not sessions:
        raise SystemExit(f"no sessions under {args.results}")
    out = Path(args.out) if args.out else ROOT / "report" / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    results: dict = {}
    results["rag_qa"] = analyze_rag(sessions, args.min_coverage)
    mcq_dirs = sorted({p.name for s in sessions for p in s.glob("mcq_*") if p.is_dir()})
    for d in mcq_dirs:
        results[d] = analyze_simple(sessions, args.min_coverage, d, ["correct"], "accuracy", _mcq_extra)
    results["ifeval"] = analyze_simple(sessions, args.min_coverage, "ifeval", ["strict", "check_rate"], "strict", _ifeval_extra)
    results["tools"] = analyze_simple(sessions, args.min_coverage, "tools", ["pass"], "pass", _tools_extra)
    results["decision"] = analyze_simple(sessions, args.min_coverage, "decision", ["birim_ok", "aciliyet_ok", "iptal_ok"],
                                         "birim_ok", _decision_extra)
    results["docextract"] = analyze_simple(sessions, args.min_coverage, "docextract", ["all_correct", "field_acc"],
                                           "all_correct", _docextract_extra)
    scanned = scanned_invoices()
    for d in sorted({p.name for s in sessions for p in s.glob("docextract_ocr_*") if p.is_dir()}):
        results[d] = analyze_simple(sessions, args.min_coverage, d, ["all_correct", "field_acc"], "all_correct",
                                    _docextract_extra, keep_items=scanned or None,
                                    keep_note="Restricted to the invoices that have a scan in the OCR set; {n} invoices "
                                              "without a scan are left out (the plan holds every invoice, nothing was "
                                              "sent for them).")
    results["docextract_text_vs_ocr"] = docextract_text_vs_ocr(results)
    results["guard_all"] = analyze_guard(sessions, args.min_coverage)
    results["latency"] = analyze_latency(sessions)
    cap_df, cap_notes = deployment_capacity(results["latency"], reg)
    results["deployment"] = {"parity": deployment_parity(results, reg), "capacity": cap_df, "notes": cap_notes}
    results["retrieval_rag"] = analyze_retrieval(sessions, "rag")
    results["retrieval_xquad_tr"] = analyze_retrieval(sessions, "xquad_tr")
    results["ocr"] = analyze_ocr(sessions)
    results["latency_ocr"] = analyze_latency_ocr(sessions)
    results["asr"] = analyze_asr(sessions)
    probes = {}
    for p in (Path(args.results) / "capabilities").glob("*/*.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        probes[d.get("model_id")] = d
    matrix = capability_matrix(results, reg, probes)
    eps = sorted({manifest(s)["endpoint"] for s in sessions})
    footer = f"trbench | endpoint(s): {', '.join(eps)} | sessions: {len(sessions)} | generated {datetime.now():%Y-%m-%d}"
    imgs = make_charts(out, results, footer)
    write_report(out, results, sessions, imgs, matrix, args, reg)
    if args.include_limited or args.plan:
        n, why = 0, "--include-limited / --plan analysis"
    else:
        n, why = update_kb(Path(args.kb), results, {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                     "endpoint": ",".join(eps), "sessions": ";".join(s.name for s in sessions)}), ""
    print(f"report: {out / 'report.html'}\nknowledge base: {args.kb} (+{n} rows{'; not updated: ' + why if why else ''})")


if __name__ == "__main__":
    main()
