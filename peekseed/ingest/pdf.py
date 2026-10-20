"""PDF text extraction for peekseed.

Extraction is deliberately the only place a document parser is imported: the rest
of the pipeline consumes plain ``{"page": int, "text": str}`` dictionaries, so the
chunker, tests and the index writer never need a PDF library themselves.

pypdf (pure Python) is the default parser. PyMuPDF is optional: it is used only
when pypdf is unavailable or cannot read a particular file and PyMuPDF happens to
be installed. It is never required.

Every parser failure is raised as :class:`IngestError` with a message that is safe
to show a user. Messages name the uploaded file (not a machine path), explain
whether the PDF is encrypted or corrupt, and never include credentials.
"""

import os
import re

__all__ = [
    "IngestError",
    "available",
    "backend_name",
    "extract_pdf",
    "normalize_text",
    "page_count",
]

# Control characters are stripped from page text; newline (0x0a) and tab (0x09)
# are layout, not noise. Form feeds (0x0c) are removed because pages are already
# separate records here.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_BLANK = re.compile(r"\n{3,}")

_BACKENDS = None


class IngestError(Exception):
    """A user-facing ingestion failure (missing, encrypted or corrupt input)."""


def normalize_text(text):
    """Normalise one page of extracted text without destroying its content.

    Hard wraps are kept (the chunker can join them when it forms paragraphs), but
    CR/CRLF become LF, NULs and other control characters are removed, trailing
    spaces are dropped and runs of blank lines are collapsed to one.
    """
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\ufeff", "")
    value = _CONTROL_CHARS.sub("", value)
    lines = [line.rstrip() for line in value.split("\n")]
    value = "\n".join(lines)
    value = _MULTI_BLANK.sub("\n\n", value)
    return value.strip("\n")


def _backends():
    """Import the optional parsers once and cache what is actually installed."""
    global _BACKENDS
    if _BACKENDS is not None:
        return _BACKENDS
    found = {}
    try:
        import pypdf  # noqa: F401  (probed only; imported lazily on purpose)

        found["pypdf"] = pypdf
    except ImportError:
        pass
    mupdf = None
    try:
        import pymupdf as mupdf  # recent name of the same optional package
    except ImportError:
        try:
            import fitz as mupdf  # older import name
        except ImportError:
            mupdf = None
    if mupdf is not None:
        found["pymupdf"] = mupdf
    _BACKENDS = found
    return found


def available():
    """True when at least one PDF parser is installed."""
    return bool(_backends())


def backend_name():
    """The parser that would be tried first: ``"pypdf"``, ``"pymupdf"`` or ``""``."""
    found = _backends()
    if "pypdf" in found:
        return "pypdf"
    if "pymupdf" in found:
        return "pymupdf"
    return ""


def _display_name(path):
    return os.path.basename(str(path)) or "the PDF"


def _parser_order():
    found = _backends()
    order = []
    if "pypdf" in found:
        order.append(("pypdf", found["pypdf"]))
    if "pymupdf" in found:
        order.append(("pymupdf", found["pymupdf"]))
    return order


def _check_input(path):
    if not path:
        raise IngestError("A PDF path is required.")
    target = str(path)
    if not os.path.isfile(target):
        raise IngestError("PDF not found: '%s'." % _display_name(target))
    return target


def _pypdf_pages(module, target):
    name = _display_name(target)
    try:
        reader = module.PdfReader(target)
    except Exception as exc:  # corrupt file, unsupported encryption, ...
        raise IngestError("Could not open '%s' as a PDF: %s" % (name, exc)) from None
    if getattr(reader, "is_encrypted", False):
        try:
            unlocked = reader.decrypt("")
        except Exception:
            unlocked = 0
        if not unlocked:
            raise IngestError(
                "'%s' is password-protected; remove the password and upload it "
                "again." % name
            )
    try:
        total = len(reader.pages)
    except Exception as exc:
        raise IngestError("Could not read the page list of '%s': %s" % (name, exc)) from None
    pages = []
    for number in range(total):
        try:
            text = reader.pages[number].extract_text() or ""
        except Exception as exc:
            raise IngestError(
                "Could not extract text from page %d of '%s': %s"
                % (number + 1, name, exc)
            ) from None
        pages.append({"page": number + 1, "text": normalize_text(text)})
    return pages


def _mupdf_pages(module, target):
    name = _display_name(target)
    try:
        document = module.open(target)
    except Exception as exc:
        raise IngestError("Could not open '%s' as a PDF: %s" % (name, exc)) from None
    try:
        if getattr(document, "needs_pass", False):
            raise IngestError(
                "'%s' is password-protected; remove the password and upload it "
                "again." % name
            )
        pages = []
        for number in range(int(document.page_count)):
            try:
                text = document.load_page(number).get_text("text") or ""
            except Exception as exc:
                raise IngestError(
                    "Could not extract text from page %d of '%s': %s"
                    % (number + 1, name, exc)
                ) from None
            pages.append({"page": number + 1, "text": normalize_text(text)})
        return pages
    finally:
        try:
            document.close()
        except Exception:
            pass


def extract_pdf(path):
    """Extract one PDF as ``[{"page": 1-based int, "text": normalised str}, ...]``.

    All pages are returned in order, including pages with no text, so page numbers
    keep their meaning for citations. The first parser that succeeds wins; if one
    parser fails on a file the next installed parser gets a chance before the
    error is reported.
    """
    target = _check_input(path)
    parsers = _parser_order()
    if not parsers:
        raise IngestError(
            "No PDF parser is installed; install 'pypdf' to read PDF files."
        )
    failures = []
    for label, module in parsers:
        try:
            if label == "pypdf":
                return _pypdf_pages(module, target)
            return _mupdf_pages(module, target)
        except IngestError as exc:
            failures.append(str(exc))
    raise IngestError(failures[-1] if failures else "Could not read '%s'." % _display_name(target))


def page_count(path):
    """Return the number of pages without extracting text (for provenance info)."""
    target = _check_input(path)
    parsers = _parser_order()
    if not parsers:
        raise IngestError(
            "No PDF parser is installed; install 'pypdf' to read PDF files."
        )
    failures = []
    for label, module in parsers:
        try:
            if label == "pypdf":
                reader = module.PdfReader(target)
                if getattr(reader, "is_encrypted", False):
                    try:
                        unlocked = reader.decrypt("")
                    except Exception:
                        unlocked = 0
                    if not unlocked:
                        raise IngestError(
                            "'%s' is password-protected; remove the password and "
                            "upload it again." % _display_name(target)
                        )
                return len(reader.pages)
            document = module.open(target)
            try:
                if getattr(document, "needs_pass", False):
                    raise IngestError(
                        "'%s' is password-protected; remove the password and "
                        "upload it again." % _display_name(target)
                    )
                return int(document.page_count)
            finally:
                try:
                    document.close()
                except Exception:
                    pass
        except IngestError as exc:
            failures.append(str(exc))
        except Exception as exc:
            failures.append(
                "Could not read '%s': %s" % (_display_name(target), exc)
            )
    raise IngestError(failures[-1] if failures else "Could not read '%s'." % _display_name(target))
