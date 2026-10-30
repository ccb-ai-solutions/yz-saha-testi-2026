"""OpenAI-compatible async HTTP client with measurement-grade streaming timing.

Works against EVREN, OpenAI, Anthropic's OpenAI-compatibility layer, vLLM,
SGLang, Ollama, TGI ... anything that speaks /v1/chat/completions.

Key properties
  * SSE parsed by hand: comment lines (": keep-alive") are skipped but counted,
    reasoning deltas (delta.reasoning / delta.reasoning_content / delta.thinking)
    are separated from answer deltas, tool-call deltas are accumulated.
  * Every request records: time to response headers, first event, first
    reasoning token, first answer token, first tool-call token, last token, end.
  * Failures are classified into stable outcome codes (429 quota vs rate,
    503, timeouts, broken streams, truncation ...).  Callers decide whether to
    retry: latency runs never retry inside a measured window, quality runs do.
"""
from __future__ import annotations

import asyncio
import base64
import email.utils
import json
import mimetypes
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from urllib.parse import quote

from .config import Endpoint

USER_AGENT = "trbench/0.1 (+ccb-ai model-comparison-turkish)"

# ----------------------------------------------------------------------------- outcome codes
OK = "ok"
TRUNCATED = "truncated"                      # finish_reason=length but an answer exists
TRUNCATED_REASONING = "truncated_in_reasoning"  # budget exhausted while thinking, no answer
EMPTY_ANSWER = "empty_answer"
CONTENT_FILTER = "content_filter"
STREAM_INCOMPLETE = "stream_incomplete"      # stream ended without [DONE]/finish_reason
STREAM_BROKEN = "stream_broken"              # connection dropped mid-stream
STREAM_ERROR = "stream_error"                # error object inside the stream
TIMEOUT_CONNECT = "timeout_connect"
TIMEOUT_TTFT = "timeout_ttft"
TIMEOUT_IDLE = "timeout_idle"
TIMEOUT_TOTAL = "timeout_total"
TIMEOUT_READ = "timeout_read"
CONN_ERROR = "conn_error"
CLIENT_ERROR = "client_error"
PARSE_ERROR = "parse_error"

# outcomes that mean "the server gave us a usable response"
USABLE = {OK, TRUNCATED}
# model/service behaviour: scored as failures in quality runs, never retried, count toward coverage
MODEL_FAILURES = {TRUNCATED_REASONING, EMPTY_ANSWER, CONTENT_FILTER, "http_400_content_filter"}
INPUT_MISSING = "input_missing"   # nothing to send (e.g. the OCR model failed on this page): final, scored 0
# a record is FINAL (counts toward coverage, gets scored) only for these outcomes; everything else —
# infrastructure or harness problems such as 404 model removed, 400 bad parameter, 5xx, timeouts —
# is stored as non-final, excluded from scoring/coverage, and retried on --session resume.
FINAL_OUTCOMES = USABLE | MODEL_FAILURES | {INPUT_MISSING}
# infrastructure failures worth retrying immediately in quality runs
RETRYABLE = {"http_429_rate", "http_429", "http_500", "http_502", "http_503", "http_504", "http_520",
             TIMEOUT_CONNECT, TIMEOUT_READ, TIMEOUT_TTFT, TIMEOUT_IDLE, TIMEOUT_TOTAL, CONN_ERROR, STREAM_BROKEN,
             STREAM_INCOMPLETE, STREAM_ERROR}
FATAL = {"http_401", "http_403", "http_403_terms", "http_429_quota"}
KNOWN_FINISH = {"stop", "length", "tool_calls", "function_call", "content_filter", "eos", "end_turn",
                "stop_sequence", "max_tokens"}


THINK_TAGS = (("<think>", "</think>"), ("[THINK]", "[/THINK]"))   # Qwen/DeepSeek/GLM style, Mistral (Magistral) style


def is_final(outcome: str) -> bool:
    return outcome in FINAL_OUTCOMES

HEADER_KEEP = ("x-request-id", "request-id", "x-evren-daily-remaining-tokens", "retry-after",
               "x-ratelimit-remaining-tokens", "x-ratelimit-remaining-requests", "x-ratelimit-reset-tokens",
               "x-evren-credits-hold", "x-evren-credits-remaining", "openai-processing-ms", "server", "date")


class QuotaExhausted(RuntimeError):
    """Daily quota is gone: stop the run cleanly, progress is kept for --resume."""


