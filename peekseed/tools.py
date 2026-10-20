#!/usr/bin/env python
"""Evidence-returning dataset tools for the research loop.

Why this exists: **a search result is a pointer, and a pointer is not evidence**.
The one-shot flow hands the model a fixed top-N result set and no way to act on
it, so a question whose evidence ranked eleventh — or lives in a section the
retriever under-weighted, or in the half of a chunk a cap cut — cannot be
answered from what it was given, however good the model is.

These tools let the model do the retrieval itself, over the SAME files the ingest
step wrote, and every tool returns evidence rather than another pointer:

  * the whole chunk text (not the snippet the page holds);
  * a SECTION by its printed number ("2.1"), not by guessing keywords;
  * every chunk that owns a topic, in document order;
  * the datasets that were never embedded at all — discovery is file-based, so a
    dataset with chunks but no vector index is still fully readable. Nothing here
    needs an embedding to reach it.

The module is pure file access plus one injected callable — how to run a semantic
search — so it is testable against a tiny fixture dataset with no model and no
network, and so the service can decide that without this file knowing about any
transport or model client. A tool result is data (rows + images + a note); the
agent layer decides how to number it, truncate it and show it.

Naming note: a ``dataset`` is one ingested document collection; the directory
name under ``<data root>/datasets`` is its id and ``dataset.json`` carries its
display name. Sections are derived from chunk metadata (``path`` / ``numbering``
/ ``section`` / ``chapter``), never from a separate sections file.
"""
import collections
import copy
import difflib
import json
import os
import re
import threading
import time


# Datasets are discovered by the presence of this file: it is what ingest always
# writes and what every read below is anchored to.
CHUNKS_FILE = "chunks.jsonl"
DATASET_FILE = "dataset.json"

# The largest text a single row may carry, in characters. Every service-side cap
# is a latency/faithfulness knob, not a context limit: a smaller, sharper
# observation produces a better-grounded next step than a dump. Rows are
# truncated with an explicit marker and the offset to continue from, never
# silently.
ROW_CHARS = 6000
# A hit's text inside a search/grep listing: enough to judge whether it is worth
# reading in full, which is what ``read_chunk`` is for.
HIT_CHARS = 2500

# Chapters that are pure navigation. A grep for a term that appears in the table
# of contents returns the contents page, which is never the answer — this is the
# single biggest source of useless hits in a converted document, so those chunks
# are ranked last instead of being dropped (the page number they carry is
# sometimes the useful part).
NAV_CHAPTERS = ("contents", "index", "revision history")

# Outline breadth. A converted book can have thousands of leaf sections; the
# outline is navigation, so it lists the top level and asks the caller to name a
# chapter for the rest.
OUTLINE_ROWS = 80


class ToolError(Exception):
    """A tool call the model can fix: bad arguments, unknown id, unknown dataset."""


def _norm_id(value):
    """A chunk id as the model may spell it: stripped, and without a file suffix.

    Ids in ``chunks.jsonl`` are plain (``docs-000004``), but a model that saw an
    id in an older export may append ``.md``; dropping that suffix is a cheap
    round-trip saver, not a supported layout.
    """
    value = (value or "").strip()
    return value[:-3] if value.endswith(".md") else value


def _short(text, cap):
    """``(text, truncated)`` — cut on a word boundary when there is one near the cap."""
    text = text or ""
    if len(text) <= cap:
        return text, False
    cut = text[:cap]
    space = cut.rfind(" ")
    if space > cap * 0.8:
        cut = cut[:space]
    return cut.rstrip() + "\n[… truncated]", True


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        return []
    return out


def _datasets_base(root):
    """Where dataset directories live under a data root.

    The documented layout is ``<data root>/datasets/<id>/chunks.jsonl``. A
    caller that already holds the datasets directory is also accepted, because
    that is what the app's paths object exposes and re-deriving the parent from
    it would be guesswork.
    """
    root = root or ""
    nested = os.path.join(root, "datasets")
    return nested if os.path.isdir(nested) else root


def discover_datasets(root):
    """Every dataset under ``<root>/datasets`` that has a ``chunks.jsonl``.

    A directory scan rather than a registry: a dataset appears the moment ingest
    finishes writing, with no code change and no shared state to drift. Hidden
    directories are skipped, and the scan is one level deep on purpose — the
    layout only ever nests ``dataset/index/<space>/`` below the chunks file.
    """
    base = _datasets_base(root)
    found = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return found
    for name in names:
        if name.startswith("."):
            continue
        path = os.path.join(base, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, CHUNKS_FILE)):
            found.append((name, path))
    return found


