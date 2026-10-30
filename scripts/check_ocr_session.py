"""Has an OCR session read every page with every OCR model?  The extraction over OCR output builds its plan from the
OCR text, so it must not start while the OCR step is still running or after it ended with pages missing
(scripts/run_phase3e.ps1 asks this before its extraction steps).  Offline: reads result files only.

    python scripts/check_ocr_session.py evren-ocr dots-ocr deepseek-ocr-2     # exit 0: every page read; 3: not yet
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.config import DATA_DIR, RESULTS_DIR  # noqa: E402
from harness.store import read_jsonl, safe_name  # noqa: E402


def ocr_progress(results: Path, session: str, models: list[str], pages: list[str]) -> dict[str, int] | None:
    """{model: pages read with a final 'ok'} for one session, or None when the session has no OCR results yet."""
    hits = sorted(results.glob(f"track-*/*/{session}/ocr"))
    if not hits:
        return None
    want, out = set(pages), {}
    for m in models:
        p = hits[0] / f"{safe_name(m)}.jsonl"
        recs = read_jsonl(p) if p.exists() else []
        out[m] = len({r.get("item_id") for r in recs
                      if r.get("final", True) and (r.get("result") or {}).get("outcome") == "ok"} & want)
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    session, models = argv[0], argv[1:]
    pages = [m["id"] for m in read_jsonl(DATA_DIR / "ocr" / "manifest.jsonl")]
    got = ocr_progress(RESULTS_DIR, session, models, pages)
    if got is None:
        print(f"session {session}: no OCR results yet")
        return 3
    print(f"session {session}: " + ", ".join(f"{m} {n}/{len(pages)} pages read" for m, n in got.items()))
    return 0 if all(n == len(pages) for n in got.values()) else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
