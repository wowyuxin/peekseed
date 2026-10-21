"""Text extraction: one interface that turns any supported source into pages.

The extraction stage is the only place peekseed reads document formats. Its
output is deliberately plain -- ordered ``{"page": int, "text": str}`` records --
so the chunker, the scan cache and the tests never need a parser themselves::

    {"pages": [{"page": 1, "text": "..."}, ...],
     "extractor": {"name": str, "version": str},
     "pages_count": int,
     "needs_ocr": bool}

Core formats use only the standard library, with PDF text coming from
:mod:`peekseed.ingest.pdf` (``pypdf``): PDF, Markdown, HTML/XHTML, plain text,
EPUB and email (``.eml``). Optional extras are detected lazily and are never
imported while this module is imported:

* ``office`` (``python-docx``, ``python-pptx``): Word ``.docx`` and PowerPoint
  ``.pptx`` files.
* ``tables`` (``openpyxl``): Excel ``.xlsx`` files.
* ``ocr`` (``pytesseract`` + Tesseract): scanned PDF pages. A page image comes
  from the optional ``layout`` renderer (``pymupdf``) when installed, or from
  the scan image embedded in the PDF via pypdf.

A page whose extracted text has fewer than :data:`MIN_TEXT_CHARS` non-whitespace
characters is treated as having no text layer: a document that is mostly such
pages needs OCR, and without the ``[ocr]`` extra :class:`OcrRequired` names the
first such page instead of silently indexing an empty document.

Extractor identity is provenance, not decoration: the extraction cache key is the
content hash alone (``cache_key``), so a cache document must store the identity
and only a matching identity is a cache hit. A parser version change therefore
invalidates chunks built from the old output.

Third-party backends can be registered in-process with :func:`register_extractor`
or published in the ``peekseed.extractors`` entry-point group; discovery is lazy
and a broken optional plugin is ignored rather than breaking extraction.
"""

import email
import importlib
import importlib.metadata
import importlib.util
import os
import posixpath
import re
import shutil
import zipfile
from email import policy
from html.parser import HTMLParser
from xml.etree import ElementTree

from . import contracts
from .ingest.pdf import IngestError, extract_pdf, normalize_text

__all__ = [
    "ENTRY_POINT_GROUP",
    "EXTRACTOR_API",
    "MIN_TEXT_CHARS",
    "SUPPORTED_EXTENSIONS",
    "OcrRequired",
    "available_extractors",
    "cache_key",
    "extract",
    "extractor_identity_for",
    "get_extractor",
    "ocr_available",
    "register_extractor",
]

#: The extraction interface version. A backend published through the entry-point
#: group may declare ``api`` and expect this value; it is also part of the
#: stdlib backends' identity so an interface change invalidates cached output.
EXTRACTOR_API = 1

#: Entry-point group used to discover third-party extraction backends.
ENTRY_POINT_GROUP = "peekseed.extractors"

#: A page with fewer non-whitespace characters than this has no usable text
#: layer. Scanned pages usually extract as ``""`` or a stray form-feed artefact.
MIN_TEXT_CHARS = 20

#: Supported file extensions mapped to their format name. PDF, Markdown, HTML,
#: TXT, EPUB and email are core (stdlib or ``pypdf``); the office/tables formats
#: are supported when their extra is installed and raise an install hint
#: otherwise.
SUPPORTED_EXTENSIONS = {
    ".pdf": "pdf",
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".txt": "text",
    ".text": "text",
    ".epub": "epub",
    ".eml": "email",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
}

_FORMAT_VERSION = str(EXTRACTOR_API)
_CORE_FORMAT_LIST = "PDF, Markdown (.md), HTML, TXT, EPUB and email (.eml)"
_NON_WHITESPACE = re.compile(r"\s+")
_SKIP_HTML = frozenset(
    {"head", "script", "style", "nav", "noscript", "template", "svg", "iframe"}
)
_BLOCK_HTML = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
        "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
        "h6", "header", "hr", "li", "main", "ol", "p", "pre", "section",
        "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)


class OcrRequired(IngestError):
    """A page has no text layer and OCR is needed to read it.

    The message names the file and the page and tells the user what to install
    or enable. ``file`` and ``page`` are also available programmatically so a
    caller can react without parsing the message.
    """

    def __init__(self, message, *, file=None, page=None):
        super().__init__(message)
        self.file = file
        self.page = page


