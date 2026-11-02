"""peekseed ingestion: storage-only uploads + peelfeed corpus -> vector index.

Uploads never parse a document: :func:`add_source` stores the bytes and records
the sha256/bytes. Extraction is owned by the peelfeed engine, which writes a peel
corpus per source; :func:`build_index` reads those corpora (gated on the peel-root
allowlist, the source sha256, the recorded conversion id and zero validation
errors), embeds the canonical chunks through a configured link and writes a
plain-numpy index that needs no external service to read.

Nothing outside this package needs to know the embed protocol or the on-disk
index layout; the public functions below are the whole interface. There is no
in-process PDF parser or chunker here (design §1.6).
"""

from .errors import IngestError
from .index import (
    IndexCancelled,
    NeedsReindex,
    NoExtractedSources,
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
    find_source,
    index_state,
    job_cancel_requested,
    job_path,
    list_jobs,
    load_index,
    mark_interrupted_jobs,
    peel_root,
    probe_cosine,
    probe_cosine_for_generation,
    read_chunks,
    read_job,
    remove_source,
    reindex,
    request_cancel,
    sanitize_filename,
    stored_source_path,
    update_source,
    verify_probe,
    verify_probe_for_generation,
    write_chunks,
    write_job,
)
from .lock import CollectionBusy, CollectionLock, collection_lock

__all__ = [
    "CollectionBusy",
    "CollectionLock",
    "IndexCancelled",
    "IngestError",
    "NeedsReindex",
    "NoExtractedSources",
    "PROBE_MIN_COSINE",
    "ProbeMismatch",
    "active_job_for",
    "add_source",
    "build_index",
    "collection_lock",
    "create_dataset",
    "dataset_dir",
    "dataset_indexes",
    "dataset_info",
    "embed_query",
    "embed_space_key",
    "embed_texts",
    "find_source",
    "index_state",
    "job_cancel_requested",
    "job_path",
    "list_jobs",
    "load_index",
    "mark_interrupted_jobs",
    "peel_root",
    "probe_cosine",
    "probe_cosine_for_generation",
    "read_chunks",
    "read_job",
    "remove_source",
    "reindex",
    "request_cancel",
    "sanitize_filename",
    "stored_source_path",
    "update_source",
    "verify_probe",
    "verify_probe_for_generation",
    "write_chunks",
    "write_job",
]