class Dataset:
    """One dataset directory, loaded lazily and read only.

    Everything a tool needs is a file this class opens on first use; the loads are
    guarded by one lock because the service answers questions on worker threads.
    """

    def __init__(self, root, dataset_id, name=""):
        self.root = root
        self.id = dataset_id
        self._name = name
        self._lock = threading.Lock()
        self._chunks = None
        self._by_id = None
        self._sections = None
        self._chapters = None
        self._info = None
        self._summary = None

    # --- loading -----------------------------------------------------------
    def _load_chunks(self):
        chunks, by_id = [], {}
        path = os.path.join(self.root, CHUNKS_FILE)
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                chunks.append(rec)
                by_id[_norm_id(rec.get("id"))] = rec
        return chunks, by_id

    def _ensure(self):
        if self._chunks is None:
            with self._lock:
                if self._chunks is None:
                    self._chunks, self._by_id = self._load_chunks()

    # --- identity ----------------------------------------------------------
    def info(self):
        """``dataset.json`` content, or ``{}`` when the file is missing/unreadable."""
        if self._info is None:
            self._info = _read_json(os.path.join(self.root, DATASET_FILE), {})
        return self._info if isinstance(self._info, dict) else {}

    @property
    def name(self):
        """Display label: ``dataset.json`` "name", falling back to the directory id."""
        if not self._name:
            name = (self.info().get("name") or "").strip()
            self._name = name or self.id
        return self._name

    @property
    def title(self):
        return self.name

    # --- access ------------------------------------------------------------
    @property
    def chunks(self):
        self._ensure()
        return self._chunks

    def chunk(self, chunk_id):
        """The chunk record for an id, or None."""
        self._ensure()
        return self._by_id.get(_norm_id(chunk_id))

    def resolve_chunk_id(self, chunk_id):
        rec = self.chunk(chunk_id)
        return rec.get("id") if rec else ""

    def sections(self):
        """Section records derived from the chunks, in document order.

        A converted document has no sections file in this project: the section
        tree IS the ordered set of (chapter, numbering, path, title) tuples the
        chunker stamped on the chunks. Deriving it here keeps the on-disk format
        to one canonical file and makes sections impossible to disagree with the
        chunks that reference them.
        """
        self._ensure()
        if self._sections is None:
            with self._lock:
                if self._sections is None:
                    self._sections = _sections_from_chunks(self._chunks)
        return self._sections

    def chapters(self):
        """``(chapter name, first page)`` in document order, from the section tree."""
        if self._chapters is None:
            seen = collections.OrderedDict()
            for sec in self.sections():
                name = (sec.get("chapter") or "").strip()
                if name and name not in seen:
                    seen[name] = sec.get("page_start")
            self._chapters = [(k, v) for k, v in seen.items()]
        return self._chapters

    def summary(self):
        """``{dataset, name, chunks, pages}`` WITHOUT parsing the chunks.

        ``list_datasets`` is the first call a model usually makes, and the obvious
        implementation — load every dataset and count — gets expensive over a
        library, on a machine that is also holding embedding and answer models in
        memory. Counting lines and reading the small ``dataset.json`` costs a
        fraction of that and answers the same question: how big is this document.
        """
        if self._summary is None:
            chunks = 0
            try:
                with open(os.path.join(self.root, CHUNKS_FILE), "rb") as f:
                    chunks = sum(1 for line in f if line.strip())
            except OSError:
                chunks = 0
            pages = 0
            for source in self.info().get("sources") or []:
                if not isinstance(source, dict):
                    continue
                try:
                    pages += int(source.get("pages") or 0)
                except (TypeError, ValueError):
                    continue
            self._summary = {"dataset": self.id, "name": self.name,
                             "chunks": chunks, "pages": pages}
        return self._summary

    # --- text the tools return --------------------------------------------
    def chunk_body(self, chunk):
        """What a chunk should be READ as: its reader text, else its embedded doc.

        ``text`` is the reader view (page markers included) and is the better
        thing to hand a model; it is optional in the record, so ``doc`` — the
        canonical breadcrumb + body text — is the fallback rather than an empty
        observation.
        """
        chunk = chunk or {}
        return chunk.get("text") or chunk.get("doc") or ""

    def label(self, chunk):
        """``2.1 Overview · p.12`` — the provenance an answer cites."""
        chunk = chunk or {}
        num = (chunk.get("numbering") or "").strip()
        sec = (chunk.get("section") or "").strip()
        name = sec if (num and sec.startswith(num)) else " ".join(p for p in (num, sec) if p)
        parts = [name or (chunk.get("chapter") or "").strip()]
        if chunk.get("page_start"):
            page = "p.%s" % chunk["page_start"]
            if chunk.get("page_end") not in (None, chunk.get("page_start")):
                page += "–%s" % chunk["page_end"]
            parts.append(page)
        return " · ".join(p for p in parts if p)


