"""Build a self-contained bundle for a closed (air-gapped) client network.

Run on a connected machine with the SAME OS / Python version as the target:
    python scripts/make_offline_bundle.py --out bundle --with-local-models

Bundle contents
  bundle/wheels/          pip wheels for requirements.txt (+ requirements-local.txt with --with-local-models)
  bundle/hf_cache/        Hugging Face snapshots for the local baselines (bge-m3, e5, bge-reranker, faster-whisper)
  bundle/trbench/         this repository without results/, report/, .env
  bundle/INSTALL_OFFLINE.md
Nothing in the bundle phones home: set HF_HUB_OFFLINE=1 and TRBENCH_OFFLINE=1 on the target.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HF_MODELS = ["BAAI/bge-m3", "intfloat/multilingual-e5-large", "intfloat/multilingual-e5-large-instruct",
             "ytu-ce-cosmos/turkish-e5-large", "google/embeddinggemma-300m",
             "BAAI/bge-reranker-v2-m3", "Systran/faster-whisper-large-v3"]
# never shipped to a client site: secrets, results, other clients' data, licence-restricted samples
EXCLUDE = ("results", "report", "bundle", ".env", ".venv", "__pycache__", ".pytest_cache", "_fleurs_cache",
           "client_data", "knowledge_base", "trmmlu_sample.jsonl", "global_mmlu_tr_sample.jsonl", "*.log")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="bundle")
    ap.add_argument("--with-local-models", action="store_true")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    (out / "wheels").mkdir(parents=True, exist_ok=True)
    reqs = ["requirements.txt"] + (["requirements-local.txt"] if a.with_local_models else [])
    for r in reqs:
        subprocess.check_call([sys.executable, "-m", "pip", "download", "-r", str(ROOT / r), "-d", str(out / "wheels")])
    if a.with_local_models:
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            raise SystemExit("pip install huggingface_hub first")
        for m in HF_MODELS:
            print("downloading", m)
            snapshot_download(m, cache_dir=str(out / "hf_cache"))      # read back via HF_HUB_CACHE
    dst = out / "trbench"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(*EXCLUDE))
    local = "pip install --no-index --find-links wheels -r trbench/requirements-local.txt" if a.with_local_models else ""
    (out / "INSTALL_OFFLINE.md").write_text(f"""# Offline install (closed network)

PowerShell:
```
python -m venv .venv ; .\\.venv\\Scripts\\Activate.ps1
pip install --no-index --find-links wheels -r trbench\\requirements.txt
{local}
$env:HF_HUB_CACHE = "$PWD\\hf_cache" ; $env:HF_HUB_OFFLINE = "1" ; $env:TRBENCH_OFFLINE = "1"
cd trbench ; python -m pytest tests -q
```
Linux:
```
python3 -m venv .venv && source .venv/bin/activate
pip install --no-index --find-links wheels -r trbench/requirements.txt
{local}
export HF_HUB_CACHE=$PWD/hf_cache HF_HUB_OFFLINE=1 TRBENCH_OFFLINE=1
cd trbench && python -m pytest tests -q
```
Then point LOCAL_BASE_URL at the on-prem inference server, set `model: judge-local` in configs/judge.yaml,
and follow README.md.  With TRBENCH_OFFLINE=1 every non-private endpoint is refused (judge included).
Knowledge MCQ files are not shipped (licences): build them on a connected machine and copy data/mcq/ if allowed.
""", encoding="utf-8")
    print(f"bundle ready: {out}")


if __name__ == "__main__":
    main()
