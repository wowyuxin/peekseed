"""Offline fixtures for peekseed's test suite.

Nothing here needs a model or the network: a small HTTP server implements the
four model contracts peekseed speaks (OpenAI embeddings, custom /embed, /rerank,
OpenAI chat), a tiny PDF writer produces valid one-page uploads,
:func:`make_peel` builds a fake peelfeed peel corpus (manifest/chunks/blocks/
validation/review/assets + source copy) so the corpus read path can be tested
without the extraction engine, and :class:`FakePeelfeedCli` is a fake peelfeed
executable so the CLI seam (config, capabilities handshake, ingest polling,
review/decide/rebuild/event/archive) can be tested without the real engine.
"""

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, status, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server = self.server
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._send(200, {"object": "list",
                             "data": [{"id": m} for m in server.fake.models]})
            return
        if self.path.rstrip("/") in ("/health", ""):
            self._send(200, {"ok": True, "model": server.fake.models[0],
                             "dim": server.fake.dim})
            return
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        path = self.path.split("?")[0].rstrip("/")
        server.fake.requests.append((path, body, dict(self.headers)))
        if path in ("/v1/embeddings", "/embeddings"):
            self._embeddings(body)
            return
        if path == "/embed":
            self._embeddings(body, custom=True)
            return
        if path == "/rerank":
            self._rerank(body)
            return
        if path in ("/v1/chat/completions", "/chat/completions"):
            self._chat(body)
            return
        self._send(404, {"error": {"message": "not found"}})

    def _embeddings(self, body, custom=False):
        texts = body.get("input") if isinstance(body.get("input"), list) else []
        if custom:
            texts = [item.get("text", "") for item in body.get("inputs") or []
                     if isinstance(item, dict)]
        vectors = [self.server.fake.vector(text) for text in texts]
        if custom:
            self._send(200, {"embeddings": vectors})
        else:
            self._send(200, {"object": "list", "model": body.get("model") or "",
                             "data": [{"object": "embedding", "index": i,
                                       "embedding": vec}
                                      for i, vec in enumerate(vectors)]})

    def _rerank(self, body):
        query = ((body.get("query") or {}).get("text") or "").lower()
        words = set(query.split())
        scores = []
        for doc in body.get("documents") or []:
            text = ((doc or {}).get("text") or "").lower()
            scores.append(float(len(words & set(text.split()))))
        self._send(200, {"scores": scores})

    def _chat(self, body):
        fake = self.server.fake
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(payload):
            chunk = "data: " + json.dumps(payload) + "\n\n"
            self.wfile.write(chunk.encode())
            self.wfile.flush()

        script = fake.next_chat_script(body)
        try:
            for event in script:
                if event.get("tool_calls"):
                    emit({"choices": [{"delta": {"tool_calls": event["tool_calls"]},
                                       "finish_reason": None}]})
                elif event.get("content") is not None:
                    emit({"choices": [{"delta": {"content": event["content"]},
                                       "finish_reason": None}]})
                elif event.get("reasoning") is not None:
                    emit({"choices": [{"delta": {"reasoning_content":
                                                 event["reasoning"]},
                                       "finish_reason": None}]})
                if event.get("finish"):
                    emit({"choices": [{"delta": {}, "finish_reason": event["finish"]}]})
            emit({"choices": [{"delta": {}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                            "total_tokens": 15}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        finally:
            self.close_connection = True


class FakeModelServer:
    """A local HTTP server implementing the four model contracts.

    Deterministic by design: the embedding of a text is a hash-derived unit
    vector, so a search over the same fixtures always ranks the same way, and a
    test can assert semantics (the query's own chunk first) rather than a
    hard-coded float.
    """

    def __init__(self, dim=8, models=None):
        self.dim = dim
        self.models = list(models or ["test-answer-model"])
        self.requests = []
        self._chat_scripts = []
        self._chat_default = None
        self._httpd = None
        self._thread = None

    # --- lifecycle --------------------------------------------------------
    def start(self):
        if self._httpd is not None:
            return self
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
        self._httpd.fake = self
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="fake-models", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
            self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def port(self):
        return self._httpd.server_address[1]

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    @property
    def openai_base(self):
        return self.base + "/v1"

    # --- behaviour --------------------------------------------------------
    def vector(self, text):
        """A deterministic bag-of-words embedding.

        A hash of the whole string would make unrelated texts look random and
        rank a query against nothing; a hashed bag of words keeps the one
        property retrieval tests need: a query shares dimensions with the
        documents that contain its words, so the matching chunk ranks first.
        """
        values = [0.0] * self.dim
        for token in re.findall(r"[a-z0-9]+", (text or "").lower()):
            if len(token) < 2:
                continue
            values[zlib.crc32(token.encode()) % self.dim] += 1.0
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [round(value / norm, 6) for value in values]

    def set_chat_script(self, scripts):
        """Queue chat SSE scripts; each item is a list of delta dicts.

        A dict item is ``{"content": "..."}``, ``{"reasoning": "..."}`` or
        ``{"tool_calls": [...]}``. When the queue is empty the default reply is
        used.
        """
        self._chat_scripts = list(scripts)

    def set_default_chat(self, events):
        self._chat_default = list(events)

    def next_chat_script(self, body):
        if self._chat_scripts:
            return self._chat_scripts.pop(0)
        if self._chat_default is not None:
            return self._chat_default
        return [{"content": "Answer from the excerpts [1].", "finish": "stop"}]


# --- PDF generation --------------------------------------------------------

def _pdf_escape(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path, pages=None):
    """Write a minimal, valid PDF with real text so pypdf can extract it.

    Hand-built rather than pulling a PDF library in as a test dependency: the
    object layout is small, and the fixture is then stable across pypdf versions.
    """
    pages = list(pages or [
        "Widget installation guide. The widget is mounted on the frame with "
        "two screws. Torque is 4 Nm.",
        "Widget operation. Press the green button to start the widget. The "
        "status light turns blue.",
    ])
    objects = []
    font_id = 3 + len(pages) * 2
    kids = " ".join("%d 0 R" % (3 + index * 2) for index in range(len(pages)))
    objects.append("<< /Type /Catalog /Pages 2 0 R >>")
    objects.append("<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(pages)))
    for index, text in enumerate(pages):
        page_id = 3 + index * 2
        content_id = page_id + 1
        stream = ("BT /F1 12 Tf 72 720 Td (%s) Tj ET"
                  % _pdf_escape(text))
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (font_id, content_id))
        objects.append("<< /Length %d >>\nstream\n%s\nendstream"
                       % (len(stream), stream))
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = ["%PDF-1.4"]
    offsets = [0]
    position = len(out[0]) + 1
    for number, body in enumerate(objects, start=1):
        line = "%d 0 obj\n%s\nendobj" % (number, body)
        offsets.append(position)
        out.append(line)
        position += len(line) + 1
    xref_at = position
    xref = ["xref", "0 %d" % (len(objects) + 1), "0000000000 65535 f "]
    for offset in offsets[1:]:
        xref.append("%010d 00000 n " % offset)
    out.extend(xref)
    out.append("trailer\n<< /Size %d /Root 1 0 R >>" % (len(objects) + 1))
    out.append("startxref\n%d\n%%%%EOF" % xref_at)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\n".join(out) + "\n").encode("latin-1"))
    return path


# --- data roots and datasets ----------------------------------------------

@contextlib.contextmanager
def temp_data_root():
    """Point peekseed at a throwaway home + shared folder and reset every cache.

    Yields the home directory (``<tmp>/home``); the shared folder is
    ``<tmp>/shared`` (``config.paths().shared_dir``).
    """
    from peekseed import app as app_module
    from peekseed import config, links

    previous_home = os.environ.get("PEEKSEED_HOME")
    previous_shared = os.environ.get("PEEKSEED_SHARED_DIR")
    base = Path(tempfile.mkdtemp(prefix="peekseed-test-"))
    home = base / "home"
    shared = base / "shared"
    home.mkdir()
    shared.mkdir()
    os.environ["PEEKSEED_HOME"] = str(home)
    os.environ["PEEKSEED_SHARED_DIR"] = str(shared)
    config.reset_cache()
    links.reset_cache()
    app_module.reset_state()
    try:
        yield home
    finally:
        app_module.reset_state()
        config.reset_cache()
        links.reset_cache()
        if previous_home is None:
            os.environ.pop("PEEKSEED_HOME", None)
        else:
            os.environ["PEEKSEED_HOME"] = previous_home
        if previous_shared is None:
            os.environ.pop("PEEKSEED_SHARED_DIR", None)
        else:
            os.environ["PEEKSEED_SHARED_DIR"] = previous_shared
        shutil.rmtree(str(base), ignore_errors=True)


def make_dataset(name="Docs", pages=None, note=""):
    """Create a dataset with a real PDF, returning its metadata dict."""
    from peekseed import config, ingest

    dataset = ingest.create_dataset(config.paths(), name, note=note)
    scratch = Path(tempfile.mkdtemp(prefix="peekseed-pdf-"))
    try:
        pdf = make_pdf(scratch / "manual.pdf", pages=pages)
        ingest.add_source(dataset, pdf)
    finally:
        shutil.rmtree(str(scratch), ignore_errors=True)
    return dataset


def write_chunks(dataset, chunks):
    from peekseed import ingest
    ingest.write_chunks(Path(dataset["dir"]), chunks)


def sample_chunks(dataset_id="docs", source="manual.pdf"):
    """A tiny three-chunk dataset used by the engine/tools/history tests."""
    rows = [
        ("Install the widget", "2.1 Installation",
         "The widget is mounted on the frame with two screws. Torque is 4 Nm.",
         1),
        ("Start the widget", "2.2 Operation",
         "Press the green button to start the widget. The status light turns blue.",
         2),
        ("Stop the widget", "2.3 Shutdown",
         "Hold the red button for two seconds to stop the widget safely.", 3),
    ]
    chunks = []
    for index, (title, section, body, page) in enumerate(rows, start=1):
        chunks.append({
            "id": "%s-%06d" % (dataset_id, index),
            "dataset": dataset_id,
            "source": source,
            "page_start": page,
            "page_end": page,
            "section": section,
            "chapter": "2 Using the widget",
            "numbering": section.split(" ")[0],
            "parent": "2 Using the widget",
            "path": "2 Using the widget / " + section,
            "doc": "2 Using the widget\n\n" + body,
            "text": body,
            "heading_only": False,
        })
    return chunks


# --- fake peelfeed peels ---------------------------------------------------

def _peel_slug(name):
    text = re.sub(r"[^a-z0-9._-]+", "-", str(name or "").lower())
    return text.strip("-._")[:40] or "peel"


#: The chunk sizes test embed links use (peelfeed set ``c8000-12000-i3``).
EMBED_CHUNK = {"target_chars": 8000, "max_chars": 12000, "image_cap": 3}


def write_chunk_sets(corpus, chunks, chunk_sets, conversion_id):
    """Write ``versions.json`` + ``chunks_all_ver/<set>/chunks.jsonl`` like peelfeed.

    Every set re-packs the canonical chunks one-to-one (enough for the indexer),
    with set-specific ids and the canonical unit ids in ``canonical_ids``.
    """
    corpus = Path(corpus)
    versions = {}
    for sizes in chunk_sets:
        name = "c%d-%d-i%d" % (sizes["target_chars"], sizes["max_chars"],
                               sizes["image_cap"])
        versions[name] = {"dir": name,
                          "chunk_target_chars": sizes["target_chars"],
                          "chunk_max_chars": sizes["max_chars"],
                          "rerank_img_cap": sizes["image_cap"],
                          "conversion_id": conversion_id,
                          "chunk_count": len(chunks)}
        rows = [dict(chunk, id="fake-%s-%06d" % (name, number),
                     canonical_ids=[str(chunk.get("id") or "")])
                for number, chunk in enumerate(chunks, start=1)]
        feed = corpus / "chunks_all_ver" / name / "chunks.jsonl"
        feed.parent.mkdir(parents=True, exist_ok=True)
        feed.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                for row in rows), encoding="utf-8")
    registry = {"active": next(iter(versions), ""), "versions": versions,
                "conversion_id": conversion_id}
    (corpus / "versions.json").write_text(json.dumps(registry), encoding="utf-8")


def make_peel(dataset, *, pages=None, source=None, profile="generic",
              validation_errors=None, peel_root=None, engine="peelfeed-test@1",
              conversion_salt="1", chunks=None, chunk_sets=(EMBED_CHUNK,)):
    """Create a fake peelfeed peel for one dataset source and mark it extracted.

    ``dataset`` is a dataset metadata dict (``dir``) or a dataset id. The peel is
    written under ``config.paths().peels_dir`` with the files
    :func:`peekseed.ingest.build_index` consumes: ``manifest.json``,
    ``chunks.jsonl``, ``blocks.jsonl``, ``assets.jsonl``, ``validation.json``,
    ``review_queue.json`` and a copy of the source under ``source/``. The dataset
    source entry is updated to ``status="extracted"`` with the peel identity.

    Returns ``{"peel_id", "peel_dir", "conversion_id"}``.
    """
    from peekseed import config, contracts, ingest

    if isinstance(dataset, dict):
        dataset_dir = Path(dataset["dir"])
        dataset_id = str(dataset.get("id") or dataset_dir.name)
    else:
        dataset_id = str(dataset)
        dataset_dir = Path(config.paths().datasets_dir) / dataset_id
    metadata = contracts.read_json(dataset_dir / "dataset.json")
    sources = [entry for entry in metadata.get("sources") or []
               if isinstance(entry, dict)]
    entry = None
    for candidate in sources:
        if source is None or str(candidate.get("name") or "") == source:
            if str(candidate.get("sha256") or ""):
                entry = candidate
                break
    if entry is None:
        raise AssertionError("no source with a sha256 in %s" % dataset_dir)
    sha = str(entry["sha256"]).lower()
    name = str(entry["name"])
    source_file = ingest.stored_source_path(dataset_dir, entry)
    if source_file is None:
        raise AssertionError("no stored source file for %s in %s"
                             % (name, dataset_dir))
    if peel_root is None:
        peel_root = config.paths().peels_dir
    peel_root = Path(peel_root)
    peel_id = "%s-%s" % (_peel_slug(Path(name).stem), sha[:8])
    peel_dir = peel_root / peel_id
    corpus = peel_dir / "extract" / "corpus"
    (peel_dir / "source").mkdir(parents=True, exist_ok=True)
    corpus.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(source_file), str(peel_dir / "source" / name))

    conversion_id = hashlib.sha256(
        ("conv|%s|%s|%s" % (sha, profile, conversion_salt)).encode()
    ).hexdigest()
    if pages is None:
        pages = [
            "Widget installation. The widget is mounted on the frame with two screws.",
            "Widget operation. Press the green button to start the widget.",
        ]
    if chunks is None:
        chunks = []
        for number, page_text in enumerate(pages, start=1):
            section = "Section %d" % number
            chunks.append({
                "schema_version": 2,
                "id": "fake-unit-%06d" % number,
                "conversion_id": conversion_id,
                "kind": "text",
                "section": section,
                "numbering": "",
                "parent": "",
                "path": section,
                "page_start": number,
                "page_end": number,
                "source_pages": [number],
                "pdf_y": 0,
                "image_paths": [],
                "block_ids": ["p%04d-b0000" % number],
                "canonical_ids": ["fake-unit-%06d" % number],
                "char_count": len(page_text),
                "parts": 1,
                "part": 1,
                "text": "# %s\n\n<!-- page: %d -->\n\n%s"
                        % (section, number, page_text),
            })
    blocks = []
    for chunk in chunks:
        for block_id in chunk.get("block_ids") or []:
            blocks.append({
                "id": block_id,
                "kind": chunk.get("kind") or "text",
                "page": int(chunk.get("page_start") or 1),
                "text": str(chunk.get("text") or ""),
            })
    manifest = {
        "schema_version": 2,
        "conversion_id": conversion_id,
        "title": Path(name).stem,
        "profile": profile,
        "source": name,
        "source_sha256": sha,
        "pages": len(pages),
        "engine": engine,
        "chunks": len(chunks),
    }
    validation = {
        "schema_version": 2,
        "conversion_id": conversion_id,
        "partial": False,
        "errors": list(validation_errors or []),
        "checks": {},
        "review_items": 0,
        "char_limit_exceptions": 0,
    }
    review_queue = {"warnings": [], "char_limit_exceptions": [],
                    "outline_reconciliations": []}
    contracts.write_json_atomic(corpus / "manifest.json", manifest)
    contracts.write_json_atomic(corpus / "validation.json", validation)
    contracts.write_json_atomic(corpus / "review_queue.json", review_queue)
    (corpus / "assets.jsonl").write_text("", encoding="utf-8")
    (corpus / "blocks.jsonl").write_text(
        "\n".join(json.dumps(block, ensure_ascii=False) for block in blocks)
        + ("\n" if blocks else ""),
        encoding="utf-8",
    )
    (corpus / "chunks.jsonl").write_text(
        "\n".join(json.dumps(chunk, ensure_ascii=False) for chunk in chunks)
        + ("\n" if chunks else ""),
        encoding="utf-8",
    )
    if chunk_sets:
        write_chunk_sets(corpus, chunks, chunk_sets, conversion_id)
    entry["status"] = "extracted"
    entry["pages"] = len(pages)
    entry["extraction"] = {
        "state": "extracted",
        "peel_id": peel_id,
        "conversion_id": conversion_id,
        "profile": profile,
        "engine": engine,
        "review_jobs": {},
        "validation": {"errors": 0, "review_items": 0},
        "started_at": contracts.utc_now(),
        "finished_at": contracts.utc_now(),
        "error": None,
    }
    metadata["schema"] = contracts.DATASET_SCHEMA
    metadata["sources"] = sources
    metadata["extractor"] = "peelfeed@" + engine
    metadata["chunker"] = contracts.chunker_label()
    metadata["updated_at"] = contracts.utc_now()
    contracts.write_json_atomic(dataset_dir / "dataset.json", metadata)
    return {"peel_id": peel_id, "peel_dir": str(peel_dir),
            "conversion_id": conversion_id}


