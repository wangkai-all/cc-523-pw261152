"""Pure statistics + drift-distance functions (standard library only).

All functions take plain Python lists and return plain dicts/numbers so they
are trivial to unit-test and free of third-party dependencies.
"""
from __future__ import annotations

import bisect
import math
from collections import Counter

_EPS = 1e-6


# ------------------------------------------------------------------ basics
def quantile(sorted_values, q):
    """Linear-interpolation quantile. ``q`` in [0, 1]."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_values[lo])
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def mean(values):
    return sum(values) / len(values) if values else None


def stddev(values, mu=None):
    """Population standard deviation."""
    n = len(values)
    if n == 0:
        return None
    mu = mean(values) if mu is None else mu
    var = sum((v - mu) ** 2 for v in values) / n
    return math.sqrt(var)


def _fixed_edges(lo, hi, bins=10):
    if lo == hi:
        pad = 1.0 if lo == 0 else abs(lo) * 0.01
        return [lo - pad, hi + pad]
    width = (hi - lo) / bins
    return [lo + width * i for i in range(bins + 1)]


def histogram(values, bins=10, edges=None):
    """Equal-width histogram. If ``edges`` is given (baseline edges) the same
    edges are reused so two histograms share a common support."""
    if not values:
        return None, []
    if edges is None:
        edges = _fixed_edges(min(values), max(values), bins)
    counts = [0] * (len(edges) - 1)
    for v in values:
        idx = _bin_index(v, edges)
        counts[idx] += 1
    return edges, counts


def _bin_index(v, edges):
    # values outside the support are clamped into the nearest end bin so the
    # current window can reuse baseline edges without losing mass
    idx = bisect.bisect_right(edges, v) - 1
    if idx < 0:
        return 0
    if idx >= len(edges) - 1:
        return len(edges) - 2
    return idx


# ------------------------------------------------------------------ distances
def _proportions(counts, total):
    """Convert counts to proportions, smoothing *structural zeros* only.

    A blanket epsilon floor would dominate genuinely small (but non-zero)
    proportions in sparse windows and inflate PSI, so zero bins are smoothed
    with a half-pseudocount and everything else is left untouched.
    """
    return [((c + 0.5) / (total + 0.5 * len(counts))) if c == 0
            else c / total for c in counts]


def psi(expected_counts, actual_counts):
    """Population Stability Index over aligned count vectors."""
    p = _proportions(expected_counts, sum(expected_counts))
    q = _proportions(actual_counts, sum(actual_counts))
    return sum((b - a) * math.log(b / a) for a, b in zip(p, q))


def jensen_shannon(expected_counts, actual_counts):
    """JS divergence (0..ln2) over aligned count vectors."""
    p = _proportions(expected_counts, sum(expected_counts))
    q = _proportions(actual_counts, sum(actual_counts))
    m = [(a + b) / 2 for a, b in zip(p, q)]

    def kl(x, y):
        return sum(a * math.log(a / b) for a, b in zip(x, y))

    return 0.5 * kl(p, m) + 0.5 * kl(q, m)


def categorical_histograms(baseline_counts: dict, current_counts: dict):
    """Align two categorical count dicts on their shared key universe."""
    keys = sorted(set(baseline_counts) | set(current_counts))
    exp = [baseline_counts.get(k, 0) for k in keys]
    act = [current_counts.get(k, 0) for k in keys]
    return keys, exp, act


# ------------------------------------------------------------------ summaries
def numeric_summary(values, *, outlier_fences=None):
    """Descriptive summary for one numeric window.

    ``outlier_fences`` = (lower, upper) from the baseline; when supplied the
    outlier count is measured against the *baseline* fence, otherwise the
    window's own IQR fences are used (used when building a baseline).
    """
    n = len(values)
    if n == 0:
        return {"n": 0}
    sv = sorted(values)
    mu = mean(values)
    sd = stddev(values, mu)
    q1, med, q3 = quantile(sv, 0.25), quantile(sv, 0.5), quantile(sv, 0.75)
    iqr = q3 - q1
    if outlier_fences is not None:
        lower, upper = outlier_fences
    else:
        lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    n_out = sum(1 for v in values if v < lower or v > upper)
    edges, counts = histogram(values)
    return {
        "n": n,
        "mean": mu,
        "std": sd,
        "min": sv[0],
        "max": sv[-1],
        "q1": q1,
        "median": med,
        "q3": q3,
        "iqr": iqr,
        "lower_fence": lower,
        "upper_fence": upper,
        "n_outliers": n_out,
        "outlier_rate": n_out / n,
        "histogram_edges": edges,
        "histogram_counts": counts,
    }


def categorical_summary(values):
    if not values:
        return {"n": 0}
    counts = dict(Counter(values))
    return {"n": len(values), "category_counts": counts}


def compare_numeric(baseline: dict, current: dict) -> dict:
    """Drift comparison of two numeric summaries."""
    out: dict = {
        "n_current": current.get("n", 0),
        "missing_rate_delta": current.get("missing_rate", 0.0)
        - baseline.get("missing_rate", 0.0),
        "outlier_rate_delta": current.get("outlier_rate", 0.0)
        - baseline.get("outlier_rate", 0.0),
    }
    if current.get("n"):
        # rebuild the current histogram on baseline edges for a common support
        edges = baseline.get("histogram_edges")
        if edges and current.get("_raw_values") is not None:
            _, cur_counts = histogram(current["_raw_values"], edges=edges)
        else:
            cur_counts = current.get("histogram_counts", [])
        base_counts = baseline.get("histogram_counts", [])
        if len(cur_counts) == len(base_counts):
            out["psi"] = psi(base_counts, cur_counts)
            out["js_divergence"] = jensen_shannon(base_counts, cur_counts)
        b_std = baseline.get("std") or 0.0
        if b_std > 0 and baseline.get("mean") is not None:
            out["mean_z"] = (current["mean"] - baseline["mean"]) / b_std
        else:
            out["mean_z"] = 0.0
    return out


def compare_categorical(baseline: dict, current: dict) -> dict:
    out = {
        "n_current": current.get("n", 0),
        "missing_rate_delta": current.get("missing_rate", 0.0)
        - baseline.get("missing_rate", 0.0),
        "outlier_rate_delta": 0.0,
    }
    base_counts = baseline.get("category_counts") or {}
    cur_counts = current.get("category_counts") or {}
    if cur_counts:
        _, exp, act = categorical_histograms(base_counts, cur_counts)
        out["psi"] = psi(exp, act)
        out["js_divergence"] = jensen_shannon(exp, act)
        out["mean_z"] = 0.0
    return out