# --- small helpers ---------------------------------------------------------


def _display_name(path):
    """The user-facing name of a file: its basename, never a machine path."""
    return os.path.basename(os.fspath(path)) or "the document"


def _extension(path):
    """The lower-case extension of a path, including the leading dot."""
    return os.path.splitext(os.fspath(path))[1].lower()


def _module_installed(name):
    """True when ``name`` is importable, without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _distribution_version(distribution):
    """The installed version of a distribution, or ``None`` when absent."""
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _read_text(path):
    """Decode a text file as UTF-8, replacing bytes that are not valid UTF-8."""
    try:
        with open(os.fspath(path), "rb") as handle:
            data = handle.read()
    except OSError:
        raise IngestError("Could not read '%s'." % _display_name(path)) from None
    return normalize_text(data.decode("utf-8", errors="replace"))


def _textless(text):
    """True when ``text`` has too little content to be a usable text layer."""
    return len(_NON_WHITESPACE.sub("", str(text or ""))) < MIN_TEXT_CHARS


# --- HTML (also used for XHTML and HTML email bodies) ----------------------


class _VisibleText(HTMLParser):
    """Collect visible text from HTML as one line per block element.

    Script/style/nav/head-ish elements are skipped wholesale, including their
    contents, so a page's navigation or inline JavaScript never ends up in the
    extracted text. Block boundaries become line breaks; inline whitespace is
    collapsed. ``convert_charrefs`` makes entities like ``&amp;`` readable.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._lines = []
        self._buffer = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_HTML:
            self._skipping += 1
        elif not self._skipping and tag in _BLOCK_HTML:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if not self._skipping and tag in _BLOCK_HTML:
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_HTML:
            if self._skipping:
                self._skipping -= 1
        elif not self._skipping and tag in _BLOCK_HTML:
            self._flush()

    def handle_data(self, data):
        if self._skipping:
            return
        text = " ".join(str(data).split())
        if text:
            self._buffer.append(text)

    def _flush(self):
        line = " ".join(self._buffer).strip()
        if line:
            self._lines.append(line)
        self._buffer = []

    def lines(self):
        """Finish parsing and return the collected non-empty lines."""
        self._flush()
        return self._lines


def _html_text(markup):
    """Extract normalised visible text lines from an HTML/XHTML string."""
    parser = _VisibleText()
    parser.feed(str(markup or ""))
    parser.close()
    return normalize_text("\n".join(parser.lines()))


# --- core format extractors ------------------------------------------------


def _extract_markdown(path, *, ocr=None):
    """Markdown: keep the text as-is so ``#`` heading lines survive for the chunker."""
    pages = [{"page": 1, "text": _read_text(path)}]
    return {"pages": pages, "needs_ocr": False}


def _extract_html(path, *, ocr=None):
    """HTML/XHTML: visible text lines with script/style/nav noise removed."""
    pages = [{"page": 1, "text": _html_text(_read_text(path))}]
    return {"pages": pages, "needs_ocr": False}


def _extract_text(path, *, ocr=None):
    """Plain text: decoded UTF-8 (with replacement) and normalised."""
    pages = [{"page": 1, "text": _read_text(path)}]
    return {"pages": pages, "needs_ocr": False}


def _local_name(tag):
    """The local part of an XML tag (namespace prefixes removed)."""
    value = str(tag)
    return value.rsplit("}", 1)[-1] if "}" in value else value


def _find_element(element, local_name):
    """The first descendant element with the given local tag name."""
    for child in element.iter():
        if _local_name(child.tag) == local_name:
            return child
    return None


def _archive_xml(archive, member, name):
    """Read and parse one XML member of an EPUB archive."""
    try:
        data = archive.read(member)
    except KeyError:
        raise IngestError(
            "Could not read '%s' as an EPUB; it is missing its package files." % name
        ) from None
    except (OSError, RuntimeError):
        raise IngestError("Could not read '%s' as an EPUB." % name) from None
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise IngestError(
            "Could not read '%s' as an EPUB; its package XML is invalid." % name
        ) from None