# --- fake peelfeed CLI -----------------------------------------------------

_FAKE_CLI_SCRIPT = r'''#!%(python)s
"""A fake peelfeed CLI for tests: prints JSON, records calls, can fail/timeout."""
import hashlib
import json
import os
import sys
import time

STATE = %(state)r
LOG = %(log)r

DEFAULT_CAPABILITIES = {
    "engine": "peelfeed",
    "version": "0.1.0",
    "cli_protocol": 1,
    "corpus_schema": 2,
    "chunk_contract": 1,
    "peel_event_schema": 1,
    "chunker_version": 1,
    "review_schema": 2,
    "operations": ["inherit_header_from", "set_header_rows"],
    "resolutions": ["confirm_headerless", "confirm_known", "defer",
                    "keep_original_figure", "keep_visual_fallback"],
    "strategies": [{"id": "use_first_row_header", "version": 1,
                    "name": "本表首行作表头"}],
    "formats": ["pdf"],
    "profiles": ["cxl", "ddr5", "dwc", "generic", "pcie", "pipe"],
    "chunk_profiles": ["default", "large", "small"],
    "llm_modes": ["off", "review", "aggressive"],
    "peel_root": "/tmp/fake-peels",
    "docling": {"python": "/tmp/docling-python", "python_present": True,
                "models_present": True},
}

# --- schema-2 review payloads (the shapes the real peelfeed CLI prints) ----

STRATEGY_OPTIONS = [
    {"id": "use_first_row_header", "version": 1, "name": "本表首行作表头",
     "description": "把本表第 0 行作为表头；文本来自本表 native cells。",
     "recommended": True, "available": True, "unavailable_reason": None,
     "capabilities": ["native_lines"], "params_schema": {},
     "action": {"type": "operation", "name": "set_header_rows"},
     "min_tier": "R2", "batch_safe": True,
     "risk": "误把数据行作表头；须过签名阈值和逐表 validator"},
    {"id": "use_ocr_labels", "version": 1, "name": "OCR 标签",
     "description": "用按需 OCR 恢复标签（需要 OCR）。",
     "recommended": False, "available": False,
     "unavailable_reason": "OCR is not configured",
     "capabilities": ["ocr"], "params_schema": {},
     "action": {"type": "operation", "name": "set_asset_labels_from_ocr"},
     "min_tier": "R2", "batch_safe": False,
     "risk": "OCR 文本不是 native evidence"},
]

CLUSTER_HEADER = {
    "cluster_id": "cl-0f1e2d3c4b5a",
    "cluster_key": {"code": "table_header_absent", "asset_kind": "table",
                    "profile": "generic", "operation": "set_header_rows",
                    "layout": "structured|cols=2|rows=6-11|caption=yes|pages=1",
                    "evidence": "native_lines"},
    "code": "table_header_absent",
    "title": "表格缺少表头",
    "kind": "table",
    "item_count": 1,
    "human_pending": 1,
    "representative_item_id": "it-3f9c2a4b6d8e0f1a",
    "states": {"needs_decision": 1},
    "conclusion": None,
    "suggestions": [],
    "strategies": STRATEGY_OPTIONS,
    "item_ids": ["it-3f9c2a4b6d8e0f1a"],
}

CLUSTER_FIGURE = {
    "cluster_id": "cl-9a8b7c6d5e4f",
    "cluster_key": {"code": "no_native_labels", "asset_kind": "figure",
                    "profile": "generic", "operation": None,
                    "layout": "figure|pages=2", "evidence": "page_image"},
    "code": "no_native_labels",
    "title": "图内没有 native 文本",
    "kind": "figure",
    "item_count": 1,
    "human_pending": 0,
    "representative_item_id": "it-8b7a6c5d4e3f2a10",
    "states": {"pending_evidence": 1},
    "conclusion": None,
    "suggestions": [],
    "strategies": STRATEGY_OPTIONS,
    "item_ids": ["it-8b7a6c5d4e3f2a10"],
}

ITEM_HEADER = {
    "id": "it-3f9c2a4b6d8e0f1a",
    "code": "table_header_absent",
    "kind": "table",
    "severity": "warning",
    "blocking": False,
    "asset_id": "table-3-19",
    "pages": [109],
    "cluster_id": "cl-0f1e2d3c4b5a",
    "message": "table_header_absent: table-3-19; only explicit column "
               "positions are named.",
    "triage": "deterministic_candidate",
    "status": "needs_decision",
    "evidence": {"hash": "9c1f2e3d4b5a69788796a5b4c3d2e1f0",
                 "crops": ["tables/table-3-19_p0109.png"],
                 "native_line_ids": [123], "ocr_evidence": []},
    "current": {"status": "structured", "row_count": 5, "col_count": 2,
                "header_rows": []},
    "candidates": [{
        "strategy": {"id": "use_first_row_header", "version": 1},
        "action": {"type": "operation", "name": "set_header_rows",
                   "params": {"rows": [0]}},
        "source": "rule",
        "validator": {"status": "ready", "reasons": []},
        "evidence_refs": ["asset:table-3-19", "row:0"],
    }],
    "parent_candidates": [{
        "asset_id": "table-p1041-01",
        "pages": [1041],
        "col_count": 2,
        "header_rows": [0],
        "header_cells": [["Bits", "Variable"]],
        "header_signature": "bits|variable",
        "source_refs": [{"segment": "table-p1041-01", "source_row": 0,
                         "cells": ["Bits", "Variable"],
                         "evidence_hash": "ev-p1041"}],
        "parent_evidence_hash": "ev-p1041",
        "header_source": "native",
    }],
    "strategies": STRATEGY_OPTIONS,
    "entry": None,
    "resolution": None,
    "layout": {"cols": 2, "rows_bucket": "2-5", "page_span": 1,
               "caption": "Table 3-19. RSP_PRE Encodings"},
    "stale": False,
}

ITEM_FIGURE = {
    "id": "it-8b7a6c5d4e3f2a10",
    "code": "no_native_labels",
    "kind": "figure",
    "severity": "warning",
    "blocking": False,
    "asset_id": "figure-1-1",
    "pages": [2],
    "cluster_id": "cl-9a8b7c6d5e4f",
    "message": "no_native_labels: figure-1-1; use OCR evidence or keep the "
               "original figure.",
    "triage": "needs_ocr",
    "status": "pending_evidence",
    "evidence": {"hash": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
                 "crops": ["images/fig_1_1.png"], "native_line_ids": [],
                 "ocr_evidence": []},
    "current": {"status": "visual_fallback", "row_count": 0, "col_count": 0,
                "header_rows": []},
    "candidates": [],
    "parent_candidates": [],
    "strategies": STRATEGY_OPTIONS,
    "entry": None,
    "resolution": None,
    "layout": {"cols": 0, "rows_bucket": "0", "page_span": 1,
               "caption": "Figure 1-1. Widget frame"},
    "stale": False,
}

# CP2 W9: a stale ``inherit_header_from`` entry. The stored snapshot keeps the
# old lower-case parent cells; the parent candidate carries the source original
# case ("Bits"/"Variable"), so re-confirm changes content and bumps revision
# (wire §2.7). A hash-only refresh test keeps the cells identical and changes
# only ``evidence_hash`` (no revision bump).
STALE_ENTRY = {
    "entry_id": "e-5a7c8b9d0e1f2a3b",
    "item_id": "it-5a7c8b9d0e1f2a3b",
    "status": "confirmed",
    "stale": True,
    "stale_reasons": ["evidence_changed", "parent_changed"],
    "locked": False,
    "strategy": {"id": "inherit_parent_header", "version": 1},
    "action": {"type": "operation", "name": "inherit_header_from",
               "params": {
                   "parent_asset_id": "table-p1041-01",
                   "parent_header_rows": [0],
                   "parent_header_cells": [["bits", "variable"]],
                   "parent_source_refs": [{"segment": "table-p1041-01",
                                           "source_row": 0,
                                           "cells": ["bits", "variable"],
                                           "evidence_hash": "ev-p1041-old"}],
                   "parent_header_source": "rule"}},
    "evidence": {"hash": "5d6e7f8091a2b3c4", "refs": []},
    "dependencies": [{"parent_asset_id": "table-p1041-01",
                      "parent_evidence_hash": "ev-p1041-old",
                      "item_id": None, "correction_hash": None}],
    "capability_snapshot": {"native_lines": True, "ocr": None, "vlm": None},
    "source": "user", "revision": 2, "batch_id": None, "notes": None,
}

ITEM_STALE = {
    "id": "it-5a7c8b9d0e1f2a3b",
    "code": "table_header_absent",
    "kind": "table",
    "severity": "warning",
    "blocking": False,
    "asset_id": "table-4-07",
    "pages": [204],
    "cluster_id": "cl-1a2b3c4d5e6f",
    "message": "table_header_absent: table-4-07; inherited parent evidence "
               "changed.",
    "triage": "needs_decision",
    "status": "stale",
    "evidence": {"hash": "5d6e7f8091a2b3c4",
                 "crops": ["tables/table-4-07_p0204.png"],
                 "native_line_ids": [7], "ocr_evidence": []},
    "current": {"status": "structured", "row_count": 4, "col_count": 2,
                "header_rows": []},
    "candidates": [],
    "parent_candidates": [{
        "asset_id": "table-p1041-01",
        "pages": [1041],
        "col_count": 2,
        "header_rows": [0],
        "header_cells": [["Bits", "Variable"]],
        "header_signature": "bits|variable",
        "source_refs": [{"segment": "table-p1041-01", "source_row": 0,
                         "cells": ["Bits", "Variable"],
                         "evidence_hash": "ev-p1041-new"}],
        "parent_evidence_hash": "ev-p1041-new",
        "header_source": "rule",
    }],
    "strategies": STRATEGY_OPTIONS,
    "entry": STALE_ENTRY,
    "resolution": None,
    "layout": {"cols": 2, "rows_bucket": "2-5", "page_span": 1,
               "caption": "Table 4-07. Inherited header"},
    "stale": True,
}

CLUSTER_STALE = {
    "cluster_id": "cl-1a2b3c4d5e6f",
    "cluster_key": {"code": "table_header_absent", "asset_kind": "table",
                    "profile": "generic", "operation": "inherit_header_from",
                    "layout": "table|pages=204", "evidence": "native"},
    "code": "table_header_absent",
    "title": "表头缺失（继承父表后父表证据变化）",
    "kind": "table",
    "item_count": 1,
    "human_pending": 1,
    "representative_item_id": "it-5a7c8b9d0e1f2a3b",
    "states": {"stale": 1},
    "conclusion": None,
    "suggestions": [],
    "strategies": STRATEGY_OPTIONS,
    "item_ids": ["it-5a7c8b9d0e1f2a3b"],
}

DEFAULT_REVIEW = {
    "peel_id": "peel",
    "review_schema": 2,
    "generated_at": "2026-11-05T00:00:00Z",
    "profile": "generic",
    # ``converter_code_hash`` is a diagnostic hash of the converter code
    # (``sha256(canonical converter_hashes)[:16]``). It is NOT
    # ``normalize.engine_fingerprint()`` and must never drive stale/CAS
    # decisions (contract v1.3 §2/§13.8; renamed from ``engine_fingerprint``
    # in CP1.1). The real CXL value is d6ef5d97b7865966.
    "derived_from": {
        "uncorrected_identity": "55959d86" + "0" * 56,
        "converter_code_hash": "d6ef5d97b7865966",
        "derivation_version": 1,
    },
    "capabilities": {"review_schema": 2, "native_lines": True, "ocr": False,
                     "ocr_detail": None, "vlm": False},
    "counts": {"raw_items": 3, "items": 3, "clusters": 3, "human_pending": 2,
               "human_pending_clusters": 2, "pending_evidence": 1,
               "auto_resolved": 0, "needs_decision": 1, "draft": 0,
               "confirmed": 0, "applied": 0, "deferred": 0, "failed_check": 1,
               "stale": 1, "unsupported_items": 0},
    "triage_counts": {"deterministic_candidate": 1, "needs_decision": 1,
                      "needs_ocr": 1, "informational": 0, "unsupported": 0},
    "plan": {"plan_version": 0, "entries": 0, "draft": 0, "confirmed": 0,
             "locked": 0, "stale": 0},
    "report": None,
    "clusters": [CLUSTER_HEADER, CLUSTER_FIGURE, CLUSTER_STALE],
    "items": [ITEM_HEADER, ITEM_FIGURE, ITEM_STALE],
}

DEFAULT_PLAN = {
    "peel_id": "peel",
    "review_schema": 2,
    "plan_version": 0,
    "updated_at": "2026-11-05T00:00:00Z",
    "entries": [],
    "entry": None,
    "resolution": None,
}

DEFAULT_REPORT = {
    "peel_id": "peel",
    "status": "ready",
    "report_path": "extract/review/report.json",
    "report": {
        "schema_version": 1,
        "status": "ready",
        "stale": False,
        "generated_at": "2026-11-05T00:00:00Z",
        "provider": {"kind": "rule", "model": None, "prompt_version": None},
        "clusters": [{
            "cluster_id": "cl-0f1e2d3c4b5a",
            "item_ids": ["it-3f9c2a4b6d8e0f1a"],
            "conclusion": "规则建议把第 0 行作为表头。",
            "suggestions": [{
                "item_id": "it-3f9c2a4b6d8e0f1a",
                "strategy": {"id": "use_first_row_header", "version": 1},
                "params": {},
                "evidence_refs": ["asset:table-3-19"],
                "validator": {"status": "ready", "reasons": []},
                "in_plan": False,
                "state": "ready",
            }],
            "clarifications": [],
        }, {
            "cluster_id": "cl-9a8b7c6d5e4f",
            "item_ids": ["it-8b7a6c5d4e3f2a10"],
            "conclusion": "图内无 native 文本：保留原始图（R0 resolution）。",
            "suggestions": [{
                "item_id": "it-8b7a6c5d4e3f2a10",
                "strategy": {"id": "keep_visual_fallback", "version": 1},
                "params": {},
                "evidence_refs": ["asset:figure-1-1"],
                "validator": {"status": "ready", "reasons": []},
                "in_plan": False,
                "state": "ready",
            }],
            "clarifications": [],
        }, {
            "cluster_id": "cl-1a2b3c4d5e6f",
            "item_ids": ["it-5a7c8b9d0e1f2a3b"],
            "conclusion": "父表证据变化，候选校验未通过。",
            "suggestions": [{
                "item_id": "it-5a7c8b9d0e1f2a3b",
                "strategy": {"id": "inherit_parent_header", "version": 1},
                "params": {"parent_asset_id": "table-p1041-01"},
                "evidence_refs": ["asset:table-p1041-01"],
                "validator": {"status": "failed_check",
                              "reasons": ["parent_evidence_changed"]},
                "in_plan": False,
                "state": "failed_check",
            }],
            "clarifications": [],
        }],
        "auto_archived": [],
        "unsupported": [],
    },
    "counts": {"suggestions": 3, "auto_archived": 0, "unsupported": 0,
               "needs_decision": 1},
}


def load_state():
    try:
        with open(STATE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def emit(payload):
    print(json.dumps(payload))
    return 0


def copy_json(value):
    return json.loads(json.dumps(value))


def fail(message, code=2, error_code="error", detail=None):
    """Emit the real peelfeed error protocol: stderr line + JSON error object."""
    print("error [" + str(error_code) + "] " + str(message), file=sys.stderr)
    print(json.dumps({"error": {"code": str(error_code),
                                "message": str(message),
                                "detail": detail or {}}}))
    return code


def read_stdin_json():
    try:
        return json.loads(sys.stdin.read() or "null")
    except ValueError:
        return None


# CP1.1 (contract v1.3 §11.1): selecting an R0 strategy creates a
# ``type=resolution`` action instead of a corpus operation; confirming that
# entry writes the resolution and applies it immediately. Operation strategies
# still produce ``type=operation`` entries that wait for a future ApplyBatch.
RESOLUTION_STRATEGIES = {
    "confirm_no_header": "confirm_headerless",
    "keep_visual_fallback": "keep_visual_fallback",
    "keep_original_figure": "keep_original_figure",
}
OPERATION_STRATEGIES = {
    "use_first_row_header": "set_header_rows",
    "use_first_two_rows_header": "set_header_rows",
    "use_header_rows": "set_header_rows",
    "inherit_parent_header": "inherit_header_from",
    "use_ocr_labels": "set_asset_labels_from_ocr",
}


def resolution_record(item_id, name, entry_id=None, revision=0,
                      reason_code=None, reason=None, evidence_hash=None):
    """A resolutions.jsonl row (contract §11.6), the shape ``plan defer`` uses."""
    material = "|".join([str(item_id), str(name), str(entry_id or ""),
                         str(int(revision or 0))])
    return {
        "schema_version": 1,
        "resolution_id": "r-" + hashlib.sha256(
            material.encode("utf-8")).hexdigest()[:16],
        "change": "add",
        "supersedes": None,
        "idempotency_key": hashlib.sha256(
            (material + "|add").encode("utf-8")).hexdigest(),
        "item_id": str(item_id),
        "name": str(name),
        "reason_code": reason_code,
        "reason": reason,
        "evidence_hash": evidence_hash,
        "source": "user",
        "recorded_at": "2026-11-05T00:00:00Z",
    }


def revert_resolution_record(target):
    """A ``revert`` resolutions.jsonl row (contract §11.6), written by clear."""
    row = dict(target)
    row["change"] = "revert"
    row["supersedes"] = target.get("resolution_id")
    material = "%%s|revert" %% target.get("resolution_id")
    row["resolution_id"] = "r-" + hashlib.sha256(
        material.encode("utf-8")).hexdigest()[:16]
    row["idempotency_key"] = hashlib.sha256(material.encode("utf-8")).hexdigest()
    row["recorded_at"] = "2026-11-05T00:00:00Z"
    return row


def report_suggestions(report_payload):
    """Flatten a review-report payload into its per-item suggestions."""
    payload = report_payload if isinstance(report_payload, dict) else {}
    inner = payload.get("report")
    report = inner if isinstance(inner, dict) else payload
    suggestions = []
    for cluster in report.get("clusters") or []:
        if not isinstance(cluster, dict):
            continue
        for suggestion in cluster.get("suggestions") or []:
            if isinstance(suggestion, dict):
                suggestions.append(suggestion)
    return suggestions


def _item_evidence_hash(review, item_id):
    """The review item's current evidence hash (used for accepted entries)."""
    for item in review.get("items") or []:
        if isinstance(item, dict) and item.get("id") == item_id:
            return (item.get("evidence") or {}).get("hash")
    return None


def _strip_evidence_hashes(value):
    """Drop ``evidence_hash`` keys so a hash-only refresh compares unchanged."""
    if isinstance(value, dict):
        return {key: _strip_evidence_hashes(item) for key, item in value.items()
                if key != "evidence_hash"}
    if isinstance(value, list):
        return [_strip_evidence_hashes(item) for item in value]
    return value


def strategy_action(strategy_id, params):
    """The plan entry action for one strategy, like ``plan select`` (wire §2.6)."""
    params = dict(params or {})
    if strategy_id in RESOLUTION_STRATEGIES:
        return {"type": "resolution", "name": RESOLUTION_STRATEGIES[strategy_id]}
    if strategy_id in OPERATION_STRATEGIES:
        name = OPERATION_STRATEGIES[strategy_id]
        if name == "set_header_rows" and not params:
            params = {"rows": [0]}
        return {"type": "operation", "name": name, "params": params}
    return None


def refresh_stale_entry(match, review):
    """Refresh one stale entry's fixed parent snapshot (wire §2.7).

    The fake CLI has no real parent table, so it refreshes from the item's
    ``parent_candidates`` (the source original-case cells). ``changed`` is true
    only when the refreshed ``action.params``/``source_refs`` content differs
    from the stored values; a hash-only evidence change keeps the revision.
    """
    item = next((candidate for candidate in (review.get("items") or [])
                 if candidate.get("id") == match.get("item_id")), None)
    candidates = (item or {}).get("parent_candidates") or []
    candidate = candidates[0] if candidates else {}
    action = match.get("action") or {}
    before_params = copy_json(action.get("params")) if action.get("params") else None
    after_params = copy_json(before_params) if before_params is not None else None
    if after_params is not None:
        if candidate.get("header_cells") is not None:
            after_params["parent_header_cells"] = copy_json(
                candidate.get("header_cells"))
        if candidate.get("source_refs") is not None:
            after_params["parent_source_refs"] = copy_json(
                candidate.get("source_refs"))
        if candidate.get("header_source"):
            after_params["parent_header_source"] = candidate.get("header_source")
    before_refs = before_params.get("parent_source_refs") if before_params else None
    after_refs = after_params.get("parent_source_refs") if after_params else None
    changed = (_strip_evidence_hashes(before_params)
               != _strip_evidence_hashes(after_params)
               or _strip_evidence_hashes(before_refs)
               != _strip_evidence_hashes(after_refs))
    revision_before = int(match.get("revision") or 0)
    revision_after = revision_before + 1 if changed else revision_before
    match["action"] = dict(action, params=after_params)
    match["revision"] = revision_after
    match["stale"] = False
    match["stale_reasons"] = []
    match["evidence"] = dict(match.get("evidence") or {},
                             hash=((item or {}).get("evidence") or {}).get("hash")
                             or (match.get("evidence") or {}).get("hash"))
    return {
        "changed": changed,
        "revision_before": revision_before,
        "revision_after": revision_after,
        "action_params_before": before_params,
        "action_params_after": after_params,
        "source_refs_before": before_refs,
        "source_refs_after": after_refs,
    }


def option(rest, name):
    for index, item in enumerate(rest):
        if item == name and index + 1 < len(rest):
            return rest[index + 1]
    return None


VALUE_FLAGS = {
    "--root", "--item", "--decision", "--note", "--evidence", "--model",
    "--confidence", "--event", "--data", "--reason", "--profile",
    "--chunk-profile", "--llm", "--limit", "--pages", "--slug",
    "--collection", "--device", "--group", "--status", "--entry",
    "--base-plan-version", "--input",
}


def positionals(rest):
    values = []
    skip = False
    for item in rest:
        if skip:
            skip = False
            continue
        if item in VALUE_FLAGS:
            skip = True
            continue
        if item.startswith("-"):
            continue
        values.append(item)
    return values


def first_positional(rest):
    values = positionals(rest)
    return values[0] if values else None


def main():
    argv = sys.argv[1:]
    if argv[:2] == ["-m", "peelfeed.cli"]:
        argv = argv[2:]
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "argv": argv,
            "peel_shared_dir": os.environ.get("PEELFEED_SHARED_DIR"),
            "pythonpath": os.environ.get("PYTHONPATH"),
        }) + "\n")
    if not argv:
        return fail("missing command")
    command, rest = argv[0], argv[1:]
    state = load_state()
    root = option(rest, "--root")

    forced_error = (state.get("errors") or {}).get(command)
    if forced_error:
        return fail(forced_error.get("message") or "simulated failure",
                    int(forced_error.get("exit") or 2),
                    forced_error.get("code") or "error",
                    forced_error.get("detail"))

    if command == "capabilities":
        delay = float(state.get("capabilities_delay") or 0)
        if delay:
            time.sleep(delay)
        if state.get("capabilities_fail"):
            return fail("capabilities failed")
        payload = dict(DEFAULT_CAPABILITIES)
        payload.update(state.get("capabilities") or {})
        shared = os.environ.get("PEELFEED_SHARED_DIR") or ""
        payload["peel_root"] = root or ((shared.rstrip("/") + "/peels")
                                        if shared else "")
        return emit(payload)

    peels = state.get("peels") or {}

    if command == "status":
        peel_id = first_positional(rest)
        if peel_id is None:
            # Mirror the real CLI: the list form is a *summary* (the source is a
            # name, not the {sha256,...} dict; no conversion_id).
            summaries = []
            for identifier, peel in peels.items():
                summaries.append({
                    "peel_id": identifier,
                    "state": peel.get("state"),
                    "profile": peel.get("profile"),
                    "counts": peel.get("counts"),
                    "validation": peel.get("validation"),
                    "source": (peel.get("source") or {}).get("name"),
                })
            return emit(summaries)
        if peel_id not in peels:
            return fail("no such peel", 3)
        return emit(peels[peel_id])

    if command == "ingest":
        source = first_positional(rest)
        if not source or not os.path.isfile(source):
            return fail("source not found")
        with open(source, "rb") as handle:
            sha = hashlib.sha256(handle.read()).hexdigest()
        if state.get("ingest_fail"):
            return fail("simulated ingest failure")
        peel_id = state.get("ingest_peel_id") or ("peel-" + sha[:8])
        pages = int(state.get("ingest_pages") or 2)
        review_items = int(state.get("ingest_review_items") or 0)
        errors = list(state.get("ingest_errors") or [])
        peels[peel_id] = {
            "peel_id": peel_id,
            "state": "converting",
            "source": {"name": os.path.basename(source),
                       "size": os.path.getsize(source),
                       "sha256": sha, "pages": pages},
            "profile": state.get("ingest_profile") or "generic",
            "conversion_id": "",
            "counts": {"pages": pages, "chunks": pages},
            "validation": {"errors": [], "review_items": review_items},
            "review_items": review_items,
            "error": None,
        }
        state["peels"] = peels
        save_state(state)
        time.sleep(float(state.get("ingest_seconds") or 0))
        conversion_id = hashlib.sha256(("conv|" + sha).encode()).hexdigest()
        summary = dict(peels[peel_id])
        summary.update({
            "state": "failed" if errors else "ready",
            "conversion_id": conversion_id,
            "validation": {"errors": errors, "review_items": review_items},
            "error": "validation failed" if errors else None,
        })
        peels[peel_id] = summary
        state["peels"] = peels
        save_state(state)
        emit({
            "ok": not errors,
            "peel": summary,
            "ingest": {"reused": bool(state.get("ingest_reused")),
                       "pages": pages},
            "profile": {"name": summary["profile"],
                        "matched_by": "fixture", "confidence": 0.5},
            "convert": {"cached": False, "seconds": {}},
            "normalize": {"cached": False, "reasons": [], "counts": {}},
            "gates": {"pass": not errors, "errors": errors, "warnings": [],
                      "checks": 1},
            "adjudicate": {},
        })
        return int(state.get("ingest_exit") or 0)

    if command == "review":
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        payload = copy_json(state.get("review") or DEFAULT_REVIEW)
        payload["peel_id"] = peel_id
        item_id = option(rest, "--item")
        if item_id:
            item = next((candidate for candidate in payload.get("items") or []
                         if candidate.get("id") == item_id), None)
            if item is None:
                return fail("no such review item", 3, "not_found")
            return emit({"peel_id": peel_id,
                         "review_schema": payload.get("review_schema", 2),
                         "item": item})
        status_filter = option(rest, "--status")
        if status_filter:
            items = [candidate for candidate in payload.get("items") or []
                     if candidate.get("status") == status_filter]
            cluster_ids = {candidate.get("cluster_id") for candidate in items}
            payload["items"] = items
            clusters = []
            for cluster in payload.get("clusters") or []:
                if cluster.get("cluster_id") not in cluster_ids:
                    continue
                states = {}
                for candidate in items:
                    if candidate.get("cluster_id") == cluster.get("cluster_id"):
                        state_name = candidate.get("status")
                        states[state_name] = states.get(state_name, 0) + 1
                cluster = copy_json(cluster)
                cluster["states"] = states
                clusters.append(cluster)
            payload["clusters"] = clusters
        return emit(payload)

    if command == "plan":
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        plan = copy_json(state.get("plan") or DEFAULT_PLAN)
        plan["peel_id"] = peel_id
        plan.setdefault("review_schema", 2)
        values = positionals(rest)
        action = values[1] if len(values) > 1 else "show"
        if action == "show":
            return emit(plan)
        if action not in ("select", "confirm", "lock", "unlock", "defer",
                          "accept", "clear"):
            return fail("unknown plan action: " + str(action), 2, "validation")
        entries = plan.get("entries") or []
        entry = None
        resolution = None
        refresh = None
        accepted = None
        cleared = None
        review = copy_json(state.get("review") or DEFAULT_REVIEW)
        report = copy_json(state.get("review_report") or DEFAULT_REPORT)
        if action in ("select", "defer"):
            request = read_stdin_json()
            if not isinstance(request, dict):
                return fail("plan input must be a JSON object", 2, "validation")
            base = request.get("base_plan_version")
            if base != plan.get("plan_version"):
                return fail("base_plan_version mismatch", 2, "conflict")
            item_id = request.get("item_id")
            if not item_id:
                return fail("plan item_id is required", 2, "validation")
            if action == "select":
                strategy = request.get("strategy") or {}
                strategy_id = str(strategy.get("id") or "")
                entry_id = "e-" + hashlib.sha256(
                    str(item_id).encode("utf-8")).hexdigest()[:16]
                match = next((candidate for candidate in entries
                              if candidate.get("entry_id") == entry_id), None)
                # Locked entries are read-only: select is conflict, never a
                # silent replacement (contract v1.3 §11.1; CP1.1 A3).
                if match is not None and match.get("locked"):
                    return fail("entry " + entry_id + " is locked; unlock it "
                                "first", 2, "conflict")
                params = request.get("params") or {}
                if strategy_id in RESOLUTION_STRATEGIES:
                    created_action = {
                        "type": "resolution",
                        "name": RESOLUTION_STRATEGIES[strategy_id],
                    }
                elif strategy_id in OPERATION_STRATEGIES:
                    name = OPERATION_STRATEGIES[strategy_id]
                    if name == "set_header_rows" and not params:
                        params = {"rows": [0]}
                    # Strategy id and params are passed through verbatim; for
                    # inherit_header_from that keeps the fixed parent header
                    # evidence the caller selected.
                    created_action = {"type": "operation", "name": name,
                                      "params": dict(params)}
                else:
                    return fail("unknown strategy: " + strategy_id,
                                2, "validation")
                revision = 1
                if match is not None:
                    revision = int(match.get("revision") or 0) + 1
                    entries = [candidate for candidate in entries
                               if candidate.get("entry_id") != entry_id]
                entry = {
                    "entry_id": entry_id,
                    "item_id": str(item_id),
                    "status": "draft",
                    "stale": False,
                    "stale_reasons": [],
                    "locked": False,
                    "strategy": {"id": strategy_id,
                                 "version": int(strategy.get("version") or 1)},
                    "action": created_action,
                    "evidence": {"hash": "9c1f2e3d4b5a69788796a5b4c3d2e1f0",
                                 "refs": []},
                    "dependencies": [],
                    "capability_snapshot": {"native_lines": True,
                                            "ocr": None, "vlm": None},
                    "source": "user",
                    "revision": revision,
                    "batch_id": None,
                    "notes": request.get("notes"),
                }
                entries.append(entry)
            else:
                # Defer is an R0 resolution; a locked entry cannot be deferred
                # and is never deleted (contract v1.3 §11.1; CP1.1 A3).
                match = next((candidate for candidate in entries
                              if candidate.get("item_id") == str(item_id)),
                             None)
                if match is not None and match.get("locked"):
                    return fail("entry " + str(match.get("entry_id"))
                                + " is locked; unlock it before deferring",
                                2, "conflict")
                resolution = resolution_record(
                    item_id, "defer",
                    reason_code=request.get("reason_code"),
                    reason=request.get("reason"))
                state.setdefault("resolutions", []).append(resolution)
        elif action == "accept":
            # Wire §2.6: all-or-nothing. Adopt each item's own ``ready`` report
            # suggestion, fail the whole request with per-item ``detail.reasons``
            # when any item is locked/stale/unsuggested/invalid, and bump
            # ``plan_version`` exactly once.
            request = read_stdin_json()
            if not isinstance(request, dict):
                return fail("plan input must be a JSON object", 2, "validation")
            base = request.get("base_plan_version")
            if base != plan.get("plan_version"):
                return fail("base_plan_version mismatch", 2, "conflict")
            item_ids = request.get("item_ids")
            if not isinstance(item_ids, list) or not item_ids:
                return fail("item_ids is required", 2, "validation")
            state.setdefault("accept_requests", []).append(copy_json(request))
            items_by_id = {item.get("id"): item
                           for item in review.get("items") or []
                           if isinstance(item, dict)}
            ready = {}
            failed = {}
            for suggestion in report_suggestions(report):
                item_id = suggestion.get("item_id")
                status = (suggestion.get("validator") or {}).get("status")
                if status == "ready":
                    ready.setdefault(item_id, suggestion)
                elif status == "failed_check":
                    failed.setdefault(item_id, suggestion)
            reasons = []
            resolved = []
            for raw_id in item_ids:
                item_id = str(raw_id)
                item = items_by_id.get(item_id)
                if item is None:
                    reasons.append({"item_id": item_id, "code": "item_missing",
                                    "message": "review item not found"})
                    continue
                match = next((candidate for candidate in entries
                              if candidate.get("item_id") == item_id), None)
                if match is not None and match.get("locked"):
                    reasons.append({"item_id": item_id, "code": "locked",
                                    "message": "entry is locked"})
                    continue
                item_entry = item.get("entry") or {}
                stale_reasons = (item_entry.get("stale_reasons")
                                 or item.get("stale_reasons") or [])
                if item.get("stale") or item_entry.get("stale"):
                    reasons.append({
                        "item_id": item_id, "code": "stale",
                        "message": "item is stale: " + ", ".join(
                            str(code) for code in stale_reasons)})
                    continue
                suggestion = ready.get(item_id)
                if suggestion is None:
                    if item_id in failed:
                        failed_reasons = ((failed[item_id].get("validator") or {})
                                          .get("reasons") or [])
                        reasons.append({
                            "item_id": item_id, "code": "validator_failed",
                            "message": "ready suggestion failed validation: "
                                       + "; ".join(str(reason)
                                                   for reason in failed_reasons)})
                    else:
                        reasons.append({
                            "item_id": item_id, "code": "no_ready_suggestion",
                            "message": "report has no ready suggestion for "
                                       "this item"})
                    continue
                resolved.append((item_id, suggestion))
            if reasons:
                return fail(
                    "plan accept failed: %%d item(s) are not acceptable"
                    %% len(reasons), 2, "validation",
                    {"item_ids": [str(raw) for raw in item_ids],
                     "reasons": reasons})
            accepted = []
            for item_id, suggestion in resolved:
                strategy = suggestion.get("strategy") or {}
                strategy_id = str(strategy.get("id") or "")
                created_action = strategy_action(
                    strategy_id, suggestion.get("params") or {})
                if created_action is None:
                    return fail("unknown strategy: " + strategy_id,
                                2, "validation")
                entry_id = "e-" + hashlib.sha256(
                    item_id.encode("utf-8")).hexdigest()[:16]
                match = next((candidate for candidate in entries
                              if candidate.get("entry_id") == entry_id), None)
                replaced = match is not None
                revision = 1
                if match is not None:
                    revision = int(match.get("revision") or 0) + 1
                    entries = [candidate for candidate in entries
                               if candidate.get("entry_id") != entry_id]
                match = {
                    "entry_id": entry_id,
                    "item_id": item_id,
                    "status": "draft",
                    "stale": False,
                    "stale_reasons": [],
                    "locked": False,
                    "strategy": {"id": strategy_id,
                                 "version": int(strategy.get("version") or 1)},
                    "action": created_action,
                    "evidence": {"hash": _item_evidence_hash(review, item_id),
                                 "refs": []},
                    "dependencies": [],
                    "capability_snapshot": {"native_lines": True,
                                            "ocr": None, "vlm": None},
                    "source": suggestion.get("source") or "rule",
                    "revision": revision,
                    "batch_id": None,
                    "notes": None,
                }
                # Confirm immediately, like ``plan select`` + ``plan confirm``:
                # a resolution becomes ``applied`` (R0 written now); an operation
                # becomes ``confirmed`` and waits for a batch.
                if created_action.get("type") == "resolution":
                    resolution = resolution_record(
                        item_id, created_action.get("name"),
                        entry_id=entry_id, revision=revision,
                        evidence_hash=match["evidence"]["hash"])
                    state.setdefault("resolutions", []).append(resolution)
                    match["status"] = "applied"
                else:
                    match["status"] = "confirmed"
                entries.append(match)
                accepted.append({
                    "item_id": item_id,
                    "entry_id": entry_id,
                    "strategy": match["strategy"],
                    "action": created_action,
                    "status": match["status"],
                    "revision": revision,
                    "replaced_existing": replaced,
                })
            # Accept reports applied R0 rows through ``accepted``; the top-level
            # ``resolution`` stays null (wire §2.6).
            resolution = None
        elif action == "clear":
            # Wire §2.6: delete one entry; locked/batch-bound entries are a
            # conflict and an applied R0 entry writes a ``revert`` resolution.
            entry_id = option(rest, "--entry")
            base_raw = option(rest, "--base-plan-version")
            try:
                base = int(base_raw)
            except (TypeError, ValueError):
                return fail("--base-plan-version is required", 2, "validation")
            if base != plan.get("plan_version"):
                return fail("base_plan_version mismatch", 2, "conflict")
            state.setdefault("clear_requests", []).append(
                {"entry_id": entry_id, "base_plan_version": base})
            match = next((candidate for candidate in entries
                          if candidate.get("entry_id") == entry_id), None)
            if match is None:
                return fail("no such plan entry", 3, "not_found")
            if match.get("locked"):
                return fail("entry " + str(entry_id) + " is locked", 2,
                            "conflict")
            if match.get("batch_id"):
                return fail("entry " + str(entry_id) + " is in a batch", 2,
                            "conflict")
            status_before = match.get("status")
            reverted = None
            created_action = match.get("action") or {}
            if (status_before == "applied"
                    and created_action.get("type") == "resolution"):
                target = None
                for candidate in reversed(state.get("resolutions") or []):
                    if (candidate.get("change") != "revert"
                            and candidate.get("item_id") == match.get("item_id")
                            and candidate.get("name")
                            == created_action.get("name")):
                        target = candidate
                        break
                if target is not None:
                    reverted = revert_resolution_record(target)
                    state.setdefault("resolutions", []).append(reverted)
            entries = [candidate for candidate in entries
                       if candidate.get("entry_id") != entry_id]
            cleared = {"entry_id": entry_id,
                       "item_id": match.get("item_id"),
                       "status_before": status_before,
                       "reverted_resolution": reverted}
        else:
            entry_id = option(rest, "--entry")
            base_raw = option(rest, "--base-plan-version")
            try:
                base = int(base_raw)
            except (TypeError, ValueError):
                return fail("--base-plan-version is required", 2, "validation")
            if base != plan.get("plan_version"):
                return fail("base_plan_version mismatch", 2, "conflict")
            match = next((candidate for candidate in entries
                          if candidate.get("entry_id") == entry_id), None)
            if match is None:
                return fail("no such plan entry", 3, "not_found")
            if action == "confirm":
                # Wire §2.7: a stale entry whose only reasons are
                # evidence_changed/parent_changed is re-confirmed with a refresh
                # (revision +1 only when the content changed; hash-only keeps
                # the revision). Any other stale reason is a conflict.
                if match.get("stale"):
                    codes = list(match.get("stale_reasons") or [])
                    refreshable = bool(codes) and all(
                        code in ("evidence_changed", "parent_changed")
                        for code in codes)
                    if not refreshable:
                        return fail("entry is stale", 2, "conflict")
                    refresh = refresh_stale_entry(match, review)
                # R0 entries apply on confirm (contract v1.3 §11.1): write the
                # resolution and move straight to ``applied``. Only operation
                # entries become ``confirmed`` and wait for an ApplyBatch.
                created_action = match.get("action") or {}
                if created_action.get("type") == "resolution":
                    resolution = resolution_record(
                        match.get("item_id"), created_action.get("name"),
                        entry_id=match.get("entry_id"),
                        revision=match.get("revision"),
                        evidence_hash=(match.get("evidence") or {}).get("hash"))
                    state.setdefault("resolutions", []).append(resolution)
                    match["status"] = "applied"
                else:
                    match["status"] = "confirmed"
            elif action == "lock":
                match["locked"] = True
            else:
                match["locked"] = False
            entry = match
        plan["entries"] = entries
        plan["entry"] = entry
        plan["resolution"] = resolution
        plan["plan_version"] = int(plan.get("plan_version") or 0) + 1
        plan["updated_at"] = "2026-11-05T00:00:00Z"
        state["plan"] = plan
        response = copy_json(plan)
        # Additive CP2 response objects live only in the response (wire §2.6/§2.7).
        response["refresh"] = refresh
        if accepted is not None:
            response["accepted"] = accepted
        if cleared is not None:
            response["cleared"] = cleared
        save_state(state)
        return emit(response)

    if command == "review-report":
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        request = {}
        if option(rest, "--input") is not None:
            request = read_stdin_json() or {}
            state.setdefault("review_report_requests", []).append(request)
            save_state(state)
        payload = copy_json(state.get("review_report") or DEFAULT_REPORT)
        payload["peel_id"] = peel_id
        return emit(payload)

    if command == "rebuild":
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3)
        if state.get("rebuild_fail"):
            return fail("simulated rebuild failure")
        peels[peel_id]["state"] = "rebuilding"
        save_state(state)
        time.sleep(float(state.get("rebuild_seconds") or 0))
        peels[peel_id]["state"] = "ready"
        peels[peel_id]["error"] = None
        save_state(state)
        return emit({"ok": True, "peel": peels[peel_id],
                     "normalize": {"cached": False, "counts": {},
                                   "reasons": []},
                     "gates": {"pass": True, "errors": []}})

    if command == "event":
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3)
        event = option(rest, "--event")
        if event not in ("embed", "feedback"):
            return fail("event must be embed|feedback")
        raw = option(rest, "--data")
        state.setdefault("events", []).append({
            "peel_id": peel_id,
            "event": event,
            "data": json.loads(raw) if raw else {},
        })
        save_state(state)
        return emit({"ok": True, "peel_id": peel_id,
                     "event": {"event": event}})

    if command == "archive":
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3)
        peels[peel_id]["state"] = "archived"
        save_state(state)
        return emit({"ok": True, "peel_id": peel_id, "state": "archived",
                     "note": "peel archived (source kept)"})

    return fail("unknown command: " + command)


if __name__ == "__main__":
    sys.exit(main())
'''


