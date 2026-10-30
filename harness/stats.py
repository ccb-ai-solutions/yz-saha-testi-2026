"""Statistics used by the analysis.  Items (not requests) are the unit of resampling.

Repeats of the same item are averaged first and the *items* are bootstrapped
(cluster bootstrap); comparisons between systems are always paired on the
intersection of items both systems completed.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Hashable, Mapping, Sequence

import numpy as np

DEFAULT_BOOT = 2000


def _item_means(by_item: Mapping[Hashable, Sequence[float]]) -> dict:
    return {k: float(np.mean(v)) for k, v in by_item.items() if len(v)}


def bootstrap_mean_ci(by_item: Mapping[Hashable, Sequence[float]], n_boot: int = DEFAULT_BOOT,
                      alpha: float = 0.05, seed: int = 7) -> tuple[float, float, float, int]:
    m = _item_means(by_item)
    if not m:
        return (float("nan"),) * 3 + (0,)
    x = np.array(list(m.values()))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(n_boot, len(x)))
    boots = x[idx].mean(axis=1)
    return float(x.mean()), float(np.quantile(boots, alpha / 2)), float(np.quantile(boots, 1 - alpha / 2)), len(x)


def paired_diff(a: Mapping, b: Mapping, n_boot: int = DEFAULT_BOOT, n_perm: int = 10000,
                alpha: float = 0.05, seed: int = 11) -> dict:
    ma, mb = _item_means(a), _item_means(b)
    keys = sorted(set(ma) & set(mb), key=str)
    if not keys:
        return {"n": 0}
    d = np.array([ma[k] - mb[k] for k in keys])
    rng = np.random.default_rng(seed)
    boots = d[rng.integers(0, len(d), size=(n_boot, len(d)))].mean(axis=1)
    obs = abs(d.mean())
    signs = rng.choice([-1.0, 1.0], size=(n_perm, len(d)))
    perm = np.abs((signs * d).mean(axis=1))
    p = (np.sum(perm >= obs - 1e-12) + 1) / (n_perm + 1)
    return {"n": len(d), "diff": float(d.mean()), "lo": float(np.quantile(boots, alpha / 2)),
            "hi": float(np.quantile(boots, 1 - alpha / 2)), "p": float(p)}


def holm(pvals: Mapping[Hashable, float]) -> dict:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    out, running = {}, 0.0
    for i, (k, p) in enumerate(items):
        adj = min(1.0, (m - i) * p)
        running = max(running, adj)
        out[k] = running
    return out


def rank_intervals(scores: Mapping[str, Mapping[Hashable, Sequence[float]]], n_boot: int = DEFAULT_BOOT,
                   higher_is_better: bool = True, seed: int = 13) -> dict:
    """Bootstrap the item set jointly for all systems; report P(rank 1) and a 95% rank interval."""
    means = {s: _item_means(v) for s, v in scores.items()}
    systems = [s for s in means if means[s]]
    common = sorted(set.intersection(*(set(means[s]) for s in systems)), key=str) if systems else []
    if not common or len(systems) < 2:
        return {}
    mat = np.array([[means[s][k] for k in common] for s in systems])  # systems x items
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(common), size=(n_boot, len(common)))
    boot_means = mat[:, idx].mean(axis=2)  # systems x n_boot
    sign = 1.0 if higher_is_better else -1.0
    b = sign * boot_means
    # competition ranking: rank = 1 + number of systems strictly better (ties share the better rank)
    ranks = 1 + (b[None, :, :] > b[:, None, :] + 1e-12).sum(axis=1)
    out = {}
    for i, s in enumerate(systems):
        r = ranks[i]
        out[s] = {"p_best": float(np.mean(r == 1)), "rank_median": float(np.median(r)),
                  "rank_lo": int(np.quantile(r, 0.025)), "rank_hi": int(np.quantile(r, 0.975)),
                  "n_items": len(common)}
    return out


def percentile(values: Sequence[float], q: float) -> float:
    v = [x for x in values if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return float(np.percentile(v, q)) if v else float("nan")


def median_ci(values: Sequence[float], n_boot: int = DEFAULT_BOOT, alpha: float = 0.05, seed: int = 5) -> tuple:
    v = np.array([x for x in values if x is not None and not math.isnan(x)])
    if len(v) == 0:
        return (float("nan"),) * 3
    rng = np.random.default_rng(seed)
    boots = np.median(v[rng.integers(0, len(v), size=(n_boot, len(v)))], axis=1)
    return float(np.median(v)), float(np.quantile(boots, alpha / 2)), float(np.quantile(boots, 1 - alpha / 2))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return (float("nan"),) * 3
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for discordant counts b, c."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def cohen_kappa(a: Sequence, b: Sequence) -> float:
    n = len(a)
    if n == 0:
        return float("nan")
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    # pe == 1: both raters used one single label — agreement beyond chance is undefined, not perfect
    return (po - pe) / (1 - pe) if pe < 1 else float("nan")


def kappa_ci(a: Sequence, b: Sequence, n_boot: int = DEFAULT_BOOT, alpha: float = 0.05, seed: int = 13) -> tuple:
    """Cohen's kappa with a percentile bootstrap interval (the rated items are resampled in pairs; resamples in
    which kappa is undefined — one single label for both raters — are left out)."""
    k = cohen_kappa(a, b)
    n = len(a)
    if n == 0:
        return (float("nan"),) * 3
    a, b = list(a), list(b)
    rng = np.random.default_rng(seed)
    boots = [v for v in (cohen_kappa([a[i] for i in idx], [b[i] for i in idx])
                         for idx in rng.integers(0, n, size=(n_boot, n))) if not math.isnan(v)]
    if not boots:
        return k, float("nan"), float("nan")
    return k, float(np.quantile(boots, alpha / 2)), float(np.quantile(boots, 1 - alpha / 2))


def quadratic_kappa(a: Sequence[int], b: Sequence[int], k: int) -> float:
    if not a:
        return float("nan")
    o = np.zeros((k, k))
    for x, y in zip(a, b):
        o[int(x), int(y)] += 1
    w = np.array([[(i - j) ** 2 / (k - 1) ** 2 for j in range(k)] for i in range(k)])
    ea = o.sum(axis=1)
    eb = o.sum(axis=0)
    e = np.outer(ea, eb) / o.sum()
    den = (w * e).sum()
    return float(1 - (w * o).sum() / den) if den else float("nan")


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) < 3:
        return float("nan")
    ra = np.argsort(np.argsort(a))
    rb = np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def auroc(y: Sequence[int], s: Sequence[float]) -> float:
    pos = [x for x, t in zip(s, y) if t == 1]
    neg = [x for x, t in zip(s, y) if t == 0]
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for q in neg:
            wins += 1.0 if p > q else 0.5 if p == q else 0.0
    return wins / (len(pos) * len(neg))


def brier(y: Sequence[int], p: Sequence[float]) -> float:
    return float(np.mean([(pp - yy) ** 2 for yy, pp in zip(y, p)])) if y else float("nan")


def macro_f1(y_true: Sequence, y_pred: Sequence, labels: Sequence) -> float:
    f1s = []
    for lab in labels:
        tp = sum(t == lab and p == lab for t, p in zip(y_true, y_pred))
        fp = sum(t != lab and p == lab for t, p in zip(y_true, y_pred))
        fn = sum(t == lab and p != lab for t, p in zip(y_true, y_pred))
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return float(np.mean(f1s)) if f1s else float("nan")
