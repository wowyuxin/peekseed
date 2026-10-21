"""Corpus import: a versioned manifest that rebuilds a workspace from a bundle.

A deployment (the Mac mini, a migration from smart-search, a colleague's
workspace) can describe a corpus once and reproduce it on another machine with
``peekseed import --manifest corpus.json``. The manifest is a contract: its
schema is versioned, a newer schema is refused, and an import is designed to be
safe to re-run.

What the contract promises
--------------------------

* **Deterministic ids.** A collection's dataset id is the slug of its manifest
  name, so the same manifest always produces the same ids.
* **Idempotent.** A second import of an unchanged corpus writes nothing and
  embeds nothing per chunk; it only re-runs the probe canary. Uploaded/imported
  documents are compared by content hash, not by path.
* **Zero re-embed for a prebuilt collection.** When the manifest points at an
  existing peekseed dataset directory (``dataset``), its ``chunks.jsonl`` and
  vector index are copied unchanged. Before the copy is trusted, the stored
  probe is re-embedded through the configured embed link and must match at
  cosine >= 0.999 (the same gate search uses at query time); a mismatch aborts
  the import instead of serving vectors from a different embedding space.
* **All-or-nothing per collection.** A collection is staged in a temporary
  directory and renamed into place only after documents, chunks, index and probe
  verification succeed. A failed import leaves the previous dataset untouched.
* **Documents are copies.** The manifest's ``documents`` files are copied into
  the dataset's ``original/`` directory, hashed and recorded as provenance; the
  source files are never modified.

Manifest schema 1
-----------------

    {
      "schema": 1,
      "collections": [
        {
          "name": "cxl",                     # required; slugged into the id
          "title": "CXL",                    # optional display name
          "documents": ["../original/CXL.pdf"],   # optional if dataset is given
          "dataset": "../export/datasets/cxl",    # optional prebuilt dataset dir
          "space": "qwen3-...",              # optional index space selector
          "sha256": {"CXL.pdf": "..."}       # optional expected hashes
        }
      ],
      "queries": ["coherency", "..."]        # optional overlap harness queries
    }

Paths in the manifest are resolved relative to the manifest file's directory
when relative, which keeps a bundle portable. The prebuilt ``dataset`` directory
must be a peekseed dataset: ``dataset.json``, ``chunks.jsonl`` and
``index/<space>/generations/<uuid>/`` with ``info.json``, ``vectors.npy``,
``meta.json``, ``probe.npy`` and ``CURRENT``.
"""

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import numpy as np

from . import contracts, ingest, links
from . import workspace as workspace_module

__all__ = [
    "MANIFEST_SCHEMA",
    "CorpusImportError",
    "collect_plan",
    "import_corpus",
    "load_manifest",
]

MANIFEST_SCHEMA = 1
_PROBE_MIN_COSINE = ingest.PROBE_MIN_COSINE


class CorpusImportError(ingest.IngestError):
    """The manifest or a source bundle cannot be imported."""


# --- manifest --------------------------------------------------------------


def _slug(name):
    """The deterministic dataset id for a collection name."""
    text = str(name or "").strip().lower()
    out = []
    for char in text:
        if char.isascii() and (char.isalnum() or char in "._-"):
            out.append(char)
        elif char.isspace() or not char.isascii():
            out.append("-")
        else:
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-._")[:64].rstrip("-._")
    return slug or "dataset"


def _require_str(value, field, where):
    if not isinstance(value, str) or not value.strip():
        raise CorpusImportError("%s: '%s' is required" % (where, field))
    return value.strip()


def _resolve(base, value):
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = Path(base) / path
    return path.resolve()