class FakePeelfeedCli:
    """A fake ``peelfeed`` executable for tests (no daemon, no HTTP).

    Writes a small Python script to a temp directory, records every invocation
    (argv + the ``PEELFEED_SHARED_DIR`` it was given) to a JSON Lines log, and
    answers from a JSON state file the test controls. The script can simulate slow
    ``capabilities``, a failing ``ingest``, gate errors and rebuild timing.
    """

    def __init__(self, root, state=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "fake-peelfeed-state.json"
        self.log_path = self.root / "fake-peelfeed-calls.jsonl"
        self.path = self.root / "fake-peelfeed"
        self.write(state or {})

    def write(self, state):
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.log_path.write_text("", encoding="utf-8")
        script = _FAKE_CLI_SCRIPT % {
            "python": sys.executable,
            "state": str(self.state_path),
            "log": str(self.log_path),
        }
        self.path.write_text(script, encoding="utf-8")
        self.path.chmod(0o755)
        return self

    def update(self, **values):
        state = self.state()
        state.update(values)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        return state

    def state(self):
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def calls(self):
        calls = []
        try:
            text = self.log_path.read_text(encoding="utf-8")
        except OSError:
            return calls
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                calls.append(json.loads(line))
            except ValueError:
                continue
        return calls

    def clear_calls(self):
        self.log_path.write_text("", encoding="utf-8")

    def commands(self):
        return [call["argv"][0] for call in self.calls() if call.get("argv")]