class FatalApiError(RuntimeError):
    """Auth / terms / forbidden: retrying cannot help."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class ChatResult:
    outcome: str = OK
    http_status: int | None = None
    error_code: str | None = None
    error: str | None = None
    content: str = ""
    reasoning: str = ""
    tool_calls: list = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict | None = None
    served_model: str | None = None
    reasoning_field: str | None = None       # which delta field carried reasoning ('inline_think_tags' if <think> in content)
    wall_start: str = ""
    t_headers: float | None = None
    t_first_event: float | None = None
    t_first_reasoning: float | None = None
    t_first_content: float | None = None
    t_first_tool: float | None = None
    t_last_token: float | None = None
    t_end: float | None = None
    n_events: int = 0
    n_reasoning_events: int = 0
    n_content_events: int = 0
    n_keepalive: int = 0
    saw_done: bool = False
    content_times: list = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    retry_after: float | None = None
    attempts: list = field(default_factory=list)

    def to_record(self, *, keep_reasoning: int | None = None, keep_times: bool = True) -> dict:
        d = asdict(self)
        if keep_reasoning is not None and len(self.reasoning) > keep_reasoning:
            d["reasoning"] = self.reasoning[:keep_reasoning] + f"…[truncated {len(self.reasoning) - keep_reasoning} chars]"
        d["reasoning_chars"] = len(self.reasoning)
        d["content_chars"] = len(self.content)
        if not keep_times:
            d.pop("content_times", None)
        else:
            d["content_times"] = [round(x, 4) for x in self.content_times]
        for k in ("t_headers", "t_first_event", "t_first_reasoning", "t_first_content", "t_first_tool",
                  "t_last_token", "t_end"):
            if d[k] is not None:
                d[k] = round(d[k], 5)
        return d


@dataclass
class JsonResult:
    outcome: str = OK
    http_status: int | None = None
    error_code: str | None = None
    error: str | None = None
    data: Any = None
    latency: float | None = None
    wall_start: str = ""
    headers: dict = field(default_factory=dict)
    retry_after: float | None = None
    attempts: list = field(default_factory=list)


# ----------------------------------------------------------------------------- error classification
def _parse_retry_after(headers: httpx.Headers | dict, body: dict | None) -> float | None:
    v = headers.get("retry-after") if headers else None
    if v:
        try:
            return float(v)
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(v)
                return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
            except Exception:
                pass
    if isinstance(body, dict):
        ev = (body.get("error") or {}).get("evren") if isinstance(body.get("error"), dict) else None
        for src in (ev, body.get("error") if isinstance(body.get("error"), dict) else None, body):
            if isinstance(src, dict):
                for k in ("retry_after", "retry_after_seconds", "retry_after_s", "bekleme_saniye"):
                    if isinstance(src.get(k), (int, float)):
                        return float(src[k])
    return None


def classify_http_error(status: int, body_text: str, headers: httpx.Headers | dict) -> tuple[str, str | None, str, float | None]:
    """Return (outcome, provider_error_code, message, retry_after)."""
    body: dict | None = None
    try:
        body = json.loads(body_text) if body_text else None
    except json.JSONDecodeError:
        body = None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, str):
        err = {"message": err}
    err = err or (body if isinstance(body, dict) else {}) or {}
    code = str(err.get("code") or err.get("type") or "") or None
    msg = str(err.get("message") or err.get("detail") or body_text[:500] or "")
    low = (msg + " " + (code or "")).lower()
    retry_after = _parse_retry_after(headers, body)
    if status == 400:
        if code == "endpoint_mismatch" or "endpoint_mismatch" in low:
            out = "http_400_endpoint_mismatch"
        elif any(k in low for k in ("content_filter", "content filter", "safety", "moderation", "policy violation")):
            out = "http_400_content_filter"
        elif any(k in low for k in ("context length", "context_length", "maximum context", "too many tokens",
                                    "prompt is too long", "input is too long")):
            out = "http_400_context_length"
        elif any(k in low for k in ("unsupported parameter", "unsupported value", "not supported", "unrecognized",
                                    "extra inputs", "extra_forbidden", "unknown parameter", "unexpected keyword",
                                    "is not allowed", "invalid parameter", "does not support")):
            out = "http_400_param"
        else:
            out = "http_400"
    elif status == 401:
        out = "http_401"
    elif status == 403:
        out = "http_403_terms" if ("terms" in low or "şart" in low) else "http_403"
    elif status == 429:
        # Only a DAILY remaining count decides by number.  EVREN, 12 Oct: "Dakikalık token limitiniz aşıldı." (the
        # per-minute limit) was taken for the daily quota — a generic remaining count (remaining_tokens / kalan:
        # the 5-minute window's) said 0 — and the OCR step stopped although 9M tokens were left for the day.
        remaining = headers.get("x-evren-daily-remaining-tokens") if headers else None
        evren = err.get("evren") if isinstance(err, dict) else None
        daily_left = None
        try:
            daily_left = float(remaining) if remaining is not None else None
        except ValueError:
            daily_left = None
        if isinstance(evren, dict):
            for k in ("daily_remaining", "daily_remaining_tokens", "gunluk_kalan", "günlük_kalan"):
                if isinstance(evren.get(k), (int, float)):
                    daily_left = float(evren[k])
        rate_words = any(k in low for k in ("per minute", "per-minute", "dakika", "tpm", "rpm", "rate limit",
                                            "rate_limit", "too many requests", " sn ", "saniye", "window", "pencere"))
        quota_words = any(k in low for k in ("daily", "günlük", "gunluk", "quota exceeded", "quota exhausted",
                                             "insufficient_quota"))
        if daily_left is not None:
            out = "http_429_quota" if daily_left <= 0 else "http_429_rate"     # the daily number decides
        elif rate_words:
            out = "http_429_rate"
        else:
            out = "http_429_quota" if quota_words else "http_429_rate"
    elif status in (404, 408, 409, 413, 422, 500, 502, 503, 504, 520):
        out = f"http_{status}"
        if status == 408:
            out = TIMEOUT_READ
    else:
        out = f"http_{status}"
    return out, code, msg[:1000], retry_after


def classify_exception(exc: BaseException) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return TIMEOUT_CONNECT
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return TIMEOUT_READ
    if isinstance(exc, httpx.ConnectError):
        return CONN_ERROR
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)):
        return STREAM_BROKEN
    if isinstance(exc, httpx.HTTPError):
        return CLIENT_ERROR
    return CLIENT_ERROR


def _pick_headers(h: httpx.Headers) -> dict:
    return {k: h.get(k) for k in HEADER_KEEP if h.get(k) is not None}


# ----------------------------------------------------------------------------- text extraction helpers
_TEXT_KEYS = ("text", "markdown", "content", "transcript", "result", "output", "ocr_text", "md")


def extract_text(obj: Any) -> str:
    """Best-effort extraction of the main text from an unknown JSON response shape."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        parts = [extract_text(x) for x in obj]
        return "\n\n".join(p for p in parts if p)
    if isinstance(obj, dict):
        if "choices" in obj and obj["choices"]:
            ch = obj["choices"][0]
            msg = ch.get("message") or {}
            if isinstance(msg, dict) and msg.get("content"):
                return extract_text(msg["content"])
            if ch.get("text"):
                return ch["text"]
        for k in ("pages", "results", "data", "segments"):
            if isinstance(obj.get(k), list) and obj[k]:
                t = extract_text(obj[k])
                if t:
                    return t
        for k in _TEXT_KEYS:
            if k in obj and obj[k]:
                t = extract_text(obj[k])
                if t:
                    return t
    return ""