def load_manifest(path):
    """Read and validate a corpus manifest; returns a normalised copy.

    Refuses a newer schema with an actionable message (the data contract rule:
    never guess at a future format) and rejects duplicate collection ids before
    any file is touched.
    """
    path = Path(path).expanduser()
    try:
        raw = contracts.read_json(path)
    except contracts.ContractError as exc:
        raise CorpusImportError("manifest is not readable: %s" % exc) from None
    if not isinstance(raw, dict):
        raise CorpusImportError("manifest must be a JSON object")
    declared = raw.get("schema")
    if not isinstance(declared, int):
        raise CorpusImportError("manifest 'schema' must be an integer")
    if declared > MANIFEST_SCHEMA:
        raise CorpusImportError(
            "manifest declares schema %d, newer than this peekseed build (schema "
            "%d); upgrade peekseed to import this corpus" % (declared, MANIFEST_SCHEMA)
        )
    if declared < 1:
        raise CorpusImportError("manifest schema %d is not supported" % declared)
    collections = raw.get("collections")
    if not isinstance(collections, list) or not collections:
        raise CorpusImportError("manifest 'collections' must be a non-empty list")
    base = path.parent
    seen = set()
    normalised = []
    for index, item in enumerate(collections, 1):
        where = "collection %d" % index
        if not isinstance(item, dict):
            raise CorpusImportError("%s must be an object" % where)
        name = _require_str(item.get("name"), "name", where)
        collection_id = _slug(name)
        if collection_id in seen:
            raise CorpusImportError(
                "duplicate collection id '%s' (from name '%s')" % (collection_id, name)
            )
        seen.add(collection_id)
        documents = item.get("documents") or []
        if not isinstance(documents, list):
            raise CorpusImportError("%s 'documents' must be a list" % where)
        resolved_docs = []
        for doc in documents:
            if not isinstance(doc, str) or not doc.strip():
                raise CorpusImportError(
                    "%s 'documents' entries must be non-empty paths" % where
                )
            resolved_docs.append(_resolve(base, doc))
        dataset = item.get("dataset")
        resolved_dataset = _resolve(base, dataset) if dataset else None
        chroma = item.get("chroma_export")
        resolved_chroma = _resolve(base, chroma) if chroma else None
        if not resolved_docs and resolved_dataset is None and resolved_chroma is None:
            raise CorpusImportError(
                "%s must declare 'documents', a prebuilt 'dataset' or a "
                "'chroma_export'" % where
            )
        if resolved_dataset is not None and not (resolved_dataset / "dataset.json").is_file():
            raise CorpusImportError(
                "%s: prebuilt dataset '%s' has no dataset.json" % (where, resolved_dataset)
            )
        if resolved_chroma is not None:
            if not resolved_docs:
                raise CorpusImportError(
                    "%s: 'chroma_export' needs 'documents' for provenance and /pdf"
                    % where
                )
            if not resolved_chroma.is_file():
                raise CorpusImportError(
                    "%s: chroma export '%s' does not exist" % (where, resolved_chroma)
                )
        hashes = item.get("sha256") or {}
        if not isinstance(hashes, dict):
            raise CorpusImportError("%s 'sha256' must be an object" % where)
        normalised.append({
            "name": name,
            "title": str(item.get("title") or name),
            "id": collection_id,
            "documents": resolved_docs,
            "dataset": resolved_dataset,
            "chroma_export": resolved_chroma,
            "space": str(item.get("space") or ""),
            "sha256": {str(key): str(value).lower() for key, value in hashes.items()},
        })
    queries = raw.get("queries") or []
    if not isinstance(queries, list) or any(not isinstance(q, str) for q in queries):
        raise CorpusImportError("manifest 'queries' must be a list of strings")
    return {"schema": MANIFEST_SCHEMA, "collections": normalised,
            "queries": list(queries), "path": str(path.resolve())}


# --- planning --------------------------------------------------------------


def _dataset_state(dataset_dir):
    """``(dataset.json dict, chunk count, info dicts)`` for one dataset dir."""
    try:
        dataset = contracts.read_json(Path(dataset_dir) / "dataset.json")
    except contracts.ContractError as exc:
        raise CorpusImportError(str(exc)) from None
    if not isinstance(dataset, dict):
        raise CorpusImportError("dataset.json is not a JSON object: %s" % dataset_dir)
    try:
        dataset = contracts.ensure_current(dataset, "dataset.json")
    except contracts.ContractError as exc:
        raise CorpusImportError(str(exc)) from None
    chunks = ingest.read_chunks(dataset_dir)
    indexes = ingest.dataset_indexes(dataset_dir)
    return dataset, chunks, indexes


