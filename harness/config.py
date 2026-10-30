"""Configuration loading: endpoints, model registry, suites, judge.

All YAML lives in configs/.  Secrets come only from environment variables
(optionally loaded from a local .env file); they are never written to results.
"""
from __future__ import annotations

import copy
import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
# overridable for tests / client engagements that keep their own configs, data or results elsewhere
CONFIG_DIR = Path(os.environ.get("TRBENCH_CONFIG_DIR", ROOT / "configs"))
DATA_DIR = Path(os.environ.get("TRBENCH_DATA_DIR", ROOT / "data"))
PROMPT_DIR = ROOT / "prompts" / "tr"
RESULTS_DIR = Path(os.environ.get("TRBENCH_RESULTS_DIR", ROOT / "results"))


def _dotenv_value(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    # unquoted: drop an inline comment ("KEY=value  # note", "KEY=   # note")
    v = re.split(r"(?:^|\s)#", v, maxsplit=1)[0]
    return v.strip().strip('"').strip("'")


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (KEY=VALUE lines, optional `export `, BOM-safe for Windows editors).
    A variable already set to a non-empty value in the environment is never overridden."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        if k and not os.environ.get(k):
            os.environ[k] = _dotenv_value(v)


def _read_yaml(name: str) -> dict:
    p = CONFIG_DIR / name
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------- endpoints
DEFAULT_TIMEOUTS = {"connect": 15.0, "ttft": 300.0, "idle": 45.0, "total": 900.0, "request": 300.0}


@dataclass
class Endpoint:
    name: str
    base_url: str
    api_key_env: str | None = None
    auth: str = "bearer"                     # bearer | x-api-key | none
    track: str = "a"
    hardware: str = ""
    network: str = "public"                  # public | private (private = on-prem / closed network)
    limits: dict = field(default_factory=dict)
    quota_path: str | None = None
    terms_prefix: str | None = None
    reasoning_fields: list[str] = field(default_factory=lambda: ["reasoning_content", "reasoning", "thinking"])
    timeouts: dict = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS))
    extra_headers: dict = field(default_factory=dict)
    paths: dict = field(default_factory=dict)  # override endpoint paths (ocr, rerank, systemone, ...)
    api_style: str = "openai"                 # openai | bedrock_invoke (Amazon Bedrock InvokeModel, bearer API key)
    pricing_note: str = ""                    # free text shown by the dry-run cost table
    verify_tls: bool | str = True             # True | False | path to CA bundle (on-prem PKI)

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    def path(self, key: str, default: str) -> str:
        return self.paths.get(key, default)

    def is_private(self) -> bool:
        host = urlparse(self.base_url).hostname or ""
        if host in ("localhost",) or host.endswith((".local", ".internal", ".lan", ".corp", ".localhost")):
            return True
        try:
            return ipaddress.ip_address(host).is_private
        except ValueError:
            return self.network == "private"


# --------------------------------------------------------------------------- models
@dataclass
class ModelSpec:
    id: str
    endpoint: str
    api_model: str
    task: str = "chat"                       # chat | embedding | rerank | ocr | asr | guard | decision | local_embedding | local_asr | local_ocr
    family: str = ""
    label: str = ""
    params_total_b: float | None = None
    params_active_b: float | None = None
    architecture: str = ""
    context_k: int | None = None
    license: str = ""
    is_router: bool = False
    thinking: dict = field(default_factory=dict)
    extra_body: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)  # task-specific options (e.g. embedding query instruction)
    available_until: str | None = None
    price: dict = field(default_factory=dict)    # {in, out} per 1M tokens | {per_second} | {free_until}; + currency
    deploy: dict = field(default_factory=dict)   # on-prem facts: {fits: "1x96GB BF16 ...", quant: ...} (report only)
    tier: str = ""                               # lineup role: deployable-1gpu | deployable-2gpu | ceiling | evren | ...
    notes: str = ""

    @property
    def display(self) -> str:
        return self.label or self.id


VARIANT_MODES = ("default", "nothink", "think", "think_min")