def _extract_epub(path, *, ocr=None):
    """EPUB: one page per spine item, in reading order (page 1..n)."""
    name = _display_name(path)
    pages = []
    try:
        with zipfile.ZipFile(os.fspath(path)) as archive:
            container = _archive_xml(archive, "META-INF/container.xml", name)
            rootfile = _find_element(container, "rootfile")
            if rootfile is None:
                raise IngestError(
                    "Could not read '%s' as an EPUB; it has no package document." % name
                )
            package_path = rootfile.get("full-path") or ""
            package = _archive_xml(archive, package_path, name)
            manifest = {}
            spine = []
            for element in package.iter():
                local = _local_name(element.tag)
                if local == "item":
                    identifier = element.get("id")
                    href = element.get("href")
                    if identifier and href:
                        manifest[identifier] = href
                elif local == "itemref":
                    reference = element.get("idref")
                    if reference:
                        spine.append(reference)
            if not spine:
                raise IngestError(
                    "Could not read '%s' as an EPUB; it has no reading order." % name
                )
            base = posixpath.dirname(package_path)
            for page_number, reference in enumerate(spine, start=1):
                href = manifest.get(reference)
                text = ""
                if href:
                    member = posixpath.normpath(posixpath.join(base, href))
                    try:
                        markup = archive.read(member).decode("utf-8", errors="replace")
                    except KeyError:
                        markup = ""
                    text = _html_text(markup)
                pages.append({"page": page_number, "text": text})
    except zipfile.BadZipFile:
        raise IngestError(
            "Could not read '%s' as an EPUB; the file is not a valid ZIP archive." % name
        ) from None
    except OSError:
        raise IngestError("Could not read '%s' as an EPUB." % name) from None
    return {"pages": pages, "needs_ocr": False}


def _email_body(message):
    """The readable body of an email message: text/plain, else stripped HTML."""
    if message.is_multipart():
        plain = None
        html = None
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_maintype() != "text":
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain" and plain is None:
                plain = part.get_content()
            elif content_type == "text/html" and html is None:
                html = part.get_content()
        if plain is not None:
            return str(plain)
        if html is not None:
            return _html_text(html)
        return ""
    try:
        content = message.get_content()
    except Exception:
        content = None
    if isinstance(content, str):
        if message.get_content_type() == "text/html":
            return _html_text(content)
        return content
    payload = message.get_payload(decode=True) or b""
    charset = message.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, errors="replace")
    except LookupError:
        text = payload.decode("utf-8", errors="replace")
    return _html_text(text) if message.get_content_type() == "text/html" else text


def _extract_email(path, *, ocr=None):
    """.eml: envelope headers plus the readable body as one page."""
    name = _display_name(path)
    try:
        with open(os.fspath(path), "rb") as handle:
            message = email.message_from_binary_file(handle, policy=policy.default)
    except (OSError, ValueError):
        raise IngestError("Could not read '%s' as an email message." % name) from None
    lines = []
    for label, header in (("Subject", "Subject"), ("From", "From"), ("Date", "Date")):
        value = message.get(header)
        if value:
            lines.append("%s: %s" % (label, str(value).strip()))
    body = _email_body(message)
    if lines and body:
        lines.append("")
    lines.append(body)
    pages = [{"page": 1, "text": normalize_text("\n".join(lines))}]
    return {"pages": pages, "needs_ocr": False}


# --- PDF text and OCR ------------------------------------------------------


def _rasterizer_available():
    """True when a PDF page rasterizer is installed (never imported here)."""
    return _module_installed("pymupdf") or _module_installed("fitz")


def _rasterizer():
    """Import the optional PyMuPDF rasterizer, or return ``None``."""
    for module_name in ("pymupdf", "fitz"):
        if not _module_installed(module_name):
            continue
        try:
            return importlib.import_module(module_name)
        except ImportError:
            continue
    return None


def _page_image(path, page_number, Image):
    """A PIL image of one PDF page, or ``None`` when none can be produced.

    PyMuPDF renders any page when the ``layout`` extra is installed (most
    reliable). Without it, the scan image embedded in a textless page is used
    instead: pypdf can hand back the page's image objects, which is exactly the
    picture OCR needs for a scanned page. Both paths are lazy and optional.
    """
    module = _rasterizer()
    if module is not None:
        document = None
        try:
            document = module.open(os.fspath(path))
        except Exception:
            document = None
        if document is not None:
            try:
                pixmap = document.load_page(page_number - 1).get_pixmap(dpi=200)
                return Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples
                )
            except Exception:
                pass
            finally:
                try:
                    document.close()
                except Exception:
                    pass
    if not _module_installed("pypdf"):
        return None
    try:
        import io

        import pypdf

        reader = pypdf.PdfReader(os.fspath(path))
        images = list(reader.pages[page_number - 1].images)
    except Exception:
        return None
    for image_file in images:
        try:
            return Image.open(io.BytesIO(image_file.data)).convert("RGB")
        except Exception:
            continue
    return None


