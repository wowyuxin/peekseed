"""Ingestion error type shared by storage, peel reading and index builds.

It lives in its own module so the deleted PDF/chunker modules are not needed to
import it; the web layer maps :class:`IngestError` to a short HTTP detail.
"""


class IngestError(Exception):
    """A user-facing ingestion/storage error (never carries a secret)."""


__all__ = ["IngestError"]
