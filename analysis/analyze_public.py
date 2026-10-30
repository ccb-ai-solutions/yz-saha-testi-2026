"""Public recompute: analyze.py without the systems that are not published.

    python analysis/analyze_public.py --out report/public-20261030        # same options as analyze.py

Left out of the public version (they stay in the internal master): EVREN's 'auto' router (its result depends on
EVREN's routing at the time; five different models were seen behind it) and the three models measured on NVIDIA's
API trial endpoints, whose terms allow internal evaluation only.  Their records are dropped as they are read, and
NVIDIA's sessions are skipped, so every fairness gate, pooled statistic, bootstrap rank interval and Holm family is
computed on the published systems alone — not a copy of the full report with rows deleted.  The knowledge base is
not updated by this run.  analyze.py itself is unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as A  # noqa: E402

EXCLUDED_MODELS = {"auto", "muse-glimmer-30b", "nemotron-3.5-lightning", "nemotron-3.5-super-vl"}
EXCLUDED_ENDPOINTS = {"nvidia"}


def _excluded(r) -> bool:
    if not isinstance(r, dict):
        return False
    mid = r.get("model_id") or str(r.get("variant") or "").split(":")[0]
    return mid in EXCLUDED_MODELS or r.get("endpoint") in EXCLUDED_ENDPOINTS


_read_jsonl = A.read_jsonl
A.read_jsonl = lambda p: [r for r in _read_jsonl(p) if not _excluded(r)]

_find_sessions = A.find_sessions
A.find_sessions = lambda *a, **k: [s for s in _find_sessions(*a, **k)
                                   if A.manifest(s).get("endpoint") not in EXCLUDED_ENDPOINTS]

_human_labels = A._human_labels


def _human_labels_public(sessions, signature):
    d = _human_labels(sessions, signature)
    if d is None:
        return None
    d = d[~d["variant"].astype(str).str.split(":").str[0].isin(EXCLUDED_MODELS)]
    return d if len(d) else None


A._human_labels = _human_labels_public

_method_notes = A.method_notes
A.method_notes = lambda results, sessions, reg: (
    ["Public version: EVREN's auto router and the three models measured on NVIDIA's API trial (muse-glimmer-30b, "
     "nemotron-3.5-lightning, nemotron-3.5-super-vl) are left out; their records are dropped before any statistic "
     "is computed (analysis/analyze_public.py)."] + _method_notes(results, sessions, reg))

A.update_kb = lambda *a, **k: 0

if __name__ == "__main__":
    A.main()