def _ocr_page(path, page_number):
    """OCR one PDF page; returns ``""`` when the OCR tools cannot produce text.

    OCR is entirely optional: pytesseract runs Tesseract (``ocr`` extra) on an
    image of the page rendered by :func:`_page_image`. Failures return an empty
    string so the caller can decide between ``needs_ocr`` and
    :class:`OcrRequired`.
    """
    if not _module_installed("pytesseract") or not _module_installed("PIL"):
        return ""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    image = _page_image(path, page_number, Image)
    if image is None:
        return ""
    try:
        return normalize_text(pytesseract.image_to_string(image))
    except Exception:
        return ""


def _ocr_message(path, page_number, ocr):
    """A user-facing explanation of why a page needs OCR and what to do."""
    name = _display_name(path)
    if ocr_available():
        if ocr is True:
            hint = (
                "OCR is installed but could not read this page; check that "
                "Tesseract can read the scan."
            )
        else:
            hint = "OCR is available, so pass ocr=True to read scanned pages."
        return "'%s' page %d has no extractable text; %s" % (name, page_number, hint)
    return (
        "'%s' page %d has no extractable text; install peekseed[ocr] "
        "(pytesseract + Tesseract) to read scanned documents."
        % (name, page_number)
    )


def _extract_pdf(path, *, ocr=None):
    """PDF text layer via pypdf, with OCR gating for scanned pages."""
    pages = extract_pdf(os.fspath(path))
    textless = [item["page"] for item in pages if _textless(item["text"])]
    if not textless:
        return {"pages": pages, "needs_ocr": False}
    most_textless = len(textless) * 2 > len(pages)
    if ocr is not False and ocr_available():
        for page_number in textless:
            text = _ocr_page(path, page_number)
            if text:
                pages[page_number - 1]["text"] = text
    remaining = [item["page"] for item in pages if _textless(item["text"])]
    if remaining and (most_textless or ocr is True):
        raise OcrRequired(
            _ocr_message(path, remaining[0], ocr),
            file=_display_name(path),
            page=remaining[0],
        )
    return {"pages": pages, "needs_ocr": bool(remaining)}


# --- optional office/tables extractors -------------------------------------


def _install_message(path, extra, dependency, purpose):
    """The user-facing "this extra is not installed" message for one format."""
    return (
        "Reading '%s' requires the %s extra. Install peekseed[%s] (%s) "
        "to read %s." % (_display_name(path), extra, extra, dependency, purpose)
    )


def _extract_docx(path, *, ocr=None):
    """Word ``.docx`` via python-docx (office extra): paragraphs and tables."""
    name = _display_name(path)
    try:
        import docx
    except ImportError:
        raise IngestError(
            _install_message(path, "office", "python-docx", "Word documents")
        ) from None
    try:
        document = docx.Document(os.fspath(path))
    except Exception:
        raise IngestError(
            "Could not read '%s' as a Word document; the file may be corrupt "
            "or password-protected." % name
        ) from None
    lines = []
    for paragraph in document.paragraphs:
        text = str(paragraph.text or "")
        if text.strip():
            lines.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            line = "\t".join(cell for cell in cells if cell)
            if line:
                lines.append(line)
    pages = [{"page": 1, "text": normalize_text("\n".join(lines))}]
    return {"pages": pages, "needs_ocr": False}


def _extract_pptx(path, *, ocr=None):
    """PowerPoint ``.pptx`` via python-pptx (office extra): one page per slide."""
    name = _display_name(path)
    try:
        import pptx
    except ImportError:
        raise IngestError(
            _install_message(path, "office", "python-pptx", "PowerPoint presentations")
        ) from None
    try:
        presentation = pptx.Presentation(os.fspath(path))
    except Exception:
        raise IngestError(
            "Could not read '%s' as a PowerPoint presentation; the file may be "
            "corrupt or password-protected." % name
        ) from None
    pages = []
    for page_number, slide in enumerate(presentation.slides, start=1):
        lines = []
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for paragraph in shape.text_frame.paragraphs:
                text = str(paragraph.text or "")
                if text.strip():
                    lines.append(text)
        pages.append({"page": page_number, "text": normalize_text("\n".join(lines))})
    return {"pages": pages, "needs_ocr": False}