@dataclass(frozen=True)
class Variant:
    """A system-under-test: one model in one reasoning mode."""
    model_id: str
    mode: str = "default"

    @property
    def key(self) -> str:
        return f"{self.model_id}:{self.mode}"

    @staticmethod
    def parse(s: str) -> "Variant":
        if ":" in s:
            m, mode = s.rsplit(":", 1)
            if mode not in VARIANT_MODES:
                raise ValueError(f"unknown variant mode '{mode}' (choose from {VARIANT_MODES})")
            return Variant(m, mode)
        return Variant(s, "default")


# --------------------------------------------------------------------------- registry
@dataclass
class Registry:
    endpoints: dict[str, Endpoint]
    models: dict[str, ModelSpec]
    suites: dict[str, Any]
    judge: dict[str, Any]
    policy: dict[str, Any]

    def model(self, mid: str) -> ModelSpec:
        if mid not in self.models:
            raise KeyError(f"unknown model id '{mid}'. Known: {', '.join(sorted(self.models))}")
        return self.models[mid]

    def endpoint_of(self, mid: str) -> Endpoint:
        return self.endpoints[self.model(mid).endpoint]

    def models_for(self, task: str, endpoint: str | None = None, include_router: bool = True) -> list[ModelSpec]:
        out = [m for m in self.models.values() if m.task == task and (endpoint is None or m.endpoint == endpoint)]
        if not include_router:
            out = [m for m in out if not m.is_router]
        return out

    def suite(self, name: str) -> dict:
        base = copy.deepcopy(self.suites.get("defaults", {}))
        base.update(copy.deepcopy(self.suites.get(name, {})))
        return base

    def decoding(self, profile: str) -> dict:
        profiles = self.suites.get("decoding_profiles", {})
        if profile not in profiles:
            raise KeyError(f"unknown decoding profile '{profile}'")
        return dict(profiles[profile])

    def check_network_policy(self, endpoint_names: list[str]) -> None:
        """In closed-network mode refuse to talk to anything that is not private."""
        if not self.policy.get("offline"):
            return
        bad = [n for n in endpoint_names if not self.endpoints[n].is_private()]
        if bad:
            raise PermissionError(
                f"policy.offline is on: endpoints {bad} are not private/on-prem. "
                "Remove them from the run or set policy.offline: false in configs/policy.yaml.")


def _expand_env(obj: Any) -> Any:
    if isinstance(obj, str):
        # shell semantics: ${VAR:-default} also falls back when VAR is set but empty
        return re.sub(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}",
                      lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def load_registry() -> Registry:
    load_dotenv()
    ep_raw = _expand_env(_read_yaml("endpoints.yaml").get("endpoints", {}))
    endpoints: dict[str, Endpoint] = {}
    for name, d in ep_raw.items():
        d = dict(d)
        t = dict(DEFAULT_TIMEOUTS)
        t.update(d.pop("timeouts", {}) or {})
        endpoints[name] = Endpoint(name=name, timeouts=t, **d)
    models: dict[str, ModelSpec] = {}
    for d in _expand_env(_read_yaml("models.yaml").get("models", [])):
        if isinstance(d.get("thinking"), dict):
            # YAML 1.1 reads unquoted `on:` / `off:` keys as booleans — `thinking: {off: ..., on: ...}` would be
            # silently ignored (th.get("off") is None) without this
            d = {**d, "thinking": {({True: "on", False: "off"}.get(k, k) if isinstance(k, bool) else k): v
                                   for k, v in d["thinking"].items()}}
        m = ModelSpec(**d)
        if m.id in models:
            raise ValueError(f"duplicate model id {m.id}")
        if m.endpoint not in endpoints and not m.task.startswith("local_"):
            raise ValueError(f"model {m.id}: unknown endpoint {m.endpoint}")
        models[m.id] = m
    suites = _read_yaml("suites.yaml")
    judge = _expand_env(_read_yaml("judge.yaml"))
    policy = _read_yaml("policy.yaml")
    if os.environ.get("TRBENCH_OFFLINE") == "1":
        policy["offline"] = True
    return Registry(endpoints, models, suites, judge, policy)