def _section_key(chunk):
    numbering = (chunk.get("numbering") or "").strip()
    path = (chunk.get("path") or "").strip()
    title = (chunk.get("section") or "").strip()
    chapter = (chunk.get("chapter") or "").strip()
    if numbering:
        return "n:" + numbering
    if path:
        return "p:" + path
    if title:
        return "t:" + chapter + "\x00" + title
    return "c:" + chapter


def _section_depth(chunk):
    """How deep a section sits in the tree, from metadata that always exists.

    ``numbering`` is the printed structure ("2.1.3" is depth 3); ``path`` is the
    breadcrumb written by the chunker; a chapter-only chunk is depth 1. The
    highest number wins so a section and one of its chunks never disagree.
    """
    numbering = (chunk.get("numbering") or "").strip()
    path = (chunk.get("path") or "").strip()
    by_number = numbering.count(".") + 1 if numbering else 0
    by_path = len([p for p in path.split("/") if p.strip()]) if path else 0
    return max(by_number, by_path, 1)


def _sections_from_chunks(chunks):
    """Build the ordered section list from chunk metadata.

    One record per distinct section, in first-seen (document) order, carrying the
    page range its chunks cover. A section with several chunks is still one
    outline row; its chunks are resolved later by numbering/path.
    """
    order = []
    by_key = {}
    for chunk in chunks or []:
        key = _section_key(chunk)
        page_start = chunk.get("page_start")
        page_end = chunk.get("page_end")
        sec = by_key.get(key)
        if sec is None:
            sec = {
                "id": key,
                "numbering": (chunk.get("numbering") or "").strip(),
                "title": (chunk.get("section") or "").strip(),
                "section": (chunk.get("section") or "").strip(),
                "chapter": (chunk.get("chapter") or "").strip(),
                "path": (chunk.get("path") or "").strip(),
                "page_start": page_start,
                "page_end": page_end,
                "depth": _section_depth(chunk),
                "source": (chunk.get("source") or "").strip(),
            }
            by_key[key] = sec
            order.append(sec)
            continue
        if page_start is not None and (sec.get("page_start") is None
                                       or page_start < sec["page_start"]):
            sec["page_start"] = page_start
        if page_end is not None and (sec.get("page_end") is None
                                     or page_end > sec["page_end"]):
            sec["page_end"] = page_end
    return order