def _extract_xlsx(path, *, ocr=None):
    """Excel ``.xlsx`` via openpyxl (tables extra): one page per worksheet."""
    name = _display_name(path)
    try:
        import openpyxl
    except ImportError:
        raise IngestError(
            _install_message(path, "tables", "openpyxl", "spreadsheets")
        ) from None
    try:
        workbook = openpyxl.load_workbook(
            os.fspath(path), read_only=True, data_only=True
        )
    except Exception:
        raise IngestError(
            "Could not read '%s' as a spreadsheet; the file may be corrupt or "
            "password-protected." % name
        ) from None
    try:
        pages = []
        for page_number, sheet in enumerate(workbook.worksheets, start=1):
            lines = []
            for row in sheet.iter_rows(values_only=True):
                cells = [
                    str(value) for value in row if value is not None and str(value).strip()
                ]
                if cells:
                    lines.append("\t".join(cells))
            pages.append(
                {"page": page_number, "text": normalize_text("\n".join(lines))}
            )
        return {"pages": pages, "needs_ocr": False}
    finally:
        try:
            workbook.close()
        except Exception:
            pass


# --- backend registry and discovery ----------------------------------------


def _normalise_extension(extension):
    """Normalise one declared extension to its lower-case ``.ext`` form."""
    value = str(extension or "").strip().lower()
    if not value:
        raise IngestError("An extractor must declare a non-empty file extension.")
    if not value.startswith("."):
        value = "." + value
    if value == ".":
        raise IngestError("An extractor must declare a non-empty file extension.")
    return value


class _Backend:
    """One way to turn a set of file extensions into ordered text pages.

    Core backends, optional backends, user registrations and entry-point plugins
    all use this shape, so :func:`get_extractor` can return any of them.
    """

    def __init__(self, extensions, extract, identity, available=None):
        self.extensions = tuple(_normalise_extension(ext) for ext in extensions)
        self.extract = extract
        self._identity = identity
        self._available = available or (lambda: True)

    def identity(self):
        """The provenance record for this backend: ``{"name", "version"}``."""
        value = self._identity() if callable(self._identity) else self._identity
        return contracts.extractor_identity(
            value.get("name"), value.get("version") or "unknown"
        )

    def available(self):
        """True when this backend's dependencies are installed right now."""
        return bool(self._available())


def _static_identity(name, version=_FORMAT_VERSION):
    """An identity callable for a stdlib extractor with a fixed version."""
    return lambda: {"name": name, "version": version}


def _optional_identity(distribution):
    """An identity callable for an optional package, version read lazily."""

    def identity():
        return {
            "name": distribution,
            "version": _distribution_version(distribution) or "unknown",
        }

    return identity


def _optional_available(import_name):
    """An availability check for an optional package, without importing it."""
    return lambda: _module_installed(import_name)


def _pdf_identity():
    """The PDF extractor identity: ``pypdf`` when it is the active parser."""
    try:
        import pypdf
    except ImportError:
        pass
    else:
        return {
            "name": "pypdf",
            "version": getattr(pypdf, "__version__", None) or "unknown",
        }
    module = _rasterizer()
    if module is not None:
        return {
            "name": "pymupdf",
            "version": getattr(module, "__version__", None) or "unknown",
        }
    return {"name": "pypdf", "version": "unknown"}


def _pdf_available():
    """PDF extraction works when pypdf or the optional PyMuPDF is installed."""
    return _module_installed("pypdf") or _rasterizer_available()


_CORE_BACKENDS = (
    _Backend((".pdf",), _extract_pdf, _pdf_identity, _pdf_available),
    _Backend((".md", ".markdown"), _extract_markdown, _static_identity("markdown")),
    _Backend((".html", ".htm", ".xhtml"), _extract_html, _static_identity("html")),
    _Backend((".txt", ".text"), _extract_text, _static_identity("text")),
    _Backend((".epub",), _extract_epub, _static_identity("epub")),
    _Backend((".eml",), _extract_email, _static_identity("eml")),
)

