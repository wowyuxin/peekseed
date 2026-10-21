"""Ranking-overlap measurement for the import/cutover acceptance record.

The P4 cutover compares the imported peekseed index with the service it replaces
(smart-search) on the same 20 queries. The contract is deliberately mechanical:
take each service's top-k result ids, compute the set overlap, record every
query and the mean, and let the operator decide whether the number is acceptable.
No score is invented and no query is silently skipped.
"""

from __future__ import annotations

__all__ = ["compare", "overlap"]


def _ids(rows, key="id"):
    ids = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            ids.append(text)
    return ids


def overlap(baseline_rows, candidate_rows, *, key="id", topk=10):
    """The top-k overlap of two result lists for one query.

    Returns ``{"overlap": n, "union": n, "fraction": f, "baseline_top": [...],
    "candidate_top": [...]}``. ``fraction`` is intersection / union of the two
    top-k id sets, so a candidate that returns fewer rows is not rewarded.
    """
    baseline = _ids(baseline_rows, key)[:max(1, int(topk))]
    candidate = _ids(candidate_rows, key)[:max(1, int(topk))]
    intersection = set(baseline) & set(candidate)
    union = set(baseline) | set(candidate)
    return {
        "overlap": len(intersection),
        "union": len(union),
        "fraction": round(len(intersection) / len(union), 4) if union else 0.0,
        "baseline_top": baseline,
        "candidate_top": candidate,
    }


def compare(queries, baseline, candidate, *, key="id", topk=10):
    """Run the same queries against two result providers and summarise overlap.

    ``baseline``/``candidate`` are callables taking one query string and
    returning the result rows. A provider that raises is recorded as an error
    for that query rather than aborting the whole run; the report says so.
    """
    rows = []
    for query in queries:
        item = {"query": query}
        try:
            baseline_rows = baseline(query)
        except Exception as exc:  # noqa: BLE001 - one bad query is data
            item["error"] = "baseline: %s: %s" % (type(exc).__name__, exc)
            rows.append(item)
            continue
        try:
            candidate_rows = candidate(query)
        except Exception as exc:  # noqa: BLE001
            item["error"] = "candidate: %s: %s" % (type(exc).__name__, exc)
            rows.append(item)
            continue
        item.update(overlap(baseline_rows, candidate_rows, key=key, topk=topk))
        rows.append(item)
    measured = [row for row in rows if "error" not in row]
    fractions = [row["fraction"] for row in measured]
    return {
        "queries": rows,
        "measured": len(measured),
        "errors": len(rows) - len(measured),
        "min_overlap": round(min(fractions), 4) if fractions else 0.0,
        "mean_overlap": round(sum(fractions) / len(fractions), 4) if fractions else 0.0,
        "topk": int(topk),
        "key": key,
    }