class DatasetTools:
    """The tool registry the agent is offered, and the dispatcher that runs it.

    ``semantic`` is injected by the service:

      semantic(dataset_id, query, k) -> [(chunk record, score[, ...]), …] or None
          A vector/keyword retrieval, when an embedder is configured. The records
          come from this module (``tools.dataset(id).chunk(chunk_id)``), so the
          adapter resolves the index's stored id to the chunk it names. None
          means "not available", and ``search`` then answers from its own keyword
          ranking instead of failing — the tools must work with NO local model at
          all, because a dataset with chunks but no index is a normal state.

    ``search_log`` is an optional callback that receives a small dict per
    ``search`` call (the same row shape the history recorder stores).
    """

    def __init__(self, root, semantic=None, row_chars=ROW_CHARS,
                 hit_chars=HIT_CHARS, max_datasets=4, default_dataset=None,
                 search_log=None):
        self.root = root
        self.semantic = semantic
        self.row_chars = int(row_chars)
        self.hit_chars = int(hit_chars)
        self.max_datasets = int(max_datasets)
        self.default_dataset = default_dataset
        self.search_log = search_log
        self._lock = threading.Lock()
        self._cache = collections.OrderedDict()   # id -> Dataset, LRU

    def with_semantic(self, semantic):
        """The same tools with another retriever, sharing the dataset cache and its lock."""
        view = copy.copy(self)
        view.semantic = semantic
        return view

    def with_search_log(self, search_log):
        """The same tools with an on_search callback (records the model's searches)."""
        view = copy.copy(self)
        view.search_log = search_log
        return view

    # --- datasets ----------------------------------------------------------
    def _make(self, dataset_id, path):
        return Dataset(path, dataset_id)

    def dataset(self, name):
        """The named dataset, loaded on first use and kept in a small LRU.

        A large dataset is tens of megabytes of JSON, so the cache is bounded
        rather than a permanent map of every document: the worker is also
        holding models in memory. An evicted dataset simply reloads.
        """
        name = (name or "").strip() or (self.default_dataset or "")
        if not name:
            # With exactly one dataset on disk there is no ambiguity, and making
            # the model discover and pass an id it cannot see from inside a tool
            # call is a wasted round trip. Two or more still require the argument.
            known_only = dict(discover_datasets(self.root))
            if len(known_only) == 1:
                name = next(iter(known_only))
            else:
                raise ToolError("no dataset given and no dataset is in context")
        name = _norm_id(name)
        with self._lock:
            hit = self._cache.get(name)
            if hit is not None:
                self._cache.move_to_end(name)
                return hit
        known = dict(discover_datasets(self.root))
        if name not in known:
            close = difflib.get_close_matches(name, list(known), n=2, cutoff=0.5)
            raise ToolError("unknown dataset %r%s — call list_datasets" % (
                name, (". Did you mean %s?" % ", ".join(close)) if close else ""))
        made = self._make(name, known[name])
        with self._lock:
            self._cache[name] = made
            self._cache.move_to_end(name)
            while len(self._cache) > self.max_datasets:
                self._cache.popitem(last=False)
        return made

    def list_datasets(self):
        return [self._make(name, path).summary()
                for name, path in discover_datasets(self.root)]

    # --- schemas -----------------------------------------------------------
    def schemas(self):
        """The OpenAI tool definitions.

        Descriptions carry the METHOD, not just the signature: a model handed a
        bare ``grep`` tends to rewrite one regex into another until its budget
        runs out and never read a hit. Telling it, in the tool it is about to
        call, that a window is not a definition and that ``read_chunk`` is the
        next step is what stops that.
        """
        dataset_arg = {"type": "string",
                       "description": "dataset id, e.g. docs. Defaults to the "
                                      "dataset the question was asked on."}
        return [
            {"type": "function", "function": {
                "name": "list_datasets",
                "description": "Every document collection available, with its chunk "
                               "and page counts. Call this first when the question may "
                               "span more than one dataset.",
                "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {
                "name": "search",
                "description": "Concept search over a whole dataset. Use it for 'how "
                               "does X work' questions and for a second query built "
                               "from what you just learned. It returns the matching "
                               "chunks' TEXT. For an exact term, identifier, code or "
                               "section number use grep instead: this ranks by meaning "
                               "and will not reliably find a literal token.",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string"},
                    "dataset": dataset_arg,
                    "k": {"type": "integer", "description": "hits to return (default 5, max 10)"},
                    "mode": {"type": "string", "enum": ["auto", "semantic", "keyword"],
                             "description": "auto uses the embedding model when one is "
                                            "configured and keyword ranking when not"}},
                    "required": ["query"]}}},
            {"type": "function", "function": {
                "name": "grep",
                "description": "Find an EXACT string or regular expression in the raw "
                               "text of every chunk (case-insensitive by default). This "
                               "is the tool for tokens a concept search misses: a field "
                               "name, a code, a table number, a quoted phrase. It "
                               "returns ranked hits with the matched lines, and the "
                               "full text of the best hits — windows are context, not "
                               "the definition, so read the included text before you "
                               "answer. If a pattern finds nothing, change the wording "
                               "or the section you are looking in, not the spelling: do "
                               "not re-run near-identical patterns.",
                "parameters": {"type": "object", "properties": {
                    "pattern": {"type": "string"},
                    "dataset": dataset_arg,
                    "k": {"type": "integer", "description": "hits to return (default 5, max 12)"},
                    "case_sensitive": {"type": "boolean"},
                    "literal": {"type": "boolean",
                                "description": "treat the pattern as plain text, not a regex"}},
                    "required": ["pattern"]}}},
            {"type": "function", "function": {
                "name": "read_chunk",
                "description": "The COMPLETE text of one chunk by id, including past "
                               "any truncation in an earlier result.",
                "parameters": {"type": "object", "properties": {
                    "chunk_id": {"type": "string"},
                    "dataset": dataset_arg,
                    "offset": {"type": "integer", "description": "character offset to continue from"},
                    "limit": {"type": "integer"}},
                    "required": ["chunk_id"]}}},
            {"type": "function", "function": {
                "name": "read_section",
                "description": "Read a printed SECTION by its number or title ('2.1', "
                               "'Overview') — every chunk in it, in order. Use it to "
                               "walk a topic once you know where it lives, instead of "
                               "searching for its wording again.",
                "parameters": {"type": "object", "properties": {
                    "section": {"type": "string"},
                    "dataset": dataset_arg,
                    "limit": {"type": "integer"}},
                    "required": ["section"]}}},
            {"type": "function", "function": {
                "name": "outline",
                "description": "The document's table of contents, rebuilt from the "
                               "section metadata: chapter/section titles with their "
                               "page ranges. Navigation, not evidence.",
                "parameters": {"type": "object", "properties": {
                    "dataset": dataset_arg,
                    "chapter": {"type": "string",
                                "description": "list this chapter's sections instead of the top level"}}},
                }},
        ]

    # --- dispatch ----------------------------------------------------------
    def run(self, name, args, default_dataset=None):
        """Run one tool call. Returns ``(rows, images, note)``.

        Raises ToolError for anything the model can correct (unknown id, bad
        regex, unknown dataset); the agent turns that into an observation instead
        of ending the session, because a wrong tool call is a normal step. Images
        are always empty in this project — there is no page rendering here — but
        the tuple keeps the shape the agent loop was ported with.
        """
        args = args or {}
        if default_dataset and not args.get("dataset"):
            args = dict(args, dataset=default_dataset)
        fn = getattr(self, "_tool_" + name, None)
        if fn is None:
            raise ToolError("no such tool: %s" % name)
        try:
            result = fn(**{k: v for k, v in args.items() if v is not None}) or {}
        except TypeError as e:
            # A stray, misspelled or missing argument is a call the MODEL can fix,
            # and it will make them: classifying this as an internal failure tells
            # it the tool is broken instead of telling it what to pass. A genuine
            # bug inside a tool still raises and is reported as internal by the
            # agent layer.
            raise ToolError("bad arguments for %s: %s" % (name, e))
        return (result.get("rows") or [], result.get("images") or [],
                result.get("note") or "")

    def _row(self, dataset, chunk, text, cap=None, kind=None):
        text, cut = _short(text, self.row_chars if cap is None else cap)
        if kind is None:
            kind = "heading" if (chunk or {}).get("heading_only") else "text"
        return {"dataset": dataset.id, "chunk": (chunk or {}).get("id"),
                "label": dataset.label(chunk), "kind": kind,
                "section": (chunk or {}).get("section"),
                "numbering": (chunk or {}).get("numbering"),
                "chapter": (chunk or {}).get("chapter"),
                "source": (chunk or {}).get("source") or "",
                "page_start": (chunk or {}).get("page_start"),
                "page_end": (chunk or {}).get("page_end"), "text": text,
                "truncated": cut, "chars": len(text)}

    # --- the tools ---------------------------------------------------------
    def _tool_list_datasets(self, dataset=None):
        # `dataset` is accepted and ignored: the model sometimes fills in every
        # argument it can, and refusing the call over an irrelevant one wastes a
        # turn.
        rows = []
        for info in self.list_datasets():
            label = info.get("name") or info["dataset"]
            if info.get("pages"):
                label = "%s · %s pages" % (label, info["pages"])
            rows.append({"dataset": info["dataset"], "chunk": "", "label": label,
                         "kind": "dataset", "text": "%d chunks" % info.get("chunks", 0),
                         "truncated": False, "chars": 0})
        return {"rows": rows, "note": "%d datasets" % len(rows)}

    def _tool_outline(self, dataset=None, chapter=None):
        ds = self.dataset(dataset)
        secs = list(ds.sections())
        if chapter:
            want = chapter.strip().lower()
            secs = [s for s in secs if want in (s.get("chapter") or "").lower()
                    or want in (s.get("title") or "").lower()
                    or want in (s.get("numbering") or "").lower()]
            if not secs:
                have = ", ".join(n for n, _ in ds.chapters()[:12])
                raise ToolError("no sections matching %r. Chapters: %s" % (chapter, have))
            secs = sorted(secs, key=lambda s: (s.get("page_start") or 0, s.get("depth") or 0))
            rows = [self._outline_row(ds, s) for s in secs[:OUTLINE_ROWS]]
            note = "%d sections" % len(rows)
            if len(secs) > len(rows):
                note += " (first %d — narrow the chapter)" % len(rows)
            return {"rows": rows, "note": note}
        # Without a chapter the outline is the table of contents: one row per
        # chapter, carrying its child section titles, which keeps navigation
        # narrow AND tells the model which sections exist. A document with no
        # chapter metadata falls back to its sections themselves.
        chapters = []
        by_chapter = collections.OrderedDict()
        for sec in secs:
            name = (sec.get("chapter") or "").strip()
            if not name:
                continue
            if name not in by_chapter:
                by_chapter[name] = []
                chapters.append(name)
            by_chapter[name].append(sec)
        if not chapters:
            if secs:
                shallow = min(s.get("depth") or 1 for s in secs)
                secs = [s for s in secs if (s.get("depth") or 1) <= shallow]
            secs = sorted(secs, key=lambda s: (s.get("page_start") or 0, s.get("depth") or 0))
            rows = [self._outline_row(ds, s) for s in secs[:OUTLINE_ROWS]]
            note = "%d sections" % len(rows)
            if len(secs) > len(rows):
                note += " (first %d — name a section to narrow)" % len(rows)
            return {"rows": rows, "note": note}
        rows = []
        for name in chapters[:OUTLINE_ROWS]:
            members = by_chapter[name]
            pages = [s.get("page_start") for s in members
                     if s.get("page_start") is not None]
            end_pages = [s.get("page_end") for s in members
                         if s.get("page_end") is not None]
            children = []
            for s in members[:40]:
                title = " ".join(p for p in ((s.get("numbering") or "").strip(),
                                             (s.get("title") or "").strip()) if p)
                if title:
                    children.append(title[:60])
            if len(members) > len(children):
                children.append("…")
            number = re.match(r"^(\d+(?:\.\d+)*)\b", name)
            rows.append({
                "dataset": ds.id, "chunk": "", "kind": "chapter",
                "label": name, "section": name,
                "numbering": number.group(1) if number else "",
                "chapter": name,
                "page_start": min(pages) if pages else None,
                "page_end": max(end_pages) if end_pages else None,
                "text": "; ".join(children), "truncated": False, "chars": 0})
        note = "%d chapters — name a chapter to list its sections" % len(rows)
        if len(chapters) > len(rows):
            note += " (first %d)" % len(rows)
        return {"rows": rows, "note": note}

    @staticmethod
    def _outline_row(dataset, sec):
        title = " ".join(p for p in ((sec.get("numbering") or "").strip(),
                                     (sec.get("title") or "").strip()) if p)
        return {
            "dataset": dataset.id, "chunk": "", "kind": "section",
            "label": "%s%s" % ("  " * max(0, (sec.get("depth") or 1) - 1), title),
            "section": sec.get("title"), "numbering": sec.get("numbering"),
            "chapter": sec.get("chapter"), "page_start": sec.get("page_start"),
            "page_end": sec.get("page_end"), "text": "", "truncated": False, "chars": 0}


    def _tool_search(self, query, dataset=None, k=5, mode="auto"):
        query = (query or "").strip()
        if not query:
            raise ToolError("empty query")
        ds = self.dataset(dataset)
        k = max(1, min(int(k or 5), 10))
        note_parts = []
        ranked = []
        # stage/timing metadata for the nested search log (best-effort)
        log = {"query": query, "dataset": ds.id, "topk": k,
               "pipeline": "keyword",
               "stages": {"embed": False, "retrieve": True, "rerank": False},
               "timings": {}, "reranked": False, "results": [], "note": "",
               "error": None}
        t_log = time.time()
        semantic_used = False
        if mode in ("auto", "semantic") and self.semantic is not None:
            t0 = time.time()
            try:
                got = self.semantic(ds.id, query, k)
            except TypeError:
                got = self.semantic(ds.name, query, k)
            log["timings"]["embed_sec"] = round(time.time() - t0, 4)
            if got:
                ranked = got
                semantic_used = True
                note_parts.append("semantic")
                log["pipeline"] = "semantic"
                log["stages"]["embed"] = True
        if not ranked:
            if mode == "semantic":
                note_parts.append("no embedder available — keyword ranking instead")
            ranked = _dataset_keyword_search(ds, query, k)
            log["pipeline"] = "keyword"
            note_parts.append("keyword")
        rows = []
        for rank, item in enumerate(ranked[:k], start=1):
            if isinstance(item, dict):
                chunk, score = item, None
            elif isinstance(item, str):
                chunk, score = item, None
            else:
                chunk = item[0] if len(item) > 0 else None
                score = item[1] if len(item) > 1 else None
            if chunk is None:
                continue
            # A retriever may hand back an index row rather than the chunk record:
            # resolve it through the canonical file so the row has the reader text
            # and the section/page fields, even when the index is a stale copy.
            if isinstance(chunk, str):
                chunk = ds.chunk(chunk)
            elif isinstance(chunk, dict):
                chunk = ds.chunk(chunk.get("id")) or chunk
            if not isinstance(chunk, dict):
                continue
            row = self._row(ds, chunk, ds.chunk_body(chunk), cap=self.hit_chars)
            row["rank"] = rank
            if score is not None:
                row["score"] = score
            rows.append(row)
        log["results"] = rows
        log["note"] = "%d hits (%s)" % (len(rows), ", ".join(note_parts))
        log["timings"]["total_sec"] = round(time.time() - t_log, 4)
        if self.search_log is not None:
            try:
                self.search_log(log)
            except Exception:
                pass
        return {"rows": rows, "note": log["note"]}

    def _tool_grep(self, pattern, dataset=None, k=5, case_sensitive=False, literal=False):
        pattern = pattern or ""
        if not pattern:
            raise ToolError("empty pattern")
        ds = self.dataset(dataset)
        k = max(1, min(int(k or 5), 12))
        hits = _dataset_grep(ds, pattern, k, case_sensitive=case_sensitive,
                             literal=literal)
        if hits is None:
            raise ToolError("invalid regular expression: %r" % pattern)
        if not hits:
            return {"rows": [], "note": "no match for %r in %s (%d chunks searched)"
                                        % (pattern, ds.name, len(ds.chunks))}
        rows = []
        for chunk, count, windows in hits:
            body = ds.chunk_body(chunk)
            text = ("%d match%s in this chunk. Matched lines:\n%s\n\nFull text:\n%s"
                    % (count, "" if count == 1 else "es",
                       "\n".join("… %s …" % w for w in windows),
                       body))
            rows.append(self._row(ds, chunk, text, cap=self.hit_chars))
        return {"rows": rows, "note": "%d chunks match %r" % (len(rows), pattern)}

    def _tool_read_chunk(self, chunk_id, dataset=None, offset=0, limit=None):
        ds = self.dataset(dataset)
        rec = ds.chunk(chunk_id)
        if rec is None:
            # Say what an id looks like. A live run lost a round trip to a model
            # that passed the human label from an observation as if it were an id —
            # the message has to describe the handle, not just reject the value.
            example = next(("e.g. %s" % x.get("id") for x in ds.chunks if x.get("id")), "")
            raise ToolError("no chunk %r in %s. A chunk id is the value printed as "
                            "'chunk <id>' at the start of an observation, not its "
                            "section label %s" % (chunk_id, ds.name, example))
        body = ds.chunk_body(rec)
        offset = max(0, int(offset or 0))
        cap = int(limit) if limit else self.row_chars
        text, cut = _short(body[offset:], cap)
        row = self._row(ds, rec, text)
        row["truncated"] = cut or offset + len(text) < len(body)
        if row["truncated"]:
            row["next_offset"] = offset + len(text)
        row["chars"] = len(body)
        return {"rows": [row]}

    def _tool_read_section(self, section, dataset=None, limit=None):
        ds = self.dataset(dataset)
        matches = _dataset_find_sections(ds, section)
        if not matches:
            raise ToolError("no section matching %r in %s" % (section, ds.name))
        if len(matches) > 1:
            rows = [{"dataset": ds.id, "chunk": "", "kind": "section",
                     "label": " ".join(p for p in ((m.get("numbering") or "").strip(),
                                                   (m.get("title") or "").strip()) if p),
                     "section": m.get("title"), "numbering": m.get("numbering"),
                     "chapter": m.get("chapter"), "page_start": m.get("page_start"),
                     "page_end": m.get("page_end"), "text": "", "chars": 0,
                     "truncated": False} for m in matches[:12]]
            return {"rows": rows,
                    "note": "%d sections match %r — call read_section again with the exact "
                            "numbering or title of the one you want" % (len(matches), section)}
        sec = matches[0]
        owned = _dataset_section_chunks(ds, sec)
        if not owned:
            raise ToolError("section %r has no chunks" % section)
        cap = int(limit) if limit else self.row_chars
        budget = cap
        rows = []
        for chunk in owned:
            if budget <= 0:
                rows.append({"dataset": ds.id, "chunk": chunk.get("id"),
                             "kind": "note", "label": ds.label(chunk), "text": "",
                             "chars": 0, "truncated": False})
                continue
            body = ds.chunk_body(chunk)
            text, cut = _short(body, budget)
            budget -= len(text)
            row = self._row(ds, chunk, text)
            row["truncated"] = cut
            rows.append(row)
        return {"rows": rows, "note": "%d chunks in section %s"
                                      % (len(owned), sec.get("numbering") or sec.get("title"))}


