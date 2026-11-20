"""Shared synthetic-data helpers for tests."""
import random


def numeric_batch(seed, feature, segment, version, windows, n=40,
                  mu=100.0, sigma=10.0, missing_rate=0.0, tags=None):
    """Build one ingest batch of numeric observations."""
    rng = random.Random(seed)
    records = []
    for window in windows:
        for i in range(n):
            is_missing = rng.random() < missing_rate
            records.append({
                "feature": feature,
                "dtype": "numeric",
                "segment": segment,
                "business_version": version,
                "window_start": window,
                "value": None if is_missing else rng.gauss(mu, sigma),
                "is_missing": is_missing,
                "tags": tags or {"row": i},
            })
    return records


def categorical_batch(seed, feature, segment, version, windows,
                      probs, n=40):
    rng = random.Random(seed)
    labels = list(probs)
    weights = list(probs.values())
    records = []
    for window in windows:
        for _ in range(n):
            pick = rng.choices(labels, weights=weights, k=1)[0]
            records.append({
                "feature": feature,
                "dtype": "categorical",
                "segment": segment,
                "business_version": version,
                "window_start": window,
                "value_text": pick,
            })
    return records
