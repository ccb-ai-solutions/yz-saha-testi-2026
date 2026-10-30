"""RAG corpus utilities: deterministic chunking, evidence→chunk mapping, Turkish BM25.

The same chunk texts are used by every retriever and by every generator prompt,
so differences in scores cannot come from different preprocessing.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR
from .textnorm import norm_ws, tr_lower, words

RAG_DIR = DATA_DIR / "rag"
DERIVED = RAG_DIR / "derived"
TARGET_WORDS = 150
MAX_WORDS = 220
REFUSAL = "Belgelerde bu soruya yanıt verecek bilgi bulunmuyor."


@dataclass
class Doc:
    doc_id: str
    meta: dict
    body: str


def load_corpus(root: Path = RAG_DIR) -> list[Doc]:
    docs = []
    for p in sorted((root / "corpus").glob("*.md")):
        raw = p.read_text(encoding="utf-8")
        _, fm, body = raw.split("---\n", 2)
        meta = {}
        for line in fm.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        docs.append(Doc(meta["doc_id"], meta, body))
    return docs


def _blocks(body: str) -> list[tuple[str, str]]:
    """Split markdown into (kind, text) blocks: heading lines, tables, paragraphs/list items."""
    out: list[tuple[str, str]] = []
    para: list[str] = []
    table: list[str] = []

    def flush_para():
        if para:
            out.append(("p", "\n".join(para)))
            para.clear()

    def flush_table():
        if table:
            out.append(("t", "\n".join(table)))
            table.clear()

    for line in body.splitlines():
        s = line.rstrip()
        if re.match(r"^#{1,6}\s", s):
            flush_para(); flush_table()
            out.append(("h", s))
        elif s.strip().startswith("|"):
            flush_para()
            table.append(s)
        elif not s.strip():
            flush_para(); flush_table()
        else:
            flush_table()
            # list items are their own blocks (one fact per line in our corpus)
            if re.match(r"^\s*([-*•]|\d+[.)])\s+", s) and para and not re.match(r"^\s*([-*•]|\d+[.)])\s+", para[-1]):
                flush_para()
            para.append(s)
    flush_para(); flush_table()
    return out


def _split_long(text: str) -> list[str]:
    if len(words(text)) <= MAX_WORDS:
        return [text]
    sents = re.split(r"(?<=[.!?;:])\s+", text)
    parts, cur = [], []
    for s in sents:
        if cur and len(words(" ".join(cur + [s]))) > TARGET_WORDS:
            parts.append(" ".join(cur))
            cur = []
        cur.append(s)
    if cur:
        parts.append(" ".join(cur))
    return parts


def chunk_doc(doc: Doc) -> list[dict]:
    title = doc.meta.get("title", doc.doc_id)
    chunks: list[dict] = []
    heads: dict[int, str] = {}
    cur: list[str] = []
    cur_path = ""

    def emit():
        nonlocal cur
        if cur:
            text = "\n\n".join(cur).strip()
            chunks.append({"doc_id": doc.doc_id, "title": title, "section": cur_path, "body": text})
            cur = []

    for kind, text in _blocks(doc.body):
        if kind == "h":
            level = len(text) - len(text.lstrip("#"))
            name = text.lstrip("#").strip()
            if level == 1:
                continue
            emit()
            heads = {k: v for k, v in heads.items() if k < level}
            heads[level] = name
            cur_path = " > ".join(heads[k] for k in sorted(heads))
            continue
        for piece in (_split_long(text) if kind == "p" else [text]):
            if cur and len(words("\n\n".join(cur + [piece]))) > TARGET_WORDS:
                emit()
            cur.append(piece)
    emit()
    for i, c in enumerate(chunks):
        c["chunk_id"] = f"{doc.doc_id}#{i:03d}"
        header = f"{c['title']} — {c['section']}" if c["section"] else c["title"]
        c["text"] = f"{header}\n{c['body']}"
        c["words"] = len(words(c["body"]))
    return chunks


def build_chunks(root: Path = RAG_DIR) -> list[dict]:
    out = []
    for d in load_corpus(root):
        out.extend(chunk_doc(d))
    return out


def load_questions(root: Path = RAG_DIR) -> list[dict]:
    return [json.loads(l) for l in (root / "questions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


def map_evidence(questions: list[dict], chunks: list[dict]) -> tuple[dict, list[str]]:
    """question id -> sorted gold chunk ids (chunks containing an evidence string)."""
    normed = [(c["chunk_id"], c["doc_id"], norm_ws(c["body"])) for c in chunks]
    gold: dict[str, list[str]] = {}
    problems: list[str] = []
    for q in questions:
        ids: set[str] = set()
        for ev in q.get("evidence", []):
            nev = norm_ws(ev)
            hits = [cid for cid, did, txt in normed if nev in txt and did in q["docs"]]
            if not hits:
                # evidence straddles a split: accept chunks holding its first and last halves
                w = nev.split()
                head, tail = " ".join(w[: len(w) // 2]), " ".join(w[len(w) // 2:])
                hits = [cid for cid, did, txt in normed if did in q["docs"] and (head in txt or tail in txt)]
                if hits:
                    problems.append(f"{q['id']}: evidence split across chunks {hits}")
                else:
                    problems.append(f"{q['id']}: evidence not found in any chunk: {ev[:60]!r}")
            ids.update(hits)
        gold[q["id"]] = sorted(ids)
    return gold, problems


# ----------------------------------------------------------------------------- BM25 (Turkish, F5 stemming)
TR_STOPWORDS = set("""acaba ama ancak artık aslında az bana bazen bazı belki ben beni benim bile bir biri birkaç
birşey biz bize bizi bizim bu buna bunda bundan bunu bunun burada çok çünkü da daha dahi de defa diye en
gibi hem hep hepsi her hiç için ile ise kadar ki kim mi mı mu mü nasıl ne neden nerde nerede nereye niye
niçin o olan olarak oldu olduğu olduğunu olsa olur ona ondan onlar onları onların onu onun orada sanki şey
siz şu şuna şunda şundan şunu tüm ve veya ya yani yine ayrıca göre olan olup üzere""".split())


def bm25_tokens(text: str) -> list[str]:
    """Turkish IR tokenisation: Turkish lower-case, stopwords removed, F5 stemming (first 5 letters)."""
    return [w[:5] for w in (tr_lower(t) for t in words(text)) if w not in TR_STOPWORDS and len(w) > 1]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.toks = [bm25_tokens(d) for d in docs]
        self.tf = [Counter(t) for t in self.toks]
        self.dl = [len(t) for t in self.toks]
        self.avgdl = sum(self.dl) / max(1, len(self.dl))
        df: Counter = Counter()
        for t in self.tf:
            df.update(t.keys())
        n = len(docs)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}

    def scores(self, query: str) -> list[float]:
        q = bm25_tokens(query)
        out = []
        for tf, dl in zip(self.tf, self.dl):
            s = 0.0
            for w in q:
                f = tf.get(w)
                if f:
                    s += self.idf.get(w, 0.0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            out.append(s)
        return out

    def rank(self, query: str) -> list[int]:
        sc = self.scores(query)
        return sorted(range(len(sc)), key=lambda i: (-sc[i], i))


def oracle_contexts(questions: list[dict], chunks: list[dict], gold: dict, k: int = 8, max_gold: int = 3) -> dict:
    """Fixed generator context per question: gold chunks + BM25 hard negatives (deterministic)."""
    bm = BM25([c["text"] for c in chunks])
    ids = [c["chunk_id"] for c in chunks]
    out = {}
    for q in questions:
        g = list(gold.get(q["id"], []))
        ranking = [ids[i] for i in bm.rank(q["question"])]
        if len(g) > max_gold:   # keep the best-ranked gold chunks
            g = sorted(g, key=lambda c: ranking.index(c))[:max_gold]
        negs = [c for c in ranking if c not in gold.get(q["id"], [])][: max(0, k - len(g))]
        out[q["id"]] = {"gold": g, "negatives": negs}
    return out


def build_derived(root: Path = RAG_DIR) -> dict:
    """Write data/rag/derived/{chunks.jsonl, gold.json, contexts.json}; returns a summary."""
    chunks = build_chunks(root)
    qs = load_questions(root)
    gold, problems = map_evidence(qs, chunks)
    ctx = oracle_contexts(qs, chunks, gold)
    d = root / "derived"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "chunks.jsonl", "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    (d / "gold.json").write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "contexts.json").write_text(json.dumps(ctx, ensure_ascii=False, indent=1), encoding="utf-8")
    sizes = [c["words"] for c in chunks]
    return {"chunks": len(chunks), "min_words": min(sizes), "max_words": max(sizes),
            "mean_words": round(sum(sizes) / len(sizes), 1), "problems": problems,
            "answerable_without_gold": [q["id"] for q in qs if q["answerable"] and not gold[q["id"]]]}


def load_derived(root: Path = RAG_DIR) -> tuple[list[dict], dict, dict]:
    d = root / "derived"
    if not (d / "chunks.jsonl").exists():
        build_derived(root)
    chunks = [json.loads(l) for l in (d / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    gold = json.loads((d / "gold.json").read_text(encoding="utf-8"))
    ctx = json.loads((d / "contexts.json").read_text(encoding="utf-8"))
    return chunks, gold, ctx