# --- dataset-side search primitives -----------------------------------------
# Free functions over a Dataset rather than methods on it: each one is a policy
# (how to rank, how to match a section) that is worth testing on its own, and the
# tool methods above stay thin dispatchers.
def _dataset_keyword_search(dataset, query, k):
    """Ranking by term overlap, over the text this module already has in memory.

    Deliberately small. The engine has a real BM25 over the index, but that only
    exists for a dataset that was embedded — this has to work for every dataset,
    including the freshly ingested ones, so it uses the chunk text itself.
    """
    terms = [t for t in re.split(r"[^0-9A-Za-z_+.#-]+", query.lower()) if len(t) > 1]
    if not terms:
        return []
    scored = []
    for chunk in dataset.chunks:
        hay = ((chunk.get("section") or "") + " "
               + (chunk.get("doc") or chunk.get("text") or "")).lower()
        if not hay:
            continue
        score = 0.0
        for t in terms:
            n = hay.count(t)
            if n:
                score += 1.0 + min(n, 5) * 0.2
        if not score:
            continue
        if _is_nav(chunk):
            score *= 0.15
        scored.append((score, chunk))
    scored.sort(key=lambda t: (-t[0], t[1].get("page_start") or 0))
    return [(c, round(s, 3)) for s, c in scored[:max(k, 1)]]


