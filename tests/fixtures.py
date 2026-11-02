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
    """Point peekseed at a throwaway data root and reset every cached path."""
    from peekseed import app as app_module
    from peekseed import config, links

    previous = os.environ.get("PEEKSEED_DATA")
    root = Path(tempfile.mkdtemp(prefix="peekseed-test-"))
    os.environ["PEEKSEED_DATA"] = str(root)
    config.reset_cache()
    links.reset_cache()
    app_module.reset_state()
    try:
        yield root
    finally:
        app_module.reset_state()
        config.reset_cache()
        links.reset_cache()
        if previous is None:
            os.environ.pop("PEEKSEED_DATA", None)
        else:
            os.environ["PEEKSEED_DATA"] = previous
        shutil.rmtree(str(root), ignore_errors=True)


def make_dataset(name="Docs", pages=None, note=""):
    """Create a dataset with a real PDF, returning its metadata dict."""
    from peekseed import config, ingest

    dataset = ingest.create_dataset(config.paths(), name, note=note)
    directory = Path(dataset["dir"])
    pdf = make_pdf(directory / "original" / "manual.pdf", pages=pages)
    ingest.add_source(dataset, pdf)
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
    written under ``PEELFEED_ROOT`` (legacy ``PEEKSEED_PEELFEED_ROOT`` accepted,
    else ``<data_root>/peels``) with the files
    :func:`peekseed.ingest.build_index` consumes: ``manifest.json``,
    ``chunks.jsonl``, ``blocks.jsonl``, ``assets.jsonl``, ``validation.json``,
    ``review_queue.json`` and a copy of the source under ``source/``. The dataset
    source entry is updated to ``status="extracted"`` with the peel identity.

    Returns ``{"peel_id", "peel_dir", "conversion_id"}``.
    """
    from peekseed import config, contracts

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
    relative = str(entry.get("path") or ("original/" + name))
    candidates = [dataset_dir / relative, dataset_dir / "original" / name]
    # Workspace-scan sources live under documents/, not in the dataset copy.
    try:
        from peekseed import workspace as workspace_module
        candidates.append(Path(config.paths().workspace_root) / "documents"
                          / relative)
        candidates.append(Path(config.paths().documents_dir) / relative)
        candidates.append(workspace_module.resolve().documents_dir / relative)
    except Exception:
        pass
    source_file = next((path for path in candidates if path.is_file()), None)
    if source_file is None:
        raise AssertionError("no stored source file for %s (tried %s)"
                             % (name, ", ".join(str(path) for path in candidates)))
    if peel_root is None:
        peel_root = Path(os.environ.get("PEELFEED_ROOT")
                         or os.environ.get("PEEKSEED_PEELFEED_ROOT")
                         or (config.paths().root / "peels"))
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
    "formats": ["pdf"],
    "profiles": ["cxl", "ddr5", "dwc", "generic", "pcie", "pipe"],
    "chunk_profiles": ["default", "large", "small"],
    "llm_modes": ["off", "review", "aggressive"],
    "peel_root": "/tmp/fake-peels",
    "docling": {"python": "/tmp/docling-python", "python_present": True,
                "models_present": True},
}

DEFAULT_REVIEW = {
    "peel_id": "peel",
    "review_items": 2,
    "items": [
        {"id": "table-p0001-01/header", "message": "no header cells",
         "asset": {"id": "table-p0001-01", "kind": "table",
                   "caption": "Table 1-1", "pages": [1],
                   "path": "tables/table-1-1.json", "segments": []},
         "suggestion": None},
        {"id": "figure-1-1/labels", "message": "no native labels",
         "asset": {"id": "figure-1-1", "kind": "figure",
                   "caption": "Figure 1-1", "pages": [2],
                   "path": "images/fig_1_1.png", "segments": []},
         "suggestion": None},
    ],
    "char_limit_exceptions": [],
    "outline_reconciliations": [],
    "asset_stats": {"tables": 1, "figures": 1},
    "corrections": 0,
    "llm": {"available": False, "prompt_set": ""},
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


def fail(message, code=2):
    print("error: " + message, file=sys.stderr)
    return code


def option(rest, name):
    for index, item in enumerate(rest):
        if item == name and index + 1 < len(rest):
            return rest[index + 1]
    return None


VALUE_FLAGS = {
    "--root", "--item", "--decision", "--note", "--evidence", "--model",
    "--confidence", "--event", "--data", "--reason", "--profile",
    "--chunk-profile", "--llm", "--limit", "--pages", "--slug",
    "--collection", "--device",
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
            "peel_root": os.environ.get("PEELFEED_ROOT"),
            "pythonpath": os.environ.get("PYTHONPATH"),
        }) + "\n")
    if not argv:
        return fail("missing command")
    command, rest = argv[0], argv[1:]
    state = load_state()
    root = option(rest, "--root")

    if command == "capabilities":
        delay = float(state.get("capabilities_delay") or 0)
        if delay:
            time.sleep(delay)
        if state.get("capabilities_fail"):
            return fail("capabilities failed")
        payload = dict(DEFAULT_CAPABILITIES)
        payload.update(state.get("capabilities") or {})
        payload["peel_root"] = root or os.environ.get("PEELFEED_ROOT") or ""
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
            return fail("no such peel", 3)
        payload = dict(state.get("review") or DEFAULT_REVIEW)
        payload["peel_id"] = peel_id
        return emit(payload)

    if command == "decide":
        peel_id = first_positional(rest)
        if peel_id not in peels:
            return fail("no such peel", 3)
        item = option(rest, "--item")
        decision = option(rest, "--decision")
        if decision not in ("accept", "reject", "override"):
            return fail("decision must be accept|reject|override")
        state.setdefault("decisions", []).append({
            "peel_id": peel_id,
            "item": item,
            "decision": decision,
            "note": option(rest, "--note"),
            "evidence": option(rest, "--evidence"),
            "model": option(rest, "--model"),
            "confidence": option(rest, "--confidence"),
        })
        save_state(state)
        return emit({"ok": True, "peel_id": peel_id, "item_id": item,
                     "decision": decision, "corrections": 1,
                     "note": "corrections are append-only; run peelfeed rebuild"})

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
    (argv + the ``PEELFEED_ROOT`` it was given) to a JSON Lines log, and answers
    from a JSON state file the test controls. The script can simulate slow
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
