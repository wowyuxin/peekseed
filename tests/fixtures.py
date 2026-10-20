"""Offline fixtures for peekseed's test suite.

Nothing here needs a model or the network: a small HTTP server implements the
four contracts peekseed speaks (OpenAI embeddings, custom /embed, /rerank,
OpenAI chat), and a tiny PDF writer produces real one-page documents so the PDF
path can be tested with pypdf.
"""

import contextlib
import json
import math
import os
import re
import shutil
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
