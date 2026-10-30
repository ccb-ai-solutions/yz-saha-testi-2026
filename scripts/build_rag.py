"""Validate the RAG set and (re)build the derived files used by every suite:
data/rag/derived/chunks.jsonl, gold.json (question -> gold chunk ids), contexts.json (fixed generator contexts).
Run after any edit to data/rag/corpus/*.md or data/rag/questions.jsonl."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.rag_data import build_derived  # noqa: E402


def main() -> int:
    rc = subprocess.call([sys.executable, str(ROOT / "scripts" / "validate_rag.py"), "--root", str(ROOT / "data" / "rag")])
    if rc != 0:
        print("validation failed — derived files not rebuilt")
        return rc
    s = build_derived()
    print(f"chunks: {s['chunks']} (words min/mean/max {s['min_words']}/{s['mean_words']}/{s['max_words']})")
    for p in s["problems"]:
        print("  warning:", p)
    if s["answerable_without_gold"]:
        print("ERROR: answerable questions without gold chunks:", s["answerable_without_gold"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