def _choose_space(collection, indexes):
    wanted = collection.get("space") or ""
    spaces = [str(info.get("space") or "") for info in indexes]
    if wanted:
        if wanted not in spaces:
            raise CorpusImportError(
                "collection '%s' asks for space '%s' but the source dataset has: %s"
                % (collection["id"], wanted, ", ".join(spaces) or "none")
            )
        return wanted, next(info for info in indexes if info.get("space") == wanted)
    if not spaces:
        raise CorpusImportError(
            "prebuilt dataset for '%s' has no index to copy" % collection["id"]
        )
    if len(spaces) > 1:
        raise CorpusImportError(
            "prebuilt dataset for '%s' has several spaces (%s); set 'space' in the "
            "manifest" % (collection["id"], ", ".join(spaces))
        )
    return spaces[0], indexes[0]


def collect_plan(manifest, target_workspace):
    """Describe what an import would do, without writing anything."""
    rows = []
    for collection in manifest["collections"]:
        target = Path(target_workspace.datasets_dir) / collection["id"]
        row = {
            "id": collection["id"],
            "title": collection["title"],
            "target": str(target),
            "kind": ("prebuilt" if collection["dataset"] else
                     "chroma" if collection["chroma_export"] else "documents"),
            "documents": [str(path) for path in collection["documents"]],
            "documents_exist": all(path.is_file() for path in collection["documents"]),
        }
        if target.is_dir():
            try:
                dataset, chunks, indexes = _dataset_state(target)
                row["existing"] = True
                row["existing_chunks"] = len(chunks)
                row["existing_indexes"] = len(indexes)
            except CorpusImportError as exc:
                row["existing"] = True
                row["existing_error"] = str(exc)
        else:
            row["existing"] = False
        rows.append(row)
    return {"schema": MANIFEST_SCHEMA, "workspace": target_workspace.as_dict(),
            "collections": rows, "queries": len(manifest.get("queries") or []),
            "dry_run": True}


# --- import ----------------------------------------------------------------


def _expected_hashes(collection, documents):
    expected = dict(collection.get("sha256") or {})
    for path in documents:
        if path.name in expected:
            continue
        for key, value in expected.items():
            if Path(key).name == path.name:
                expected[path.name] = value
    return expected


def _source_expected_hashes(collection):
    """The content hashes the imported corpus must have on disk.

    For a prebuilt collection with no explicit ``documents`` list, the source
    dataset's own provenance supplies the hashes; that is what lets a re-run
    prove "same corpus" without guessing from paths.
    """
    expected = _expected_hashes(collection, collection["documents"])
    if expected or not collection["dataset"]:
        return expected
    source_dataset, _chunks, _indexes = _dataset_state(Path(collection["dataset"]))
    return {str(entry.get("name") or ""): str(entry.get("sha256") or "")
            for entry in source_dataset.get("sources") or []
            if isinstance(entry, dict) and entry.get("sha256")}


