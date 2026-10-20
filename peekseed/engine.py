"""Local retrieval over one dataset index: BM25 + cosine, fused, optionally reranked.

The engine is deliberately transport-free. It owns the ranking maths and the
on-disk index format and nothing else: it does not know how an embedding or a
rerank model is reached, so it is unit-testable offline and usable by any caller
(the HTTP app, the CLI, the research loop). Everything that needs a model is
injected:

* the query vector arrives already embedded;
* reranking is a callable ``rerank_fn(query, docs) -> list[float] | None``,
  supplied per store, per index, or per ``search()`` call.

That separation is what lets the same index serve a one-shot search, a streamed
search, and a research loop that wants retrieval without reranking latency.
"""

from __future__ import annotations

import collections
import json
import logging
import math
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

# Reciprocal-rank-fusion constant. 60 is the value from the original ranking
# recipe; it keeps the first few ranks close and the tail flat, which is what
# makes fusion robust when the two retrievers disagree.
RRF_K = 60

# A short list is enough: these words carry no retrieval signal in any document
# set, and dropping them keeps a natural-language question from matching on
# "the"/"of" in every chunk. Lexical matching for identifiers is untouched.
STOPWORDS = set("""a an and are as at be by for from has have if in into is it its of on or
that the to was were will with not this these those we you they their there here when where
which what who whom whose can could should would may might must do does did done shall""".split())

_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# Length of the text used when planning rerank groups, when the caller clips
# documents to a budget before sending them. None/0 means "use the real length".
DEFAULT_RERANK_MAX_CHARS = 0

LOG = logging.getLogger("peekseed.engine")


def tokenize(text: str) -> List[str]:
    """Split text into lowercase word tokens for BM25.

    Underscores are kept inside a token so an identifier such as ``vin_sel`` is
    one term, and the parts are also emitted separately so a query for ``vin``
    still finds it. Tokens shorter than two characters and stopwords are dropped.
    """
    out: List[str] = []
    for tok in _TOKEN_RE.findall((text or "").lower()):
        if tok in STOPWORDS or len(tok) < 2:
            continue
        out.append(tok)
        if "_" in tok:
            out.extend(part for part in tok.split("_")
                       if len(part) > 1 and part not in STOPWORDS)
    return out


