"""OCR worker for models that need their own Python environment — Unlimited-OCR (baidu/Unlimited-OCR).

Unlimited-OCR's code (trust_remote_code) was tested with transformers 4.57.1 / torch 2.10 (model card), while the
harness environment (.venv) has transformers 5.x.  So the model runs here, in .venv-ocr
(scripts/setup_unlimited_ocr.ps1), and the harness talks to this process over JSON lines
(harness/local/backends.py: WorkerOCR).  This file imports nothing from the harness.

Protocol — stdout carries ONLY protocol lines (whatever the model code prints is moved to stderr = the worker log):
  -> {"event": "ready", "device": ..., "load_s": ..., ...}      once the model is loaded, or {"event": "fatal", ...}
  <- {"id": 1, "image": "<path>"}                              one page per line on stdin
  -> {"id": 1, "text": ..., "raw": ..., "infer_s": ..., "peak_vram_gb": ..., "error": null, ...}
  <- {"cmd": "quit"}

The page text is the model card's own post-processing of the raw output (parse_document).  Timing: infer_s is
model.infer() for one page (image preprocessing + generation, GPU synchronised), measured here; the harness also
records the round trip.  --fake: no torch, no model — answers from the page's ground truth (tests only).
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

# The model card's OmniDocBench post-processing: a raw block is "<|det|>TYPE [x1, y1, x2, y2]<|/det|>text" followed
# by plain lines; image blocks are dropped, a block's lines are joined with "\n", blocks with a blank line.
DET = re.compile(r"<\|det\|>([^<\s]+)(?:\s*\[[^\]]*\])?\s*<\|/det\|>(.*)", re.DOTALL)


def parse_document(raw: str) -> str:
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for line in (raw or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        m = DET.match(line.strip())
        if m:
            if m.group(1) == "image":         # as in the model card: an image block is skipped, the current text
                continue                       # block stays open
            cur = []
            blocks.append(cur)
            if m.group(2).strip():
                cur.append(m.group(2).strip())
            continue
        if cur is None:
            cur = []
            blocks.append(cur)
        cur.append(line)
    return "\n\n".join("\n".join(b) for b in blocks if b).strip()


class Engine:
    def __init__(self, a: argparse.Namespace):
        import torch
        import transformers
        from transformers import AutoModel, AutoTokenizer
        if not torch.cuda.is_available():
            raise RuntimeError("torch sees no CUDA GPU (Unlimited-OCR's code calls .cuda()); check the torch build in "
                               ".venv-ocr: python -c \"import torch; print(torch.version.cuda, torch.cuda.is_available())\"")
        t0 = time.perf_counter()
        kw = {"trust_remote_code": True, "revision": a.revision}
        self.tok = AutoTokenizer.from_pretrained(a.model, **kw)
        model = AutoModel.from_pretrained(a.model, use_safetensors=True, torch_dtype=torch.bfloat16, **kw)
        self.model = model.eval().cuda()
        torch.cuda.synchronize()
        self.torch, self.a = torch, a
        self.out_dir = tempfile.mkdtemp(prefix="trbench-uocr-")      # infer() wants an output folder (unused)
        props = torch.cuda.get_device_properties(0)
        self.device = f"{props.name} ({props.total_memory / 2**30:.1f} GB)"
        self.info = {"device": self.device, "load_s": round(time.perf_counter() - t0, 1), "model": a.model,
                     "revision": a.revision, "dtype": "bfloat16", "torch": torch.__version__,
                     "torch_cuda": torch.version.cuda, "transformers": transformers.__version__,
                     "weights_vram_gb": round(torch.cuda.memory_allocated() / 2**30, 2),
                     "settings": {"prompt": a.prompt, "base_size": a.base_size, "image_size": a.image_size,
                                  "crop_mode": bool(a.crop_mode), "max_length": a.max_length,
                                  "no_repeat_ngram_size": a.no_repeat_ngram_size, "ngram_window": a.ngram_window,
                                  "fallback_base_mode": bool(a.fallback_base_mode)}}

    def _infer(self, image: Path, crop: bool) -> str:
        a = self.a
        out = self.model.infer(self.tok, prompt=a.prompt, image_file=str(image), output_path=self.out_dir,
                               base_size=a.base_size, image_size=a.image_size if crop else a.base_size,
                               crop_mode=crop, save_results=False, eval_mode=True, max_length=a.max_length,
                               no_repeat_ngram_size=a.no_repeat_ngram_size, ngram_window=a.ngram_window)
        if not isinstance(out, str):
            raise TypeError(f"model.infer returned {type(out).__name__}, not the page text (eval_mode)")
        return out

    def read(self, image: Path) -> dict:
        torch = self.torch
        crop = bool(self.a.crop_mode)
        mode = "gundam (crops)" if crop else "base"
        torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        oom = False
        try:
            raw = self._infer(image, crop)
        except torch.cuda.OutOfMemoryError:
            if not (crop and self.a.fallback_base_mode):
                raise
            oom = True        # retried below, AFTER the handler: inside it the failed call's tensors are still held
        if oom:
            self.recover()                           # the page again in base mode (one 1024 view, no crops)
            torch.cuda.reset_peak_memory_stats()
            mode = "base (gundam ran out of GPU memory)"
            t0 = time.perf_counter()
            raw = self._infer(image, False)
        torch.cuda.synchronize()
        dt = time.perf_counter() - t0
        n_tok = len(self.tok.encode(raw, add_special_tokens=False)) if raw else 0
        return {"text": parse_document(raw), "raw": raw, "infer_s": round(dt, 3), "tokens_out": n_tok,
                "tokens_per_s": round(n_tok / dt, 1) if dt > 0 else None, "mode": mode, "device": self.device,
                "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2)}

    def recover(self) -> None:
        gc.collect()                             # after a failed page: free what it held for the next one
        self.torch.cuda.empty_cache()


class FakeEngine:
    """Tests: no torch, no model.  Answers with the page's ground truth in the model's raw block format."""

    def __init__(self, a: argparse.Namespace):
        self.info = {"device": "fake", "load_s": 0.0, "model": a.model, "revision": a.revision}

    def read(self, image: Path) -> dict:
        print("directly resize")                     # model code prints to stdout: must not reach the protocol
        gt = image.with_suffix("").with_suffix(".gt.txt")
        lines = [x for x in gt.read_text(encoding="utf-8").splitlines() if x.strip()] if gt.exists() else ["?"]
        raw = (f"<|det|>title [10, 10, 900, 40]<|/det|>{lines[0]}\n<|det|>image [0, 0, 5, 5]<|/det|>\n"
               + "<|det|>text [10, 50, 900, 990]<|/det|>" + "\n".join(lines[1:]) + "\n")
        return {"text": parse_document(raw), "raw": raw, "infer_s": 0.01, "tokens_out": len(raw) // 3,
                "tokens_per_s": None, "mode": "fake", "device": "fake", "peak_vram_gb": 0.0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default="baidu/Unlimited-OCR")
    ap.add_argument("--revision")
    ap.add_argument("--prompt", default="<image>document parsing.")
    ap.add_argument("--base-size", type=int, default=1024)
    ap.add_argument("--image-size", type=int, default=640)
    ap.add_argument("--crop-mode", type=int, default=1)
    ap.add_argument("--max-length", type=int, default=8192)
    ap.add_argument("--no-repeat-ngram-size", type=int, default=35)
    ap.add_argument("--ngram-window", type=int, default=128)
    ap.add_argument("--fallback-base-mode", type=int, default=1)
    ap.add_argument("--fake", action="store_true")
    a = ap.parse_args()
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)                       # fd 1 (C-level prints too) -> stderr; Python prints as well
    sys.stdout = sys.stderr

    def send(obj: dict) -> None:
        proto.write(json.dumps(obj, ensure_ascii=True) + "\n")
        proto.flush()

    try:
        engine = FakeEngine(a) if a.fake else Engine(a)
    except BaseException as exc:        # noqa: BLE001 — reported to the harness, which stops this model
        traceback.print_exc()
        send({"event": "fatal", "error": f"{type(exc).__name__}: {exc}"[:2000]})
        return 2
    send({"event": "ready", **engine.info})
    for line in sys.stdin.buffer:
        try:
            req = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if req.get("cmd") == "quit":
            break
        out: dict = {"id": req.get("id")}
        failed = False
        try:
            out.update(engine.read(Path(req["image"])))
            out["error"] = None
        except Exception as exc:        # noqa: BLE001 — one page must not end the worker
            traceback.print_exc()
            out.update({"text": "", "error": f"{type(exc).__name__}: {exc}"[:1000]})
            failed = True
        if failed:                      # outside the handler, so the failed page's GPU memory can be released
            getattr(engine, "recover", lambda: None)()
        send(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
