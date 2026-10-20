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
    add_source,
    build_index,
    create_dataset,
    dataset_dir,
    dataset_indexes,
    dataset_info,
    embed_query,
    embed_space_key,
    embed_texts,
    read_chunks,
    sanitize_filename,
    write_chunks,
)
from .pdf import IngestError, available, backend_name, extract_pdf, page_count

__all__ = [
    "IngestError",
    "add_source",
    "available",
    "backend_name",
    "build_index",
    "chunk_pages",
    "create_dataset",
    "dataset_dir",
    "dataset_indexes",
    "dataset_info",
    "embed_query",
    "embed_space_key",
    "embed_texts",
    "extract_pdf",
    "page_count",
    "read_chunks",
    "sanitize_filename",
    "write_chunks",
]
