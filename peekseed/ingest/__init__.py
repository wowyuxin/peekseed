"""peekseed ingestion: PDF text -> canonical chunks -> compact vector index.

The three stages are importable independently, but the pipeline is normally one
call: :func:`build_index` extracts PDFs when ``chunks.jsonl`` does not exist yet,
chunks them, embeds through a configured link and writes a plain-numpy index that
needs no external service to read.

Nothing outside this package needs to know the PDF parser, the embed protocol or
the on-disk index layout; the public functions below are the whole interface.
"""

from .chunk import chunk_pages
from .index import (
    IndexCancelled,
    NeedsReindex,
    PROBE_MIN_COSINE,
    ProbeMismatch,
    active_job_for,
    add_source,
    build_index,
    create_dataset,
    dataset_dir,
    dataset_indexes,
    dataset_info,
    embed_query,
    embed_space_key,
    embed_texts,
    index_state,
    job_cancel_requested,
    job_path,
    list_jobs,
    load_index,
    mark_interrupted_jobs,
    probe_cosine,
    probe_cosine_for_generation,
    read_chunks,
    read_job,
    reindex,
    request_cancel,
    sanitize_filename,
    verify_probe,
    verify_probe_for_generation,
    write_chunks,
    write_job,
)
from .lock import CollectionBusy, CollectionLock, collection_lock
from .pdf import IngestError, available, backend_name, extract_pdf, page_count

__all__ = [
    "CollectionBusy",
    "CollectionLock",
    "IndexCancelled",
    "IngestError",
    "NeedsReindex",
    "PROBE_MIN_COSINE",
    "ProbeMismatch",
    "active_job_for",
    "add_source",
    "available",
    "backend_name",
    "build_index",
    "chunk_pages",
    "collection_lock",
    "create_dataset",
    "dataset_dir",
    "dataset_indexes",
    "dataset_info",
    "embed_query",
    "embed_space_key",
    "embed_texts",
    "extract_pdf",
    "index_state",
    "job_cancel_requested",
    "job_path",
    "list_jobs",
    "load_index",
    "mark_interrupted_jobs",
    "page_count",
    "probe_cosine",
    "probe_cosine_for_generation",
    "read_chunks",
    "read_job",
    "reindex",
    "request_cancel",
    "sanitize_filename",
    "verify_probe",
    "verify_probe_for_generation",
    "write_chunks",
    "write_job",
]
