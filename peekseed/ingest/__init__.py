"""peekseed ingestion: storage-only uploads + peelfeed corpus -> vector index.

Uploads never parse a document: :func:`add_source` stores the bytes and records
the sha256/bytes. Extraction is owned by the peelfeed engine, which writes a peel
corpus per source; :func:`build_index` reads those corpora (gated on the peel-root
allowlist, the source sha256, the recorded conversion id and zero validation
errors), embeds the chunk set matching the embed link's chunk sizes through a
configured link and writes a plain-numpy index that needs no external service
to read; the canonical chunks stay the reading layer.

Nothing outside this package needs to know the embed protocol or the on-disk
index layout; the public functions below are the whole interface. There is no
in-process PDF parser or chunker here (design §1.6).
"""

from .errors import IngestError
from .index import (
    IndexCancelled,
    NeedsReindex,
    NoChunkSizes,
    NoExtractedSources,
    PROBE_MIN_COSINE,
    ProbeMismatch,
    active_job_for,
    add_source,
    build_index,
    chunk_set_name,
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
    link_chunk_sizes,
    list_jobs,
    load_index,
    mark_interrupted_jobs,
    peel_root,
    probe_cosine,
    probe_cosine_for_generation,
    read_chunks,
    read_job,
    release_shared_source,
    remove_source,
    reindex,
    request_cancel,
    sanitize_filename,
    store_shared_source,
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
    "NoChunkSizes",
    "NoExtractedSources",
    "PROBE_MIN_COSINE",
    "ProbeMismatch",
    "active_job_for",
    "add_source",
    "build_index",
    "chunk_set_name",
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
    "link_chunk_sizes",
    "list_jobs",
    "load_index",
    "mark_interrupted_jobs",
    "peel_root",
    "probe_cosine",
    "probe_cosine_for_generation",
    "read_chunks",
    "read_job",
    "release_shared_source",
    "remove_source",
    "reindex",
    "request_cancel",
    "sanitize_filename",
    "store_shared_source",
    "stored_source_path",
    "update_source",
    "verify_probe",
    "verify_probe_for_generation",
    "write_chunks",
    "write_job",
]