def _already_imported(target, expected_hashes, space, link):
    """``(chunks, probe_cosine)`` when the target already holds this corpus.

    Returns None when it does not. A probe mismatch is not "already imported":
    it raises, because silently skipping would leave an index that no longer
    matches the live embedder.
    """
    try:
        dataset, chunks, indexes = _dataset_state(target)
    except CorpusImportError:
        return None
    if not chunks:
        return None
    info = next((item for item in indexes
                 if str(item.get("space") or "") == space), None)
    if info is None:
        return None
    if expected_hashes:
        existing = {str(entry.get("name") or ""): str(entry.get("sha256") or "")
                    for entry in dataset.get("sources") or []
                    if isinstance(entry, dict)}
        for name, digest in expected_hashes.items():
            if digest and existing.get(name) != digest:
                return None
    generation = str(info.get("generation") or "")
    generation_dir = Path(target) / "index" / space / "generations" / generation
    if not generation_dir.is_dir():
        return None
    try:
        cosine = ingest.probe_cosine_for_generation(link, generation_dir, space)
    except ingest.NeedsReindex:
        return None
    except ingest.ProbeMismatch as exc:
        raise CorpusImportError(str(exc)) from None
    if cosine < _PROBE_MIN_COSINE:
        raise CorpusImportError(
            "the target already holds %d chunk(s) for space '%s', but the live "
            "embedder no longer reproduces its probe (cosine %.6f < %.3f); fix the "
            "embed link or rebuild the index instead of importing" %
            (len(chunks), space, cosine, _PROBE_MIN_COSINE)
        )
    return len(chunks), cosine


def _read_chroma_export(path):
    """Read a portable Chroma export (JSON Lines) and validate its rows.

    The deployment-side exporter (from smart-search's Chroma store) writes one
    object per line:

        {"id": "...", "document": "chunk text",
         "metadata": {"source": "DDR5.pdf", "source_sha256": "...",
                       "page_start": 3, "page_end": 3, "section": "...",
                       "chapter": "...", "numbering": "...", "doc": "breadcrumb"},
         "embedding": [0.1, -0.2, ...]}

    Embeddings are copied unchanged (zero re-embed); only the fixed probe text
    is embedded once to bind the space. The row ``id`` is not trusted: peekseed
    derives a content id from the source hash and row position, which makes the
    import deterministic.
    """
    path = Path(path)
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise CorpusImportError(
                    "chroma export line %d is not valid JSON: %s" % (number, exc)
                ) from None
            if not isinstance(row, dict):
                raise CorpusImportError(
                    "chroma export line %d is not an object" % number
                )
            document = row.get("document")
            metadata = row.get("metadata") or {}
            embedding = row.get("embedding")
            if not isinstance(document, str) or not document.strip():
                raise CorpusImportError(
                    "chroma export line %d has no document text" % number
                )
            if not isinstance(metadata, dict):
                raise CorpusImportError(
                    "chroma export line %d metadata is not an object" % number
                )
            if not isinstance(embedding, list) or not embedding:
                raise CorpusImportError(
                    "chroma export line %d has no embedding vector" % number
                )
            vector = []
            for value in embedding:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise CorpusImportError(
                        "chroma export line %d embedding contains a non-number" % number
                    )
                vector.append(float(value))
            rows.append({"document": document, "metadata": metadata,
                         "embedding": vector})
    if not rows:
        raise CorpusImportError("chroma export is empty: %s" % path)
    return rows


def _chroma_chunk(collection_id, row, sources, ordinal):
    """One canonical chunk row derived from an exported row."""
    metadata = row["metadata"]
    source_name = Path(str(metadata.get("source") or "")).name
    entry = sources.get(source_name)
    if entry is None:
        raise CorpusImportError(
            "chroma export row %d names source '%s', which is not one of the "
            "manifest documents (%s)"
            % (ordinal, source_name, ", ".join(sorted(sources)) or "none")
        )
    expected_sha = str(metadata.get("source_sha256") or "").lower()
    if expected_sha and expected_sha != entry["sha256"]:
        raise CorpusImportError(
            "chroma export row %d source hash does not match '%s'"
            % (ordinal, source_name)
        )
    page_start = metadata.get("page_start")
    page_end = metadata.get("page_end")
    try:
        page_start = max(1, int(page_start if page_start is not None else 1))
        page_end = max(page_start, int(page_end if page_end is not None else page_start))
    except (TypeError, ValueError):
        raise CorpusImportError(
            "chroma export row %d has invalid page metadata" % ordinal
        ) from None
    text = str(row["document"]).strip()
    doc = str(metadata.get("doc") or text)
    return contracts.validate_chunk({
        "id": contracts.chunk_id(entry["sha256"], ordinal, 0),
        "dataset": collection_id,
        "source": source_name,
        "source_sha256": entry["sha256"],
        "page_start": page_start,
        "page_end": page_end,
        "section": str(metadata.get("section") or ""),
        "chapter": str(metadata.get("chapter") or ""),
        "numbering": str(metadata.get("numbering") or ""),
        "doc": doc,
        "text": text,
        "heading_only": bool(metadata.get("heading_only")),
        "block_index": ordinal,
        "chunk_index": 0,
        "chunker_version": contracts.CHUNKER_VERSION,
        "ordinal": ordinal,
    }, derive_id=True)


