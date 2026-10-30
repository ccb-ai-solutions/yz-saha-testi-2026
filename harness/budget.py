"""Offline request / token / cost estimates — what a command WOULD send, computed without any network call.

Every command that talks to a provider (probe, run, judge, ...) prints this table first and only sends
requests when called with --yes.  The numbers are estimates:
  * prompt tokens = characters / CHARS_PER_TOKEN (3.0 for Turkish: deliberately on the high side);
  * completion tokens = typical answer length per suite + a thinking allowance when the variant thinks
    (from the capability probe; an unprobed model is assumed to think — the conservative side);
  * cost = tokens × the price in configs/models.yaml (`price:`), per currency; EVREN is free until
    2026-11-01 and is reported against its 15M tokens/day quota instead.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from .capabilities import load_caps
from .config import Registry, Variant

CHARS_PER_TOKEN = 3.0
# Measured on the smoke test of 10-11 Oct (prompt characters per prompt token the server reported, Turkish RAG
# prompts): Claude (the judge) 2.08, DeepSeek V4 / V4.1 Flash 2.33-2.36; every other model 2.9-3.6, i.e. the default.
FAMILY_CHARS_PER_TOKEN = {"anthropic": 2.1, "deepseek": 2.35}
# suite -> (answer tokens, extra tokens when the model thinks at default effort).  judge: Opus 5.5 wrote 415 tokens per
# verdict on average on the smoke test (max 772).
COMPLETION_GUESS = {
    "rag_qa": (250, 900), "mcq": (40, 800), "ifeval": (350, 800), "tools": (150, 600), "decision": (80, 600),
    "docextract": (350, 700), "guard": (12, 0), "guard_policy": (60, 500), "judge": (450, 0),
    "latency_chat": (150, 700), "latency_rag": (300, 900), "latency_long": (1100, 900), "warmup": (40, 200),
    "probe": (200, 1500),
}
THINK_MIN_FRACTION = 0.3


def prompt_factor(reg: Registry, model_id: str) -> float:
    """Prompt tokens of this model relative to the CHARS_PER_TOKEN estimate (its tokenizer on Turkish)."""
    cpt = FAMILY_CHARS_PER_TOKEN.get(reg.model(model_id).family or "")
    return CHARS_PER_TOKEN / cpt if cpt else 1.0


def tok(obj: Any) -> int:
    if obj is None:
        return 0
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return int(math.ceil(len(s) / CHARS_PER_TOKEN))


@dataclass
class Line:
    what: str
    system: str
    endpoint: str
    requests: int
    tokens_in: int
    tokens_out: int
    cost: float | None = None
    currency: str | None = None
    note: str = ""


@dataclass
class Budget:
    title: str
    lines: list[Line] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, line: Line) -> None:
        self.lines.append(line)

    def totals(self, reg: Registry) -> dict:
        by_ep: dict[str, dict] = {}
        for l in self.lines:
            e = by_ep.setdefault(l.endpoint, {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cost": {}})
            e["requests"] += l.requests
            e["tokens_in"] += l.tokens_in
            e["tokens_out"] += l.tokens_out
            if l.cost is not None and l.currency:
                e["cost"][l.currency] = e["cost"].get(l.currency, 0.0) + l.cost
        for name, e in by_ep.items():
            ep = reg.endpoints.get(name)
            daily = ((ep.limits or {}).get("daily_tokens") if ep else None)
            if daily:
                e["quota_days"] = (e["tokens_in"] + e["tokens_out"]) / (daily * 0.9)
        return by_ep

    def render(self, reg: Registry) -> str:
        out = [f"== {self.title}", f"{'what':22} {'system':34} {'endpoint':13} {'requests':>8} {'tokens in':>11} "
               f"{'tokens out':>11} {'est. cost':>12}  note"]
        for l in self.lines:
            cost = "free" if l.currency == "FREE" else (f"{l.cost:,.2f} {l.currency}" if l.cost is not None else "– (no price)")
            out.append(f"{l.what[:22]:22} {l.system[:34]:34} {l.endpoint[:13]:13} {l.requests:>8,} {l.tokens_in:>11,} "
                       f"{l.tokens_out:>11,} {cost:>12}  {l.note}")
        out.append("-- totals per endpoint")
        for name, e in self.totals(reg).items():
            money = ", ".join(f"{v:,.2f} {c}" for c, v in e["cost"].items() if c != "FREE") or "–"
            q = f" | ≈{e['quota_days']:.2f} days of the daily quota" if "quota_days" in e else ""
            out.append(f"   {name:13} {e['requests']:>8,} requests  {e['tokens_in'] + e['tokens_out']:>12,} tokens  "
                       f"cost {money}{q}")
        out += [f"   note: {n}" for n in self.notes]
        return "\n".join(out)

    def as_dict(self, reg: Registry) -> dict:
        return {"title": self.title, "lines": [l.__dict__ for l in self.lines], "totals": self.totals(reg),
                "notes": self.notes}


def price_line(reg: Registry, model_id: str, tokens_in: int, tokens_out: int, *, seconds: float = 0.0,
               queries: int = 0, batch: bool = False) -> tuple[float | None, str | None]:
    spec = reg.model(model_id)
    p = spec.price or {}
    if p.get("free_until") or p.get("free"):
        return 0.0, "FREE"
    if not any(k in p for k in ("in", "out", "per_second", "per_1k_queries")):
        return None, None                      # e.g. a self-hosted deployment: GPU-hours, not tokens
    cur = p.get("currency", "USD")
    cost = (tokens_in * float(p.get("in", 0)) + tokens_out * float(p.get("out", 0))) / 1e6
    cost += seconds * float(p.get("per_second", 0)) + queries * float(p.get("per_1k_queries", 0)) / 1000
    if batch:
        cost *= float(p.get("batch", 1.0))     # e.g. Anthropic Message Batches: batch: 0.5
    return cost, cur


def variant_thinks(reg: Registry, v: Variant) -> bool | None:
    """True / False from the probe, None if unknown (unprobed model, default mode)."""
    if v.mode == "nothink":
        return False
    if v.mode in ("think", "think_min"):
        return True
    spec = reg.model(v.model_id)
    caps = load_caps(reg, v.model_id)
    if caps.data.get("assumed"):
        hint = (spec.thinking or {}).get("default_thinks")
        return hint if isinstance(hint, bool) else None
    return bool(caps.data.get("default_thinks"))


def completion_for(kind: str, thinks: bool | None, mode: str) -> int:
    ans, extra = COMPLETION_GUESS[kind]
    if thinks is False:
        return ans
    if mode == "think_min":
        return ans + int(extra * THINK_MIN_FRACTION)
    return ans + extra


def chat_plan(reg: Registry, b: Budget, suite: str, kind: str, plan: list[dict], variants: list[Variant], *,
              followup_frac: float = 0.0) -> None:
    sent = [it for it in plan if not it.get("skip_request")]
    p_in = sum(tok(it["messages"]) + tok(it.get("tools")) for it in sent)
    n = len(sent)
    for v in variants:
        spec = reg.model(v.model_id)
        th = variant_thinks(reg, v)
        per_out = completion_for(kind, th, v.mode)
        n_req = n + int(round(n * followup_frac))
        t_in = int(round((p_in + p_in * followup_frac * 1.15) * prompt_factor(reg, v.model_id)))
        t_out = per_out * n_req
        cost, cur = price_line(reg, v.model_id, t_in, t_out)
        note = "" if (th is not None or COMPLETION_GUESS[kind][1] == 0) else "not probed — default mode counted as thinking"
        b.add(Line(suite, v.key, spec.endpoint, n_req, t_in, t_out, cost, cur, note))


def probe_lines(reg: Registry, b: Budget, model_ids: list[str]) -> None:
    for mid in model_ids:
        spec = reg.model(mid)
        if spec.task not in ("chat", "guard"):
            continue
        caps = load_caps(reg, mid)
        thinks = True if caps.data.get("assumed") else bool(caps.data.get("default_thinks"))
        th = spec.thinking or {}
        configured = th.get("budget_min") or []
        configured = configured if isinstance(configured, list) else [configured]
        if spec.task == "guard" or spec.is_router:
            n, out = 3, 60
        elif th.get("budget_probe") or configured:
            # base probe 14 + budget reference 2 + 2 per candidate (documented ones; all 7 generic with budget_probe)
            n_cand = len(configured) + (7 if th.get("budget_probe") or not configured else 0)
            n, out = 16 + 2 * n_cand, 1200 if thinks else 300
        else:
            n, out = 14, 1200 if thinks else 300
        t_in, t_out = n * 90, n * out
        cost, cur = price_line(reg, mid, t_in, t_out)
        b.add(Line("probe (typical)", mid, spec.endpoint, n, t_in, t_out, cost, cur,
                   "worst case ≈2.5× if no thinking switch is found"))


# ----------------------------------------------------------------------------- campaign report as Markdown
def _money(cost: dict) -> str:
    parts = [f"{v:,.2f} {c}" for c, v in cost.items() if c != "FREE"]
    if parts:
        return ", ".join(parts)
    return "free" if "FREE" in cost else "–"


def campaign_markdown(report: dict, *, generated: str = "") -> str:
    """Render the dict written by `run.py budget --out` as Markdown tables (one per phase + grand total)."""
    md = [f"_Generated by `python harness/run.py budget --campaign {report.get('campaign')}`"
          + (f" on {generated}" if generated else "") + ". Offline estimate — nothing was sent. "
          "A probed model counts only the modes its probe found; an unprobed model counts every mode it could have, with "
          "its default mode assumed to think (upper bound)._", ""]
    for ph in report.get("phases", []):
        md += [f"### {ph['name']}", ""]
        if ph.get("why"):
            md += [ph["why"], ""]
        md += ["| Command (append `--yes` to execute) | Endpoint | Requests | Tokens in | Tokens out | Est. cost |",
               "|---|---|---:|---:|---:|---:|"]
        sub: dict[str, dict] = {}
        for c in ph.get("commands", []):
            b = c.get("budget")
            if not b:
                md.append(f"| `{c['command']}` | – | – | – | – | no tokens |")
                continue
            first = True
            for ep, t in b.get("totals", {}).items():
                q = f" (≈{t['quota_days']:.2f} quota days)" if "quota_days" in t else ""
                md.append(f"| {'`' + c['command'] + '`' if first else '″'} | {ep} | {t['requests']:,} | "
                          f"{t['tokens_in']:,} | {t['tokens_out']:,} | {_money(t.get('cost', {}))}{q} |")
                first = False
                s = sub.setdefault(ep, {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cost": {}})
                s["requests"] += t["requests"]
                s["tokens_in"] += t["tokens_in"]
                s["tokens_out"] += t["tokens_out"]
                for cur, v in (t.get("cost") or {}).items():
                    s["cost"][cur] = s["cost"].get(cur, 0.0) + v
        for ep, s in sub.items():
            md.append(f"| **phase total** | {ep} | **{s['requests']:,}** | **{s['tokens_in']:,}** | **{s['tokens_out']:,}** | "
                      f"**{_money(s['cost'])}** |")
        md.append("")
    md += ["### Grand total per endpoint", "", "| Endpoint | Requests | Tokens (in + out) | Est. cost |", "|---|---:|---:|---:|"]
    for ep, g in (report.get("grand_total") or {}).items():
        md.append(f"| {ep} | {g['requests']:,} | {g['tokens']:,} | {_money(g.get('cost', {}))} |")
    return "\n".join(md) + "\n"