def _is_nav(chunk):
    chapter = (chunk.get("chapter") or "").strip().lower()
    return any(nav in chapter for nav in NAV_CHAPTERS)


def _dataset_grep(dataset, pattern, k, case_sensitive=False, literal=False):
    """Ranked literal/regex search; ``None`` means the pattern would not compile.

    Ranking is (match count, then a bonus when the match is in the chunk's own
    heading, then navigation chunks last, then page order). The heading bonus is
    what puts "2.1 Overview" above a passing mention on a contents page — a
    first-N implementation otherwise returns front matter and Contents and the
    model never reaches the section it wanted — and the navigation penalty keeps
    a table-of-contents entry from outranking the section it points at.
    """
    expr = re.escape(pattern) if literal else pattern
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        rx = re.compile(expr, flags)
    except re.error:
        return None
    hits = []
    for chunk in dataset.chunks:
        text = dataset.chunk_body(chunk)
        if not text:
            continue
        matches = list(rx.finditer(text))
        if not matches:
            continue
        windows = []
        for m in matches[:3]:
            start = max(0, m.start() - 160)
            end = min(len(text), m.end() + 160)
            windows.append(re.sub(r"\s+", " ", text[start:end]).strip())
        heading = "%s %s" % (chunk.get("numbering") or "", chunk.get("section") or "")
        score = min(len(matches), 8)
        if rx.search(heading):
            score += 3
        if _is_nav(chunk):
            score -= 4
        hits.append((score, -len(matches), chunk, len(matches), windows))
    hits.sort(key=lambda t: (-t[0], t[1], t[2].get("page_start") or 0))
    return [(chunk, count, windows) for _s, _n, chunk, count, windows in hits[:max(k, 1)]]