class BM25:
    """Okapi BM25 over a fixed list of token lists (one per chunk).

    Pure python and small on purpose: an index here is thousands of chunks, not
    millions, and keeping this dependency-free makes the ranking path testable
    without numpy tricks or a search library.
    """

    def __init__(self, token_lists: List[List[str]]):
        self.N = len(token_lists)
        self.k1, self.b = 1.5, 0.75
        self.docs = token_lists
        df: collections.Counter = collections.Counter()
        for doc in token_lists:
            for term in set(doc):
                df[term] += 1
        self.avgdl = sum(len(d) for d in token_lists) / max(self.N, 1)
        self.idf = {t: math.log(1 + (self.N - f + 0.5) / (f + 0.5))
                    for t, f in df.items()}

    def score(self, query_tokens: List[str]) -> np.ndarray:
        """BM25 score for every document, shape [N], higher is better."""
        res = np.zeros(self.N, dtype=np.float64)
        qset = set(query_tokens)
        for i in range(self.N):
            doc = self.docs[i]
            if not doc:
                continue
            dl = len(doc)
            tf = collections.Counter(doc)
            total = 0.0
            for term in qset:
                count = tf.get(term)
                if not count:
                    continue
                total += self.idf.get(term, 0.0) * count * (self.k1 + 1) / (
                    count + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            res[i] = total
        return res


def rerank_buckets(texts: List[str], batch: int = 8,
                   max_chars: Optional[int] = None) -> List[List[int]]:
    """Group candidate documents into length-sorted batches, one request each.

    A rerank server pads every row of a request to the longest row in that
    request, so a single request carrying the whole candidate pool charges the
    pool's longest document to every row — the same forward pass for padding as
    for real text. Sorting by length and cutting into groups of ``batch`` keeps
    each group's padding close to its own typical length. Because a rerank model
    scores each (query, document) row independently, regrouping cannot change a
    score; it only removes wasted compute.

    ``max_chars`` lets a caller that clips documents before sending them plan on
    the clipped length instead of the stored one (0 or None means no cap).

    Returns index groups covering every input exactly once. ``batch`` <= 0, or
    ``batch`` >= ``len(texts)``, returns one group: the caller's old
    single-request behaviour, preserved exactly.
    """
    n = len(texts)
    if n == 0:
        return []
    if batch is None or batch <= 0 or batch >= n:
        return [list(range(n))]
    cap = max_chars if max_chars and max_chars > 0 else None

    def measure(i: int) -> int:
        length = len(texts[i] or "")
        return min(length, cap) if cap else length

    order = sorted(range(n), key=measure)
    return [order[i:i + batch] for i in range(0, n, batch)]


class DatasetIndex:
    """One embedding space of one dataset: vectors, chunk metadata, and search.

    Instances are built by :class:`IndexStore` from ``index/<space>/`` files, but
    they are plain objects: a caller can construct one directly in a test with
    in-memory vectors and metadata.

    ``meta`` rows are expected to carry the chunk keys the app renders
    (``id``, ``section``, ``doc``, pages, ...). Missing keys degrade to empty
    strings rather than raising, because a hand-edited or partially migrated
    index should still be searchable.
    """

    def __init__(self, dataset: str, space: str, info: Optional[Dict[str, Any]] = None,
                 vectors: Optional[Any] = None, meta: Optional[List[Dict[str, Any]]] = None,
                 rerank_fn: Optional[Callable[[str, List[str]], Optional[List[float]]]] = None):
        self.dataset = str(dataset)
        self.space = str(space)
        self.info: Dict[str, Any] = dict(info or {})
        self.meta: List[Dict[str, Any]] = list(meta or [])
        self.rerank_fn = rerank_fn

        arr = np.zeros((0, 0), dtype=np.float32) if vectors is None else np.asarray(vectors)
        if arr.ndim == 1 and arr.size:
            arr = arr.reshape(1, -1)
        if arr.ndim != 2 or arr.shape[0] != len(self.meta):
            raise ValueError(
                "index %s/%s: %d vectors for %d chunk records"
                % (self.dataset, self.space,
                   arr.shape[0] if arr.ndim == 2 else -1, len(self.meta)))
        self.vectors = arr.astype(np.float32, copy=False)

        # Normalise once at load time: cosine is then one matrix-vector product
        # per search. A zero vector stays zero instead of dividing by zero.
        norm = np.linalg.norm(self.vectors, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        self.vn = self.vectors / norm

        # BM25 scores section + body, not the whole exported `doc`. When a chunk
        # carries a reader `text`, `doc` is "breadcrumb\n\nbody" and the
        # breadcrumb repeats every ancestor title; scoring it per descendant
        # would let a section's title outrank the paragraph that actually
        # discusses it. Without `text`, `doc` is already body-only.
        self.tokens = [tokenize(self._index_text(m)) for m in self.meta]
        self.bm25 = BM25(self.tokens)

        # A heading-only chunk has no content of its own; its children carry the
        # same breadcrumb, so matching it adds a duplicate hit with no text.
        self.searchable = np.array(
            [not m.get("heading_only") for m in self.meta], dtype=bool)

        self.by_id: Dict[str, Dict[str, Any]] = {
            m["id"]: m for m in self.meta if m.get("id")}

        self.dim = int(self.vectors.shape[1]) if self.vectors.shape[0] else int(
            self.info.get("dim") or 0)
        self.last_reranked = False
        self.last_timing: Dict[str, Any] = {}

    @staticmethod
    def _index_text(m: Dict[str, Any]) -> str:
        doc = m.get("doc") or ""
        body = doc.partition("\n")[2] if "text" in m else doc
        return (m.get("section") or "") + " " + body

    def _doc_text(self, i: int) -> str:
        m = self.meta[i]
        return m.get("doc") or m.get("text") or ""

    def search(self, qvec: Any, query: str, top: int, rerank_pool: int = 0,
               rerank_batch: int = 8, on_stage: Optional[Callable[[Dict[str, Any]], None]] = None,
               rerank: bool = True,
               rerank_fn: Optional[Callable[[str, List[str]], Optional[List[float]]]] = None
               ) -> Tuple[List[int], bool, List[float], Dict[str, Any]]:
        """Rank this index and return ``(order, reranked, scores, timings)``.

        ``order`` is the full fused candidate ordering as row indices into
        ``meta``; callers slice ``order[:top]``. ``scores`` is row-aligned with
        ``order`` (the rerank score when reranking ran, the RRF fusion score
        otherwise) so a caller can build result objects by zipping the two — the
        original internal candidate order is not exposed, and returning scores in
        that order would leave callers unable to pair them back.

        ``rerank_pool`` caps how many fused candidates are reranked (0 means a
        size derived from ``top``). ``rerank`` false skips the model stage
        entirely, which is what a mid-loop agent tool wants: a cross-encoder
        pass buys little when the caller is about to read the candidates itself.

        ``on_stage`` receives ``{"stage": "retrieve"|"rerank", "status":
        "start"|"done"|"skip", "sec": float|None}`` as each stage begins and
        ends, so a streaming endpoint can show progress. A stage that does not
        run reports "skip" with ``sec`` None — never a fake instant success.
        """
        def stage(name: str, status: str, sec: Optional[float] = None) -> None:
            if on_stage is not None:
                on_stage({"stage": name, "status": status, "sec": sec})

        top = max(int(top or 0), 0)
        timings: Dict[str, Any] = {"retrieve_sec": None, "rerank_sec": None}

        t_start = time.perf_counter()
        stage("retrieve", "start")
        n = len(self.meta)
        if n == 0:
            timings["retrieve_sec"] = time.perf_counter() - t_start
            stage("retrieve", "done", timings["retrieve_sec"])
            stage("rerank", "skip")
            self.last_reranked = False
            self.last_timing = timings
            return [], False, [], timings

        q = np.asarray(qvec, dtype=np.float32).ravel()
        if self.dim and q.size != self.dim:
            raise ValueError(
                "query vector has %d dimensions, index %s/%s has %d"
                % (q.size, self.dataset, self.space, self.dim))

        # Vector retrieval: cosine via the pre-normalised matrix.
        qn = q / (float(np.linalg.norm(q)) or 1.0)
        vscore = self.vn @ qn
        vorder = np.argsort(-vscore, kind="stable")
        vorder = vorder[self.searchable[vorder]]

        # Keyword retrieval: BM25 over section + body. No document-domain
        # weighting here: rank every document set by the same rules.
        bmscore = self.bm25.score(tokenize(query))
        bmorder = np.argsort(-bmscore, kind="stable")
        bmorder = bmorder[self.searchable[bmorder]]

        # Fuse the two rankings with RRF. RRF only uses ranks, so the two scores
        # need no calibration against each other, and a candidate that only one
        # retriever likes still surfaces.
        pool = max(top * 3, 10)
        rrf: Dict[int, float] = collections.defaultdict(float)
        for rank, i in enumerate(vorder[:pool]):
            rrf[int(i)] += 1.0 / (RRF_K + rank + 1)
        for rank, i in enumerate(bmorder[:pool]):
            rrf[int(i)] += 1.0 / (RRF_K + rank + 1)

        cand = [i for i, _ in sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)]
        cand = cand[:rerank_pool] if rerank_pool and rerank_pool > 0 else cand[:max(top * 4, 40)]

        timings["retrieve_sec"] = time.perf_counter() - t_start
        stage("retrieve", "done", timings["retrieve_sec"])

        # Rerank the narrow fused pool. Unavailability is never a failed search:
        # fall back to the fusion order and report the stage as skipped.
        order: List[int] = list(cand)
        reranked = False
        rerank_scores: Optional[List[float]] = None
        fn = rerank_fn if rerank_fn is not None else self.rerank_fn
        if not cand or not rerank:
            stage("rerank", "skip")
        else:
            stage("rerank", "start")
            t_rerank = time.perf_counter()
            texts = [self._doc_text(i) for i in cand]
            rerank_scores = self._rerank(fn, query, texts, rerank_batch)
            if rerank_scores is not None:
                reranked = True
                timings["rerank_sec"] = time.perf_counter() - t_rerank
                order = [i for _, i in sorted(
                    zip(rerank_scores, cand), key=lambda pair: pair[0], reverse=True)]
                stage("rerank", "done", timings["rerank_sec"])
            else:
                stage("rerank", "skip")

        # Row-align the scores with the returned order in both paths.
        if reranked and rerank_scores is not None:
            by_row = dict(zip(cand, rerank_scores))
            scores = [float(by_row[i]) for i in order]
        else:
            scores = [float(rrf.get(i, 0.0)) for i in order]

        self.last_reranked = reranked
        self.last_timing = timings
        return order, reranked, scores, timings

    @staticmethod
    def _rerank(fn: Optional[Callable[[str, List[str]], Optional[List[float]]]],
                query: str, texts: List[str], batch: int) -> Optional[List[float]]:
        """Score ``texts`` with the injected rerank callable, or return None.

        The callable is invoked once per length-sorted bucket (see
        :func:`rerank_buckets`), so a caller that batches over HTTP gets the
        padding saving while this function stays transport-free. A short or
        failed response means "rerank unavailable" — the caller falls back to
        fusion order rather than failing the search.
        """
        if fn is None:
            return None
        scores: List[Optional[float]] = [None] * len(texts)
        try:
            for group in rerank_buckets(texts, batch):
                got = fn(query, [texts[j] for j in group])
                if got is None:
                    return None
                got = [float(x) for x in got]
                if len(got) != len(group):
                    LOG.warning(
                        "rerank returned %d scores for %d documents; "
                        "falling back to fusion order", len(got), len(group))
                    return None
                for j, value in zip(group, got):
                    scores[j] = value
        except Exception as exc:  # noqa: BLE001 - model stage must never break search
            LOG.warning("rerank failed (%s: %s); falling back to fusion order",
                        type(exc).__name__, exc)
            return None
        if any(s is None for s in scores):
            return None
        return [float(s) for s in scores]


class IndexStore:
    """Every usable dataset index found under one ``datasets`` directory.

    ``datasets_dir`` is the data root's ``datasets/`` directory; the store scans
    ``<datasets_dir>/<dataset>/index/<space>/{info.json,vectors.npy,meta.json}``.
    One unreadable index is skipped with a warning: a half-written or corrupt
    directory must not take the whole service down, and every other dataset
    stays searchable.
    """

    def __init__(self, datasets_dir: Any,
                 rerank_fn: Optional[Callable[[str, List[str]], Optional[List[float]]]] = None):
        self.datasets_dir = str(datasets_dir)
        self.rerank_fn = rerank_fn
        self._indexes: Dict[Tuple[str, str], DatasetIndex] = {}

    def load(self) -> None:
        """(Re)scan the datasets directory, replacing the current index map.

        Replacing rather than merging is what makes a rebuilt or deleted dataset
        show up without a restart: the scan is the whole truth.
        """
        found: Dict[Tuple[str, str], DatasetIndex] = {}
        try:
            datasets = sorted(os.listdir(self.datasets_dir))
        except OSError:
            datasets = []
        for dataset in datasets:
            index_dir = os.path.join(self.datasets_dir, dataset, "index")
            if not os.path.isdir(index_dir):
                continue
            try:
                spaces = sorted(os.listdir(index_dir))
            except OSError:
                continue
            for space in spaces:
                space_dir = os.path.join(index_dir, space)
                if not os.path.isdir(space_dir):
                    continue
                index = self._load_index(dataset, space, space_dir)
                if index is not None:
                    found[(dataset, space)] = index
        self._indexes = found

    def _load_index(self, dataset: str, space: str,
                    path: str) -> Optional[DatasetIndex]:
        try:
            with open(os.path.join(path, "info.json"), encoding="utf-8") as fh:
                info = json.load(fh)
            vectors = np.load(os.path.join(path, "vectors.npy"))
            with open(os.path.join(path, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
            if not isinstance(info, dict):
                raise ValueError("info.json is not an object")
            if not isinstance(meta, list):
                raise ValueError("meta.json is not an array")
            return DatasetIndex(
                dataset, space,
                info=info,
                vectors=vectors,
                meta=[m for m in meta if isinstance(m, dict)],
                rerank_fn=self.rerank_fn,
            )
        except Exception as exc:  # noqa: BLE001 - skip a bad index, keep the rest
            LOG.warning("skipping index %s (%s: %s)",
                        path, type(exc).__name__, exc)
            return None

    def all(self) -> Dict[Tuple[str, str], DatasetIndex]:
        """Every loaded index keyed by ``(dataset, space)``."""
        return dict(self._indexes)

    def for_dataset(self, dataset: str) -> List[DatasetIndex]:
        """All loaded index spaces of one dataset, in a stable order."""
        return [index for (name, _space), index in sorted(self._indexes.items())
                if name == dataset]

    def get(self, dataset: str, space: str) -> Optional[DatasetIndex]:
        return self._indexes.get((dataset, space))

    def find(self, dataset: str, link_id: str,
             model: Optional[str] = None) -> Optional[DatasetIndex]:
        """The index built with a specific embed link + model, if present.

        Matched on ``info.json``'s provenance fields first, because those are
        what index building wrote; the space-key fallback covers an index from
        before a model string was recorded.
        """
        if not link_id:
            return None
        model = model or ""
        for index in self.for_dataset(dataset):
            info = index.info or {}
            if info.get("link_id") == link_id and (info.get("model") or "") == model:
                return index
        return self.get(dataset, "%s__%s" % (link_id, model or "default"))

    def set_rerank_fn(self,
                      rerank_fn: Optional[Callable[[str, List[str]], Optional[List[float]]]]
                      ) -> None:
        """Point every loaded index at the current rerank callable.

        The app resolves the enabled rerank link once and pushes it here, so a
        link change does not require reloading vectors.
        """
        self.rerank_fn = rerank_fn
        for index in self._indexes.values():
            index.rerank_fn = rerank_fn