def _write_current(space_dir, generation):
    """Atomically point the space at ``generation`` (the commit point)."""
    path = Path(space_dir) / "CURRENT"
    handle, temporary = tempfile.mkstemp(dir=str(space_dir), prefix=".CURRENT.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(generation + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _import_chroma(paths, collection, link, expected, report):
    """Import a Chroma export: chunk rows + vectors copied, probe embedded once."""
    from .ingest.index import _build_info, _meta_record

    space = links.embed_space_key(link)
    target = Path(paths.datasets_dir) / collection["id"]
    if _already_imported(target, expected, space, link) is not None:
        report["collections"].append({
            "id": collection["id"], "status": "skipped",
            "reason": "the target already holds this corpus's documents and index",
            "space": space,
        })
        return
    rows = _read_chroma_export(collection["chroma_export"])
    staged, dataset_meta = _stage_documents(paths, collection)
    try:
        sources = {entry["name"]: entry for entry in dataset_meta["sources"]}
        chunks = [_chroma_chunk(collection["id"], row, sources, ordinal)
                  for ordinal, row in enumerate(rows, 1)]
        vectors = [row["embedding"] for row in rows]
        dims = {len(vector) for vector in vectors}
        if len(dims) != 1:
            raise CorpusImportError(
                "chroma export vectors have mixed dimensions: %s"
                % sorted(dims)
            )
        dim = dims.pop()
        probe = np.asarray(ingest.embed_query(link, contracts.PROBE_TEXT),
                           dtype=np.float32).ravel()
        if int(probe.shape[0]) != dim:
            raise CorpusImportError(
                "chroma export vectors are dimension %d but the live embedder is "
                "dimension %d; export from the model that built the store, or "
                "reindex into this space" % (dim, probe.shape[0])
            )
        ingest.write_chunks(staged, chunks)
        generation = uuid.uuid4().hex[:16]
        generation_dir = (Path(staged) / "index" / space / "generations" / generation)
        generation_dir.mkdir(parents=True, exist_ok=True)
        array = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        np.save(str(generation_dir / "vectors.npy"), array / norms)
        contracts.write_json_atomic(generation_dir / "meta.json",
                                    [_meta_record(chunk) for chunk in chunks])
        probe_dir = generation_dir / "probe.npy"
        np.save(str(probe_dir), probe / (np.linalg.norm(probe) or 1.0))
        sources_summary = [
            {"name": entry["name"],
             "sha256": entry["sha256"],
             "bytes": entry.get("bytes", 0),
             "pages": entry.get("pages", 0),
             "path": entry.get("path", "")}
            for entry in dataset_meta["sources"]
        ]
        info = _build_info(
            dataset_meta, collection["id"], space, generation, dim, len(chunks),
            link, sources_summary, contracts.chunker_identity(),
            contracts.extractor_identity("chroma-export", "1"),
            contracts.documents_digest(sources_summary),
        )
        contracts.write_json_atomic(generation_dir / "info.json", info)
        space_dir = Path(staged) / "index" / space
        _write_current(space_dir, generation)
        cosine = ingest.probe_cosine_for_generation(link, generation_dir, space)
        if cosine < _PROBE_MIN_COSINE:
            raise CorpusImportError(
                "the live embedder did not reproduce the probe just written "
                "(cosine %.6f)" % cosine
            )
        _commit_staged(staged, target)
    except BaseException:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise
    report["collections"].append({
        "id": collection["id"], "status": "imported", "kind": "chroma",
        "space": space, "documents": len(dataset_meta["sources"]),
        "chunks": len(chunks), "probe_cosine": round(cosine, 6),
    })


def _import_one(paths, collection, manifest, report):
    target = Path(paths.datasets_dir) / collection["id"]

    if collection["chroma_export"]:
        _import_chroma(paths, collection, _embed_link(),
                       _expected_hashes(collection, collection["documents"]),
                       report)
        return

    if collection["dataset"]:
        source = Path(collection["dataset"])
        _source_dataset, _source_chunks, source_indexes = _dataset_state(source)
        space, source_info = _choose_space(collection, source_indexes)
        link = _embed_link()
        expected = _source_expected_hashes(collection)
        already = _already_imported(target, expected, space, link)
        if already is not None:
            report["collections"].append({
                "id": collection["id"], "status": "skipped",
                "reason": "the target already holds this corpus's documents and index",
                "space": space, "chunks": already[0],
                "probe_cosine": round(already[1], 6),
            })
            return
        staged, space, info, dataset_meta = _copy_prebuilt(collection, target, space)
        try:
            cosine = _verify_source_probe(space, info, staged)
            _update_imported_info(staged, collection, space, info, dataset_meta)
        except BaseException:
            shutil.rmtree(str(staged), ignore_errors=True)
            raise
        _commit_staged(staged, target)
        report["collections"].append({
            "id": collection["id"], "status": "imported",
            "kind": "prebuilt", "space": space,
            "documents": len(dataset_meta["sources"]),
            "chunks": len(ingest.read_chunks(target)),
            "probe_cosine": round(cosine, 6),
        })
        return

    # Documents-only import: build the index in a staged dataset, then commit it
    # only after the build succeeds (all-or-nothing per collection).
    link = _embed_link()
    space = links.embed_space_key(link)
    expected = _expected_hashes(collection, collection["documents"])
    already = _already_imported(target, expected, space, link)
    if already is not None:
        report["collections"].append({
            "id": collection["id"], "status": "skipped",
            "reason": "the target already holds this corpus's documents and index",
            "space": space, "chunks": already[0],
            "probe_cosine": round(already[1], 6),
        })
        return
    staged, dataset_meta = _stage_documents(paths, collection)
    try:
        record = contracts.new_job("index", collection=collection["id"])
        info = ingest.build_index(staged, link, job=record)
    except BaseException:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise
    _commit_staged(staged, target)
    report["collections"].append({
        "id": collection["id"], "status": "imported", "kind": "documents",
        "space": info.get("space"), "documents": len(dataset_meta["sources"]),
        "chunks": int(info.get("n") or 0),
        "probe_cosine": round(ingest.probe_cosine(
            link, target, info.get("space")), 6),
    })


def _copy_prebuilt(collection, dataset_dir, target_space):
    """Copy a source dataset's canonical data + one index, unchanged."""
    source = Path(collection["dataset"])
    dataset, chunks, indexes = _dataset_state(source)
    space, info = _choose_space(collection, indexes)

    # Documents: the source dataset's own original files, or the manifest's.
    source_original = source / "original"
    documents = collection["documents"]
    if not documents:
        documents = sorted(path for path in source_original.iterdir()
                           if path.is_file()) if source_original.is_dir() else []
    if not documents:
        raise CorpusImportError(
            "prebuilt dataset for '%s' has no original documents" % collection["id"]
        )
    for path in documents:
        if not path.is_file():
            raise CorpusImportError("document not found: %s" % path)
    # Stage the whole dataset so a failure never leaves half a collection.
    staged = Path(tempfile.mkdtemp(prefix=".import-" + collection["id"] + "-",
                                   dir=str(Path(dataset_dir).parent)))
    try:
        (staged / "original").mkdir(parents=True, exist_ok=True)
        for path in documents:
            shutil.copyfile(str(path), str(staged / "original" / path.name))
        # Copy chunks and the chosen index generation tree.
        if not (source / "chunks.jsonl").is_file():
            raise CorpusImportError(
                "prebuilt dataset for '%s' has no chunks.jsonl; the canonical "
                "chunk rows are part of the bundle" % collection["id"]
            )
        shutil.copyfile(str(source / "chunks.jsonl"), str(staged / "chunks.jsonl"))
        source_space = source / "index" / space
        if not source_space.is_dir():
            raise CorpusImportError(
                "prebuilt dataset for '%s' has no index directory '%s'"
                % (collection["id"], source_space)
            )
        shutil.copytree(str(source_space), str(staged / "index" / space))
        # Rebuild dataset.json from the copied files so provenance is local and
        # the id is the manifest's deterministic id, not the source's.
        dataset_meta = {
            "schema": contracts.DATASET_SCHEMA,
            "id": collection["id"],
            "name": collection["title"],
            "created_at": contracts.utc_now(),
            "sources": [],
            "notes": "",
        }
        for path in documents:
            entry = {
                "name": path.name,
                "path": "original/" + path.name,
                "sha256": contracts.sha256_file(staged / "original" / path.name),
                "bytes": (staged / "original" / path.name).stat().st_size,
            }
            dataset_meta["sources"].append(entry)
        # The copied chunks must belong to the documents just copied; otherwise
        # the index would be a plausible-looking mismatch.
        chunk_by_source = {
            row.get("source"): row.get("source_sha256")
            for row in ingest.read_chunks(staged)
        }
        for entry in dataset_meta["sources"]:
            chunk_sha = chunk_by_source.get(entry["name"])
            if chunk_sha is not None and chunk_sha != entry["sha256"]:
                raise CorpusImportError(
                    "prebuilt chunks for '%s' were built from a different file "
                    "(chunk source hash %s, manifest document hash %s)"
                    % (entry["name"], chunk_sha, entry["sha256"])
                )
        contracts.write_json_atomic(staged / "dataset.json",
                                    contracts.validate_dataset(dataset_meta))
        return staged, space, info, dataset_meta
    except BaseException:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise


def _verify_source_probe(space, info, staged_dataset_dir):
    """Re-embed the source generation's stored probe; refuse a space mismatch."""
    generation = str(info.get("generation") or "")
    if not generation:
        raise CorpusImportError(
            "index info for space '%s' has no generation; rebuild the source index"
            % space
        )
    generation_dir = Path(staged_dataset_dir) / "index" / space / "generations" / generation
    if not generation_dir.is_dir():
        raise CorpusImportError(
            "index generation '%s' is missing from the bundle" % generation_dir
        )
    link = links.active("embed")
    if link is None:
        raise CorpusImportError(
            "no embedding link is enabled; configure the embedder that built this "
            "index before importing (the probe canary needs it)"
        )
    try:
        cosine = ingest.probe_cosine_for_generation(link, generation_dir, space)
    except (ingest.NeedsReindex, ingest.ProbeMismatch) as exc:
        raise CorpusImportError(str(exc)) from None
    if cosine < _PROBE_MIN_COSINE:
        raise CorpusImportError(
            "the configured embedder does not reproduce the imported index's "
            "embedding space (probe cosine %.6f < %.3f); point the embed link at "
            "the model that built it, or rebuild the index instead of importing it"
            % (cosine, _PROBE_MIN_COSINE)
        )
    return cosine


def _update_imported_info(staged_dataset_dir, collection, space, info, dataset_meta):
    """Rewrite info.json so the imported index names the target dataset."""
    generation = str(info.get("generation") or "")
    info_path = Path(staged_dataset_dir) / "index" / space / "generations" / generation / "info.json"
    try:
        raw = contracts.read_json(info_path)
    except contracts.ContractError as exc:
        raise CorpusImportError(str(exc)) from None
    if not isinstance(raw, dict):
        raise CorpusImportError("imported info.json is not a JSON object")
    raw["dataset"] = collection["id"]
    raw["name"] = collection["title"]
    raw["sources"] = [
        {"name": entry["name"], "sha256": entry["sha256"],
         "bytes": entry.get("bytes", 0), "pages": entry.get("pages", 0),
         "path": entry.get("path", "")}
        for entry in dataset_meta["sources"]
    ]
    new_digest = contracts.documents_digest(raw["sources"])
    original_digest = str(raw.get("documents_digest") or "")
    if original_digest and original_digest != new_digest:
        raise CorpusImportError(
            "the manifest documents differ from the documents the prebuilt index "
            "was built from (documents digest mismatch); import the matching "
            "documents or rebuild the index instead of copying it"
        )
    raw["documents_digest"] = new_digest
    try:
        validated = contracts.validate_info(raw)
    except contracts.ContractError as exc:
        raise CorpusImportError(str(exc)) from None
    contracts.write_json_atomic(info_path, validated)


def _embed_link():
    link = links.active("embed")
    if link is None:
        raise CorpusImportError(
            "no embedding link is enabled; configure the embedder before importing "
            "documents that need an index"
        )
    return link


def _stage_documents(paths, collection):
    staged = Path(tempfile.mkdtemp(prefix=".import-" + collection["id"] + "-",
                                   dir=str(Path(paths.datasets_dir))))
    try:
        (staged / "original").mkdir(parents=True, exist_ok=True)
        dataset = {
            "schema": contracts.DATASET_SCHEMA,
            "id": collection["id"],
            "name": collection["title"],
            "created_at": contracts.utc_now(),
            "sources": [],
            "notes": "",
        }
        contracts.write_json_atomic(staged / "dataset.json",
                                    contracts.validate_dataset(dataset))
        dataset["dir"] = str(staged)
        from .ingest.index import add_source

        # add_source writes dataset.json in the staged directory as it goes.
        staged_dataset = dict(dataset)
        staged_dataset["dir"] = str(staged)
        expected = _expected_hashes(collection, collection["documents"])
        for path in collection["documents"]:
            if not path.is_file():
                raise CorpusImportError("document not found: %s" % path)
            digest = contracts.sha256_file(path)
            if path.name in expected and expected[path.name] != digest:
                raise CorpusImportError(
                    "document '%s' does not match the manifest sha256" % path.name
                )
            add_source(staged_dataset, path)
        try:
            dataset_meta = contracts.validate_dataset(
                contracts.read_json(staged / "dataset.json"))
        except contracts.ContractError as exc:
            raise CorpusImportError(str(exc)) from None
        return staged, dataset_meta
    except BaseException:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise


def _commit_staged(staged, target):
    backup = None
    if target.is_dir():
        backup = target.with_name(target.name + ".import-backup-" + uuid.uuid4().hex[:8])
        target.rename(backup)
    try:
        staged.rename(target)
    except BaseException:
        if backup is not None:
            backup.rename(target)
        raise
    if backup is not None:
        shutil.rmtree(str(backup), ignore_errors=True)


def import_corpus(manifest_path, *, workspace=None, dry_run=False):
    """Import one manifest into a workspace; returns a report dictionary.

    ``dry_run`` validates the manifest and prints the target layout without
    writing documents, indexes or probe vectors (and without embedding).
    """
    manifest = load_manifest(manifest_path)
    ws = workspace_module.resolve(workspace)
    for directory in (ws.datasets_dir, ws.jobs_dir, ws.config_dir):
        Path(directory).mkdir(parents=True, exist_ok=True)
    if dry_run:
        return collect_plan(manifest, ws)
    report = {
        "schema": MANIFEST_SCHEMA,
        "workspace": ws.as_dict(),
        "manifest": manifest["path"],
        "dry_run": False,
        "collections": [],
    }
    for collection in manifest["collections"]:
        _import_one(ws, collection, manifest, report)
    return report
