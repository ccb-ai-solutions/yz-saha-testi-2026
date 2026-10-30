"""Capability discovery ("probe") and request construction with fallbacks.

Gateways differ in what they accept: max_tokens vs max_completion_tokens,
whether temperature/top_p/seed/stream_options are allowed, and — most
importantly — HOW thinking is switched off or budgeted.  EVREN documents that
DeepSeek's thinking budget is adjustable (1–100) but not the parameter name, so
the probe tries known encodings and keeps the one whose *measured effect* is
right (reasoning disappears / shrinks).  Results are cached in
results/capabilities/<endpoint>/<model>.json and recorded in every run manifest.

Nothing measured here enters any benchmark score.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import OK, RETRYABLE, TRUNCATED, ApiClient, ChatResult, RetryPolicy, deep_merge, utc_now, with_retries
from .config import RESULTS_DIR, ROOT, ModelSpec, Registry
from .textnorm import turkish_ratio

PROBE_TTL_S = 24 * 3600

# Candidate encodings, tried in order.  Overridable per model in configs/models.yaml (thinking.*).
OFF_CANDIDATES: list[dict] = [
    {"chat_template_kwargs": {"enable_thinking": False}},     # Qwen3 / GLM on vLLM & SGLang
    {"chat_template_kwargs": {"thinking": False}},            # DeepSeek V3.x templates
    {"thinking": {"type": "disabled"}},                       # DeepSeek / Z.ai / Anthropic-style APIs
    {"reasoning_effort": "none"},                             # OpenAI-style
    {"reasoning": {"enabled": False}},                        # OpenRouter-style
    {"enable_thinking": False},                               # DashScope-style
    {"reasoning_effort": "minimal"},
]
ON_CANDIDATES: list[dict] = [
    {"chat_template_kwargs": {"enable_thinking": True}},
    {"chat_template_kwargs": {"thinking": True}},
    {"thinking": {"type": "enabled"}},
    {"reasoning_effort": "medium"},
    {"reasoning_effort": "high"},                             # Mistral Small 4: none | high
    {"enable_thinking": True},
]
BUDGET_MIN_CANDIDATES: list[dict] = [
    {"thinking_budget": 1},
    {"reasoning": {"budget": 1}},
    {"thinking": {"type": "enabled", "budget": 1}},
    {"chat_template_kwargs": {"thinking_budget": 1}},
    {"reasoning_effort": 1},
    {"reasoning_effort": "low"},
    {"thinking": {"type": "enabled", "budget_tokens": 1024}},
]

PING = [{"role": "user", "content": "Merhaba! Türkiye'nin başkenti neresidir? Tek kelimeyle yanıt ver."}]
REASONING_PROMPT = [{"role": "user", "content": (
    "Bir ürünün fiyatı önce %20 artırılıyor, ardından yeni fiyat üzerinden %20 indirim yapılıyor. "
    "Son fiyat ilk fiyata göre yüzde kaç değişmiştir? Sonucu tek cümleyle yaz.")}]
# think_min is judged on a prompt that needs a few hundred reasoning tokens: on REASONING_PROMPT several models think
# only 40-60 tokens, too little to see a budget switch work (probe of 10 Oct).  Answer: 6 A, 12 B, 12 C.
BUDGET_PROMPT = [{"role": "user", "content": (
    "Bir depoda üç tür koli var: A kolileri 12 kg, B kolileri 7 kg, C kolileri 5 kg. Toplam 30 koli var ve toplam "
    "ağırlık 216 kg. B kolilerinin sayısı A kolilerinin sayısının iki katı. Her türden kaç koli vardır? "
    "Sonucu tek cümleyle yaz.")}]
TOOL_PROMPT = [{"role": "user", "content": "Bugünkü Euro kurunu öğrenebilir misin?"}]
TOOL_DEF = [{"type": "function", "function": {
    "name": "doviz_kuru_getir", "description": "Belirtilen para biriminin TL karşısındaki güncel kurunu döndürür.",
    "parameters": {"type": "object", "properties": {"para_birimi": {"type": "string", "enum": ["USD", "EUR", "GBP"]}},
                   "required": ["para_birimi"]}}}]

PARAM_NAMES = ("max_tokens", "max_completion_tokens", "top_p", "temperature", "seed", "stream_options", "top_k",
               "tool_choice", "parallel_tool_calls")
# parameters that may be dropped WITHOUT the server naming them (they do not change the output distribution)
SAFE_BLIND_DROPS = ("stream_options", "seed")


def reasoning_tokens(res: ChatResult) -> tuple[int, bool]:
    """(reasoning_tokens, exact?) — exact when the server reports it, else estimated from characters.

    A reported 0 next to non-empty reasoning text is not trusted (OVHcloud's gpt-oss-120b, 10 Oct: reasoning_tokens 0
    with 270-360 characters of reasoning), so the think_min test compares estimates instead of 0 vs 0."""
    u = res.usage or {}
    det = u.get("completion_tokens_details") or {}
    if isinstance(det, dict) and isinstance(det.get("reasoning_tokens"), int) \
            and not (det["reasoning_tokens"] == 0 and (res.reasoning or "").strip()):
        return det["reasoning_tokens"], True
    ct = u.get("completion_tokens")
    rc, cc = len(res.reasoning), len(res.content)
    if isinstance(ct, int) and rc + cc > 0:
        return int(round(ct * rc / (rc + cc))), False
    return int(round(rc / 3.2)), False   # ~3.2 chars/token for Turkish in modern tokenizers (estimate only)


@dataclass
class Capabilities:
    data: dict

    @property
    def token_param(self) -> str:
        return self.data.get("token_param", "max_tokens")

    @property
    def drop(self) -> set:
        return set(self.data.get("drop_params", []))

    def mode(self, mode: str) -> dict | None:
        m = (self.data.get("modes") or {}).get(mode)
        return m if m and m.get("available") else None

    @staticmethod
    def default_for(spec: ModelSpec) -> "Capabilities":
        """Assumed capabilities when no probe exists (e.g. dry runs, mock server)."""
        modes = {"default": {"available": True, "params": {}}}
        th = spec.thinking or {}
        for mode, key in (("nothink", "off"), ("think", "on"), ("think_min", "budget_min")):
            if th.get(key):
                params = th[key][0] if isinstance(th[key], list) else th[key]     # budget_min may list candidates
                modes[mode] = {"available": True, "params": params, "source": "config (unverified)"}
        return Capabilities({"token_param": "max_tokens", "drop_params": [], "modes": modes, "assumed": True})


# nothink must remove the thinking, not move it: with chat_template_kwargs.enable_thinking=false GLM-5.3 on EVREN
# (smoke test, 11 Oct) left the reasoning field empty but wrote its English reasoning into the answer, in front of
# the Turkish reply.  So the answer under the switch must stay about as long as the default answer (≤ 3× + 300
# characters) and, when the default answer is Turkish, stay Turkish.
NOTHINK_RULE = "no-reasoning-and-answer-not-inflated"


def nothink_leak(base: dict, st: dict) -> str | None:
    """None when the answers under an off-switch look like plain answers, else what gave the leak away."""
    def med(xs):
        xs = sorted(x for x in (xs or []) if x is not None)
        return xs[len(xs) // 2] if xs else None
    d_len, c_len = med(base.get("content_chars")), med(st.get("content_chars"))
    if d_len is not None and c_len is not None and c_len > max(3 * d_len, d_len + 300):
        return f"the answers grew from ~{d_len} to ~{c_len} characters"
    d_tr, c_tr = med(base.get("content_turkish_ratio")), med(st.get("content_turkish_ratio"))
    if d_tr is not None and c_tr is not None and d_tr >= 0.6 and c_tr < 0.6:
        return f"the answers stopped being Turkish (Turkish share {d_tr:.2f} → {c_tr:.2f})"
    return None


# think_min is accepted only when EVERY run with the switch stays at or below `need` × the SHORTEST reference run
# (documented switch: 0.7, guessed: 0.5).  Comparing means let one long reference run pass a switch the server
# ignores: NVIDIA's Nemotron 3.5 Lightning, 11 Oct — reference [732, 2436], {"reasoning": {"budget": 1}} [691, 692];
# the 5-question RAG smoke then reasoned more with the "switch" than without it.
BUDGET_RULE = "every-run-below-shortest-reference"


def budget_verdict(ref_runs: list, cand_runs: list, need: float) -> str | None:
    """None if the candidate passes, else the reason in words."""
    ref_runs = [int(x) for x in ref_runs or []]
    cand_runs = [int(x) for x in cand_runs or []]
    if not ref_runs or not cand_runs:
        return "no runs to compare"
    if sum(ref_runs) / len(ref_runs) < 64:
        return "the reference thinks too little to measure a budget switch"
    if max(cand_runs) > need * min(ref_runs):
        return f"every run must be <= {need:.0%} of the shortest reference run ({min(ref_runs)})"
    return None


def recheck_think_min(data: dict) -> dict:
    """Apply the current acceptance rule to think_min evidence stored by an older probe (no new request)."""
    tm = (data.get("modes") or {}).get("think_min")
    if not isinstance(tm, dict) or not tm.get("available") or tm.get("rule") == BUDGET_RULE:
        return data
    ref = (tm.get("reference") or {}).get("reasoning_tokens")
    cand = (tm.get("evidence") or {}).get("reasoning_tokens")
    if not ref or not cand:                          # older evidence without per-run numbers: keep as probed
        return data
    why = budget_verdict(ref, cand, 0.7 if tm.get("documented") else 0.5)
    modes = dict(data["modes"])
    if why:
        modes["think_min"] = {"available": False, "superseded": tm,
                              "reason": f"re-checked with the current rule: runs {cand} vs reference {ref} — {why}"}
    else:
        modes["think_min"] = {**tm, "rule": BUDGET_RULE}
    return {**data, "modes": modes}


def caps_path(endpoint: str, model_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", model_id)
    return RESULTS_DIR / "capabilities" / endpoint / f"{safe}.json"


def load_caps(reg: Registry, model_id: str, *, allow_default: bool = True) -> Capabilities:
    spec = reg.model(model_id)
    p = caps_path(spec.endpoint, model_id)
    if p.exists():
        return Capabilities(recheck_think_min(json.loads(p.read_text(encoding="utf-8"))))
    if allow_default:
        return Capabilities.default_for(spec)
    raise FileNotFoundError(f"no capability probe for {model_id}; run: python harness/run.py probe --models {model_id}")


# In a mode's params (configs/models.yaml thinking.on / off / budget_min): prepend this vendor system prompt instead of
# sending a field — e.g. Magistral reasons only when Mistral's reasoning prompt is in the system message.
SYSTEM_PREFIX_KEY = "_system_prefix_file"


def with_system_prefix(messages: list, prefix_file: str) -> list:
    prefix = (ROOT / prefix_file).read_text(encoding="utf-8").strip()
    messages = [dict(m) for m in messages]
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = prefix + "\n\n" + (messages[0].get("content") or "")
    else:
        messages = [{"role": "system", "content": prefix}] + messages
    return messages


_MISTRAL_TOOL_ID = re.compile(r"[A-Za-z0-9]{9}")


def uses_mistral_tool_ids(spec: ModelSpec) -> bool:
    """Mistral's chat template (Bedrock's Magistral, vLLM with the mistral tokenizer, Mistral's API) accepts only
    tool-call ids of exactly 9 letters/digits.  On for family 'mistral'; options.tool_call_ids: mistral | standard."""
    opt = (spec.options or {}).get("tool_call_ids")
    return opt == "mistral" or (opt is None and spec.family == "mistral")


def mistral_tool_ids(messages: list) -> tuple[list, bool]:
    """Map every other tool-call id in a conversation to a stable 9-character form ('c' + 8 hex digits), in the
    assistant's tool_calls and in the tool messages that answer them alike.  The plan keeps the original ids.
    (13 Oct: 'Tool call id was call_1 but must be a-z, A-Z, 0-9, with a length of 9' on 15 Bedrock requests.)"""
    def fix(i) -> str:
        s = str(i or "")
        return s if _MISTRAL_TOOL_ID.fullmatch(s) else "c" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]

    changed = False
    out = []
    for m in messages:
        m2 = m
        if isinstance(m, dict) and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                if isinstance(tc, dict) and "id" in tc and not _MISTRAL_TOOL_ID.fullmatch(str(tc["id"])):
                    tc = {**tc, "id": fix(tc["id"])}
                    changed = True
                calls.append(tc)
            m2 = {**m2, "tool_calls": calls}
        if isinstance(m, dict) and m.get("role") == "tool" and "tool_call_id" in m \
                and not _MISTRAL_TOOL_ID.fullmatch(str(m["tool_call_id"])):
            m2 = {**m2, "tool_call_id": fix(m["tool_call_id"])}
            changed = True
        out.append(m2)
    return out, changed


def build_chat_payload(spec: ModelSpec, caps: Capabilities, mode: str, messages: list, *, max_tokens: int,
                       decoding: dict, seed: int | None = None, stream: bool = True, tools: list | None = None,
                       tool_choice: Any = None, extra: dict | None = None) -> tuple[dict, dict]:
    """Return (payload, effective_params) — effective_params is what we record for fairness audits."""
    m = caps.mode(mode)
    if m is None:
        raise ValueError(f"mode '{mode}' not available for {spec.id}")
    mode_params = dict(m.get("params") or {})
    extra = dict(extra or {})
    prefix_file = ((spec.options or {}).get("system_prefix_file")      # vendor prompt for every mode (opt-in)
                   or mode_params.pop(SYSTEM_PREFIX_KEY, None) or extra.pop(SYSTEM_PREFIX_KEY, None))
    if prefix_file:
        messages = with_system_prefix(messages, prefix_file)
    ids_mapped = False
    if uses_mistral_tool_ids(spec):
        messages, ids_mapped = mistral_tool_ids(messages)
    p: dict = {"model": spec.api_model, "messages": messages, "stream": stream}
    min_mt = caps.data.get("min_max_tokens")
    mt = max(max_tokens, min_mt) if isinstance(min_mt, int) else max_tokens
    p[caps.token_param] = mt
    for k in ("temperature", "top_p", "top_k"):
        if k in decoding and k not in caps.drop:
            p[k] = decoding[k]
    if seed is not None and "seed" not in caps.drop:
        p["seed"] = seed
    if stream and "stream_options" not in caps.drop:
        p["stream_options"] = {"include_usage": True}
    if tools:
        p["tools"] = tools
        if tool_choice is not None:
            p["tool_choice"] = tool_choice
    p = deep_merge(p, spec.extra_body or {})
    p = deep_merge(p, mode_params)
    p = deep_merge(p, extra)
    eff = {k: v for k, v in p.items() if k not in ("messages", "tools", "stream", "model")}
    eff["mode"] = mode
    if prefix_file:
        eff["system_prefix_file"] = prefix_file
    if ids_mapped:
        eff["tool_call_ids"] = "mistral (9 letters/digits)"
    # provider spellings of standard parameters (Mistral: seed -> random_seed); eff keeps the standard names
    aliases = {k: v for k, v in ((spec.options or {}).get("param_aliases") or {}).items() if k in p}
    for k, v in aliases.items():
        p[v] = p.pop(k)
    if aliases:
        eff["param_aliases"] = aliases
    return p, eff


# ----------------------------------------------------------------------------- probe
class Prober:
    def __init__(self, reg: Registry, client: ApiClient, spec: ModelSpec, log=print, governor=None):
        self.reg, self.client, self.spec, self.log = reg, client, spec, log
        self.governor = governor               # shared per endpoint: requests-per-minute cap (limits.rpm)
        self.data: dict = {"model_id": spec.id, "endpoint": spec.endpoint, "api_model": spec.api_model,
                           "probed_at": utc_now(), "token_param": "max_tokens", "drop_params": [],
                           "modes": {"default": {"available": True, "params": {}}}, "log": []}
        self.requests = 0

    def note(self, msg: str) -> None:
        self.data["log"].append(msg)
        self.log(f"  [{self.spec.id}] {msg}")

    def _payload(self, messages: list, extra: dict | None = None, max_tokens: int = 2048, tools=None) -> dict:
        caps = Capabilities(self.data)
        # seed varies per probe request so repeated probes are never answered from a response cache
        payload, _ = build_chat_payload(self.spec, caps, "default", messages, max_tokens=max_tokens,
                                        decoding={"temperature": 0.6, "top_p": 0.95}, seed=1234 + self.requests,
                                        stream=True, tools=tools, tool_choice=None, extra=extra)
        return payload

    async def _send(self, messages, extra=None, max_tokens=2048, tools=None) -> ChatResult:
        """Probe requests are not measurements: infrastructure errors (429/503/timeouts) are retried."""
        payload = self._payload(messages, extra, max_tokens, tools)

        async def once():
            if self.governor is not None:
                await self.governor.acquire(0)
            self.requests += 1
            return await self.client.stream_chat(payload, ttft_timeout=240, total_timeout=420)
        return await with_retries(once, RetryPolicy(max_attempts=5, base_delay=2.0, rate_limit_min_wait=10.0))

    async def _negotiate_params(self) -> ChatResult | None:
        """Send the ping; on 400 'param' errors adjust token_param/drop_params and retry."""
        tried_drop: list[str] = []
        for _ in range(8):
            res = await self._send(PING)
            if res.outcome in (OK, TRUNCATED) or res.outcome.startswith("truncated") or res.outcome == "empty_answer":
                return res
            if not (res.outcome.startswith("http_400") or res.outcome == "http_422"):   # 422: e.g. Mistral's extra_forbidden
                self.note(f"ping failed: {res.outcome} {res.error}")
                return res
            msg = (res.error or "").lower()
            m = re.search(r"max_tokens.*(?:at least|minimum|>=|en az)\D*(\d+)", msg)
            if m:
                self.data["min_max_tokens"] = int(m.group(1))
                self.note(f"server requires max_tokens >= {m.group(1)}")
                continue
            named = [p for p in PARAM_NAMES if re.search(rf"(?<![a-z_]){p}(?![a-z_])", msg)]
            if res.outcome == "http_422" and not named:      # a 422 is negotiated only when it names a parameter
                self.note(f"ping failed: {res.outcome} {res.error}")
                return res
            if "max_tokens" in named and self.data["token_param"] == "max_tokens" and "max_completion_tokens" in msg:
                self.data["token_param"] = "max_completion_tokens"
                self.note("switched to max_completion_tokens")
                continue
            # drop only what the server names; when both sampling knobs are named (e.g. "temperature and top_p
            # cannot both be set") keep temperature and drop top_p (PARAM_NAMES order)
            target = next((p for p in named if p not in ("max_tokens", "max_completion_tokens")
                           and p not in self.data["drop_params"]), None)
            if target is None:
                target = next((p for p in SAFE_BLIND_DROPS if p not in self.data["drop_params"]
                               and p not in tried_drop), None)
                if target is not None:
                    self.note(f"unexplained 400 — trying without '{target}' (does not change sampling)")
            if target is None:
                if self.data["token_param"] == "max_tokens":
                    self.data["token_param"] = "max_completion_tokens"
                    self.note("unexplained 400: trying max_completion_tokens")
                    continue
                self.note(f"giving up parameter negotiation (sampling parameters are never dropped unless the "
                          f"server names them): {res.error}")
                return res
            tried_drop.append(target)
            self.data["drop_params"].append(target)
            self.note(f"dropping unsupported parameter '{target}' ({res.error[:120] if res.error else ''})")
        return None

    async def _reasoning_stats(self, extra: dict | None, n: int = 2, prompt: list | None = None,
                               max_tokens: int = 4096) -> dict:
        toks, chars, ok, outcomes, field = [], [], 0, [], None
        content_chars: list = []
        content_tr: list = []
        fields: set = set()
        exact_all = True
        for _ in range(n):
            r = await self._send(prompt or REASONING_PROMPT, extra=extra, max_tokens=max_tokens)
            outcomes.append(r.outcome)
            if r.outcome.startswith("http_"):
                return {"rejected": True, "outcome": r.outcome, "error": (r.error or "")[:300]}
            rt, exact = reasoning_tokens(r)
            exact_all &= exact
            stripped = len(r.reasoning.strip())                 # whitespace-only "thinking" is not thinking
            toks.append(rt if stripped else 0)
            chars.append(stripped)
            if stripped:
                fields.add(r.reasoning_field or "unknown")
            field = field or (r.reasoning_field if stripped else None)
            ok += int(bool(r.content.strip()))
            content_chars.append(len(r.content.strip()))
            content_tr.append(round(turkish_ratio(r.content), 3) if r.content.strip() else None)
        return {"rejected": False, "reasoning_tokens": toks, "reasoning_tokens_exact": exact_all,
                "reasoning_chars": chars, "answers": ok, "n": n, "outcomes": outcomes, "reasoning_field": field,
                "reasoning_fields": sorted(fields), "content_chars": content_chars, "content_turkish_ratio": content_tr}

    async def run(self) -> dict:
        spec = self.spec
        t0 = time.perf_counter()
        self.log(f"probing {spec.id} ({spec.api_model}) on {spec.endpoint}")
        ping = await self._negotiate_params()
        if ping is None or ping.outcome.startswith("http_") or ping.outcome.startswith(("timeout", "conn", "stream")):
            self.data["available"] = False
            self.data["error"] = None if ping is None else f"{ping.outcome}: {ping.error}"
            if ping is not None and (ping.outcome in RETRYABLE or ping.outcome == "http_429_quota"
                                     or ping.outcome.startswith(("timeout", "conn", "stream"))):
                # busy / rate-limited / out of quota / network: not a property of the model — never cached, so the
                # next probe (or run) tries again instead of trusting a stale "unavailable"
                self.data["transient"] = True
                self.note(f"transient failure ({ping.outcome}) — not saved; probe this model again later")
                return self._finish(t0)
            return self._save(t0)
        self.data["available"] = True
        self.data["served_model"] = ping.served_model
        self.data["usage_in_stream"] = bool(ping.usage)
        self.data["keepalive_seen"] = ping.n_keepalive > 0
        th = spec.thinking or {}
        if spec.is_router:
            self.note("router ('auto'): only the default mode is meaningful")
            return self._save(t0)
        if spec.task != "chat":
            self.note(f"task '{spec.task}': parameter negotiation only")
            return self._save(t0)

        base = await self._reasoning_stats(None)
        if base.get("rejected"):
            self.note(f"reasoning probe failed: {base}")
            return self._save(t0)
        default_thinks = (sum(base.get("reasoning_chars", [0])) > 0)
        self.data["default_thinks"] = default_thinks
        self.data["default_reasoning"] = base
        self.data["reasoning_field"] = base.get("reasoning_field")
        self.data["reasoning_format"] = base.get("reasoning_fields")     # e.g. ['reasoning_content'] or ['inline_think_tags']
        self.note(f"default mode thinks: {default_thinks} (reasoning field(s): {base.get('reasoning_fields')})")

        # ---- thinking OFF / ON
        if default_thinks:
            cands = [th["off"]] if th.get("off") else OFF_CANDIDATES
            for c in cands:
                st = await self._reasoning_stats(c)
                if st.get("rejected"):
                    self.note(f"off-candidate rejected {c}: {st['outcome']}")
                    continue
                leak = nothink_leak(base, st)
                if sum(st["reasoning_chars"]) == 0 and st["answers"] == st["n"] and not leak:
                    self.data["modes"]["nothink"] = {"available": True, "params": c, "evidence": st,
                                                     "rule": NOTHINK_RULE}
                    self.note(f"nothink via {c}")
                    break
                if leak:
                    self.note(f"off-candidate {c} emptied the reasoning field but {leak} — the thinking moved into "
                              "the answer; not accepted")
                elif sum(st["reasoning_chars"]) == 0:
                    self.note(f"off-candidate {c} removed the reasoning but {st['n'] - st['answers']} of {st['n']} "
                              f"answers were empty ({st['outcomes']}) — not accepted")
                else:
                    self.note(f"off-candidate had no effect {c}: reasoning chars {st['reasoning_chars']}")
            else:
                self.data["modes"]["nothink"] = {"available": False, "reason": "no candidate removed reasoning"}
        else:
            cands = [th["on"]] if th.get("on") else ON_CANDIDATES
            for c in cands:
                st = await self._reasoning_stats(c)
                if st.get("rejected"):
                    self.note(f"on-candidate rejected {c}: {st['outcome']}")
                    continue
                if sum(st["reasoning_chars"]) > 0 and st["answers"] == st["n"]:
                    self.data["modes"]["think"] = {"available": True, "params": c, "evidence": st}
                    self.note(f"think via {c}")
                    break
                self.note(f"on-candidate had no effect {c}: reasoning chars {st['reasoning_chars']}, "
                          f"answers {st['answers']}/{st['n']}")
            else:
                self.data["modes"]["think"] = {"available": False, "reason": "model does not expose thinking"}

        # ---- thinking budget (only meaningful for thinking-by-default models)
        want_budget = th.get("budget_min") or th.get("budget_probe")
        if default_thinks and want_budget:
            configured = th.get("budget_min")
            configured = [] if not configured else (configured if isinstance(configured, list) else [configured])
            cands = [(c, True) for c in configured]
            if th.get("budget_probe") or not configured:
                cands += [(c, False) for c in BUDGET_MIN_CANDIDATES if c not in configured]
            ref_st = await self._reasoning_stats(None, prompt=BUDGET_PROMPT, max_tokens=8192)
            ref_runs = ref_st.get("reasoning_tokens") or [0]
            ref = sum(ref_runs) / max(1, len(ref_runs))
            if ref_st.get("rejected"):
                self.note(f"budget reference failed: {ref_st}")
                cands = []
            for c, documented in cands:
                st = await self._reasoning_stats(c, prompt=BUDGET_PROMPT, max_tokens=8192)
                if st.get("rejected"):
                    self.note(f"budget-candidate rejected {c}: {st['outcome']}")
                    continue
                need = 0.7 if documented else 0.5     # documented switch: >=30% fewer tokens; a guess: >=50%
                verdict = budget_verdict(ref_runs, st["reasoning_tokens"], need)
                if verdict is None and st["answers"] == st["n"] and sum(st["reasoning_chars"]) > 0:
                    self.data["modes"]["think_min"] = {"available": True, "params": c, "evidence": st,
                                                       "reference": ref_st, "reference_mean_reasoning_tokens": ref,
                                                       "documented": documented, "rule": BUDGET_RULE}
                    self.note(f"think_min via {c} (runs {st['reasoning_tokens']} vs reference {ref_runs} reasoning "
                              f"tokens on the budget prompt)")
                    break
                if sum(st["reasoning_chars"]) == 0:
                    self.note(f"budget-candidate {c} removed the reasoning entirely — that is nothink, not think_min")
                else:
                    self.note(f"budget-candidate no clear effect {c}: runs {st['reasoning_tokens']} vs reference "
                              f"{ref_runs} reasoning tokens ({verdict or 'an answer was missing'}; answers "
                              f"{st['answers']}/{st['n']})")
            else:
                self.data["modes"]["think_min"] = {
                    "available": False, "reference_mean_reasoning_tokens": ref,
                    "reason": ("the budget reference request failed" if ref_st.get("rejected") else
                               "the default mode thinks too little on the budget prompt to measure a budget switch"
                               if ref < 64 else "no candidate reduced reasoning enough — set thinking.budget_min in "
                               "models.yaml from the provider's documentation")}

        # ---- native tool calling
        tr = await self._send(TOOL_PROMPT, tools=TOOL_DEF, max_tokens=2048)
        if tr.outcome.startswith("http_400"):
            self.data["tools"] = {"native": False, "reason": tr.error}
        else:
            ok = bool(tr.tool_calls) and tr.tool_calls[0].get("name") == "doviz_kuru_getir"
            args_ok = False
            if ok:
                try:
                    args_ok = json.loads(tr.tool_calls[0]["arguments"] or "{}").get("para_birimi") == "EUR"
                except json.JSONDecodeError:
                    args_ok = False
            self.data["tools"] = {"native": ok, "arguments_ok": args_ok,
                                  "textual_json": (not ok) and ("doviz_kuru_getir" in tr.content)}
        self.note(f"tools: {self.data['tools']}")
        return self._save(t0)

    def _finish(self, t0: float) -> dict:
        self.data["probe_requests"] = self.requests
        self.data["probe_seconds"] = round(time.perf_counter() - t0, 1)
        return self.data

    def _save(self, t0: float) -> dict:
        self._finish(t0)
        p = caps_path(self.spec.endpoint, self.spec.id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.data


async def probe_models(reg: Registry, model_ids: list[str], *, reprobe: bool = False, log=print,
                       concurrency: int = 4) -> dict:
    """Probe several models; up to `concurrency` models per endpoint at a time (an endpoint's
    limits.max_concurrency lowers that, e.g. Ollama Free = 1).  Each model's probe is still sequential."""
    from .quota import TokenGovernor
    out: dict[str, dict] = {}
    clients: dict[str, ApiClient] = {}
    sems: dict[str, asyncio.Semaphore] = {}
    govs: dict[str, TokenGovernor | None] = {}

    async def one(mid: str) -> None:
        spec = reg.model(mid)
        if spec.task != "chat" and spec.task != "guard":
            return
        p = caps_path(spec.endpoint, mid)
        if p.exists() and not reprobe:
            age = time.time() - p.stat().st_mtime
            if age < PROBE_TTL_S:
                log(f"{mid}: using cached probe ({age / 3600:.1f} h old)")
                out[mid] = json.loads(p.read_text(encoding="utf-8"))
                return
        ep = reg.endpoints[spec.endpoint]
        if ep.name not in clients:
            clients[ep.name] = ApiClient(ep)
            cap = int((ep.limits or {}).get("max_concurrency") or concurrency)
            sems[ep.name] = asyncio.Semaphore(max(1, min(concurrency, cap)))
            rpm = (ep.limits or {}).get("rpm")
            govs[ep.name] = TokenGovernor(None, rpm_limit=rpm) if rpm else None
        async with sems[ep.name]:
            out[mid] = await Prober(reg, clients[ep.name], spec, log=log, governor=govs[ep.name]).run()

    try:
        results = await asyncio.gather(*(one(m) for m in model_ids), return_exceptions=True)
        for mid, r in zip(model_ids, results):
            if isinstance(r, BaseException):
                log(f"!! probe of {mid} failed: {type(r).__name__}: {r}")
                out[mid] = {"model_id": mid, "available": False, "error": f"{type(r).__name__}: {r}"}
    finally:
        await asyncio.gather(*(c.aclose() for c in clients.values()))
    return {m: out[m] for m in model_ids if m in out}
