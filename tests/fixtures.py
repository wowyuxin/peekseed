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
              conversion_salt="1", chunks=None, chunk_sets=(EMBED_CHUNK,),
              layout="build"):
    """Create a fake peelfeed peel for one dataset source and mark it extracted.

    ``dataset`` is a dataset metadata dict (``dir``) or a dataset id. By default
    the peel uses the CP3 build layout (wire-contract §2.1)::

        extract/corpus -> conversions/<build_id>/     relative symlink
        extract/conversions/<build_id>/manifest.json  conversion identity
        extract/conversions/<build_id>/build_summary.json

    ``layout="legacy"`` writes a real ``extract/corpus`` directory instead, which
    exercises the pre-cutover fallback. The peel is written under
    ``config.paths().peels_dir`` with the files
    :func:`peekseed.ingest.build_index` consumes: ``manifest.json``,
    ``chunks.jsonl``, ``blocks.jsonl``, ``assets.jsonl``, ``validation.json``,
    ``review_queue.json`` and a copy of the source under ``source/``. The dataset
    source entry is updated to ``status="extracted"`` with the peel identity.

    Returns ``{"peel_id", "peel_dir", "conversion_id", "build_id", "build_dir"}``.
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
    extract = peel_dir / "extract"
    conversion_id = hashlib.sha256(
        ("conv|%s|%s|%s" % (sha, profile, conversion_salt)).encode()
    ).hexdigest()
    build_id = "b20261108T120000-" + hashlib.sha256(
        ("build|%s|%s" % (conversion_id, layout)).encode()
    ).hexdigest()[:8]
    if layout == "legacy":
        corpus = extract / "corpus"
        if corpus.is_symlink():
            corpus.unlink()
        build_dir = corpus
        build_id = "cutover-" + conversion_id
    else:
        build_dir = extract / "conversions" / build_id
        corpus = build_dir
    (peel_dir / "source").mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    if layout != "legacy":
        link = extract / "corpus"
        if link.is_symlink():
            link.unlink()
        elif link.is_dir():
            shutil.rmtree(str(link))
        extract.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.join("conversions", build_id), str(link))
    shutil.copyfile(str(source_file), str(peel_dir / "source" / name))

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
    empty_hash = hashlib.sha256(b"[]").hexdigest()
    manifest = {
        "schema_version": 2,
        "conversion_id": conversion_id,
        "effective_corrections_hash": empty_hash,
        "correction_engine_version": "corr-test0000000000",
        "uncorrected_identity": conversion_id,
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
    build_summary = {
        "schema_version": 1,
        "peel_id": peel_id,
        "build_id": build_id,
        "build_kind": "cutover" if layout == "legacy" else "system",
        "batch_id": None,
        "conversion_id": conversion_id,
        "corrections": {"effective_corrections_hash": empty_hash,
                        "correction_engine_version": "corr-test0000000000"},
        "gates": {"all_pass": True, "checks": 1, "errors": []},
        "diff": {"assets_changed": 0, "blocks_changed": 0, "chunks_changed": 0,
                 "new_warnings": [], "changed_assets": [], "samples": [],
                 # CP3.1 F3: a cutover has no current build yet, so the first
                 # index must be built from scratch; system builds of an
                 # existing corpus need no reindex.
                 "reindex": ({"required": True, "reason": "no_current_build",
                              "chunks": len(chunks)}
                             if layout == "legacy" else
                             {"required": False, "reason": None, "chunks": 0})},
        "counts": {"pages": len(pages), "chunks": len(chunks),
                   "review_items": 0},
        "validation_errors": len(validation_errors or []),
        "engine": {"name": "peelfeed", "version": "0.1.0",
                   "fingerprint": engine},
        "created_utc": contracts.utc_now(),
    }
    contracts.write_json_atomic(corpus / "manifest.json", manifest)
    contracts.write_json_atomic(corpus / "build_summary.json", build_summary)
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
        "corrections_conflict": None,
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
            "conversion_id": conversion_id, "build_id": build_id,
            "build_dir": str(build_dir)}


def write_peel_build(peel, *, conversion_id=None, build_id=None,
                     chunk_sets=None, engine=None):
    """Move a fake peel's current pointer to a (possibly new) build dir.

    Simulates a peelfeed publish / system rebuild on the peel filesystem only:
    the new build directory copies the current corpus, optionally records a new
    ``conversion_id`` and/or adds chunk sets, and ``extract/corpus`` is replaced
    with the relative symlink. ``dataset.json`` is deliberately **not** touched,
    so a reconcile test can prove the shared-dataset refresh updates it.

    A chunk-set-only change keeps ``conversion_id`` (new ``build_id``, same
    content identity), which must leave existing indexes valid.
    """
    from peekseed import contracts

    peel_dir = Path(peel["peel_dir"])
    extract = peel_dir / "extract"
    link = extract / "corpus"
    current_dir = link.resolve()
    manifest = json.loads((current_dir / "manifest.json").read_text())
    previous_conversion = str(manifest.get("conversion_id") or "")
    conversion_id = conversion_id or previous_conversion
    if build_id is None:
        build_id = "b20261108T120000-" + hashlib.sha256(
            ("build|%s|%s" % (conversion_id, chunk_sets)).encode()
        ).hexdigest()[:8]
    build_dir = extract / "conversions" / build_id
    if build_dir.resolve() != current_dir:
        build_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(current_dir), str(build_dir), dirs_exist_ok=True)
    manifest = dict(manifest, conversion_id=conversion_id)
    contracts.write_json_atomic(build_dir / "manifest.json", manifest)
    validation_path = build_dir / "validation.json"
    if validation_path.is_file():
        validation = json.loads(validation_path.read_text())
        validation["conversion_id"] = conversion_id
        contracts.write_json_atomic(validation_path, validation)
    summary_path = build_dir / "build_summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text())
        summary.update({"build_id": build_id, "conversion_id": conversion_id})
        if engine:
            summary.setdefault("engine", {})["fingerprint"] = engine
        # CP3.1 F3: keep the fixture's build summary faithful to the additive
        # ``diff.reindex`` shape when the conversion identity changes.
        diff = summary.get("diff") if isinstance(summary.get("diff"), dict) else {}
        required = conversion_id != previous_conversion
        chunk_count = 0
        chunks_path = build_dir / "chunks.jsonl"
        if chunks_path.is_file():
            chunk_count = sum(1 for line in
                              chunks_path.read_text(encoding="utf-8").splitlines()
                              if line.strip())
        diff["reindex"] = {
            "required": required,
            "reason": (("conversion_changed" if previous_conversion
                        else "no_current_build") if required else None),
            "chunks": chunk_count if required else 0,
        }
        summary["diff"] = diff
        contracts.write_json_atomic(summary_path, summary)
    if chunk_sets:
        chunks = [json.loads(line) for line in
                  (build_dir / "chunks.jsonl").read_text().splitlines()
                  if line.strip()]
        write_chunk_sets(build_dir, chunks, chunk_sets, conversion_id)
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        shutil.rmtree(str(link))
    extract.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.join("conversions", build_id), str(link))
    return {"peel_id": peel["peel_id"], "build_id": build_id,
            "build_dir": str(build_dir), "conversion_id": conversion_id}


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
    "operations": ["inherit_header_from", "set_header_rows",
                   "set_asset_labels_from_ocr"],
    # M2.1 OCR capabilities (wire §2.8): env-only config resolved by peelfeed.
    # The fake default is unconfigured; tests override ``capabilities.ocr`` in
    # the state to advertise a configured provider/engine.
    "ocr": {"configured": False, "provider": None, "engine": None,
            "timeout_seconds": None, "min_confidence": None, "params": {}},
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

# M2.1 ``ocr-check`` payload (wire §2.8): a successful provider probe.
DEFAULT_OCR_CHECK = {
    "schema_version": 1,
    "available": True,
    "engine": "rapidocr@1.4.4",
    "provider": {
        "configured": True,
        "command_display": "/tmp/ocr-venv/bin/python "
                           "/tmp/peelfeed/scripts/ocr_provider_rapidocr.py",
        "timeout_seconds": 30.0,
        "min_confidence": 0.6,
        "params": {},
        "params_sha256": "0123456789abcdef",
    },
    "probe": {"image_sha256": "ab" * 32, "labels": 0, "seconds": 0.85},
    "checked_at": "2026-11-09T03:10:00Z",
    "error": None,
}

# The schema-2 review/plan payloads are module-level fixtures (one source
# of truth shared by the tests and this fake CLI, injected via %%r below).
DEFAULT_REVIEW = %(default_review)r
DEFAULT_PLAN = %(default_plan)r

DEFAULT_REPORT = %(default_report)r


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
                      reason_code=None, reason=None, evidence_hash=None,
                      source="user"):
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
        "source": source,
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


# CP6.1 (contract v1.10 §17.7 / wire §7.2): resolutions are projected to one
# effective row per item (last active add row wins); a row is reverted by a
# later ``revert`` row that ``supersedes`` it. The fake CLI mirrors
# ``peelfeed/review/resolutions.py`` so ``plan propagate`` can mark ``decided``
# members and revert their active R0 in the same write.
RESOLUTION_EVIDENCE_BEARING = ("defer", "keep_visual_fallback",
                               "keep_original_figure", "confirm_headerless")


def active_resolutions(rows):
    """The effective R0 resolution per item (mirror of ``projection``)."""
    active = {}
    for sequence, record in enumerate(rows or []):
        if not isinstance(record, dict):
            continue
        target = record.get("supersedes")
        if target:
            active.pop(target, None)
        if record.get("change") == "revert":
            continue
        rid = record.get("resolution_id")
        if rid:
            active[rid] = (sequence, record)
    latest = {}
    for rid, (sequence, record) in active.items():
        item_id = str(record.get("item_id") or "")
        current = latest.get(item_id)
        if current is None or sequence > current[0]:
            latest[item_id] = (sequence, record)
    return {item_id: record for item_id, (_sequence, record) in latest.items()}


def resolution_is_stale(record, item):
    """Derived stale flag (contract §11.6): evidence-bearing rows only."""
    if not isinstance(record, dict) or not isinstance(item, dict):
        return False
    if record.get("name") not in RESOLUTION_EVIDENCE_BEARING:
        return False
    recorded = record.get("evidence_hash")
    current = (item.get("evidence") or {}).get("hash")
    return bool(recorded and current and recorded != current)


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


def suggestion_source(suggestion):
    """The adoption source of one report suggestion (rule is the default)."""
    suggestion = suggestion if isinstance(suggestion, dict) else {}
    return "vlm" if str(suggestion.get("source") or "") == "vlm" else "rule"


def suggestion_action_identity(suggestion):
    """The wire §10.5 action identity: strategy id/version + canonical params."""
    suggestion = suggestion if isinstance(suggestion, dict) else {}
    strategy = suggestion.get("strategy") or {}
    try:
        params = json.dumps(suggestion.get("params") or {}, sort_keys=True)
    except (TypeError, ValueError):
        params = repr(suggestion.get("params"))
    return (str(strategy.get("id") or ""),
            int(strategy.get("version") or 1), params)


def report_origin(report_payload):
    """The plan-entry ``origin`` for a report accept (wire §10.5)."""
    payload = report_payload if isinstance(report_payload, dict) else {}
    inner = payload.get("report")
    report = inner if isinstance(inner, dict) else payload
    provider = report.get("provider") if isinstance(
        report.get("provider"), dict) else {}
    return {"kind": "report", "job_id": report.get("job_id"),
            "model": provider.get("model"),
            "prompt_version": provider.get("prompt_version")}


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


def corpus_operation(action):
    """The normalized CP3 §2.1 ``corpus_operation`` for one plan action."""
    action = action if isinstance(action, dict) else {}
    name = str(action.get("name") or "")
    params = dict(action.get("params") or {})
    if name == "set_header_rows":
        rows = sorted({int(value) for value in params.get("rows") or []}) or [0]
        cells = ["Header A", "Header B"]
        semantic = {"rows": rows}
        refs = [{"segment": 0, "source_row": row, "cells": list(cells),
                 "evidence_hash": "ev-fake"} for row in rows]
    elif name == "inherit_header_from":
        rows = sorted({int(value) for value in params.get("parent_header_rows")
                       or []}) or [0]
        cells = params.get("parent_header_cells") or [["Header A", "Header B"]]
        semantic = {
            "parent_asset_id": params.get("parent_asset_id") or "table-parent",
            "parent_header_rows": rows,
            "parent_header_cells": copy_json(cells),
            "parent_header_source": params.get("parent_header_source") or "native",
        }
        refs = params.get("parent_source_refs") or [{
            "segment": 0, "source_row": rows[0],
            "cells": copy_json(cells[0] if cells else ["Header A", "Header B"]),
            "evidence_hash": "ev-fake"}]
    else:
        return None
    return {"schema_version": 1, "name": name, "semantic": semantic,
            "source_refs": copy_json(refs)}


def new_job(job_type, *, batch_id=None, result_ref=None, status="done",
            error=None, nonce=""):
    """A deterministic CP3 §2.5 persistent job record (terminal immediately).

    The fake CLI makes jobs terminal right away (the CP2 flake hardening: a
    poller never has to wait for a fake process). Tests that need queued/running/
    failed/interrupted records set ``state["jobs"]`` directly.
    """
    job_id = str(job_type) + "-20261108T120000-" + hashlib.sha256(
        (str(job_type) + "|" + str(batch_id) + "|" + str(result_ref)
         + "|" + str(nonce)).encode()
    ).hexdigest()[:4]
    now = "2026-11-08T12:00:00Z"
    return {
        "schema_version": 1,
        "job_id": job_id,
        "type": str(job_type),
        "status": str(status),
        "progress": {"stage": "done" if status == "done" else status,
                     "done": 1 if status == "done" else 0,
                     "total": 1},
        "result_ref": result_ref,
        "error": error,
        "pid": None,
        "process_started_at": None,
        "created_at": now,
        "started_at": now,
        "updated_at": now,
        "log_ref": "jobs/" + job_id + ".log",
        "batch_id": batch_id,
    }


def default_batch(peel_id, entry_ids, entries, state):
    """A deterministic ready_to_publish ApplyBatch record (CP3 wire §2.3)."""
    empty_hash = hashlib.sha256(b"[]").hexdigest()
    changes = []
    for entry_id in entry_ids:
        entry = entries.get(str(entry_id)) or {}
        action = entry.get("action") or {}
        operation = corpus_operation(action)
        changes.append({
            "change": "add",
            "entry_id": str(entry_id),
            "item_id": entry.get("item_id"),
            "operation": operation,
            "supersedes": None,
        })
    batch_id = "batch-20261108T120000-" + hashlib.sha256(
        "|".join(str(item) for item in entry_ids).encode()).hexdigest()[:4]
    conversion_id = state.get("batch_conversion_id") or (
        hashlib.sha256(("batch|" + batch_id).encode()).hexdigest())
    build_id = "b20261108T120000-" + hashlib.sha256(
        ("build|" + batch_id).encode()).hexdigest()[:8]
    # CP3.1 F3 (wire §2.2): the additive ``diff.reindex`` object. ``required``
    # is true iff the candidate conversion differs from the current one (or
    # there is no current build yet); ``chunks`` is the full candidate chunk
    # count (the re-embed scope), not the changed-chunk count.
    peel = (state.get("peels") or {}).get(peel_id) or {}
    override_current = state.get("publish_current")
    if isinstance(override_current, dict):
        current_conversion = str(override_current.get("conversion_id") or "")
    else:
        current_conversion = str(peel.get("conversion_id") or "")
    reindex = {"required": False, "reason": None, "chunks": 0}
    if conversion_id != current_conversion:
        reindex = {
            "required": True,
            "reason": ("conversion_changed" if current_conversion
                       else "no_current_build"),
            "chunks": int(state.get("reindex_chunks")
                          or (peel.get("counts") or {}).get("chunks")
                          or len(changes)),
        }
    override_reindex = state.get("batch_reindex")
    if isinstance(override_reindex, dict):
        reindex = copy_json(override_reindex)
    base = {
        "conversion_id": conversion_id,
        "effective_corrections_hash": state.get("effective_corrections_hash")
        or empty_hash,
        "entry_revisions": {str(entry_id): int(
            (entries.get(str(entry_id)) or {}).get("revision") or 1)
            for entry_id in entry_ids},
        "evidence_hashes": {str(entry_id): (
            (entries.get(str(entry_id)) or {}).get("evidence") or {}).get("hash")
            for entry_id in entry_ids},
        "engine_fingerprint": state.get("engine_fingerprint") or "test-fingerprint",
        "profile": "generic",
        "chunk_sets": ["c8000-12000-i3"],
        "correction_engine_version": "corr-test0000000000",
    }
    projected = hashlib.sha256(json.dumps(
        changes, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return {
        "schema_version": 1,
        "batch_id": batch_id,
        "status": "ready_to_publish",
        "created_at": "2026-11-08T12:00:00Z",
        "updated_at": "2026-11-08T12:00:00Z",
        "changes": changes,
        "base": base,
        "projected_effective_corrections_hash": projected,
        "plan_level": {"tier": "R2", "reasons": ["corpus_operation"]},
        "estimate": {"items": len(changes), "assets": len(changes),
                     "pages": [], "docling_rerun": False, "seconds": 120,
                     "reindex": None},
        "build": {
            "build_id": build_id,
            "candidate_conversion_id": conversion_id,
            "gates": {"all_pass": True, "checks": 12, "errors": []},
            "diff": {"assets_changed": len(changes), "blocks_changed": 0,
                     "chunks_changed": len(changes), "new_warnings": [],
                     "changed_assets": [change["item_id"] for change in changes],
                     "samples": [], "reindex": reindex},
            "carried_corrections": 0,
        },
        "publish": {"committed_at": None, "current_build_id": None,
                    "error": None},
        "failures": [],
    }


def batch_current(batch, state):
    """The ``current`` block a publish/discard response carries (wire §2.6)."""
    override = state.get("publish_current")
    if isinstance(override, dict):
        return copy_json(override)
    build = batch.get("build") or {}
    return {
        "build_id": build.get("build_id") or "",
        "conversion_id": build.get("candidate_conversion_id") or "",
        "effective_corrections_hash": (
            batch.get("projected_effective_corrections_hash") or ""),
        "published": True,
    }


def option(rest, name):
    for index, item in enumerate(rest):
        if item == name and index + 1 < len(rest):
            return rest[index + 1]
    return None


# CP3.1 F5 (contract §14.7): peelfeed validates batch_id/job_id against the
# generated shape before joining any path; anything else is
# ``error [validation]``. The fake CLI mirrors that check so peekseed's
# validation->400 mapping is exercised without a real peelfeed.
BATCH_ID_PREFIXES = ("batch", "system")
JOB_ID_PREFIXES = ("report", "build", "rollback", "ocr", "discuss")


def _generated_id_shape(value, prefixes):
    text = str(value or "")
    if not text or text != text.strip():
        return False
    if "/" in text or "\\" in text or ".." in text:
        return False
    for prefix in prefixes:
        if text.startswith(prefix + "-") and len(text) > len(prefix) + 1:
            return True
    return False


def valid_batch_id(value):
    text = str(value or "")
    if not _generated_id_shape(text, BATCH_ID_PREFIXES):
        return False
    # System rebuild batches are ``system-<build_id>`` (build ids start with b).
    if text.startswith("system-"):
        return text.startswith("system-b") and len(text) > len("system-b")
    return True


def valid_job_id(value):
    return _generated_id_shape(value, JOB_ID_PREFIXES)


VALUE_FLAGS = {
    "--root", "--item", "--decision", "--note", "--evidence", "--model",
    "--confidence", "--event", "--data", "--reason", "--profile",
    "--chunk-profile", "--llm", "--limit", "--pages", "--slug",
    "--collection", "--device", "--group", "--status", "--entry",
    "--base-plan-version", "--input", "--batch", "--job",
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
            # M2.1: peekseed forwards every PEELFEED_* env var (the OCR config
            # is env-only), so the fake records them for the seam tests.
            "ocr_env": {key: value for key, value in sorted(os.environ.items())
                        if key.startswith("PEELFEED_OCR_")},
            # CP5: the VLM provider env is child-only (report/discuss). The fake
            # records it so tests can prove the token reaches exactly those
            # children and never a rules report/plan/job call.
            "llm_env": {key: value for key, value in sorted(os.environ.items())
                        if key.startswith("PEELFEED_LLM_")},
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
        delay = float(state.get("ingest_seconds") or 0)
        # CP5.1 H9: simulate a re-ingest of an already-ready peel. The real
        # engine keeps the previous peel summary visible until the new ingest
        # publishes, so a status poll during the delay still reports the old
        # ``ready`` state (the bug H9 fixes). Without this flag the fake writes
        # an intermediate ``converting`` state immediately (CP3 flake hardening).
        keep_previous = bool(state.get("ingest_keep_previous_state"))
        if not (keep_previous and peel_id in peels):
            peels[peel_id] = {
                "peel_id": peel_id,
                # CP3 flake hardening: with no simulated delay the terminal state
                # is observable immediately (no intermediate "converting" write
                # for a 0.05s poller to catch and persist late).
                "state": "converting" if delay else (
                    "failed" if errors else "ready"),
                "source": {"name": os.path.basename(source),
                           "size": os.path.getsize(source),
                           "sha256": sha, "pages": pages},
                "profile": state.get("ingest_profile") or "generic",
                "conversion_id": "" if delay else hashlib.sha256(
                    ("conv|" + sha).encode()).hexdigest(),
                "counts": {"pages": pages, "chunks": pages},
                "validation": {"errors": [] if delay else errors,
                               "review_items": review_items},
                "review_items": review_items,
                "error": None if delay or not errors else "validation failed",
            }
            state["peels"] = peels
            save_state(state)
        if delay:
            time.sleep(delay)
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
        if action == "patch":
            # CP5 wire §5: synchronous targeted plan patch (no model call).
            # stdin {base_plan_version, patch, source:"vlm|user",
            # conversation_id?}; returns the new plan + targeted diff.
            if option(rest, "--input") is None:
                save_state(state)
                return fail("--input is required for plan patch", 2,
                            "validation")
            request = read_stdin_json()
            if not isinstance(request, dict):
                save_state(state)
                return fail("plan patch input must be a JSON object", 2,
                            "validation")
            state.setdefault("plan_patch_requests", []).append(
                copy_json(request))
            base = request.get("base_plan_version")
            if isinstance(base, bool) or not isinstance(base, int) or base < 0:
                save_state(state)
                return fail("base_plan_version must be an integer >= 0", 2,
                            "validation")
            patch = request.get("patch")
            if not isinstance(patch, dict):
                save_state(state)
                return fail("patch must be a JSON object", 2, "validation")
            source = request.get("source")
            if source not in ("vlm", "user"):
                save_state(state)
                return fail("source must be 'vlm' or 'user'", 2, "validation")
            conversation_id = request.get("conversation_id")
            turn = request.get("turn")
            conversations = state.get("conversations") or {}
            if conversation_id:
                if conversation_id not in conversations:
                    save_state(state)
                    return fail("unknown conversation: "
                                + str(conversation_id), 3, "not_found")
            # CP5.1 H5 (wire §10.5): a VLM-sourced patch must name the archived
            # assistant turn; the fake compares the submitted patch with that
            # turn (identical -> source vlm, edited -> source user) and records
            # the conversation origin either way.
            origin = None
            effective_source = source
            if source == "vlm":
                if not conversation_id:
                    save_state(state)
                    return fail("source 'vlm' requires conversation_id",
                                2, "validation")
                if (isinstance(turn, bool) or not isinstance(turn, int)
                        or turn < 1):
                    save_state(state)
                    return fail("source 'vlm' requires turn (int >= 1)",
                                2, "validation")
                archived = next(
                    (candidate for candidate in
                     conversations[conversation_id].get("assistant_turns") or []
                     if candidate.get("turn") == turn), None)
                if archived is None or not archived.get("patch"):
                    save_state(state)
                    return fail("no archived assistant patch for turn "
                                + str(turn), 2, "validation")
                effective_source = ("vlm" if archived.get("patch") == patch
                                    else "user")
                origin = {"kind": "conversation",
                          "conversation_id": conversation_id,
                          "turn": turn,
                          "model": archived.get("model"),
                          "prompt_version": archived.get("prompt_version")}
            if base != plan.get("plan_version"):
                save_state(state)
                return fail("base_plan_version mismatch", 2, "conflict")
            error = state.get("plan_patch_error")
            if error:
                save_state(state)
                return fail(error.get("message") or "plan patch rejected",
                            int(error.get("exit") or 2),
                            error.get("code") or "validation",
                            error.get("detail"))
            review = copy_json(state.get("review") or DEFAULT_REVIEW)
            item_cluster = {}
            all_item_ids = []
            for item in review.get("items") or []:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or "")
                if not item_id:
                    continue
                all_item_ids.append(item_id)
                item_cluster[item_id] = str(item.get("cluster_id") or "")
            scope = patch.get("scope") if isinstance(
                patch.get("scope"), dict) else {}
            kind = str(scope.get("kind") or "")
            if kind == "item":
                members = [str(scope.get("item_id") or "")]
            elif kind == "cluster":
                cluster_id = str(scope.get("cluster_id") or "")
                members = [item_id for item_id in all_item_ids
                           if item_cluster.get(item_id) == cluster_id]
            elif kind == "peel":
                members = list(all_item_ids)
            else:
                save_state(state)
                return fail("patch.scope.kind must be item, cluster or peel",
                            2, "validation")
            members = [item_id for item_id in members if item_id]
            exclusions = [str(item_id) for item_id in
                          (patch.get("exclusions") or [])]
            target = sorted(set(members) - set(exclusions))
            declared = patch.get("members")
            if declared is not None and sorted(
                    str(item_id) for item_id in declared) != target:
                save_state(state)
                return fail("patch.members must equal the resolved target set",
                            2, "validation")
            changes = patch.get("changes")
            if not isinstance(changes, list):
                save_state(state)
                return fail("patch.changes must be a list", 2, "validation")
            by_item = {}
            for change in changes:
                if not isinstance(change, dict):
                    save_state(state)
                    return fail("patch change must be an object", 2,
                                "validation")
                item_id = str(change.get("item_id") or "")
                if not item_id or item_id not in target:
                    save_state(state)
                    return fail("patch change item is not in the target set: "
                                + str(item_id), 2, "validation")
                if item_id in by_item:
                    save_state(state)
                    return fail("duplicate patch change for " + item_id, 2,
                                "validation")
                op = change.get("op")
                if op not in ("select", "remove"):
                    save_state(state)
                    return fail("unknown patch op: " + str(op), 2,
                                "validation")
                by_item[item_id] = change
            missing = [item_id for item_id in target
                       if item_id not in by_item]
            if missing:
                save_state(state)
                return fail("patch is missing a change for " + missing[0], 2,
                            "validation")
            existing = {entry.get("entry_id"): entry
                        for entry in plan.get("entries") or []
                        if isinstance(entry, dict)}
            entries = [copy_json(entry) for entry in
                       (plan.get("entries") or [])]
            conflicts = []
            for item_id, change in by_item.items():
                entry_id = "e-" + hashlib.sha256(
                    item_id.encode("utf-8")).hexdigest()[:16]
                current = next((entry for entry in entries
                                if entry.get("entry_id") == entry_id), None)
                if current is not None and (current.get("locked")
                                            or current.get("batch_id")):
                    conflicts.append(item_id)
            if conflicts:
                save_state(state)
                return fail("patch touches locked/batch-bound entries", 2,
                            "conflict", {"conflicts": sorted(conflicts)})
            added = []
            changed = []
            removed = []
            unchanged = 0
            bumped = False
            for item_id, change in by_item.items():
                entry_id = "e-" + hashlib.sha256(
                    item_id.encode("utf-8")).hexdigest()[:16]
                current = next((entry for entry in entries
                                if entry.get("entry_id") == entry_id), None)
                if change.get("op") == "remove":
                    if current is None:
                        unchanged += 1
                        continue
                    entries = [entry for entry in entries
                               if entry.get("entry_id") != entry_id]
                    removed.append(copy_json(current))
                    bumped = True
                    continue
                strategy = change.get("strategy") or {}
                strategy_id = str(strategy.get("id") or "")
                action_entry = strategy_action(
                    strategy_id, change.get("params") or {})
                if action_entry is None:
                    save_state(state)
                    return fail("unknown strategy: " + strategy_id, 2,
                                "validation")
                params = dict(change.get("params") or {})
                if (current is not None
                        and current.get("strategy", {}).get("id") == strategy_id
                        and (current.get("action") or {}).get("params")
                        == params):
                    # No-op select: keep the entry and do not bump the version.
                    unchanged += 1
                    continue
                revision = 1
                if current is not None:
                    revision = int(current.get("revision") or 0) + 1
                after = {
                    "entry_id": entry_id,
                    "item_id": item_id,
                    "status": "draft",
                    "stale": False,
                    "stale_reasons": [],
                    "locked": False,
                    "strategy": {"id": strategy_id,
                                 "version": int(strategy.get("version") or 1)},
                    "action": action_entry,
                    "evidence": {"hash": "9c1f2e3d4b5a69788796a5b4c3d2e1f0",
                                 "refs": []},
                    "dependencies": [],
                    "capability_snapshot": {"native_lines": True,
                                            "ocr": None, "vlm": None},
                    "source": effective_source,
                    "origin": copy_json(origin),
                    "revision": revision,
                    "batch_id": None,
                    "notes": change.get("notes"),
                }
                entries = [entry for entry in entries
                           if entry.get("entry_id") != entry_id]
                entries.append(after)
                if current is None:
                    added.append(copy_json(after))
                else:
                    changed.append({"item_id": item_id,
                                    "before": copy_json(current),
                                    "after": copy_json(after)})
                bumped = True
            entries.sort(key=lambda entry: str(entry.get("entry_id") or ""))
            if bumped:
                plan["plan_version"] = int(plan.get("plan_version") or 0) + 1
                plan["updated_at"] = "2026-11-09T03:10:00Z"
            plan["entries"] = entries
            state["plan"] = plan
            response = {
                "peel_id": peel_id,
                "review_schema": 2,
                "plan_version": int(plan.get("plan_version") or 0),
                "entries": entries,
                "diff": {"added": added, "changed": changed,
                         "removed": removed, "unchanged": unchanged},
            }
            save_state(state)
            return emit(response)
        if action == "propagate":
            # CP6 wire §1: synchronous cluster-level propagation. Preview
            # (dry_run) returns members/positives/boundary/exclusions and writes
            # nothing; apply writes draft entries + the propagation audit
            # record, or fails conflict/validation all-or-nothing.
            if option(rest, "--input") is None:
                save_state(state)
                return fail("--input is required for plan propagate", 2,
                            "validation")
            request = read_stdin_json()
            if not isinstance(request, dict):
                save_state(state)
                return fail("plan propagate input must be a JSON object", 2,
                            "validation")
            state.setdefault("propagate_requests", []).append(
                copy_json(request))
            error = state.get("propagate_error")
            if error:
                save_state(state)
                return fail(error.get("message") or "plan propagate failed",
                            int(error.get("exit") or 2),
                            error.get("code") or "validation",
                            error.get("detail"))
            base = request.get("base_plan_version")
            if isinstance(base, bool) or not isinstance(base, int) or base < 0:
                save_state(state)
                return fail("base_plan_version must be an integer >= 0", 2,
                            "validation")
            if base != plan.get("plan_version"):
                save_state(state)
                return fail("base_plan_version mismatch", 2, "conflict")
            scope = request.get("scope")
            if not isinstance(scope, dict):
                save_state(state)
                return fail("scope is required", 2, "validation")
            strategy = request.get("strategy")
            if not isinstance(strategy, dict):
                save_state(state)
                return fail("strategy is required", 2, "validation")
            strategy_id = str(strategy.get("id") or "")
            strategy_version = int(strategy.get("version") or 1)
            params = request.get("params") if isinstance(
                request.get("params"), dict) else {}
            exclusions = [str(item) for item in request.get("exclusions") or []]
            # CP6.1 (wire §7.2): optional overrides of ``decided`` members.
            overrides = []
            raw_overrides = request.get("overrides")
            if raw_overrides is None:
                raw_overrides = []
            if not isinstance(raw_overrides, list):
                save_state(state)
                return fail("overrides must be a list of item ids", 2,
                            "validation")
            for raw in raw_overrides:
                if not isinstance(raw, str) or not raw.strip():
                    save_state(state)
                    return fail("overrides must contain non-empty item ids", 2,
                                "validation")
                item_id = raw.strip()
                if item_id not in overrides:
                    overrides.append(item_id)
            dry_run = bool(request.get("dry_run"))
            operator = str(request.get("operator") or "user")
            review = copy_json(state.get("review") or DEFAULT_REVIEW)
            items = [item for item in review.get("items") or []
                     if isinstance(item, dict) and item.get("id")]
            by_id = {str(item.get("id")): item for item in items}
            cluster_key = {}
            for cluster in review.get("clusters") or []:
                if isinstance(cluster, dict):
                    cluster_key[str(cluster.get("cluster_id") or "")] = \
                        cluster.get("cluster_key")
            kind = str(scope.get("kind") or "")
            if kind == "item":
                item_id = str(scope.get("item_id") or "")
                if item_id not in by_id:
                    save_state(state)
                    return fail("scope item is not in this peel", 2,
                                "validation")
                members = [item_id]
            elif kind == "cluster":
                cluster_id = str(scope.get("cluster_id") or "")
                members = [str(item.get("id")) for item in items
                           if str(item.get("cluster_id") or "") == cluster_id]
                if not members:
                    save_state(state)
                    return fail("scope cluster is not in this peel", 2,
                                "validation")
            elif kind == "peel":
                members = [str(item.get("id")) for item in items]
            else:
                save_state(state)
                return fail("scope.kind must be item, cluster or peel", 2,
                            "validation")

            def member_priority(item_id):
                item = by_id.get(item_id) or {}
                status = str(item.get("status") or "")
                human = 0 if status in ("needs_decision", "pending_evidence",
                                        "pending_report", "failed_check") else 1
                return (human, item_id)

            members = sorted(members, key=member_priority)
            source_item_id = request.get("source_item_id")
            if source_item_id is not None:
                source_item_id = str(source_item_id)
                if source_item_id not in members:
                    save_state(state)
                    return fail("source_item_id is not in the resolved "
                                "member set", 2, "validation")
            else:
                source_item_id = members[0] if members else None
            # CP6.1 (wire §7.2): overrides must be members and must not overlap
            # exclusions; peelfeed validates all-or-nothing (400, nothing
            # written). The fake mirrors that so peekseed's passthrough mapping
            # is exercised without a real peelfeed.
            outside = [item_id for item_id in overrides
                       if item_id not in members]
            if outside:
                save_state(state)
                return fail("overrides must be a subset of the resolved member "
                            "set; not in scope: " + ", ".join(outside),
                            2, "validation")
            overlap = [item_id for item_id in overrides
                       if item_id in exclusions]
            if overlap:
                save_state(state)
                return fail("overrides must not overlap exclusions: "
                            + ", ".join(overlap), 2, "validation")
            entries = [copy_json(entry) for entry in plan.get("entries") or []]
            entries_by_item = {str(entry.get("item_id")): entry
                               for entry in entries if isinstance(entry, dict)}
            not_applicable = {str(item) for item in
                              state.get("propagate_not_applicable") or []}
            failed_map = state.get("propagate_failed") or {}
            active_by_item = active_resolutions(
                state.get("resolutions") or [])
            resolved = []
            locked_conflicts = []
            failed_members = []
            overridden_members = []
            for item_id in members:
                if item_id in exclusions:
                    resolved.append({"item_id": item_id, "status": "excluded",
                                     "reasons": [], "action": None})
                    continue
                existing = entries_by_item.get(item_id)
                if existing is not None and existing.get("locked"):
                    resolved.append({"item_id": item_id, "status": "locked",
                                     "reasons": [], "action": None})
                    locked_conflicts.append(item_id)
                    continue
                if existing is not None and existing.get("batch_id"):
                    resolved.append({"item_id": item_id, "status": "in_batch",
                                     "reasons": [], "action": None})
                    locked_conflicts.append(item_id)
                    continue
                action = strategy_action(strategy_id, params)
                failed_reasons = failed_map.get(item_id)
                # CP6.1 §7.2: a pre-existing decision is ``decided`` unless the
                # member is in ``overrides``. Detection happens after
                # locked/in_batch and before not_applicable/failed_check:
                #   * a confirmed entry whose rebuilt candidate differs or
                #     cannot be rebuilt;
                #   * an applied R0 entry;
                #   * an active projected R0 resolution other than
                #     ``confirm_known`` (a derived stale row still counts).
                decided_existing = None
                if existing is not None:
                    entry_status = str(existing.get("status") or "")
                    entry_strategy = existing.get("strategy") or {}
                    entry_action = existing.get("action") or {}
                    if entry_status in ("confirmed", "applied"):
                        same = (
                            entry_status == "confirmed"
                            and action is not None
                            and not failed_reasons
                            and entry_strategy.get("id") == strategy_id
                            and int(entry_strategy.get("version") or 0)
                            == strategy_version
                            and entry_action.get("params") == action.get("params"))
                        if entry_status == "applied" or not same:
                            decided_existing = {
                                "kind": "entry",
                                "status": entry_status,
                                "strategy": {
                                    "id": str(entry_strategy.get("id") or ""),
                                    "version": int(
                                        entry_strategy.get("version") or 1)},
                                "name": str(entry_action.get("name") or ""),
                            }
                if decided_existing is None:
                    active = active_by_item.get(item_id)
                    if (active is not None
                            and active.get("name") != "confirm_known"):
                        decided_existing = {
                            "kind": "resolution",
                            "status": "stale" if resolution_is_stale(
                                active, by_id.get(item_id) or {})
                                else "active",
                            "strategy": None,
                            "name": str(active.get("name") or ""),
                        }
                if decided_existing is not None and item_id not in overrides:
                    resolved.append({"item_id": item_id, "status": "decided",
                                     "reasons": [], "action": None,
                                     "existing": decided_existing})
                    continue
                if item_id in not_applicable or action is None:
                    resolved.append({"item_id": item_id,
                                     "status": "not_applicable",
                                     "reasons": ["strategy_not_applicable"],
                                     "action": None})
                    failed_members.append(item_id)
                    continue
                if failed_reasons:
                    resolved.append({"item_id": item_id,
                                     "status": "failed_check",
                                     "reasons": [str(reason) for reason in
                                                 failed_reasons],
                                     "action": None})
                    failed_members.append(item_id)
                    continue
                if decided_existing is not None:
                    # An overridden ``decided`` member is always written as
                    # ``ready`` (even when the rebuilt action equals the old
                    # one) so that the override is explicit (wire §7.2).
                    resolved.append({"item_id": item_id, "status": "ready",
                                     "reasons": [], "action": action,
                                     "existing": decided_existing})
                    overridden_members.append(item_id)
                    continue
                if (existing is not None
                        and (existing.get("strategy") or {}).get("id")
                        == strategy_id
                        and (existing.get("action") or {}).get("params")
                        == action.get("params")):
                    resolved.append({"item_id": item_id, "status": "already",
                                     "reasons": [], "action": action})
                    continue
                resolved.append({"item_id": item_id, "status": "ready",
                                 "reasons": [], "action": action})
            representative = by_id.get(source_item_id) or {}
            rep_key = cluster_key.get(
                str(representative.get("cluster_id") or ""))
            positives = []
            boundary_cases = []
            for row in resolved:
                if row["status"] != "ready":
                    continue
                item = by_id.get(row["item_id"]) or {}
                key = cluster_key.get(str(item.get("cluster_id") or ""))
                if key == rep_key:
                    positives.append(row["item_id"])
                else:
                    boundary_cases.append(row["item_id"])
            counts = {
                "members": len(resolved),
                "ready": sum(1 for row in resolved
                             if row["status"] == "ready"),
                "already": sum(1 for row in resolved
                               if row["status"] == "already"),
                "decided": sum(1 for row in resolved
                               if row["status"] == "decided"),
                "failed": sum(1 for row in resolved
                              if row["status"] in ("failed_check",
                                                   "not_applicable")),
                "locked": sum(1 for row in resolved
                              if row["status"] == "locked"),
                "excluded": sum(1 for row in resolved
                                if row["status"] == "excluded"),
            }
            response = {
                "peel_id": peel_id,
                "review_schema": 2,
                "dry_run": dry_run,
                "plan_version": int(plan.get("plan_version") or 0),
                "scope": copy_json(scope),
                "strategy": copy_json(strategy),
                "members": resolved,
                "positives": positives,
                "boundary_cases": boundary_cases,
                "exclusions": exclusions,
                "overrides": overrides,
                "counts": counts,
            }
            if dry_run:
                save_state(state)
                return emit(response)
            if locked_conflicts:
                save_state(state)
                return fail("propagate touches locked/batch-bound members", 2,
                            "conflict",
                            {"conflicts": sorted(locked_conflicts)})
            if failed_members:
                save_state(state)
                return fail("propagate has non-excluded failing members", 2,
                            "validation",
                            {"reasons": [
                                {"item_id": row["item_id"],
                                 "code": row["status"],
                                 "reasons": row["reasons"]}
                                for row in resolved
                                if row["item_id"] in failed_members]})
            kept = [row["item_id"] for row in resolved
                    if row["status"] == "decided"]
            overridden = list(overridden_members)
            # §7.2: ``selection`` stays "all non-excluded member ids" (kept
            # members included).
            selection = [row["item_id"] for row in resolved
                         if row["status"] != "excluded"]
            propagation_id = "prop-20261110T120000-" + hashlib.sha256(
                json.dumps([base, scope, strategy, selection, overrides],
                           sort_keys=True).encode()).hexdigest()[:4]
            added = []
            changed = []
            unchanged = 0
            bumped = False
            new_entries = [copy_json(entry) for entry in entries]
            resolutions_state = state.setdefault("resolutions", [])
            for row in resolved:
                item_id = row["item_id"]
                if row["status"] == "already":
                    unchanged += 1
                    continue
                if row["status"] != "ready":
                    # ``decided`` members not in ``overrides`` are kept: no
                    # entry written, no resolution reverted (wire §7.2).
                    continue
                entry_id = "e-" + hashlib.sha256(
                    item_id.encode("utf-8")).hexdigest()[:16]
                current = next((entry for entry in new_entries
                                if entry.get("entry_id") == entry_id), None)
                revision = 1
                if current is not None:
                    revision = int(current.get("revision") or 0) + 1
                    new_entries = [entry for entry in new_entries
                                   if entry.get("entry_id") != entry_id]
                after = {
                    "entry_id": entry_id,
                    "item_id": item_id,
                    "status": "draft",
                    "stale": False,
                    "stale_reasons": [],
                    "locked": False,
                    "strategy": {"id": strategy_id,
                                 "version": strategy_version},
                    "action": copy_json(row["action"]),
                    "evidence": {"hash": (by_id[item_id].get("evidence")
                                          or {}).get("hash"), "refs": []},
                    "dependencies": [],
                    "capability_snapshot": {"native_lines": True,
                                            "ocr": None, "vlm": None},
                    "source": "user",
                    "origin": {"kind": "propagation",
                               "propagation_id": propagation_id,
                               "scope": copy_json(scope),
                               "strategy": {"id": strategy_id,
                                            "version": strategy_version},
                               "base_plan_version": base},
                    "revision": revision,
                    "batch_id": None,
                    "notes": None,
                }
                new_entries.append(after)
                if current is None:
                    added.append(copy_json(after))
                else:
                    changed.append({"item_id": item_id,
                                    "before": copy_json(current),
                                    "after": copy_json(after)})
                bumped = True
                # CP6.1 §7.1: writing a new entry for an item reverts that
                # item's active R0 resolution in the same review lock (every
                # active name, including ``confirm_known``). Kept ``decided``
                # members never reach this branch.
                active = active_by_item.get(item_id)
                if active is not None:
                    reverted = revert_resolution_record(active)
                    resolutions_state.append(reverted)
            new_entries.sort(key=lambda entry: str(entry.get("entry_id") or ""))
            plan_version_after = int(plan.get("plan_version") or 0)
            if bumped:
                plan_version_after += 1
                plan["plan_version"] = plan_version_after
                plan["updated_at"] = "2026-11-10T12:00:00Z"
            plan["entries"] = new_entries
            state["plan"] = plan
            diff = {"added": added, "changed": changed, "removed": [],
                    "unchanged": unchanged}
            record = {
                "schema_version": 1,
                "propagation_id": propagation_id,
                "peel_id": peel_id,
                "created_at": "2026-11-10T12:00:00Z",
                "operator": operator,
                "scope": copy_json(scope),
                "strategy": copy_json(strategy),
                "params": copy_json(params),
                "base_plan_version": base,
                "exclusions": exclusions,
                "overrides": overrides,
                "selection": selection,
                "kept": kept,
                "overridden": overridden,
                "members": copy_json(resolved),
                "positives": positives,
                "boundary_cases": boundary_cases,
                "changed": bool(bumped),
                "plan_version_after": plan_version_after,
                "diff": diff,
            }
            state.setdefault("propagations", {})[propagation_id] = record
            response.update({
                "propagation_id": propagation_id,
                "plan_version": plan_version_after,
                "diff": diff,
                "changed": bool(bumped),
                # CP6.1 §7.5 lists kept/overridden among the passed-through
                # response fields; the authoritative audit record also carries
                # them (§7.2). The fake is additive here (the UI ignores them).
                "kept": kept,
                "overridden": overridden,
            })
            save_state(state)
            return emit(response)
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
            # CP5.1 H5 (wire §10.5): optional per-item adoption sources. Keys
            # must be requested item ids, values rule|vlm.
            sources = request.get("sources")
            requested = [str(raw) for raw in item_ids]
            if sources is not None:
                if not isinstance(sources, dict):
                    return fail("sources must be an object", 2, "validation")
                for raw_key, raw_value in sources.items():
                    if str(raw_key) not in requested:
                        return fail("sources key is not a requested item id: "
                                    + str(raw_key), 2, "validation")
                    if raw_value not in ("rule", "vlm"):
                        return fail("sources value must be 'rule' or 'vlm'",
                                    2, "validation")
            items_by_id = {item.get("id"): item
                           for item in review.get("items") or []
                           if isinstance(item, dict)}
            ready = {}
            failed = {}
            for suggestion in report_suggestions(report):
                item_id = suggestion.get("item_id")
                status = (suggestion.get("validator") or {}).get("status")
                if status == "ready":
                    ready.setdefault(item_id, []).append(suggestion)
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
                suggestions = ready.get(item_id) or []
                suggestion = None
                if sources is not None and item_id in sources:
                    wanted = sources[item_id]
                    suggestion = next(
                        (candidate for candidate in suggestions
                         if suggestion_source(candidate) == wanted), None)
                    if suggestion is None:
                        reasons.append({
                            "item_id": item_id, "code": "source_missing",
                            "message": "no ready %%s suggestion for this item"
                                       %% wanted})
                        continue
                elif len(suggestions) == 1:
                    suggestion = suggestions[0]
                elif len(suggestions) > 1:
                    identities = {suggestion_action_identity(candidate)
                                  for candidate in suggestions}
                    if len(identities) == 1:
                        # Same action identity: adopt the rule one (rule
                        # preferred, wire §10.5).
                        suggestion = next(
                            (candidate for candidate in suggestions
                             if suggestion_source(candidate) == "rule"),
                            suggestions[0])
                    else:
                        reasons.append({
                            "item_id": item_id,
                            "code": "ambiguous_suggestion",
                            "message": "several ready suggestions with "
                                       "different actions; specify "
                                       "sources[item_id]"})
                        continue
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
            accept_origin = report_origin(report)
            for item_id, suggestion in resolved:
                strategy = suggestion.get("strategy") or {}
                strategy_id = str(strategy.get("id") or "")
                created_action = strategy_action(
                    strategy_id, suggestion.get("params") or {})
                if created_action is None:
                    return fail("unknown strategy: " + strategy_id,
                                2, "validation")
                adopted_source = suggestion_source(suggestion)
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
                    "source": adopted_source,
                    "origin": copy_json(accept_origin),
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
                    "source": adopted_source,
                    "origin": copy_json(accept_origin),
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
        with_vlm = "--with-vlm" in rest
        request = {}
        if option(rest, "--input") is not None:
            request = read_stdin_json() or {}
            if with_vlm:
                state.setdefault("review_vlm_requests", []).append(
                    copy_json(request))
            else:
                state.setdefault("review_report_requests", []).append(request)
        if with_vlm:
            # CP5 wire §2.1: ``--with-vlm`` requires the frozen VLM request
            # {mode:"vlm", confirmation:{…}}; the real peelfeed re-checks the
            # confirmation against its env and the resolved scope.
            if not isinstance(request, dict) or request.get("mode") != "vlm":
                save_state(state)
                return fail("--with-vlm needs {mode:'vlm', confirmation:{…}}",
                            2, "validation")
            confirmation = request.get("confirmation")
            if (not isinstance(confirmation, dict)
                    or confirmation.get("confirmed") is not True):
                save_state(state)
                return fail("privacy confirmation is required",
                            2, "privacy_confirmation_required")
            error = state.get("review_vlm_error")
            if error:
                save_state(state)
                return fail(error.get("message") or "VLM report failed",
                            int(error.get("exit") or 2),
                            error.get("code") or "vlm_unavailable",
                            error.get("detail"))
            job = new_job("report", result_ref="report.json",
                          nonce=len(state.get("jobs") or {}))
            state.setdefault("jobs", {})[job["job_id"]] = job
            # The real job replaces report.json with the VLM report; tests can
            # install the post-job review payload to assert the UI render.
            if state.get("review_after_vlm_report"):
                state["review"] = copy_json(state["review_after_vlm_report"])
            save_state(state)
            return emit({"job_id": job["job_id"], "status": "queued",
                         "result_ref": None})
        # CP3: a persistent ``report`` job; the fake job is terminal immediately
        # (the poller never waits for a fake process) and the report payload is
        # still available in ``state["review_report"]`` for assertions.
        job = new_job("report", result_ref="report.json",
                      nonce=len(state.get("jobs") or {}))
        state.setdefault("jobs", {})[job["job_id"]] = job
        save_state(state)
        return emit({"job_id": job["job_id"], "status": "queued",
                     "result_ref": None})

    if command == "discuss":
        # CP5 wire §2.1/§4: one persistent ``discuss`` job; stdin is the frozen
        # {conversation_id?, scope, message, base_plan_version, confirmation}.
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        if option(rest, "--input") is None:
            return fail("--input is required for discuss", 2, "validation")
        request = read_stdin_json()
        if not isinstance(request, dict):
            save_state(state)
            return fail("discuss input must be a JSON object", 2, "validation")
        state.setdefault("discuss_requests", []).append(copy_json(request))
        confirmation = request.get("confirmation")
        if (not isinstance(confirmation, dict)
                or confirmation.get("confirmed") is not True):
            save_state(state)
            return fail("privacy confirmation is required",
                        2, "privacy_confirmation_required")
        scope = request.get("scope")
        if not isinstance(scope, dict):
            save_state(state)
            return fail("scope is required", 2, "validation")
        selectors = 0
        for key in ("item_id", "cluster_id"):
            if scope.get(key) is not None:
                selectors += 1
        if scope.get("peel") is not None:
            if scope.get("peel") is not True:
                save_state(state)
                return fail("scope.peel must be true", 2, "validation")
            selectors += 1
        if selectors != 1:
            save_state(state)
            return fail("scope needs exactly one selector", 2, "validation")
        message = request.get("message")
        if not isinstance(message, str) or not message.strip():
            save_state(state)
            return fail("message must be non-empty", 2, "validation")
        if len(message.strip()) > 4000:
            save_state(state)
            return fail("message is longer than 4000 characters", 2,
                        "validation")
        base = request.get("base_plan_version")
        if isinstance(base, bool) or not isinstance(base, int) or base < 0:
            save_state(state)
            return fail("base_plan_version must be an integer >= 0", 2,
                        "validation")
        error = state.get("discuss_error")
        if error:
            save_state(state)
            return fail(error.get("message") or "discussion failed",
                        int(error.get("exit") or 2),
                        error.get("code") or "vlm_unavailable",
                        error.get("detail"))
        conversations = state.setdefault("conversations", {})
        conversation_id = request.get("conversation_id")
        if conversation_id:
            if conversation_id not in conversations:
                save_state(state)
                return fail("unknown conversation: " + str(conversation_id),
                            3, "not_found")
        else:
            conversation_id = "conv-20261109T031000-" + hashlib.sha256(
                ("conv|" + peel_id + "|" + str(len(conversations))).encode()
            ).hexdigest()[:4]
            conversations[conversation_id] = {
                "schema_version": 1, "type": "conversation",
                "conversation_id": conversation_id, "peel_id": peel_id,
                "scope": copy_json(scope), "base_plan_version": base}
        job = new_job("discuss",
                      result_ref="conversations/" + conversation_id + ".jsonl",
                      nonce=len(state.get("jobs") or {}))
        state.setdefault("jobs", {})[job["job_id"]] = job
        # Additive assistant-turn result summary (wire §4.3) served through the
        # peel asset route (GET …/assets/extract/review/jobs/<id>.result.json).
        # CP5.1 H5: the assistant turn number is archived so ``plan patch`` with
        # source:"vlm" can compare the submitted patch with it (identical -> vlm,
        # edited -> user) and record the conversation origin.
        conversation = conversations[conversation_id]
        assistant_turns = conversation.setdefault("assistant_turns", [])
        turn_number = 2 + 2 * len(assistant_turns)
        result = {
            "schema_version": 1, "job_id": job["job_id"], "peel_id": peel_id,
            "build_id": "b20261109T031000-abcdef12",
            "conversation_id": conversation_id,
            "turn": turn_number, "role": "assistant", "at": "2026-11-09T03:10:00Z",
            "intent": "explain", "reply": "按当前报告，该簇的规则建议是可靠的。",
            "uncertainties": [], "clarifications": [],
            "patch": None,
            "model": os.environ.get("PEELFEED_LLM_MODEL") or "fake-model",
            "link_id": os.environ.get("PEELFEED_LLM_LINK_ID") or "env",
            "prompt_version": "peel-reviews@1", "vision": False,
            "cached": False,
            "usage": {"input_tokens": 10, "output_tokens": 20,
                      "cost_usd": "0.00"},
            "evidence_hashes": {}, "base_plan_version": base,
            "diff": None,
        }
        override = state.get("discuss_result")
        if isinstance(override, dict):
            result.update(copy_json(override))
        assistant_turns.append({
            "turn": int(result.get("turn") or turn_number),
            "role": "assistant",
            "patch": copy_json(result.get("patch")),
            "model": result.get("model"),
            "prompt_version": result.get("prompt_version"),
        })
        conversation["turns"] = int(conversation.get("turns") or 0) + 1
        state.setdefault("discuss_job_results", {})[job["job_id"]] = result
        if state.get("review_after_discuss"):
            state["review"] = copy_json(state["review_after_discuss"])
        save_state(state)
        return emit({"job_id": job["job_id"],
                     "conversation_id": conversation_id, "status": "queued"})

    if command == "ocr-check":
        # M2.1 wire §2.8: short provider probe; optional peel id persists the
        # status file. Never writes the corpus, never creates a job.
        peel_id = first_positional(rest)
        if peel_id is not None and peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        if option(rest, "--input") is None:
            return fail("--input is required for ocr-check", 2, "validation")
        request = read_stdin_json()
        if not isinstance(request, dict):
            save_state(state)
            return fail("ocr-check input must be a JSON object", 2,
                        "ocr_config_invalid")
        state.setdefault("ocr_check_requests", []).append(copy_json(request))
        provider = request.get("provider")
        if provider is not None and not isinstance(provider, dict):
            save_state(state)
            return fail("provider must be an object", 2, "ocr_config_invalid")
        error = state.get("ocr_check_error")
        if error:
            save_state(state)
            return fail(error.get("message") or "OCR provider unavailable",
                        int(error.get("exit") or 2),
                        error.get("code") or "ocr_unavailable",
                        error.get("detail"))
        payload = copy_json(DEFAULT_OCR_CHECK)
        payload.update(copy_json(state.get("ocr_check") or {}))
        payload["checked_at"] = "2026-11-09T03:10:00Z"
        payload["error"] = None
        state["ocr_status"] = copy_json(payload)
        save_state(state)
        return emit(payload)

    if command == "ocr-refresh":
        # M2.1 wire §2.8: creates one persistent ``type=ocr`` job. The fake job
        # is terminal immediately by default (``ocr_job_status`` can simulate
        # queued/running/failed/interrupted records); the result summary is kept
        # in ``state["ocr_job_results"]`` for assertions.
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        if option(rest, "--input") is None:
            return fail("--input is required for ocr-refresh", 2, "validation")
        request = read_stdin_json()
        if not isinstance(request, dict):
            save_state(state)
            return fail("ocr-refresh input must be a JSON object", 2,
                        "validation")
        state.setdefault("ocr_refresh_requests", []).append(copy_json(request))
        item_ids = request.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            save_state(state)
            return fail("item_ids must be a non-empty list of item ids", 2,
                        "validation")
        cleaned = []
        seen = set()
        for raw in item_ids:
            if not isinstance(raw, str) or not raw.strip():
                save_state(state)
                return fail("item_ids must contain only non-empty strings", 2,
                            "validation")
            item_id = raw.strip()
            if item_id in seen:
                save_state(state)
                return fail("item_ids must be unique (duplicate "
                            + item_id + ")", 2, "validation")
            seen.add(item_id)
            cleaned.append(item_id)
        provider = request.get("provider")
        if provider is not None and not isinstance(provider, dict):
            save_state(state)
            return fail("provider must be an object", 2, "ocr_config_invalid")
        if state.get("ocr_not_ready"):
            save_state(state)
            return fail("no current build; run a build first", 2, "not_ready")
        error = state.get("ocr_refresh_error")
        if error:
            save_state(state)
            return fail(error.get("message") or "OCR refresh failed",
                        int(error.get("exit") or 2),
                        error.get("code") or "ocr_unavailable",
                        error.get("detail"))
        status = str(state.get("ocr_job_status") or "done")
        if status not in ("queued", "running", "done", "failed",
                          "interrupted"):
            status = "done"
        job = new_job("ocr", result_ref=None,
                      status="running" if status == "interrupted" else status,
                      error=({"code": "interrupted",
                              "message": "the job process is no longer running"}
                             if status == "interrupted" else None),
                      nonce=len(state.get("jobs") or {}))
        if status == "done":
            job["result_ref"] = "jobs/" + job["job_id"] + ".result.json"
        if status == "interrupted":
            # The real ``job show`` derives ``interrupted`` from a dead running
            # job; the fake stores the same marker (contract §8).
            job["interrupted"] = True
        elif status == "failed":
            job["error"] = state.get("ocr_job_error") or {
                "code": "ocr_unavailable",
                "message": "OCR provider unavailable"}
        state.setdefault("jobs", {})[job["job_id"]] = job
        summary = {
            "schema_version": 1,
            "job_id": job["job_id"],
            "peel_id": peel_id,
            "build_id": "b20261108T120000-12345678",
            "engine": "rapidocr@1.4.4",
            "params_sha256": "0123456789abcdef",
            "requested_item_ids": cleaned,
            "items": [],
            "counts": dict(state.get("ocr_job_counts") or {
                "requested": len(cleaned), "refreshed": len(cleaned),
                "cache_hits": 0, "failed": 0, "empty": 0,
                "low_confidence": 0}),
            "created_at": "2026-11-09T03:10:00Z",
        }
        state.setdefault("ocr_job_results", {})[job["job_id"]] = summary
        # The real OCR job re-derives items/report when it finishes; tests can
        # install the post-refresh review payload to assert the coverage update.
        if state.get("ocr_review_after_refresh"):
            state["review"] = copy_json(state["ocr_review_after_refresh"])
        save_state(state)
        return emit({"job_id": job["job_id"], "status": "queued",
                     "result_ref": None})

    if command == "apply":
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        if option(rest, "--input") is None:
            return fail("--input is required for apply", 2, "validation")
        request = read_stdin_json()
        if not isinstance(request, dict):
            return fail("apply input must be a JSON object", 2, "validation")
        for forbidden in ("tier", "scope", "operations"):
            if forbidden in request:
                return fail("apply accepts only entry_ids; " + forbidden
                            + " is chosen by the planner and cannot be passed",
                            2, "validation")
        entry_ids = request.get("entry_ids")
        if not isinstance(entry_ids, list) or not entry_ids:
            return fail("entry_ids must be a non-empty list of entry ids",
                        2, "validation")
        state.setdefault("apply_requests", []).append(copy_json(request))
        plan = copy_json(state.get("plan") or DEFAULT_PLAN)
        entries = {entry.get("entry_id"): entry
                   for entry in plan.get("entries") or []
                   if isinstance(entry, dict)}
        for entry_id in entry_ids:
            entry = entries.get(str(entry_id))
            if entry is None:
                return fail("no such plan entry: " + str(entry_id), 3,
                            "not_found")
            if entry.get("locked") or entry.get("batch_id"):
                return fail("entry " + str(entry_id) + " is locked or already "
                            "in a batch", 2, "conflict")
            if str(entry.get("status") or "") != "confirmed":
                return fail("entry " + str(entry_id) + " is not confirmed",
                            2, "validation")
            if (entry.get("action") or {}).get("type") != "operation":
                return fail("only operation entries enter a batch", 2,
                            "validation")
        batch = default_batch(peel_id, [str(item) for item in entry_ids],
                              entries, state)
        state.setdefault("batches", {})[batch["batch_id"]] = batch
        job = new_job("build", batch_id=batch["batch_id"],
                      result_ref="batches/" + batch["batch_id"] + ".json")
        state.setdefault("jobs", {})[job["job_id"]] = job
        save_state(state)
        return emit({"job_id": job["job_id"], "batch_id": batch["batch_id"],
                     "status": "queued"})

    if command == "batch":
        # Real CLI order is action BEFORE the peel id (wire §2.6).
        values = positionals(rest)
        action = values[0] if values else None
        peel_id = values[1] if len(values) > 1 else ""
        if peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        if action != "show":
            return fail("unknown batch action: " + str(action), 2, "validation")
        batch_id = option(rest, "--batch")
        if not batch_id:
            return fail("--batch is required for batch show", 2, "validation")
        if not valid_batch_id(batch_id):
            return fail("invalid batch id: " + str(batch_id), 2, "validation")
        batch = (state.get("batches") or {}).get(str(batch_id))
        if not isinstance(batch, dict):
            return fail("unknown batch: " + str(batch_id), 3, "not_found")
        return emit(copy_json(batch))

    if command == "publish":
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        batch_id = option(rest, "--batch")
        if not batch_id:
            return fail("--batch is required for publish", 2, "validation")
        if not valid_batch_id(batch_id):
            return fail("invalid batch id: " + str(batch_id), 2, "validation")
        batch = (state.get("batches") or {}).get(str(batch_id))
        if not isinstance(batch, dict):
            return fail("unknown batch: " + str(batch_id), 3, "not_found")
        status = str(batch.get("status") or "")
        if status not in ("ready_to_publish", "publishing"):
            return fail("batch is not ready to publish (status " + status + ")",
                        2, "conflict")
        current = batch_current(batch, state)
        batch = copy_json(batch)
        batch["status"] = "published"
        batch["updated_at"] = "2026-11-08T12:00:05Z"
        batch["publish"] = {"committed_at": "2026-11-08T12:00:05Z",
                            "current_build_id": current.get("build_id"),
                            "error": None}
        state.setdefault("batches", {})[str(batch_id)] = batch
        state.setdefault("published_batches", []).append(str(batch_id))
        save_state(state)
        return emit({"batch": batch, "current": current})

    if command == "discard":
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        batch_id = option(rest, "--batch")
        if not batch_id:
            return fail("--batch is required for discard", 2, "validation")
        if not valid_batch_id(batch_id):
            return fail("invalid batch id: " + str(batch_id), 2, "validation")
        batch = (state.get("batches") or {}).get(str(batch_id))
        if not isinstance(batch, dict):
            return fail("unknown batch: " + str(batch_id), 3, "not_found")
        if str(batch.get("status") or "") in ("published", "publishing"):
            return fail("batch is already published", 2, "conflict")
        batch = copy_json(batch)
        batch["status"] = "discarded"
        batch["updated_at"] = "2026-11-08T12:00:05Z"
        state.setdefault("batches", {})[str(batch_id)] = batch
        current = state.get("publish_current") or {
            "build_id": "", "conversion_id": "",
            "effective_corrections_hash": "", "published": False}
        save_state(state)
        return emit({"batch": batch, "current": copy_json(current)})

    if command == "rollback":
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        state.setdefault("rollback_requests", []).append(
            {"peel_id": peel_id, "reason": option(rest, "--reason")})
        batch_id = "batch-20261108T120000-" + hashlib.sha256(
            ("rollback|" + peel_id + "|" + str(len(
                state.get("rollback_requests") or []))).encode()
        ).hexdigest()[:4]
        batch = {
            "schema_version": 1,
            "batch_id": batch_id,
            "status": "ready_to_publish",
            "created_at": "2026-11-08T12:00:00Z",
            "updated_at": "2026-11-08T12:00:00Z",
            "changes": [{"change": "revert", "entry_id": "e-revert",
                         "item_id": "it-revert", "operation": None,
                         "supersedes": "c-revert"}],
            "base": {"conversion_id": "", "effective_corrections_hash": "",
                     "entry_revisions": {}, "evidence_hashes": {},
                     "engine_fingerprint": "test-fingerprint",
                     "profile": "generic", "chunk_sets": ["c8000-12000-i3"],
                     "correction_engine_version": "corr-test0000000000"},
            "projected_effective_corrections_hash": hashlib.sha256(
                b"[]").hexdigest(),
            "plan_level": {"tier": "R2", "reasons": ["corpus_operation"]},
            "estimate": {"items": 1, "assets": 1, "pages": [],
                         "docling_rerun": False, "seconds": 120,
                         "reindex": None},
            "build": None,
            "publish": {"committed_at": None, "current_build_id": None,
                        "error": None},
            "failures": [],
        }
        state.setdefault("batches", {})[batch_id] = batch
        job = new_job("rollback", batch_id=batch_id,
                      result_ref="batches/" + batch_id + ".json")
        state.setdefault("jobs", {})[job["job_id"]] = job
        save_state(state)
        return emit({"job_id": job["job_id"], "batch_id": batch_id,
                     "status": "queued"})

    if command == "job":
        # Real CLI order is action BEFORE the peel id (wire §2.6).
        values = positionals(rest)
        action = values[0] if values else None
        peel_id = values[1] if len(values) > 1 else ""
        if peel_id not in peels:
            return fail("no such peel", 3, "not_found")
        jobs = state.get("jobs") or {}
        if action == "show":
            job_id = option(rest, "--job")
            if not job_id:
                return fail("--job is required for job show", 2, "validation")
            if not valid_job_id(job_id):
                return fail("invalid job id: " + str(job_id), 2, "validation")
            job = jobs.get(str(job_id))
            if not isinstance(job, dict):
                return fail("unknown job: " + str(job_id), 3, "not_found")
            payload = copy_json(job)
            if payload.get("status") == "running" and payload.get("interrupted"):
                payload["status"] = "interrupted"
                payload["result_ref"] = None
                payload["error"] = {
                    "code": "interrupted",
                    "message": "the job process is no longer running"}
                payload["derived_interrupted"] = True
            return emit(payload)
        if action == "list":
            status_filter = option(rest, "--status")
            rows = [copy_json(job) for job in jobs.values()
                    if not status_filter or job.get("status") == status_filter]
            return emit(rows)
        return fail("unknown job action: " + str(action), 2, "validation")

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



# --- schema-2 review payloads (the shapes the real peelfeed CLI prints) --
# Module-level so tests can import them and the fake CLI receives them via
# %r substitutions (one source of truth).

def copy_json(value):
    return json.loads(json.dumps(value))


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


# --- M2.1 CP4: OCR review payload fixtures (wire §2.8, plan §5.3/§5.5) ------
# ``review.capabilities`` additive OCR keys for a configured / unconfigured
# provider (env-only ``PEELFEED_OCR_*`` resolved by peelfeed), the frozen
# ``payload.ocr`` coverage summary, and the per-item ``evidence.ocr_evidence``
# entries. The additive ``labels`` list is what the Review UI renders inline;
# the evidence file itself keeps the frozen §11.8 shape.

REVIEW_CAPABILITIES_OCR = {
    "review_schema": 2, "native_lines": True,
    "ocr": True, "ocr_configured": True, "ocr_detail": None,
    "ocr_engine": "rapidocr@1.4.4", "ocr_min_confidence": 0.6,
    "ocr_status": {"available": True, "checked_at": "2026-11-09T03:10:00Z",
                   "error": None},
    "vlm": False, "vlm_detail": "VLM 报告未实现（M2.2）",
}
REVIEW_CAPABILITIES_NO_OCR = {
    "review_schema": 2, "native_lines": True,
    "ocr": False, "ocr_configured": False,
    "ocr_detail": "OCR 未配置：设置 PEELFEED_OCR_PROVIDER/ENGINE",
    "ocr_engine": None, "ocr_min_confidence": None,
    "ocr_status": {"available": False, "checked_at": None, "error": None},
    "vlm": False, "vlm_detail": "VLM 报告未实现（M2.2）",
}
OCR_COVERAGE_COVERED = {
    "items": 1, "with_evidence": 1, "empty": 0, "low_confidence": 0,
    "uncovered": 0, "invalid_evidence": 0, "evidence_files": 1,
    "engine": "rapidocr@1.4.4", "min_confidence": 0.6,
}
OCR_COVERAGE_UNCOVERED = dict(OCR_COVERAGE_COVERED, with_evidence=0,
                              uncovered=1, evidence_files=0)
OCR_COVERAGE_LOW = dict(OCR_COVERAGE_COVERED, low_confidence=1)
OCR_COVERAGE_EMPTY = dict(OCR_COVERAGE_COVERED, empty=1)
# The R0 resolution strategies stay selectable when OCR is unavailable.
RESOLUTION_STRATEGY_OPTIONS = [{
    "id": "keep_original_figure", "version": 1, "name": "保留原始图",
    "description": "不改变 corpus，保留原始图与视觉回退。",
    "recommended": True, "available": True, "unavailable_reason": None,
    "capabilities": [], "params_schema": {},
    "action": {"type": "resolution", "name": "keep_original_figure"},
    "min_tier": "R0", "batch_safe": False,
    "risk": "图内文字不会进入检索",
}]


def ocr_strategy_options(*, available=True, reason=None, extra=None):
    """``item.strategies`` with ``use_ocr_labels`` available/grayed with reason."""
    options = copy_json(STRATEGY_OPTIONS)
    for option in options:
        if option["id"] == "use_ocr_labels":
            option["available"] = bool(available)
            option["unavailable_reason"] = reason
            option["recommended"] = bool(available)
    if extra:
        options.extend(copy_json(extra))
    return options


def ocr_evidence(*, labels=None, engine="rapidocr@1.4.4",
                 asset_id="figure-1-1", crop_ref="images/fig_1_1.png",
                 image_sha256="ab" * 32, created_at="2026-11-09T03:10:00Z"):
    """One ``evidence.ocr_evidence`` entry (plan §3.2) incl. display labels."""
    labels = copy_json(labels or [])
    digest = hashlib.sha256(
        json.dumps(labels, sort_keys=True).encode()).hexdigest()
    confidences = [float(label.get("confidence") or 0) for label in labels]
    return {
        "path": "extract/ocr/%s/%s-%s.json"
                % (asset_id, image_sha256[:16], engine),
        "crop_ref": crop_ref,
        "image_sha256": image_sha256,
        "engine": engine,
        "params_sha256": "0123456789abcdef",
        "evidence_sha256": digest,
        "label_count": len(labels),
        "max_confidence": max(confidences) if confidences else None,
        "min_confidence": min(confidences) if confidences else None,
        "created_at": created_at,
        "labels": labels,
    }


OCR_EVIDENCE = ocr_evidence(labels=[
    {"text": "Compute Express Link", "bbox": [4, 6, 210, 30],
     "confidence": 0.99},
    {"text": "CXL 3.1", "bbox": [4, 40, 96, 64], "confidence": 0.96},
    {"text": "Figure 1-1", "bbox": [4, 72, 140, 96], "confidence": 0.94},
])
OCR_EVIDENCE_LOW = ocr_evidence(labels=[
    {"text": "模糊的图注文字", "bbox": [4, 6, 120, 26], "confidence": 0.55},
    {"text": "低置信标签", "bbox": [4, 32, 96, 52], "confidence": 0.42},
])
OCR_EVIDENCE_EMPTY = ocr_evidence(labels=[])


def ocr_figure_item(*, evidence=None, strategies=None, status=None,
                    candidates=None):
    """The ``no_native_labels`` figure item with (or without) OCR evidence."""
    item = copy_json(ITEM_FIGURE)
    if evidence is not None:
        item["evidence"]["ocr_evidence"] = [copy_json(evidence)]
        item["evidence"]["hash"] = hashlib.sha256(json.dumps(
            item["evidence"], sort_keys=True).encode()).hexdigest()
    item["status"] = status or ("needs_decision" if evidence is not None
                                else "pending_evidence")
    item["strategies"] = (strategies if strategies is not None
                          else ocr_strategy_options(available=evidence
                                                    is not None))
    if candidates is not None:
        item["candidates"] = copy_json(candidates)
    elif evidence is not None and (evidence.get("labels") or []):
        item["candidates"] = [{
            "strategy": {"id": "use_ocr_labels", "version": 1},
            "action": {"type": "operation",
                       "name": "set_asset_labels_from_ocr",
                       "params": {"labels": copy_json(evidence["labels"])}},
            "source": "rule",
            "validator": {"status": "ready", "reasons": []},
            "evidence_refs": ["asset:figure-1-1",
                              "ocr:" + evidence["evidence_sha256"]],
        }]
    return item


def ocr_figure_cluster(item):
    """The ``no_native_labels`` cluster matching :func:`ocr_figure_item`."""
    cluster = copy_json(CLUSTER_FIGURE)
    cluster["strategies"] = copy_json(item.get("strategies")
                                      or ocr_strategy_options())
    cluster["item_ids"] = [item["id"]]
    cluster["representative_item_id"] = item["id"]
    cluster["states"] = {item.get("status") or "pending_evidence": 1}
    cluster["suggestions"] = []
    return cluster


def review_ocr_payload(*, capabilities=None, coverage=None, figure_item=None,
                       report=None, items=None, clusters=None):
    """A schema-2 review payload with the M2.1 additive OCR keys.

    Defaults to the covered case (OCR configured, one figure item with one
    evidence file); pass ``coverage``/``figure_item`` for the uncovered,
    low-confidence or empty variants and ``capabilities`` for no-OCR.
    """
    review = copy_json(DEFAULT_REVIEW)
    review["capabilities"] = copy_json(capabilities or REVIEW_CAPABILITIES_OCR)
    review["ocr"] = copy_json(coverage or OCR_COVERAGE_COVERED)
    if figure_item is not None:
        review["items"] = [copy_json(ITEM_HEADER),
                           copy_json(figure_item), copy_json(ITEM_STALE)]
        review["clusters"] = [copy_json(CLUSTER_HEADER),
                              ocr_figure_cluster(figure_item),
                              copy_json(CLUSTER_STALE)]
    if report is not None:
        review["report"] = copy_json(report)
    if items is not None:
        review["items"] = copy_json(items)
    if clusters is not None:
        review["clusters"] = copy_json(clusters)
    return review


REVIEW_OCR = review_ocr_payload(figure_item=ocr_figure_item(
    evidence=OCR_EVIDENCE))
REVIEW_OCR_UNCOVERED = review_ocr_payload(
    coverage=OCR_COVERAGE_UNCOVERED,
    figure_item=ocr_figure_item(
        strategies=ocr_strategy_options(
            available=False, reason="需要先运行局部 OCR")))
REVIEW_OCR_LOW_CONFIDENCE = review_ocr_payload(
    coverage=OCR_COVERAGE_LOW,
    figure_item=ocr_figure_item(
        evidence=OCR_EVIDENCE_LOW, status="needs_decision",
        strategies=ocr_strategy_options(available=True)))
REVIEW_OCR_EMPTY = review_ocr_payload(
    coverage=OCR_COVERAGE_EMPTY,
    figure_item=ocr_figure_item(
        evidence=OCR_EVIDENCE_EMPTY, status="pending_evidence",
        strategies=ocr_strategy_options(
            available=False, reason="OCR 结果为空")))
REVIEW_OCR_UNAVAILABLE = review_ocr_payload(
    capabilities=REVIEW_CAPABILITIES_NO_OCR,
    coverage=dict(OCR_COVERAGE_UNCOVERED, engine=None,
                  min_confidence=None),
    figure_item=ocr_figure_item(
        strategies=ocr_strategy_options(
            available=False, reason="缺少能力：局部 OCR（M2.1）",
            extra=RESOLUTION_STRATEGY_OPTIONS)))





# --- M2.2 CP5: VLM report/discussion fixtures (wire §2.3/§3.5/§4.2/§7) -----
# The answer link peekseed resolves for report/discuss children (wire §1.3).
VLM_LINK_ID = "answer-main"
VLM_MODEL = "qwen2.5-vl-72b"
VLM_LINK_BASE = "http://127.0.0.1:11434/v1"
VLM_LINK_KEY_ENV = "PEEKSEED_ANSWER_API_KEY"
VLM_LINK_KEY = "sk-test-vlm-secret"

# The frozen privacy confirmation (wire §1.2); scope sets are the resolved
# target item/cluster ids, sorted unique.
VLM_CONFIRMATION = {
    "confirmed": True,
    "provider": "answer_link",
    "link_id": VLM_LINK_ID,
    "model": VLM_MODEL,
    "scope": {
        "item_ids": ["it-3f9c2a4b6d8e0f1a"],
        "cluster_ids": ["cl-0f1e2d3c4b5a"],
        "include_crop": True,
        "include_original_page": True,
        "include_adjacent_pages": True,
        "include_native": True,
        "include_ocr": True,
    },
}

# ``review.capabilities`` additive VLM fields (wire §2.3).
REVIEW_CAPABILITIES_VLM = {
    "review_schema": 2, "native_lines": True,
    "ocr": True, "ocr_configured": True, "ocr_detail": None,
    "ocr_engine": "rapidocr@1.4.4", "ocr_min_confidence": 0.6,
    "ocr_status": {"available": True, "checked_at": "2026-11-09T03:10:00Z",
                   "error": None},
    "vlm": True, "vlm_detail": None, "vlm_vision": True,
    "vlm_model": VLM_MODEL, "vlm_link_id": VLM_LINK_ID,
    "vlm_budget_usd": 2.0, "vlm_spent_usd": 0.0, "vlm_calls": 0,
    "vlm_vision_checked_at": "2026-11-09T03:10:00Z",
}

# The additive report object fields (wire §3.5) merged over DEFAULT_REPORT's
# report object: a VLM report with one partial failure and clarifications.
DEFAULT_VLM_REPORT = copy_json(DEFAULT_REPORT["report"])
DEFAULT_VLM_REPORT.update({
    "status": "partial",
    "provider": {"kind": "vlm", "model": VLM_MODEL,
                 "prompt_version": "peel-reviews@1",
                 "link_id": VLM_LINK_ID, "vision": True, "cached": False,
                 "text_only": False},
    "counts": {"suggestions": 2, "auto_archived": 0, "unsupported": 0,
               "needs_decision": 0, "vlm_suggestions": 2,
               "clarifications": 1, "failed_checks": 0},
    "partial_failures": [{"cluster_id": "cl-1a2b3c4d5e6f",
                          "code": "vlm_bad_response",
                          "message": "cluster response was void"}],
    "llm_usage": {"calls": 2, "tokens_input": 1200, "tokens_output": 400,
                  "cost_usd": "0.00"},
})
DEFAULT_VLM_REPORT["clusters"][0]["clarifications"] = [{
    "question": "第 0 行是否是表头？",
    "options": ["是，第 0 行", "是，前两行", "不是表头"],
    "item_id": "it-3f9c2a4b6d8e0f1a",
}]

# A discuss job's assistant-turn patch (wire §4.2): draft only, never applied.
VLM_DISCUSS_PATCH = {
    "scope": {"kind": "cluster", "item_id": None,
              "cluster_id": "cl-0f1e2d3c4b5a"},
    "members": ["it-3f9c2a4b6d8e0f1a"],
    "exclusions": [],
    "boundary_cases": ["it-8b7a6c5d4e3f2a10"],
    "changes": [{
        "op": "select",
        "item_id": "it-3f9c2a4b6d8e0f1a",
        "strategy": {"id": "use_first_row_header", "version": 1},
        "params": {"rows": [0]},
        "evidence_refs": ["asset:table-3-19"],
        "uncertainties": ["row 0 是否为表头"],
    }],
}

# A discuss job's result summary (wire §4.3); the fake CLI merges overrides.
DEFAULT_VLM_DISCUSS = {
    "schema_version": 1,
    "intent": "modify",
    "reply": "按你的说明，建议把第 0 行作为表头，并保留其他条目不变。",
    "uncertainties": ["row 0 与数据行相似"],
    "clarifications": [],
    "patch": VLM_DISCUSS_PATCH,
    "model": VLM_MODEL,
    "link_id": VLM_LINK_ID,
    "prompt_version": "peel-reviews@1",
    "vision": True,
    "cached": False,
    "usage": {"input_tokens": 900, "output_tokens": 200,
              "cost_usd": "0.00"},
    "evidence_hashes": {"it-3f9c2a4b6d8e0f1a": "h"},
    "base_plan_version": 0,
    "diff": None,
}


def review_vlm_payload(*, capabilities=None, report=None, items=None,
                       clusters=None):
    """A schema-2 review payload with the CP5 additive VLM keys."""
    review = copy_json(DEFAULT_REVIEW)
    review["capabilities"] = copy_json(
        capabilities or REVIEW_CAPABILITIES_VLM)
    if report is not None:
        review["report"] = copy_json(report)
    else:
        review["report"] = {
            "status": "ready", "generated_at": "2026-11-09T03:10:00Z",
            "stale": False,
            "provider": copy_json(DEFAULT_VLM_REPORT["provider"]),
            "report": copy_json(DEFAULT_VLM_REPORT),
        }
    if items is not None:
        review["items"] = copy_json(items)
    if clusters is not None:
        review["clusters"] = copy_json(clusters)
    return review


REVIEW_VLM = review_vlm_payload()
REVIEW_VLM_NO_PROVIDER = review_vlm_payload(capabilities=dict(
    REVIEW_CAPABILITIES_VLM, vlm=False,
    vlm_detail="VLM 未配置：添加一个启用的 answer link 并设置其 api_key_env",
    vlm_model=None, vlm_link_id=None, vlm_vision=None))


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
            "default_report": DEFAULT_REPORT,
            "default_review": DEFAULT_REVIEW,
            "default_plan": DEFAULT_PLAN,
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
