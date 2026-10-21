#!/usr/bin/env python3
"""Record the top-k overlap between a baseline service and the imported one.

The P4 acceptance record asks for the same queries to be run against smart-search
and peekseed and for the top-10 overlap to be recorded. This script does exactly
that and writes the JSON report; it does not decide whether the overlap is good
enough (the operator does, and ``--min-overlap`` can make it fail a threshold).

Both services must answer a JSON search endpoint with this request shape::

    POST <base><path>  {"dataset": "...", "query": "...", "topk": 10}
    -> {"results": [{"id": "...", ...}, ...]}

A baseline may use a different body key. For example smart-search expects
``corpus`` instead of ``dataset``: use ``--baseline-body-field corpus`` and
``--baseline-dataset CXL_3p1_extracted``. ``--dataset`` remains a fallback for
both sides. Adjust ``--path``/``--id-key`` too when the result rows differ (for
example the shared ``section`` field). API keys are read from ``--token`` (both
services) or ``--baseline-token`` and sent as bearer headers; never printed.

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


def provider(base, path, dataset, topk, token, body_field="dataset"):
    url = base.rstrip("/") + path

    def search(query):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = {"query": query, "topk": topk}
        if dataset:
            body[body_field] = dataset
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=5.0)) as client:
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
    parser.add_argument("--dataset", default="",
                        help="dataset id for both services (optional fallback)")
    parser.add_argument("--baseline-dataset", default=None,
                        help="baseline dataset/corpus id (defaults to --dataset)")
    parser.add_argument("--candidate-dataset", default=None,
                        help="candidate dataset id (defaults to --dataset)")
    parser.add_argument("--baseline-body-field", default="dataset",
                        help="baseline request body key for its dataset/corpus")
    parser.add_argument("--candidate-body-field", default="dataset",
                        help="candidate request body key for its dataset")
    parser.add_argument("--path", default="/api/search", help="search path on both services")
    parser.add_argument("--id-key", default="id", help="result row field to compare")
    parser.add_argument("--topk", type=int, default=10)
    parser.add_argument("--token", default="",
                        help="bearer token for both services (never printed)")
    parser.add_argument("--baseline-token", default="",
                        help="baseline bearer token, overriding --token")
    parser.add_argument("--min-overlap", type=float, default=0.0,
                        help="exit 1 when mean overlap is below this fraction")
    parser.add_argument("--out", default="", help="write the JSON report here")
    args = parser.parse_args(argv)

    queries = load_queries(args.queries)
    if not queries:
        raise SystemExit("no queries")
    baseline_dataset = args.baseline_dataset if args.baseline_dataset is not None else args.dataset
    candidate_dataset = args.candidate_dataset if args.candidate_dataset is not None else args.dataset
    baseline_token = args.baseline_token or args.token
    report = overlap.compare(
        queries,
        provider(args.baseline, args.path, baseline_dataset, args.topk,
                 baseline_token, args.baseline_body_field),
        provider(args.candidate, args.path, candidate_dataset, args.topk,
                 args.token, args.candidate_body_field),
        key=args.id_key, topk=args.topk,
    )
    report["baseline"] = args.baseline.rstrip("/") + args.path
    report["candidate"] = args.candidate.rstrip("/") + args.path
    report["dataset"] = {
        "baseline": baseline_dataset,
        "candidate": candidate_dataset,
    }
    report["body_fields"] = {
        "baseline": args.baseline_body_field,
        "candidate": args.candidate_body_field,
    }
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