_OPTIONAL_BACKENDS = (
    _Backend(
        (".docx",),
        _extract_docx,
        _optional_identity("python-docx"),
        _optional_available("docx"),
    ),
    _Backend(
        (".pptx",),
        _extract_pptx,
        _optional_identity("python-pptx"),
        _optional_available("pptx"),
    ),
    _Backend(
        (".xlsx",),
        _extract_xlsx,
        _optional_identity("openpyxl"),
        _optional_available("openpyxl"),
    ),
)

_EXTENSION_MAP = {
    extension: backend
    for backend in _CORE_BACKENDS + _OPTIONAL_BACKENDS
    for extension in backend.extensions
}
_REGISTERED = {}
_REGISTERED_BACKENDS = []
_ENTRY_POINTS_LOADED = False


def _wrap_backend(extractor):
    """Normalise any registered backend object/function into a :class:`_Backend`."""
    if isinstance(extractor, type):
        extractor = extractor()
    declared_api = getattr(extractor, "api", None)
    if declared_api is not None and declared_api != EXTRACTOR_API:
        raise IngestError(
            "An extractor backend targets interface version %s, but this "
            "peekseed build supports version %d."
            % (declared_api, EXTRACTOR_API)
        )
    extensions = getattr(extractor, "extensions", None)
    if not extensions:
        raise IngestError("An extractor backend must declare its 'extensions'.")
    call = getattr(extractor, "extract", None)
    if callable(extractor) and not callable(call):
        call = extractor
    if not callable(call):
        raise IngestError(
            "An extractor backend must provide extract(path, *, ocr=None)."
        )
    declared = getattr(extractor, "identity", None)
    if declared is not None:
        identity = declared
    else:
        name = (
            getattr(extractor, "name", None)
            or getattr(extractor, "EXTRACTOR_NAME", None)
            or getattr(call, "__name__", "custom")
        )
        version = (
            getattr(extractor, "version", None)
            or getattr(extractor, "EXTRACTOR_VERSION", None)
            or "unknown"
        )
        identity = lambda: {"name": str(name), "version": str(version)}
    return _Backend(extensions, call, identity)


def register_extractor(extractor):
    """Register a third-party extraction backend for one or more extensions.

    ``extractor`` is an object (or a class, or a callable function) with
    ``extensions`` (extensions such as ``(".foo",)``), ``name`` and ``version``
    attributes and an ``extract(path, *, ocr=None)`` method. It returns either a
    result dictionary (``{"pages": [...], "needs_ocr": bool}``) or a list of
    ``{"page": int, "text": str}`` records. Registering an extension that a core
    backend already handles replaces that backend for the rest of the process.

    Registration is process-local. A package can instead publish a backend in
    the ``peekseed.extractors`` entry-point group, which is discovered lazily by
    :func:`get_extractor` and :func:`available_extractors`.
    """
    backend = _wrap_backend(extractor)
    for extension in backend.extensions:
        _REGISTERED[extension] = backend
    _REGISTERED_BACKENDS.append(backend)
    return backend


def _load_entry_points():
    """Load ``peekseed.extractors`` entry points once, lazily.

    A broken optional plugin is skipped: extraction of core formats must keep
    working whatever else is installed. Importing this module never triggers the
    lookup; the first :func:`get_extractor` or :func:`available_extractors` call
    does.
    """
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    try:
        found = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # Python < 3.10 returns a group -> entry point mapping
        try:
            found = importlib.metadata.entry_points().get(ENTRY_POINT_GROUP, ())
        except Exception:
            return
    except Exception:
        return
    for entry_point in found:
        try:
            register_extractor(entry_point.load())
        except Exception:
            continue


def get_extractor(path):
    """The backend that reads ``path``; raises :class:`IngestError` if none does."""
    _load_entry_points()
    extension = _extension(path)
    backend = _REGISTERED.get(extension) or _EXTENSION_MAP.get(extension)
    if backend is None:
        raise IngestError(
            "Unsupported file type '%s'; supported formats are %s."
            % (extension or "unknown", _CORE_FORMAT_LIST)
        )
    return backend


# --- public interface ------------------------------------------------------