def image_to_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ----------------------------------------------------------------------------- client
CLOSE_TAGS = tuple(cl for _, cl in THINK_TAGS)


def server_separated(reasoning_field: str | None) -> bool:
    """True when reasoning arrived apart from the answer (a reasoning field or typed thinking parts), as opposed to
    being cut out of the answer text by this client ('inline_think_tags' / 'inline_think_close_only')."""
    return reasoning_field not in (None, "inline_think_tags", "inline_think_close_only")


def split_content_parts(parts: list) -> tuple[str, str]:
    """Typed content parts -> (reasoning, answer).  Mistral's API returns thinking as
    {"type": "thinking", "thinking": [{"type": "text", "text": ...}]} next to {"type": "text", "text": ...}
    (non-streamed, and streamed while it thinks); Anthropic-style parts carry {"type": "thinking", "thinking": "..."}."""
    r, c = [], []
    for p in parts:
        if isinstance(p, str):
            c.append(p)
            continue
        if not isinstance(p, dict):
            continue
        if p.get("type") in ("thinking", "reasoning", "think"):
            v = p.get("thinking", p.get("reasoning", p.get("text", "")))
            if isinstance(v, list):
                v = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v)
            if isinstance(v, str):
                r.append(v)
        elif isinstance(p.get("text"), str):
            c.append(p["text"])
    return "".join(r), "".join(c)


