"""Local (on-prem / offline) baselines: bge-m3 & e5 embeddings, cross-encoder reranker,
Whisper (faster-whisper), Tesseract and OCR models in their own environment (Unlimited-OCR: WorkerOCR).  Heavy
dependencies are optional:

    pip install -r requirements-local.txt

GPU notes: on Volta (V100) use fp16 — bf16 is not supported.  Loaders pick float16 on CUDA and float32/int8 on
CPU unless the model's registry entry says otherwise (options.dtype: no_fp16 / fp32), and an embedder whose
half-precision output contains NaN/inf is switched to fp32 automatically (recorded in the run info).
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _device(pref: str = "auto") -> str:
    if pref != "auto":
        return pref
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def legacy_attention_mask_shim(model) -> list[str]:
    """Remote-code models written for transformers 4.x call self.get_extended_attention_mask(), which transformers 5
    removed (13 Oct: nomic-embed-text v1.5 and v2-moe failed with "'NomicBertModel' object has no attribute
    'get_extended_attention_mask'").  Their encoder classes get the transformers-4 implementation (encoder case:
    a [batch, 1, 1, seq] additive mask, 0 where attended, the dtype's minimum where padded).  Only classes loaded
    from a repository's own code (module transformers_modules.*) that lack the method are touched; decoders are
    left alone.  Returns the names of the classes patched (recorded in the run info)."""
    def get_extended_attention_mask(self, attention_mask, input_shape=None, device=None, dtype=None):
        import torch
        dtype = dtype or self.dtype
        if attention_mask.dim() == 3:
            ext = attention_mask[:, None, :, :]
        elif attention_mask.dim() == 2:
            ext = attention_mask[:, None, None, :]
        else:
            raise ValueError(f"wrong shape for attention_mask: {tuple(attention_mask.shape)}")
        ext = ext.to(dtype=dtype)
        return (1.0 - ext) * torch.finfo(dtype).min

    modules = getattr(model, "modules", None)
    if not callable(modules):
        return []
    patched = []
    for m in modules():
        cls = type(m)
        if (cls.__module__.startswith("transformers_modules") and hasattr(m, "config")
                and not getattr(m.config, "is_decoder", False) and not hasattr(cls, "get_extended_attention_mask")):
            cls.get_extended_attention_mask = get_extended_attention_mask
            patched.append(cls.__name__)
    return sorted(set(patched))


def _gpu_has_bf16() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available() and torch.cuda.is_bf16_supported())
    except Exception:
        return False


class LocalEmbedder:
    """sentence-transformers embedder.  dtype: "auto" = fp16 on CUDA, fp32 on CPU.  Models whose activations
    overflow in fp16 (EmbeddingGemma v1/v2) set dtype "no_fp16": bf16 where the GPU has it (not Volta), else fp32."""

    def __init__(self, hf_id: str, max_length: int = 1024, device: str = "auto", *, trust_remote_code: bool = False,
                 dtype: str = "auto", revision: str | None = None, attn_implementation: str | None = None):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError("local embeddings need: pip install -r requirements-local.txt") from e
        dev = _device(device)
        kw = {"trust_remote_code": True} if trust_remote_code else {}
        if revision:
            kw["revision"] = revision
        if attn_implementation:            # e.g. "sdpa" for remote code that defaults to FlashAttention 2 (UltraBERT)
            kw["model_kwargs"] = {"attn_implementation": attn_implementation}
        self.model = SentenceTransformer(hf_id, device=dev, **kw)
        self.model.max_seq_length = max_length
        # a repository's own model code written for transformers 4 (nomic) gets back what transformers 5 removed
        self.compat = [f"{c}.get_extended_attention_mask (transformers-4 shim)"
                       for c in legacy_attention_mask_shim(self.model)] if trust_remote_code else []
        self.dtype = "fp32"
        if dev == "cuda":
            if dtype in ("auto", "fp16"):
                self.model.half()
                self.dtype = "fp16"
            elif dtype in ("no_fp16", "bf16") and _gpu_has_bf16():
                self.model.bfloat16()
                self.dtype = "bf16"

    def has_prompt(self, name: str | None) -> bool:
        return bool(name) and name in (getattr(self.model, "prompts", None) or {})

    def encode(self, texts: list[str], batch_size: int = 16, prompt_name: str | None = None):
        import numpy as np
        kw = {"prompt_name": prompt_name} if self.has_prompt(prompt_name) else {}

        def run():
            v = self.model.encode(texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False,
                                  convert_to_numpy=True, **kw)
            return np.asarray(v, dtype=np.float32)

        out = run()
        if not np.isfinite(out).all() and self.dtype in ("fp16", "bf16"):
            # half precision overflowed (NaN/inf vectors): switch this model to fp32 for the rest of the run and
            # re-encode.  Recorded in the run info as e.g. "fp32 (fp16 overflow)".
            self.model.float()
            self.dtype = f"fp32 ({self.dtype} overflow)"
            out = run()
        if not np.isfinite(out).all():
            raise RuntimeError("embedder returned NaN/inf vectors even in fp32")
        return out


class LocalReranker:
    """sentence-transformers CrossEncoder.  dtype: "auto" = fp16 on CUDA; "fp32" keeps full precision (the
    Turkish ModernBERT reranker's card recommends fp32 for quality-sensitive ranking); "no_fp16" = bf16 or fp32."""

    def __init__(self, hf_id: str, device: str = "auto", *, max_length: int = 1024, dtype: str = "auto",
                 trust_remote_code: bool = False, revision: str | None = None):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            raise RuntimeError("local reranker needs: pip install -r requirements-local.txt") from e
        dev = _device(device)
        kw = {"trust_remote_code": True} if trust_remote_code else {}
        if revision:
            kw["revision"] = revision
        self.model = CrossEncoder(hf_id, device=dev, max_length=max_length, **kw)
        self.dtype = "fp32"
        if dev == "cuda":
            if dtype in ("auto", "fp16"):
                self.model.model.half()
                self.dtype = "fp16"
            elif dtype in ("no_fp16", "bf16") and _gpu_has_bf16():
                self.model.model.bfloat16()
                self.dtype = "bf16"

    def score(self, query: str, docs: list[str]) -> list[float]:
        return [float(x) for x in self.model.predict([(query, d) for d in docs], batch_size=16, show_progress_bar=False)]


_CUDA_LIB_HINTS = ("cublas", "cudnn", "cuda", "libcu", ".dll", "cufft", "nvrtc")


def _wav_16k_mono(path: Path):
    """The ASR clips (scripts/build_asr_clips.py) are 16 kHz mono 16-bit PCM WAV: decoded here into the float32
    array faster-whisper takes, so its own audio decoder (PyAV) is never needed.  None for any other format."""
    import wave

    import numpy as np
    try:
        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 1 or w.getframerate() != 16000 or w.getsampwidth() != 2:
                return None
            return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    except (wave.Error, OSError, EOFError):
        return None


class LocalWhisper:
    """faster-whisper (CTranslate2).  large-v3 on CPU is slow but works; on a V100 use float16.  CTranslate2 brings
    its own CUDA build: when its CUDA / cuDNN libraries do not match the machine (a Windows laptop with a newer torch
    build, say), loading or the first transcription fails — then the model is loaded again on the CPU (int8) and the
    run goes on; `info` records the fallback, so a missing library never turns into 22 failed transcriptions."""

    def __init__(self, size: str = "large-v3", device: str = "auto", compute_type: str | None = None):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError("local Whisper needs: pip install faster-whisper") from e
        self._cls, self.size = WhisperModel, size
        dev = _device(device)
        ct = compute_type or ("float16" if dev == "cuda" else "int8")
        self.info = {"size": size, "device": dev, "compute_type": ct}
        try:
            self.model = WhisperModel(size, device=dev, compute_type=ct)
        except Exception as exc:
            if dev != "cuda":
                raise
            self._to_cpu(f"{type(exc).__name__}: {exc}")

    def _to_cpu(self, why: str) -> None:
        self.model = self._cls(self.size, device="cpu", compute_type="int8")
        self.info.update({"device": "cpu", "compute_type": "int8", "fallback_from_cuda": why[:300]})

    def transcribe(self, path: Path, language: str = "tr") -> tuple[str, str | None]:
        audio = _wav_16k_mono(path)        # 12 Oct: faster-whisper 1.2.1 + a newer PyAV failed on every clip with
        src = audio if audio is not None else str(path)    # "open() got an unexpected keyword 'metadata_errors'"
        for attempt in (0, 1):
            try:
                segs, _ = self.model.transcribe(src, language=language, beam_size=5, temperature=0.0,
                                                condition_on_previous_text=False, vad_filter=False)
                return " ".join(s.text.strip() for s in segs).strip(), None
            except Exception as exc:  # report, never crash the run
                msg = f"{type(exc).__name__}: {exc}"
                if attempt == 0 and self.info["device"] == "cuda" and any(h in msg.lower() for h in _CUDA_LIB_HINTS):
                    self._to_cpu(msg)
                    continue
                return "", msg
        return "", "unreachable"


class TesseractOCR:
    def __init__(self, lang: str = "tur", psm: int = 3):
        self.exe = shutil.which("tesseract") or os.environ.get("TESSERACT_EXE")
        if not self.exe:
            raise RuntimeError("tesseract not found (install it and the 'tur' language data, or set TESSERACT_EXE)")
        self.lang, self.psm = lang, psm

    def run(self, image: Path) -> tuple[str, str | None]:
        try:
            p = subprocess.run([self.exe, str(image), "stdout", "-l", self.lang, "--psm", str(self.psm)],
                               capture_output=True, timeout=300)
            if p.returncode != 0:
                return "", p.stderr.decode("utf-8", "replace")[:500]
            return p.stdout.decode("utf-8", "replace"), None
        except Exception as exc:
            return "", f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ OCR models in their own Python environment
def worker_python(option: str | None) -> str:
    """The interpreter of the OCR worker's environment: options.python, else .venv-ocr (Windows or Linux layout).
    TRBENCH_OCR_WORKER_FAKE (tests): this interpreter."""
    if os.environ.get("TRBENCH_OCR_WORKER_FAKE"):
        return sys.executable
    cands = [option] if option and option != "auto" else [".venv-ocr/Scripts/python.exe", ".venv-ocr/bin/python"]
    for c in cands:
        p = Path(c) if Path(c).is_absolute() else ROOT / c
        if p.exists():
            return str(p)
    raise RuntimeError("the OCR model's own environment is missing (.venv-ocr): run "
                       "powershell -ExecutionPolicy Bypass -File scripts\\setup_unlimited_ocr.ps1 -Yes first")


class WorkerOCR:
    """An OCR model that runs in its own Python environment (Unlimited-OCR: transformers 4.57.1, while .venv has
    5.x).  harness/local/ocr_worker.py runs there and reads one page per request over JSON lines; this class starts
    it (the model loads once), sends pages and collects text, timings and peak GPU memory.  The worker's own output
    (the model's prints, tracebacks) goes to <session>/ocr/<model>.worker.log.  A worker that dies or stops
    answering is restarted for the next page (at most twice); such pages are infrastructure failures (not final,
    re-sent by the next run), a page the model itself fails on is a final local_error."""

    def __init__(self, cmd: list[str], log_path: Path, startup_timeout: float = 3600.0, page_timeout: float = 900.0):
        self.cmd, self.log_path = cmd, Path(log_path)
        self.startup_timeout, self.page_timeout = startup_timeout, page_timeout
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.log = None
        self.info: dict = {}
        self.n, self.restarts = 0, 0
        self._start()

    @classmethod
    def from_spec(cls, spec, log_dir: Path) -> WorkerOCR:
        from ..store import safe_name
        o = spec.options or {}
        cmd = [worker_python(o.get("python")), str(ROOT / "harness" / "local" / "ocr_worker.py"), "--model", spec.api_model]
        for k in ("revision", "prompt", "base_size", "image_size", "crop_mode", "max_length", "no_repeat_ngram_size",
                  "ngram_window", "fallback_base_mode"):
            if o.get(k) is not None:
                v = o[k]
                cmd += [f"--{k.replace('_', '-')}", str(int(v)) if isinstance(v, bool) else str(v)]
        if os.environ.get("TRBENCH_OCR_WORKER_FAKE"):
            cmd.append("--fake")
        return cls(cmd, Path(log_dir) / f"{safe_name(spec.id)}.worker.log",
                   startup_timeout=float(o.get("startup_timeout_s", 3600)), page_timeout=float(o.get("page_timeout_s", 900)))

    def _pump(self, proc: subprocess.Popen, q: queue.Queue) -> None:
        for line in iter(proc.stdout.readline, b""):
            q.put(line.decode("utf-8", "replace"))
        q.put(None)

    def _next(self, timeout: float) -> dict | None:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError
            try:
                line = self.q.get(timeout=left)
            except queue.Empty:
                raise TimeoutError from None
            if line is None:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            if isinstance(msg, dict):
                return msg
            self.log.write(f"[stdout, not protocol] {line}")

    def _start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(self.log_path, "a", encoding="utf-8")                 # noqa: SIM115 — lives with the worker
        self.log.write(f"\n== {datetime.now().astimezone().isoformat(timespec='seconds')} start: {' '.join(self.cmd)}\n")
        self.log.flush()
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        # Windows: its own process group, so Ctrl+C in the console stops the harness (which then closes the worker)
        # instead of killing the worker mid-page
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                     cwd=str(ROOT), env=env, creationflags=flags)
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc, self.q), daemon=True).start()
        try:
            msg = self._next(self.startup_timeout)
        except TimeoutError:
            msg = {"error": f"not ready after {self.startup_timeout:.0f} s"}
        if not msg or msg.get("event") != "ready":
            self._kill()
            why = (msg or {}).get("error") or f"it exited (code {self.proc.poll()}) before it was ready"
            raise RuntimeError(f"OCR worker did not start: {why} — see {self.log_path}")
        self.info = {k: v for k, v in msg.items() if k != "event"}

    def _kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass

    def run_detail(self, image: Path) -> dict:
        """One page.  restart_s (when the worker had to be started again first) is reported separately, so the
        caller can keep the model load out of the page's time."""
        with self.lock:
            restart_s = None
            if self.proc is None or self.proc.poll() is not None:
                if self.restarts >= 2:
                    return {"text": "", "error": f"OCR worker not running (see {self.log_path})", "infra": True}
                self.restarts += 1
                t0 = time.perf_counter()
                try:
                    self._start()
                except RuntimeError as exc:
                    return {"text": "", "error": str(exc), "infra": True}
                restart_s = round(time.perf_counter() - t0, 1)
            msg = self._page(image)
            if restart_s is not None:
                msg["restart_s"] = restart_s
            return msg

    def _page(self, image: Path) -> dict:
        self.n += 1
        try:
            self.proc.stdin.write((json.dumps({"id": self.n, "image": str(image)}) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            msg = self._next(self.page_timeout)
        except TimeoutError:
            self._kill()
            return {"text": "", "error": f"no answer within {self.page_timeout:.0f} s (worker stopped; it restarts "
                                         f"for the next page)", "infra": True}
        except OSError as exc:
            self._kill()
            return {"text": "", "error": f"worker pipe: {type(exc).__name__}: {exc}", "infra": True}
        if msg is None:
            return {"text": "", "error": f"OCR worker exited (code {self.proc.poll()}; see {self.log_path})",
                    "infra": True}
        return msg

    def run(self, image: Path) -> tuple[str, str | None]:
        d = self.run_detail(image)
        return d.get("text") or "", d.get("error")

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b'{"cmd": "quit"}\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                self._kill()
        if self.log is not None:
            self.log.close()


def make_local_ocr(spec, log_dir: Path):
    """The in-process / own-environment OCR backend of a local_ocr model (options.backend: tesseract | worker)."""
    o = spec.options or {}
    backend = o.get("backend", "tesseract")
    if backend == "tesseract":
        return TesseractOCR(o.get("lang", "tur"), o.get("psm", 3))
    if backend == "worker":
        return WorkerOCR.from_spec(spec, log_dir)
    raise RuntimeError(f"unknown local OCR backend '{backend}' for {spec.id}")


# ============================================================================ guard classifier (in-process)
def guard_label_rule(labels: list[str]) -> tuple[str | None, list[str]]:
    """Which output is the overall unsafe probability, and which labels are harm categories (every label but SAFE and
    the overall one).  Chosen from the label names alone, before any prompt is scored."""
    names = {"unsafe", "is_unsafe", "unsafety", "harmful", "toxic"}
    overall = next((lab for lab in labels if str(lab).casefold() in names), None)
    return overall, [lab for lab in labels if lab != overall and str(lab).casefold() != "safe"]


def guard_answer(scores: dict, overall: str | None, categories: list[str], threshold: float) -> dict:
    """p_unsafe = the model's overall unsafe output when it has one, else its highest harm-category probability;
    the prompt is unsafe when p_unsafe > threshold."""
    p = scores[overall] if overall else max(scores[c] for c in categories)
    return {"unsafe": p, "threshold": threshold,
            "rule": (f"p({overall})" if overall else "max harm category") + f" > {threshold}",
            "categories": [c for c in categories if scores[c] > threshold], "scores": scores}


class LocalGuardClassifier:
    """A safety classifier run in-process with transformers (ytu-ce-cosmos/modernbert-tr-guardrail: ModernBERT with
    one sigmoid per label).  fp32 (a 150M model: no half-precision surprises), the prompt as-is, truncated only
    beyond the model's length.  The revision actually loaded is recorded with every answer."""

    def __init__(self, hf_id: str, *, revision: str | None = None, device: str = "auto", threshold: float = 0.5):
        try:
            import torch
            import transformers
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as e:
            raise RuntimeError("local guard needs: pip install -r requirements-local.txt") from e
        self.torch, self.hf_id, self.threshold = torch, hf_id, float(threshold)
        self.device = _device(device)
        self.tok = AutoTokenizer.from_pretrained(hf_id, revision=revision)
        model = AutoModelForSequenceClassification.from_pretrained(hf_id, revision=revision)
        self.model = model.float().to(self.device).eval()
        cfg = self.model.config
        self.revision = getattr(cfg, "_commit_hash", None) or revision or "main"
        self.labels = [str(cfg.id2label[i]) for i in range(cfg.num_labels)]
        if all(lab.startswith("LABEL_") and lab[6:].isdigit() for lab in self.labels):
            raise RuntimeError(f"{hf_id}: the model has no label names (id2label), so its unsafe output is unknown")
        self.overall, self.categories = guard_label_rule(self.labels)
        if not self.overall and not self.categories:
            raise RuntimeError(f"{hf_id}: no unsafe output and no harm category among {self.labels}")
        self.activation = "softmax" if cfg.problem_type == "single_label_classification" else "sigmoid"
        lim = [x for x in (getattr(self.tok, "model_max_length", None), getattr(cfg, "max_position_embeddings", None))
               if isinstance(x, int) and 0 < x < 10 ** 6]
        self.max_length = min(lim) if lim else 512
        self.versions = {"torch": torch.__version__, "transformers": transformers.__version__}
        self.device_name = torch.cuda.get_device_name(0) if self.device.startswith("cuda") else "cpu"
        self._lock = threading.Lock()

    def describe(self) -> str:
        rule = (f"p({self.overall})" if self.overall else "max harm category") + f" > {self.threshold}"
        return (f"revision {self.revision}, {self.device} ({self.device_name}), fp32, {self.activation}, "
                f"max_length {self.max_length}, labels {self.labels}; unsafe when {rule}")

    def params(self) -> dict:
        return {"local": "transformers", "hf_id": self.hf_id, "revision": self.revision,
                "device": f"{self.device} ({self.device_name})", "dtype": "float32", "activation": self.activation,
                "max_length": self.max_length, "threshold": self.threshold, **self.versions}

    def classify(self, text: str) -> dict:
        torch = self.torch
        with self._lock, torch.inference_mode():
            enc = self.tok(text, truncation=True, max_length=self.max_length, return_tensors="pt").to(self.device)
            logits = self.model(**enc).logits[0].float()
            probs = torch.softmax(logits, -1) if self.activation == "softmax" else torch.sigmoid(logits)
            n_tok = int(enc["input_ids"].shape[1])
        scores = {lab: round(float(p), 6) for lab, p in zip(self.labels, probs.tolist())}
        out = guard_answer(scores, self.overall, self.categories, self.threshold)
        out["tokens"], out["truncated"] = n_tok, n_tok >= self.max_length
        return out
