"""Generic, deterministic chunker: PDF pages -> ``chunks.jsonl`` records.

The chunker is a pure function (no file or network access) so the ingest pipeline
and unit tests can exercise it on hand-written pages.

Headings are detected from generic typographic signals -- Markdown hashes,
numbered headings ("1.2.3 Title"), ALL CAPS lines and short title-case lines
without terminal punctuation. A heading starts a new section and maintains a
breadcrumb stack; the breadcrumb becomes the first line of ``doc`` (the text that
is embedded and BM25-scored), which keeps ancestor titles out of the body where
they would inflate term frequencies for every descendant.

Chunks are sized between ``target_chars`` and ``max_chars``: paragraph
boundaries are preferred, sentence boundaries are used for oversized paragraphs,
and a word-safe hard split is the last resort. ``text`` is the reader view and
keeps ``[p.N]`` markers when a chunk spans pages, so a fact stays citable to its
page.

Heading-only chunks are intentionally not emitted (``heading_only`` is always
False): a section heading with no body of its own is already represented by the
breadcrumb of its first content chunk, and keeping the record set free of
content-less rows keeps search results useful.

When the caller knows the source's content hash (the normal case for the
workspace scan) it passes ``source_sha256`` and every record gets a
content-derived id from :func:`peekseed.contracts.chunk_id`: adding file B to a
collection can never renumber file A's chunks, which is what makes incremental
embedding safe. Without a hash the historical ``"<dataset>-<NNNNNN>"`` ids are
kept, so callers that concatenate several sources with ``id_start`` behave
exactly as before.
"""

import re

from ..contracts import CHUNKER_VERSION, chunk_id

__all__ = ["chunk_pages"]

# Markdown ATX heading: 1-6 leading hashes, title, optional closing hashes.
_MD_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
# Numbered heading: "2", "2.1", "2.1.3" (up to six levels) then a title.
_NUMBERED_HEADING = re.compile(
    r"^ {0,3}(\d{1,2}(?:\.\d{1,2}){0,5})\.?[ \t]+(\S.{0,158})$"
)
# A standalone page number such as "12", "Page 12" or "12 of 40".
_PAGE_NUMBER = re.compile(
    r"^(?:page[ \t]*)?\d{1,4}(?:[ \t]*(?:of|/)[ \t]*\d{1,4})?$",
    re.IGNORECASE,
)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])[ \t]+")
_TERMINAL = ".!?;:,"
_CONNECTORS = frozenset(
    (
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "or",
        "over",
        "the",
        "to",
        "under",
        "via",
        "vs",
        "with",
    )
)
# Words that mark an ALL CAPS line as a top-level division rather than a
# subsection; generic enough to work on any long-form document.
_CHAPTER_WORDS = re.compile(r"^(chapter|part|section|appendix|book)\b", re.IGNORECASE)


def _collapse(text):
    return " ".join(str(text or "").split())


def _norm(text):
    return _collapse(text).casefold()


def _clean_title(text):
    value = _collapse(text)
    return re.sub(r"[ \t]*#{2,}[ \t]*$", "", value).strip()


def _ends_with_terminal(text):
    stripped = str(text or "").rstrip()
    return bool(stripped) and stripped[-1] in _TERMINAL


def _is_heading_title(text):
    value = str(text or "").strip()
    if not value or len(value) > 160 or len(value.split()) > 18:
        return False
    if not value[0].isalnum() or value[0].islower():
        return False
    return sum(1 for char in value if char.isalpha()) >= 2


def _is_all_caps(text):
    value = str(text or "")
    return (
        bool(value)
        and value.upper() == value
        and any(char.isupper() for char in value)
        and sum(1 for char in value if char.isalpha()) >= 2
    )


def _looks_title_case(text):
    words = str(text or "").split()
    if not words:
        return False
    if len(words) == 1:
        return words[0][:1].isupper()
    capitals = sum(1 for word in words if word[:1].isupper())
    return capitals >= len(words) * 0.6 or all(
        word[:1].isupper() or word.lower() in _CONNECTORS for word in words
    )