def _dataset_find_sections(dataset, needle):
    """Sections matching a printed number, a title, or a breadcrumb, best first.

    An exact numbering/title/path wins outright. Otherwise a PREFIX match on a
    number boundary is treated as the answer — "2.1" means section 2.1 and not
    2.10, which a live run spent a whole model turn disambiguating. Loose
    substring matches are returned only as a disambiguation list, where the model
    picks.
    """
    needle = (needle or "").strip().lower()
    if not needle:
        return []
    boundary = re.compile(re.escape(needle) + r"(?![\d.])")
    exact, prefix, partial = [], [], []
    for sec in dataset.sections():
        num = (sec.get("numbering") or "").strip().lower()
        title = (sec.get("title") or "").strip().lower()
        path = (sec.get("path") or "").strip().lower()
        if needle in (num, title, path):
            exact.append(sec)
        elif (num and boundary.match(num)) or boundary.match(title):
            prefix.append(sec)
        elif (num and num.startswith(needle)) or (needle and needle in title) \
                or (needle and needle in path):
            partial.append(sec)
    prefix.sort(key=lambda s: len((s.get("title") or "")))
    return exact or prefix or partial


def _dataset_section_chunks(dataset, section):
    """Every chunk that belongs to a printed section, in document order.

    Chunks are already in document order in ``chunks.jsonl``, so no page sort is
    needed; it is an explicit sort anyway because ingest is allowed to write a
    line per page group and a later regression should not silently reorder reads.
    """
    num = (section.get("numbering") or "").strip()
    path = (section.get("path") or "").strip()
    title = (section.get("title") or "").strip()
    chapter = (section.get("chapter") or "").strip()
    owned = []
    if num:
        owned = [c for c in dataset.chunks
                 if (c.get("numbering") or "").strip() == num]
    if not owned and path:
        owned = [c for c in dataset.chunks if (c.get("path") or "").strip() == path]
    if not owned and title:
        owned = [c for c in dataset.chunks
                 if (c.get("section") or "").strip() == title
                 and (c.get("chapter") or "").strip() == chapter]
    return sorted(owned, key=lambda c: (c.get("page_start") or 0,))
