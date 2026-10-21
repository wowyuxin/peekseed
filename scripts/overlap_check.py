#!/usr/bin/env python3
"""Record the top-k overlap between a baseline service and the imported one.

The P4 acceptance record asks for the same queries to be run against smart-search
and peekseed and for the top-10 overlap to be recorded. This script does exactly
that and writes the JSON report; it does not decide whether the overlap is good
enough (the operator does, and ``--min-overlap`` can make it fail a threshold).

Both services must answer a JSON search endpoint with this request shape::

    POST <base><path>  {"dataset": "...", "query": "...", "topk": 10}
    -> {"results": [{"id": "...", ...}, ...]}

Adjust ``--path``/``--id-key`` for a service that differs (for example a baseline
whose result rows carry ``chunk`` instead of ``id``). API keys, if needed, are
read from ``--token`` and sent as a bearer header; the token is never printed.

Usage::

    python scripts/overlap_check.py \\
        --baseline http://127.0.0.1:8030 --candidate http://127.0.0.1:8031 \\
        --queries queries.json --dataset cxl --out overlap.json

``queries.json`` is either a JSON list of strings or one query per line.
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from peekseed import overlap  # noqa: E402


def load_queries(path):
    text = Path(path).read_text(encoding="utf-8").strip()
    try:
        value = json.loads(text)
    except ValueError:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SystemExit("queries must be a JSON list of strings or one per line")
    return value


def provider(base, path, dataset, topk, token):
    url = base.rstrip("/") + path

    def search(query):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = {"query": query, "topk": topk}
        if dataset:
            body["dataset"] = dataset
        with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=120.0)) as client:
            response = client.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        rows = data.get("results") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("the service returned no results list")
        return rows

    return search


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", required=True, help="baseline base URL")
    parser.add_argument("--candidate", required=True, help="candidate base URL")
    parser.add_argument("--queries", required=True, help="JSON list or line-delimited queries")
    parser.add_argument("--dataset", default="", help="dataset id (optional)")
    parser.add_argument("--path", default="/api/search", help="search path on both services")
    parser.add_argument("--id-key", default="id", help="result row field to compare")
    parser.add_argument("--topk", type=int, default=10)
    parser.add_argument("--token", default="", help="bearer token for both services (never printed)")
    parser.add_argument("--min-overlap", type=float, default=0.0,
                        help="exit 1 when mean overlap is below this fraction")
    parser.add_argument("--out", default="", help="write the JSON report here")
    args = parser.parse_args(argv)

    queries = load_queries(args.queries)
    if not queries:
        raise SystemExit("no queries")
    report = overlap.compare(
        queries,
        provider(args.baseline, args.path, args.dataset, args.topk, args.token),
        provider(args.candidate, args.path, args.dataset, args.topk, args.token),
        key=args.id_key, topk=args.topk,
    )
    report["baseline"] = args.baseline.rstrip("/") + args.path
    report["candidate"] = args.candidate.rstrip("/") + args.path
    report["dataset"] = args.dataset
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for row in report["queries"]:
        if "error" in row:
            print("ERROR  %s: %s" % (row["query"], row["error"]))
        else:
            print("%5.1f%%  %s" % (100 * row["fraction"], row["query"]))
    print("mean overlap %.4f over %d query(s), %d error(s)"
          % (report["mean_overlap"], report["measured"], report["errors"]))
    if report["mean_overlap"] < args.min_overlap:
        print("below --min-overlap %.4f" % args.min_overlap, file=sys.stderr)
        return 1
    return 0 if report["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