def _dataset_slug(value):
    text = re.sub(r"[^a-z0-9._-]+", "-", _collapse(value).lower()).strip("-._")
    return text or "dataset"


def _normalize_sha256(value):
    """The lowercase 64-hex hash in ``value``; empty means "no content id".

    A caller that passes a malformed hash gets a clear error instead of ids the
    validator would later reject: the id is a data contract, not a guess.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("source_sha256 must be a 64-character hex sha256")
    return text


def _ordered_pages(pages):
    """Normalise page input to ``[(page_number, text)]`` sorted by page number."""
    ordered = []
    for index, item in enumerate(pages or []):
        if isinstance(item, dict):
            raw_page = item.get("page", index + 1)
            text = item.get("text") or ""
        else:
            raw_page, text = index + 1, ""
        try:
            page = int(raw_page)
        except (TypeError, ValueError):
            page = index + 1
        if page < 1:
            page = index + 1
        ordered.append((page, str(text)))
    ordered.sort(key=lambda entry: entry[0])
    return ordered


def _paragraph_text(lines):
    """Join the visual lines of one paragraph into a single reader-friendly line."""
    return _collapse(" ".join(line.strip() for line in lines if line.strip()))


def _hard_split(text, max_chars):
    """Split a word-safe prefix of at most ``max_chars`` characters repeatedly."""
    pieces = []
    remaining = str(text or "").strip()
    while len(remaining) > max_chars:
        cut = remaining.rfind(" ", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = max_chars
        piece = remaining[:cut].rstrip()
        if piece:
            pieces.append(piece)
        remaining = remaining[cut:].lstrip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _split_oversized(text, max_chars):
    """Split one oversized paragraph at sentence boundaries, then at whitespace."""
    sentences = [part.strip() for part in _SENTENCE_BREAK.split(str(text)) if part.strip()]
    if not sentences:
        sentences = [str(text)]
    pieces = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(_hard_split(sentence, max_chars))
            continue
        if current and len(current) + 1 + len(sentence) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current = (current + " " + sentence).strip() if current else sentence
    if current:
        pieces.append(current)
    return pieces or [str(text)]


def _classify_heading(line, *, prev_blank, next_blank, at_page_start):
    """Return ``{"level", "numbering", "title"}`` for a heading line, else None.

    ``level`` is an explicit outline depth for unambiguous headings and None for
    title-case headings, whose depth is inferred from the surrounding section.
    """
    text = _collapse(line)
    if not text:
        return None
    match = _MD_HEADING.match(str(line).strip())
    if match:
        title = _clean_title(match.group(2))
        if title:
            return {"level": len(match.group(1)), "numbering": "", "title": title}
    match = _NUMBERED_HEADING.match(str(line).strip())
    if match:
        number, title_text = match.group(1), _clean_title(match.group(2))
        if title_text and _is_heading_title(title_text) and not _ends_with_terminal(title_text):
            # The section label keeps its number ("2.1 Overview"); `numbering`
            # stays separate so the engine can sort/group by outline depth.
            return {
                "level": min(number.count(".") + 1, 6),
                "numbering": number,
                "title": "%s %s" % (number, title_text),
            }
    if (
        _is_all_caps(text)
        and _is_heading_title(text)
        and not _ends_with_terminal(text)
        and (prev_blank or next_blank or at_page_start)
    ):
        return {
            "level": 1 if _CHAPTER_WORDS.match(text) else None,
            "numbering": "",
            "title": _clean_title(text),
        }
    if (
        prev_blank
        and len(text) <= 80
        and not _ends_with_terminal(text)
        and _is_heading_title(text)
        and _looks_title_case(text)
    ):
        return {"level": None, "numbering": "", "title": _clean_title(text)}
    return None


def _is_running_header(line, stack):
    """A page-top line that repeats an ancestor title is a running header."""
    key = _norm(line)
    return any(_norm(entry["title"]) == key for entry in stack)


def _pack_blocks(blocks, target_chars, max_chars):
    """Group paragraph blocks into chunks of roughly target/max character size."""
    pieces = []
    for block in blocks:
        text = block["text"]
        if len(text) <= max_chars:
            pieces.append(
                {"text": text, "page": block["page"], "block": block["block"]}
            )
        else:
            for part in _split_oversized(text, max_chars):
                pieces.append(
                    {"text": part, "page": block["page"], "block": block["block"]}
                )

    groups = []
    current = []
    current_length = 0
    for piece in pieces:
        length = len(piece["text"])
        joined = current_length + (2 if current else 0) + length
        if current and (
            joined > max_chars
            or (current_length >= target_chars and joined > target_chars)
        ):
            groups.append(current)
            current = []
            current_length = 0
            joined = length
        current.append(piece)
        current_length = joined
    if current:
        groups.append(current)
    return groups


def _make_record(context, group, dataset, source, number):
    body = "\n\n".join(piece["text"] for piece in group)
    pages = sorted({int(piece["page"]) for piece in group})
    page_start, page_end = pages[0], pages[-1]
    path = context["path"]
    doc = (path + "\n\n" + body) if path else body
    if len(pages) > 1:
        # Reader view: mark every page transition so a fact stays citable.
        parts = []
        previous = None
        for piece in group:
            page = int(piece["page"])
            if page != previous:
                parts.append("[p.%d]\n%s" % (page, piece["text"]))
                previous = page
            else:
                parts.append(piece["text"])
        text = "\n\n".join(parts)
    else:
        text = body
    return {
        "id": "%s-%06d" % (dataset, number),
        "dataset": dataset,
        "source": source,
        "page_start": page_start,
        "page_end": page_end,
        "section": context["section"],
        "chapter": context["chapter"],
        "numbering": context["numbering"],
        "parent": context["parent"],
        "path": path,
        "doc": doc,
        "text": text,
        "heading_only": False,
    }


def _make_content_record(
    context, group, dataset, source, source_sha256, chunk_index
):
    """One record whose id is derived from the source content and position.

    ``block_index`` is the 0-based index of the chunk's first constituent block
    within the source (not within its section), and ``chunk_index`` the 0-based
    index of the chunk within the same source, so the pair is stable when other
    files are added or a later section grows.
    """
    record = _make_record(context, group, dataset, source, chunk_index + 1)
    block_index = int(group[0]["block"])
    record["id"] = chunk_id(source_sha256, block_index, chunk_index)
    record.update(
        {
            "source_sha256": source_sha256,
            "block_index": block_index,
            "chunk_index": chunk_index,
            "chunker_version": CHUNKER_VERSION,
            "ordinal": chunk_index + 1,
        }
    )
    return record


def chunk_pages(
    pages,
    *,
    target_chars=1200,
    max_chars=2000,
    dataset_id="",
    source="",
    id_start=1,
    source_sha256=""
):
    """Turn extracted pages into deterministic ``chunks.jsonl`` records.

    ``pages`` is ``[{"page": int, "text": str}, ...]`` (as returned by
    :func:`peekseed.ingest.pdf.extract_pdf`); extra keys are ignored. The result
    follows the data-contract record schema:
    ``{id, dataset, source, page_start, page_end, section, chapter, numbering,
    parent, path, doc, text, heading_only}``.

    ``source`` names the PDF the pages came from and is optional. ``id_start``
    lets a caller concatenate several PDFs into one dataset while keeping ids
    sequential and unique; ids are ``"<dataset_id>-<6 digits>"`` and
    ``dataset_id`` defaults to ``"dataset"`` when empty.

    ``source_sha256`` switches to content-derived ids: pass the source file's
    64-hex sha and each record also carries ``source_sha256``, ``block_index``,
    ``chunk_index``, ``chunker_version`` and ``ordinal``, with ``id`` from
    :func:`peekseed.contracts.chunk_id`. The ids of one source then never change
    because another file was added to the collection. A malformed hash is a
    ``ValueError``: inventing an id would silently break the content-id contract.

    The function does no file I/O and is deterministic: the same pages and
    arguments always produce the same records.
    """
    try:
        target = int(target_chars)
        maximum = int(max_chars)
        first_id = int(id_start)
    except (TypeError, ValueError):
        raise ValueError("target_chars, max_chars and id_start must be integers")
    if target <= 0 or maximum <= 0:
        raise ValueError("target_chars and max_chars must be positive")
    if maximum < target:
        # An inconsistent pair must never produce a chunk larger than the bound.
        maximum = target
    first_id = max(1, first_id)
    content_sha = _normalize_sha256(source_sha256)

    dataset = _dataset_slug(dataset_id)
    source_name = str(source or "")
    ordered = _ordered_pages(pages)

    chunks = []
    stack = []
    blocks = []
    next_id = first_id
    next_block = 0
    next_chunk = 0
    paragraph = []
    paragraph_page = 1
    previous_top_heading = ""

    def context():
        if not stack:
            return {
                "section": "",
                "chapter": "",
                "numbering": "",
                "parent": "",
                "path": "",
            }
        titles = [entry["title"] for entry in stack]
        return {
            "section": stack[-1]["title"],
            "chapter": stack[0]["title"],
            "numbering": stack[-1]["numbering"],
            "parent": stack[-2]["title"] if len(stack) > 1 else "",
            "path": " / ".join(titles),
        }

    def flush_paragraph():
        nonlocal next_block
        if not paragraph:
            return
        text = _paragraph_text(paragraph)
        paragraph.clear()
        if text:
            blocks.append({"text": text, "page": paragraph_page, "block": next_block})
            next_block += 1

    def flush_section():
        nonlocal next_id, next_chunk
        if not blocks:
            return
        current = context()
        groups = _pack_blocks(list(blocks), target, maximum)
        blocks.clear()
        for group in groups:
            if content_sha:
                chunks.append(
                    _make_content_record(
                        current, group, dataset, source_name, content_sha, next_chunk
                    )
                )
                next_chunk += 1
            else:
                chunks.append(
                    _make_record(current, group, dataset, source_name, next_id)
                )
                next_id += 1

    for page_number, page_text in ordered:
        lines = page_text.split("\n")
        paragraph_page = page_number
        at_page_start = True
        page_top_heading = None
        for index, raw_line in enumerate(lines):
            line = raw_line.strip()
            if not line:
                flush_paragraph()
                continue
            at_start = at_page_start
            at_page_start = False
            if _PAGE_NUMBER.match(line):
                flush_paragraph()
                continue
            if at_start and _is_running_header(line, stack):
                continue
            previous_blank = at_start or not lines[index - 1].strip()
            next_blank = index + 1 >= len(lines) or not lines[index + 1].strip()
            heading = _classify_heading(
                line,
                prev_blank=previous_blank,
                next_blank=next_blank,
                at_page_start=at_start,
            )
            if heading is not None:
                key = _norm(heading["title"])
                if at_start and page_top_heading is None:
                    if previous_top_heading and key == previous_top_heading:
                        # Same title at the top of consecutive pages: running header.
                        page_top_heading = key
                        continue
                    page_top_heading = key
                flush_paragraph()
                flush_section()
                level = heading["level"]
                if level is None:
                    level = stack[-1]["level"] + 1 if stack else 1
                level = max(1, min(int(level), 6))
                while stack and stack[-1]["level"] >= level:
                    stack.pop()
                stack.append(
                    {
                        "level": level,
                        "numbering": heading["numbering"],
                        "title": heading["title"],
                    }
                )
                continue
            if not paragraph:
                paragraph_page = page_number
            paragraph.append(line)
        flush_paragraph()
        previous_top_heading = page_top_heading or ""

    flush_section()
    return chunks