def extractor_identity_for(path):
    """The provenance identity of the backend that would read ``path``.

    Identity is format-based (a parser name and version), so this does not read
    the file; it is safe to call before hashing or extraction. The extension must
    be supported, otherwise :class:`IngestError` is raised.
    """
    return get_extractor(path).identity()


def cache_key(sha256, extractor):
    """The extraction-cache filename for one content hash: ``"<sha256>.json"``.

    One cache file per content hash keeps a moved or renamed document's
    extraction reusable. The extractor identity is stored *inside* the cache
    document (not in the file name), so a reader must compare it with
    :func:`extractor_identity_for` and treat a mismatch as a miss; the
    ``extractor`` argument is validated here to catch malformed identities early.
    """
    value = str(sha256 or "").strip().lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise contracts.ContractError(
            "An extraction cache key needs a 64-character hex sha256."
        )
    contracts.extractor_label(extractor)
    return value + ".json"


def available_extractors():
    """Sorted labels (``"name@version"``) of the extractors usable right now.

    Core text formats are always listed; PDF appears only when a parser is
    installed, and office/tables formats only when their extra is installed.
    Third-party entry-point backends are discovered (lazily) and included.
    """
    _load_entry_points()
    labels = set()
    for backend in _CORE_BACKENDS + _OPTIONAL_BACKENDS + tuple(_REGISTERED_BACKENDS):
        if backend.available():
            labels.add(contracts.extractor_label(backend.identity()))
    return sorted(labels)


def ocr_available():
    """True when scanned pages can be OCR'd on this machine.

    Requires pytesseract and Pillow (``ocr`` extra) plus a Tesseract executable
    on ``PATH``, and a way to turn a PDF page into an image: pypdf extracting the
    embedded scan image, or PyMuPDF (``layout`` extra) for a full render. Every
    check is lazy and only probes module metadata: no optional package is
    imported.
    """
    if not _module_installed("pytesseract") or not _module_installed("PIL"):
        return False
    if not (_module_installed("pypdf") or _rasterizer_available()):
        return False
    return shutil.which("tesseract") is not None


def _clean_pages(raw_pages, identity):
    """Validate a backend's page records and normalise their text."""
    label = contracts.extractor_label(identity)
    if not isinstance(raw_pages, (list, tuple)):
        raise IngestError("The %s extractor returned no page list." % label)
    pages = []
    for item in raw_pages:
        if not isinstance(item, dict):
            raise IngestError("The %s extractor returned an invalid page record." % label)
        number = item.get("page")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise IngestError("The %s extractor returned an invalid page number." % label)
        pages.append(
            {"page": number, "text": normalize_text(str(item.get("text") or ""))}
        )
    pages.sort(key=lambda item: item["page"])
    return pages


def extract(path, *, ocr=None):
    """Extract one file into ordered, normalised text pages.

    Returns ``{"pages": [{"page": 1-based int, "text": str}, ...], "extractor":
    {"name": str, "version": str}, "pages_count": int, "needs_ocr": bool}``.

    ``ocr`` controls scanned PDF pages: ``None`` (the default) uses OCR when the
    tools are installed, ``True`` requires it, and ``False`` never runs it. A
    document that is mostly textless raises :class:`OcrRequired` naming the first
    unreadable page; a document with only a few textless pages is returned with
    ``needs_ocr=True`` and empty text for those pages.

    The output is deterministic for a given file and extractor version, which is
    what makes the content-addressed extraction cache sound.
    """
    target = os.fspath(path)
    display = _display_name(target)
    if not os.path.isfile(target):
        raise IngestError("File not found: '%s'." % display)
    backend = get_extractor(target)
    identity = backend.identity()
    try:
        result = backend.extract(target, ocr=ocr)
    except IngestError:
        raise
    except Exception:
        raise IngestError(
            "Could not read '%s' with the %s extractor."
            % (display, contracts.extractor_label(identity))
        ) from None
    if isinstance(result, dict):
        raw_pages = result.get("pages")
        needs_ocr = bool(result.get("needs_ocr"))
    elif isinstance(result, (list, tuple)):
        raw_pages = result
        needs_ocr = False
    else:
        raise IngestError(
            "The %s extractor returned no pages." % contracts.extractor_label(identity)
        )
    pages = _clean_pages(raw_pages, identity)
    return {
        "pages": pages,
        "extractor": identity,
        "pages_count": len(pages),
        "needs_ocr": needs_ocr,
    }