class ApiClient:
    def __init__(self, endpoint: Endpoint, *, max_connections: int = 64):
        self.ep = endpoint
        self.base = endpoint.base_url.rstrip("/")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        key = endpoint.api_key
        if endpoint.auth in ("x-api-key", "bearer+x-api-key") and key:
            headers["X-API-Key"] = key
        if endpoint.auth in ("bearer", "bearer+x-api-key") and key:
            headers["Authorization"] = f"Bearer {key}"
        headers.update(endpoint.extra_headers or {})
        t = endpoint.timeouts
        self.http = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(connect=t["connect"], read=t["request"], write=60.0, pool=None),
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections,
                                keepalive_expiry=120.0),
            verify=endpoint.verify_tls,
            trust_env=True,
            follow_redirects=False,
        )
        self.reasoning_fields = list(endpoint.reasoning_fields or [])

    async def aclose(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> "ApiClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def url(self, path: str) -> str:
        return self.base + (path if path.startswith("/") else "/" + path)

    # ------------------------------------------------------------------ streaming chat
    async def stream_chat(self, payload: dict, *, ttft_timeout: float | None = None,
                          idle_timeout: float | None = None, total_timeout: float | None = None) -> ChatResult:
        t = self.ep.timeouts
        ttft_timeout = ttft_timeout or t["ttft"]
        idle_timeout = idle_timeout or t["idle"]
        total_timeout = total_timeout or t["total"]
        payload = dict(payload)
        payload["stream"] = True
        res = ChatResult(wall_start=utc_now())
        tool_acc: dict[int, dict] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        t0 = time.perf_counter()
        got_token = False
        try:
            stream_timeout = httpx.Timeout(connect=t["connect"], read=idle_timeout + 5, write=60.0, pool=None)
            async with self.http.stream("POST", self.url("/chat/completions"), json=payload,
                                        timeout=stream_timeout) as resp:
                res.t_headers = time.perf_counter() - t0
                res.http_status = resp.status_code
                res.headers = _pick_headers(resp.headers)
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    res.outcome, res.error_code, res.error, res.retry_after = classify_http_error(
                        resp.status_code, body, resp.headers)
                    res.t_end = time.perf_counter() - t0
                    return res
                ctype = resp.headers.get("content-type", "")
                if "text/event-stream" not in ctype and "json" in ctype:
                    # server ignored stream=true and answered in one JSON body
                    body = await resp.aread()
                    now = time.perf_counter() - t0
                    self._fill_from_message(res, json.loads(body), now)
                    res.t_end = now
                    return self._finalise(res)
                it = resp.aiter_lines()
                while True:
                    elapsed = time.perf_counter() - t0
                    if elapsed >= total_timeout:
                        res.outcome = TIMEOUT_TOTAL
                        break
                    if not got_token:
                        wait = min(ttft_timeout - elapsed, total_timeout - elapsed)
                    else:
                        wait = min(idle_timeout, total_timeout - elapsed)
                    if wait <= 0:
                        res.outcome = TIMEOUT_TTFT if not got_token else TIMEOUT_TOTAL
                        break
                    try:
                        line = await asyncio.wait_for(it.__anext__(), timeout=wait)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        elapsed = time.perf_counter() - t0
                        if not got_token and elapsed >= ttft_timeout - 0.05:
                            res.outcome = TIMEOUT_TTFT
                        elif elapsed >= total_timeout - 0.05:
                            res.outcome = TIMEOUT_TOTAL
                        else:
                            res.outcome = TIMEOUT_IDLE
                        break
                    now = time.perf_counter() - t0
                    if not line:
                        continue
                    if line.startswith(":"):
                        res.n_keepalive += 1          # ": keep-alive" heartbeat — never parsed as JSON
                        continue
                    if line.startswith("event:"):
                        if "error" in line.lower():
                            res.outcome = STREAM_ERROR
                        continue
                    if line.startswith(("id:", "retry:")):
                        continue
                    if not line.startswith("data:"):
                        # some servers emit a bare JSON error line
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                    else:
                        data = line[5:].strip()
                        if data == "[DONE]":
                            res.saw_done = True
                            break
                        try:
                            ev = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                    res.n_events += 1
                    if res.t_first_event is None:
                        res.t_first_event = now
                    if isinstance(ev, dict) and ev.get("error") and not ev.get("choices"):
                        e = ev["error"]
                        res.outcome = STREAM_ERROR
                        res.error = json.dumps(e, ensure_ascii=False)[:1000] if not isinstance(e, str) else e[:1000]
                        break
                    got_token |= self._apply_event(res, ev, now, tool_acc, content_parts, reasoning_parts)
        except Exception as exc:  # transport-level failure
            res.outcome = classify_exception(exc)
            res.error = f"{type(exc).__name__}: {exc}"[:1000]
        res.t_end = time.perf_counter() - t0
        st = res.__dict__.get("_think_state") or {}
        if st.get("buf"):                       # a held-back partial tag when the stream ended: it was text after all
            (reasoning_parts if st.get("mode") == "think" else content_parts).append(st["buf"])
            st["buf"] = ""
        res.content = "".join(content_parts)
        res.reasoning = "".join(reasoning_parts)
        if server_separated(res.reasoning_field) and res.content.lstrip().startswith(CLOSE_TAGS):
            s0 = res.content.lstrip()          # a stray closing tag left by the server's reasoning parser
            res.content = s0[next(len(t) for t in CLOSE_TAGS if s0.startswith(t)):].lstrip("\n")
        res.tool_calls = [self._finish_tool(tool_acc[i]) for i in sorted(tool_acc)]
        return self._finalise(res)

    def _apply_event(self, res: ChatResult, ev: dict, now: float, tool_acc: dict,
                     content_parts: list, reasoning_parts: list) -> bool:
        got = False
        if ev.get("model"):
            res.served_model = ev["model"]
        if ev.get("usage"):
            res.usage = ev["usage"]
        for ch in ev.get("choices") or []:
            delta = ch.get("delta") or ch.get("message") or {}
            if not isinstance(delta, dict):
                continue
            r = None
            for f in self.reasoning_fields:
                v = delta.get(f)
                if isinstance(v, str) and v:
                    r = v
                    res.reasoning_field = res.reasoning_field or f
                    break
            if r:
                self._add_reasoning(res, r, now, reasoning_parts)
                got = True
            c = delta.get("content")
            if isinstance(c, list):  # typed content parts (Mistral streams its thinking this way)
                rpart, c = split_content_parts(c)
                if rpart:
                    res.reasoning_field = res.reasoning_field or "content_thinking_parts"
                    self._add_reasoning(res, rpart, now, reasoning_parts)
                    got = True
            if isinstance(c, str) and c:
                c = self._reclassify_close_only(res, c, now, content_parts, reasoning_parts)
                # models served without a reasoning parser put thinking inline: <think> ... </think>
                for kind, piece in self._split_inline_think(res, c):
                    if not piece:
                        continue
                    if kind == "r":
                        self._add_reasoning(res, piece, now, reasoning_parts)
                    else:
                        content_parts.append(piece)
                        res.n_content_events += 1
                        res.content_times.append(now)
                        if res.t_first_content is None and piece.strip():
                            res.t_first_content = now
                        res.t_last_token = now
                        st = res.__dict__["_think_state"]
                        st["tail"] = (st.get("tail", "") + piece)[-16:]
                got = True
            for tc in delta.get("tool_calls") or []:
                if isinstance(tc.get("index"), int):
                    idx = tc["index"]
                else:   # some servers omit 'index': a new id starts a new call, otherwise continue the last one
                    ids = {a.get("id"): k for k, a in tool_acc.items() if a.get("id")}
                    if tc.get("id") and tc["id"] in ids:
                        idx = ids[tc["id"]]
                    elif tc.get("id") or not tool_acc:
                        idx = len(tool_acc)
                    else:
                        idx = max(tool_acc)
                acc = tool_acc.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                if tc.get("id"):
                    acc["id"] = tc["id"]
                fn = tc.get("function") or {}
                name = fn.get("name")
                if name:
                    acc.setdefault("name_pieces", []).append(name)     # kept for diagnosis when >1 piece arrives
                    if not acc["name"] or name.startswith(acc["name"]):
                        acc["name"] = name
                    elif not acc["name"].endswith(name):
                        acc["name"] += name
                args = fn.get("arguments")
                if isinstance(args, dict):
                    acc["arguments"] = json.dumps(args, ensure_ascii=False)
                elif isinstance(args, str):
                    acc["arguments"] += args
                if res.t_first_tool is None:
                    res.t_first_tool = now
                res.t_last_token = now
                got = True
            if ch.get("finish_reason"):
                res.finish_reason = ch["finish_reason"]
        return got

    @staticmethod
    def _add_reasoning(res: ChatResult, piece: str, now: float, reasoning_parts: list) -> None:
        reasoning_parts.append(piece)
        if not piece.strip():          # e.g. the empty '<think>\n\n</think>' of a no-think template
            return
        res.n_reasoning_events += 1
        if res.t_first_reasoning is None:
            res.t_first_reasoning = now
        res.t_last_token = now

    @staticmethod
    def _first_close(text: str) -> tuple[int, str]:
        """Earliest closing think tag in text: (index, tag) or (-1, '')."""
        best = (-1, "")
        for _, close in THINK_TAGS:
            i = text.find(close)
            if i >= 0 and (best[0] < 0 or i < best[0]):
                best = (i, close)
        return best

    @staticmethod
    def _reclassify_close_only(res: ChatResult, c: str, now: float, content_parts: list, reasoning_parts: list) -> str:
        """Templates that pre-fill the opening tag stream 'reasoning…</think>answer' (or '…[/THINK]answer') without
        an opening tag.  When a closing tag shows up first, everything streamed so far was reasoning: move it."""
        st = res.__dict__.setdefault("_think_state", {"mode": "start", "buf": ""})
        if st.get("opened") or st.get("closed_only") or st["mode"] == "think":
            return c
        if server_separated(res.reasoning_field):
            return c                            # the server separates reasoning: a closing tag in the answer is text
        window = st.get("tail", "") + c
        if ApiClient._first_close(window)[0] < 0 and not (
                st["mode"] == "start" and ApiClient._first_close(st["buf"] + c)[0] >= 0):
            return c
        joined = "".join(content_parts) + st.get("buf", "") + c
        i, close = ApiClient._first_close(joined)
        if i < 0:
            return c
        before, after = joined[:i], joined[i + len(close):]
        content_parts.clear()
        res.content_times.clear()
        moved = res.n_content_events
        res.n_content_events = 0
        first = res.t_first_content
        res.t_first_content = None
        if before.strip():
            reasoning_parts.append(before)
            res.n_reasoning_events += max(1, moved)
            res.t_first_reasoning = res.t_first_reasoning if res.t_first_reasoning is not None else (first if first is not None else now)
            res.t_last_token = now
        res.reasoning_field = res.reasoning_field or "inline_think_close_only"
        st.update(mode="answer", buf="", tail="", closed_only=True)
        return after.lstrip("\n")

    @staticmethod
    def _split_inline_think(res: ChatResult, c: str) -> list[tuple[str, str]]:
        """State machine for '<think>…</think>' or '[THINK]…[/THINK]' emitted inside content (tags may span chunks)."""
        st = res.__dict__.setdefault("_think_state", {"mode": "start", "buf": ""})
        out: list[tuple[str, str]] = []
        st["buf"] += c
        while st["buf"]:
            buf = st["buf"]
            if st["mode"] == "start":
                stripped = buf.lstrip()
                if not stripped:
                    return out                               # only whitespace so far: wait
                pair = next(((o, cl) for o, cl in THINK_TAGS if stripped.startswith(o)), None)
                if pair:
                    st["mode"], st["buf"], st["close"] = "think", stripped[len(pair[0]):], pair[1]
                    st["opened"] = True
                    res.reasoning_field = res.reasoning_field or "inline_think_tags"
                    continue
                if any(o.startswith(stripped) for o, _ in THINK_TAGS):
                    return out                               # partial tag: wait for more
                st["mode"] = "answer"
                continue
            if st["mode"] == "think":
                close = st.get("close", "</think>")
                i = buf.find(close)
                if i >= 0:
                    out.append(("r", buf[:i]))
                    st["mode"], st["buf"] = "answer", buf[i + len(close):].lstrip("\n")
                    continue
                keep = 0                                       # hold back a possible partial closing tag
                for k in range(1, len(close)):
                    if buf.endswith(close[:k]):
                        keep = k
                out.append(("r", buf[:len(buf) - keep]))
                st["buf"] = buf[len(buf) - keep:]
                return out
            out.append(("c", buf))
            st["buf"] = ""
        return out

    @staticmethod
    def _finish_tool(acc: dict) -> dict:
        out = {"id": acc.get("id"), "name": acc.get("name", ""), "arguments": acc.get("arguments", "")}
        if len(acc.get("name_pieces") or []) > 1:
            out["name_pieces"] = acc["name_pieces"]       # how the server streamed the name (diagnosis only)
        return out

    def _fill_from_message(self, res: ChatResult, body: dict, now: float) -> None:
        res.served_model = body.get("model")
        res.usage = body.get("usage")
        ch = (body.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        res.content = msg.get("content") or ""
        parts_reasoning = ""
        if isinstance(res.content, list):
            parts_reasoning, res.content = split_content_parts(res.content)
        for f in self.reasoning_fields:
            if isinstance(msg.get(f), str) and msg.get(f):
                res.reasoning = msg[f]
                res.reasoning_field = f
                break
        if parts_reasoning and not res.reasoning:
            res.reasoning = parts_reasoning
            res.reasoning_field = "content_thinking_parts"
        stripped = res.content.lstrip()
        pair = next(((o, cl) for o, cl in THINK_TAGS if stripped.startswith(o)), None)
        if pair:
            rest = stripped[len(pair[0]):]
            i = rest.find(pair[1])
            if i >= 0:
                res.reasoning += rest[:i]
                res.content = rest[i + len(pair[1]):].lstrip("\n")
            else:
                res.reasoning += rest
                res.content = ""
            res.reasoning_field = res.reasoning_field or "inline_think_tags"
        res.tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            res.tool_calls.append({"id": tc.get("id"), "name": fn.get("name", ""),
                                   "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)})
        res.finish_reason = ch.get("finish_reason")
        res.saw_done = True
        res.n_events = 1
        res.t_first_event = now
        if res.reasoning:
            res.t_first_reasoning = now
        if res.content:
            res.t_first_content = now
        if res.tool_calls:
            res.t_first_tool = now
        res.t_last_token = now

    @staticmethod
    def _finalise(res: ChatResult) -> ChatResult:
        if res.outcome != OK:
            return res
        fr = (res.finish_reason or "").lower()
        has_answer = bool(res.content.strip()) or bool(res.tool_calls)
        if fr and fr not in KNOWN_FINISH:
            res.outcome = STREAM_ERROR          # e.g. 'abort' / 'error' from the server
            res.error = res.error or f"finish_reason={fr}"
        elif fr == "content_filter":
            res.outcome = CONTENT_FILTER
        elif not res.saw_done and not res.finish_reason:
            res.outcome = STREAM_INCOMPLETE     # stream closed without [DONE]/finish: infrastructure, not the model
        elif fr in ("length", "max_tokens"):
            res.outcome = TRUNCATED if has_answer else TRUNCATED_REASONING
        elif not has_answer:
            res.outcome = EMPTY_ANSWER
        return res

    # ------------------------------------------------------------------ non-streaming chat
    async def chat(self, payload: dict, *, timeout: float | None = None) -> ChatResult:
        payload = dict(payload)
        payload["stream"] = False
        payload.pop("stream_options", None)
        res = ChatResult(wall_start=utc_now())
        t0 = time.perf_counter()
        try:
            resp = await self.http.post(self.url("/chat/completions"), json=payload,
                                        timeout=timeout or self.ep.timeouts["total"])
            now = time.perf_counter() - t0
            res.http_status = resp.status_code
            res.headers = _pick_headers(resp.headers)
            res.t_headers = now
            if resp.status_code != 200:
                res.outcome, res.error_code, res.error, res.retry_after = classify_http_error(
                    resp.status_code, resp.text, resp.headers)
                res.t_end = now
                return res
            self._fill_from_message(res, resp.json(), now)
            res.t_end = now
        except json.JSONDecodeError as exc:
            res.outcome, res.error = PARSE_ERROR, str(exc)[:500]
        except Exception as exc:
            res.outcome = classify_exception(exc)
            res.error = f"{type(exc).__name__}: {exc}"[:1000]
            res.t_end = time.perf_counter() - t0
        return self._finalise(res)

    # ------------------------------------------------------------------ generic JSON / multipart
    async def request_json(self, method: str, path: str, *, json_body: Any = None, files: Any = None,
                           data: Any = None, timeout: float | None = None) -> JsonResult:
        res = JsonResult(wall_start=utc_now())
        t0 = time.perf_counter()
        try:
            resp = await self.http.request(method, self.url(path), json=json_body, files=files, data=data,
                                           timeout=timeout or self.ep.timeouts["request"])
            res.latency = time.perf_counter() - t0
            res.http_status = resp.status_code
            res.headers = _pick_headers(resp.headers)
            if resp.status_code >= 400:
                res.outcome, res.error_code, res.error, res.retry_after = classify_http_error(
                    resp.status_code, resp.text, resp.headers)
                return res
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                res.data = resp.json()
            else:
                txt = resp.text
                try:
                    res.data = json.loads(txt)
                except json.JSONDecodeError:
                    res.data = txt
        except Exception as exc:
            res.latency = time.perf_counter() - t0
            res.outcome = classify_exception(exc)
            res.error = f"{type(exc).__name__}: {exc}"[:1000]
        return res

    async def get_json(self, path: str, timeout: float | None = None) -> JsonResult:
        return await self.request_json("GET", path, timeout=timeout)

    async def get_text(self, path: str, timeout: float | None = None) -> JsonResult:
        """GET a non-JSON body (e.g. a JSONL results file) relative to the endpoint's base URL; data = text."""
        res = JsonResult(wall_start=utc_now())
        t0 = time.perf_counter()
        try:
            resp = await self.http.get(self.url(path), headers={"Accept": "*/*"},
                                       timeout=timeout or self.ep.timeouts["request"])
            res.latency = time.perf_counter() - t0
            res.http_status = resp.status_code
            res.headers = _pick_headers(resp.headers)
            if resp.status_code >= 400:
                res.outcome, res.error_code, res.error, res.retry_after = classify_http_error(
                    resp.status_code, resp.text, resp.headers)
                return res
            res.data = resp.text
        except Exception as exc:
            res.latency = time.perf_counter() - t0
            res.outcome = classify_exception(exc)
            res.error = f"{type(exc).__name__}: {exc}"[:1000]
        return res

    async def embeddings(self, model: str, inputs: list[str], *, dimensions: int | None = None,
                         extra: dict | None = None) -> JsonResult:
        body: dict = {"model": model, "input": inputs}
        if dimensions:
            body["dimensions"] = dimensions
        body.update(extra or {})
        res = await self.request_json("POST", self.ep.path("embeddings", "/embeddings"), json_body=body)
        if res.outcome == OK:
            try:
                rows = sorted(res.data["data"], key=lambda r: r.get("index", 0))
                res.data = {"vectors": [r["embedding"] for r in rows], "usage": res.data.get("usage"),
                            "model": res.data.get("model")}
            except (KeyError, TypeError) as exc:
                res.outcome, res.error = PARSE_ERROR, f"unexpected embeddings shape: {exc}"
        return res

    async def invoke_model(self, model_id: str, body: dict) -> JsonResult:
        """Amazon Bedrock InvokeModel with a Bedrock API key (Authorization: Bearer)."""
        return await self.request_json("POST", f"/model/{quote(model_id, safe='')}/invoke", json_body=body)

    async def embeddings_bedrock(self, model_id: str, inputs: list[str], *, flavor: str, kind: str,
                                 dimensions: int | None = None) -> JsonResult:
        """Bedrock embedding models through InvokeModel: Cohere Embed (batched, input_type) or Titan (one text/call)."""
        if flavor == "cohere":
            body = {"texts": inputs, "input_type": "search_query" if kind == "query" else "search_document",
                    "truncate": "END"}
            if dimensions:
                body["output_dimension"] = dimensions
            res = await self.invoke_model(model_id, body)
            if res.outcome == OK:
                try:
                    emb = res.data["embeddings"]
                    vecs = emb.get("float") if isinstance(emb, dict) else emb
                    res.data = {"vectors": vecs, "usage": {"total_tokens": None}, "model": model_id}
                except (KeyError, TypeError, AttributeError) as exc:
                    res.outcome, res.error = PARSE_ERROR, f"unexpected Cohere embeddings shape: {exc}"
            return res
        if flavor == "titan":
            vecs, tokens, last = [], 0, None
            for t in inputs:
                body = {"inputText": t, "normalize": True}
                if dimensions:
                    body["dimensions"] = dimensions
                last = await self.invoke_model(model_id, body)
                if last.outcome != OK:
                    return last
                try:
                    vecs.append(last.data["embedding"])
                    tokens += int(last.data.get("inputTextTokenCount") or 0)
                except (KeyError, TypeError) as exc:
                    last.outcome, last.error = PARSE_ERROR, f"unexpected Titan embeddings shape: {exc}"
                    return last
            last.data = {"vectors": vecs, "usage": {"total_tokens": tokens}, "model": model_id}
            return last
        raise ValueError(f"unknown Bedrock embedding flavor {flavor}")

    async def rerank(self, model: str, query: str, documents: list[str], *, top_n: int | None = None,
                     extra: dict | None = None) -> JsonResult:
        body: dict = {"model": model, "query": query, "documents": documents, "return_documents": False}
        if top_n:
            body["top_n"] = top_n
        body.update(extra or {})
        res = await self.request_json("POST", self.ep.path("rerank", "/rerank"), json_body=body)
        if res.outcome == OK:
            try:
                rows = res.data.get("results") or res.data.get("data") or []
                scores = [None] * len(documents)
                for r in rows:
                    i = r.get("index")
                    s = r.get("relevance_score", r.get("score"))
                    if i is not None and 0 <= i < len(scores):
                        scores[i] = float(s)
                res.data = {"scores": scores, "usage": res.data.get("usage"), "model": res.data.get("model")}
            except (AttributeError, TypeError, ValueError) as exc:
                res.outcome, res.error = PARSE_ERROR, f"unexpected rerank shape: {exc}"
        return res

    async def ocr(self, model: str, image_data_url: str, *, extra: dict | None = None,
                  poll_interval: float = 2.0, poll_timeout: float = 600.0) -> JsonResult:
        body = {"model": model, "image": image_data_url}
        body.update(extra or {})
        path = self.ep.path("ocr", "/ocr")
        res = await self.request_json("POST", path, json_body=body, timeout=self.ep.timeouts["total"])
        # some batch OCR backends answer with a job id — poll it if so
        if res.outcome == OK and isinstance(res.data, dict) and str(res.data.get("status", "")).lower() in (
                "queued", "pending", "processing", "running", "in_progress") and res.data.get("id"):
            job = res.data["id"]
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < poll_timeout:
                await asyncio.sleep(poll_interval)
                pr = await self.get_json(f"{path}/{job}")
                if pr.outcome != OK:
                    return pr
                st = str((pr.data or {}).get("status", "")).lower() if isinstance(pr.data, dict) else "done"
                if st not in ("queued", "pending", "processing", "running", "in_progress"):
                    pr.latency = (res.latency or 0) + (time.perf_counter() - t0)
                    res = pr
                    break
            else:
                res.outcome, res.error = TIMEOUT_TOTAL, f"OCR job {job} not finished after {poll_timeout}s"
        if res.outcome == OK:
            res.data = {"text": extract_text(res.data), "raw": res.data}
        return res

    async def transcribe(self, model: str, audio_path: Path, *, language: str | None = None,
                         response_format: str = "json", extra: dict | None = None) -> JsonResult:
        data = {"model": model, "response_format": response_format}
        if language:
            data["language"] = language
        data.update({k: str(v) for k, v in (extra or {}).items()})
        mime = mimetypes.guess_type(str(audio_path))[0] or "audio/wav"
        files = {"file": (audio_path.name, audio_path.read_bytes(), mime)}
        res = await self.request_json("POST", self.ep.path("transcriptions", "/audio/transcriptions"),
                                      files=files, data=data, timeout=self.ep.timeouts["total"])
        if res.outcome == OK:
            res.data = {"text": extract_text(res.data) if not isinstance(res.data, str) else res.data,
                        "raw": res.data}
        return res

    async def systemone(self, model: str, state: Any, questions: dict, *, max_len: int | None = None) -> JsonResult:
        body = {"model": model, "state": state, "questions": questions}
        if max_len:
            body["max_len"] = max_len
        return await self.request_json("POST", self.ep.path("systemone", "/systemone"), json_body=body)

    async def systemone_batch(self, model: str, states: list, questions: dict, *, max_len: int | None = None) -> JsonResult:
        body = {"model": model, "states": states, "questions": questions}
        if max_len:
            body["max_len"] = max_len
        return await self.request_json("POST", self.ep.path("systemone_batch", "/systemone/batch"), json_body=body)


# ----------------------------------------------------------------------------- retry wrapper (quality runs only)
@dataclass
class RetryPolicy:
    max_attempts: int = 6
    base_delay: float = 2.0
    max_delay: float = 90.0
    rate_limit_min_wait: float = 20.0   # TPM window on EVREN is 60 s; never hammer it


async def with_retries(fn: Callable[[], Awaitable[Any]], policy: RetryPolicy,
                       on_attempt: Callable[[int, Any], None] | None = None) -> Any:
    """Run fn() until it returns a non-retryable outcome.  Raises on quota/auth problems."""
    attempts = []
    last = None
    for i in range(1, policy.max_attempts + 1):
        last = await fn()
        attempts.append({"n": i, "outcome": last.outcome, "http": last.http_status, "at": utc_now()})
        if on_attempt:
            on_attempt(i, last)
        if last.outcome == "http_429_quota":
            last.attempts = attempts
            raise QuotaExhausted(last.error or "daily quota exhausted")
        if last.outcome in ("http_401", "http_403", "http_403_terms"):
            last.attempts = attempts
            raise FatalApiError(f"{last.outcome}: {last.error}")
        if last.outcome not in RETRYABLE or i == policy.max_attempts:
            break
        delay = min(policy.max_delay, policy.base_delay * (2 ** (i - 1)))
        if last.outcome.startswith("http_429"):
            delay = max(delay, policy.rate_limit_min_wait)
        if last.retry_after:
            delay = max(delay, min(last.retry_after, policy.max_delay * 2))
        await asyncio.sleep(delay * (0.8 + 0.4 * random.random()))
    last.attempts = attempts
    return last
