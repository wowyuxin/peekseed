#!/usr/bin/env python
"""The research loop: the model drives its own retrieval, in steps.

The one-shot path gives the model ONE shot at evidence it did not choose: a fixed
top-N result set, packed once, with no way to act on it. If the answer needs a
section the retriever ranked eleventh — or the second half of a chunk a cap cut —
the answer cannot be produced, however capable the model is.

This module runs the alternative. The model is given the same references, now
labelled as HINTS, plus tools that read the dataset itself (tools.py), and it
loops: call a tool, read what comes back, decide again, answer. Everything the
one-shot path does well is kept — the deterministic first-pass retrieval is still
the seed, so a question the seed already answers costs exactly one model call.

What is pinned here, and why each is not optional:

  * **The observation ledger.** Every piece of evidence — the seed references AND
    every tool result — gets one stable number, and the model is told to cite that
    number. The ledger is what the page renders as links, so a citation can always
    be resolved back to a chunk, a page and a document. A citation the model
    invents is detectable because it is not in the ledger.
  * **Budgets.** Steps, tool calls, wall clock and observation size are all
    capped. A research loop with no ceiling is a way to spend a GPU for an hour
    and return nothing.
  * **The loop breaker.** Handed a bare ``grep``, a model tends to rewrite one
    regex into another until its budget runs out and never read a hit. An
    identical call is answered from the ledger instead of re-run, and consecutive
    steps that add no new evidence force the answer.
  * **The forced answer.** When a budget is reached the model is asked once more
    with ``tool_choice="none"`` (tools still declared: an empty ``tools`` array is
    HTTP 400 on some servers) and told to answer from what it read and to name
    what it could not verify. Ending with a blank screen is never the right
    failure.

The loop is transport-free by design: ``chat`` is a callable the service supplies,
so the loop is driven in tests by a scripted generator and the streaming parsing
stays in the one client that has it. This module imports neither the HTTP client
nor the config layer.
"""
import json
import os
import re
import time

try:                                        # normal package import
    from .prompts import (AGENT_ANSWER_INSTRUCTION, AGENT_FORCE_ANSWER,
                          AGENT_SYSTEM_PROMPT, agent_forced_answer_note,
                          agent_system_prompt)
    from .tools import ToolError
except ImportError:                         # flat-module import (script/tests)
    from prompts import (AGENT_ANSWER_INSTRUCTION, AGENT_FORCE_ANSWER,
                         AGENT_SYSTEM_PROMPT, agent_forced_answer_note,
                         agent_system_prompt)
    from tools import ToolError


# One citation form, accepted everywhere: [7]. Deliberately the same shape the
# one-shot prompt asks for, so the page's citation renderer needs no second mode.
#
# The leading guard is not decoration. Documents use bracketed subscripts
# (``field[0]``, ``field[3:0]``) as ordinary notation, and a live run was reported
# as citing a non-existent source [0] because of it. A citation never begins
# immediately after a word character, a closing bracket or a dot.
_CITE_RE = re.compile(r"(?<![\w\].])\[(\d{1,3})\]")

# The sentence boundary used by the repetition guard, and the note the loop
# reports instead of streaming the loop out.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
LOOP_NOTE = ("the model began repeating itself — generation stopped; what was "
             "written before the repetition is kept")

# Below this many characters of seed text, the model is told the references are
# not enough to answer from. Set at "less than one result's snippet" rather than
# at a round number: the page's own result set carries a few hundred characters
# per reference and the server substitutes the whole chunk, so a real seed never
# trips this — a measured loop with a 52-character seed did.
THIN_SEED_CHARS = 250

# A repeated sentence must be at least this long to count as a loop. Short
# strings repeat legitimately in documents — every row of a table can say
# "Reserved", and an answer quoting the same identifier five times is correct,
# not looping — so the guard only reacts to substantial text repeating many
# times in a row.
LOOP_MIN_SENTENCE = 20
LOOP_SENTENCE_RUN = 6
LOOP_MIN_LINE = 60
LOOP_LINE_RUN = 4


class AgentError(Exception):
    """A failure worth showing the user, with the HTTP status it maps to."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class RepetitionGuard:
    """Stop a runaway generation loop, keeping everything written before it.

    Measured with a 52-character seed whose chunk id the loaded index could not
    resolve, the model repeated one sentence about 180 times — a minute of GPU
    for an answer that is pure noise, streamed to the page as if it were an
    answer. Nothing else in the loop notices that, because each delta is
    individually plausible.

    The guard watches the tail of what has been written and trips on substantial
    text repeating in a row: the same sentence ``LOOP_SENTENCE_RUN`` times, or the
    same line ``LOOP_LINE_RUN`` times. It deliberately does NOT use a sampler
    repetition penalty instead: in a table, legitimately repeating an identifier
    is exactly what a correct answer does, and a penalty would fight it.
    Detecting the loop and stopping keeps the prefix — which is often the answer
    up to the point the model lost the thread — and reports what happened.

    Cheap: it re-examines a bounded tail only every ``check_every`` characters.
    """

    def __init__(self, min_sentence=LOOP_MIN_SENTENCE, sentence_run=LOOP_SENTENCE_RUN,
                 min_line=LOOP_MIN_LINE, line_run=LOOP_LINE_RUN, check_every=400):
        self.min_sentence = min_sentence
        self.sentence_run = sentence_run
        self.min_line = min_line
        self.line_run = line_run
        self.check_every = check_every
        self._text = ""
        self._since = 0

    def feed(self, piece):
        """Add a delta; True once the model is repeating itself."""
        self._text += piece or ""
        self._since += len(piece or "")
        if self._since < self.check_every:
            return False
        self._since = 0
        return self.looping()

    def looping(self):
        tail = self._text[-4000:]
        sentences = [s.strip() for s in _SENTENCE_RE.split(tail) if s.strip()]
        if _run_length(sentences, self.sentence_run, self.min_sentence):
            return True
        lines = [l.strip() for l in tail.splitlines() if l.strip()]
        return bool(_run_length(lines, self.line_run, self.min_line))


def _run_length(items, threshold, min_len):
    """True when some item of at least ``min_len`` repeats ``threshold`` times in a row."""
    run = 1
    for i in range(1, len(items)):
        run = run + 1 if items[i] == items[i - 1] else 1
        if run >= threshold and len(items[i]) >= min_len:
            return True
    return False


def _excerpt_label(e):
    """The provenance line for a seed reference, built when the caller left it out.

    ``ask_excerpts`` supplies a label; a raw search row may not, and an evidence
    block without its section/page is much harder for the model to cite.
    """
    label = (e.get("label") or "").strip()
    if label:
        return label
    num = (e.get("numbering") or "").strip()
    sec = (e.get("section") or "").strip()
    name = sec if (num and sec.startswith(num)) else " ".join(p for p in (num, sec) if p)
    text = name or (e.get("chapter") or "").strip()
    if e.get("page_start"):
        page = "p.%s" % e["page_start"]
        text = (text + " · " + page) if text else page
    return text


def build_agent_messages(question, excerpts, note="", tools=None, system_prompt=None):
    """The opening messages: system protocol + the seed references and question.

    The seed's own numbering is reused verbatim. ``ask_excerpts`` already numbered
    the references 1..k, the ledger continues from k+1, and that is what makes a
    citation unambiguous across both — a reference the user ticked and a chunk the
    model found itself are cited the same way, and both resolve to a page.

    Evidence goes in the USER turn, not the system turn, for the same reason the
    one-shot prompt does it: it is where a chat model is trained to read supplied
    documents, and it keeps the system prompt a cacheable prefix.
    """
    seed_chars = sum(len(e.get("text") or "") for e in excerpts or [])
    if not note and seed_chars < THIN_SEED_CHARS:
        # A seed of one 52-character snippet sent the model into a loop that
        # repeated a single sentence ~180 times: it had nothing to answer from and
        # answered anyway. Saying so in the prompt is the cheap half of that fix
        # (the repetition guard is the other).
        note = ("These references are VERY SHORT (%d characters in total) and are not "
                "enough to answer from. Read the relevant sections with the tools "
                "before you answer, and do not quote a reference beyond what it "
                "actually says." % seed_chars)
    blocks = []
    for e in excerpts or []:
        head = "[%s] %s" % (e.get("n"), _excerpt_label(e))
        blocks.append("%s\n%s" % (head.rstrip(), e.get("text") or ""))
    preface = (
        "SEED REFERENCES\n\n"
        "Ranked guesses from a first-pass similarity search over these documents. "
        "They are a STARTING POINT, not the evidence: they may be incomplete, cut "
        "short, or the wrong sections. Verify anything you rely on, and use the "
        "tools to find what they miss before you answer.\n\n")
    if note:
        preface += note.rstrip() + "\n\n"
    tail = "\n\n---\n\nQUESTION\n\n%s\n\n%s" % (question, AGENT_ANSWER_INSTRUCTION)
    body = preface + "\n\n".join(blocks) + tail
    system = system_prompt or agent_system_prompt(tools)
    return [{"role": "system", "content": system},
            {"role": "user", "content": body}]


# Auto-search: no seed, so the model writes the retrieval queries itself. The
# user's wording is often not the document's, which is the main thing a planned
# query fixes.
AUTO_PREFACE = (
    "NO SEARCH HAS BEEN RUN YET — you plan and run the retrieval yourself.\n\n"
    "`search` runs the retrieval pipeline over one dataset: keyword + vector "
    "retrieval, reranked when a reranker is configured.\n"
    "- Turn the question into search queries in the DOCUMENT's vocabulary: its terms, "
    "structure, identifiers and likely section titles. Not the user's phrasing, and "
    "not a question.\n"
    "- For a broad or multi-part question, run 2-3 differently worded searches (or one "
    "per sub-question) in the same step.\n"
    "- If a search finds nothing useful, the wording or the dataset is wrong: "
    "rephrase, or look at what outline and grep show. Never answer from hits you "
    "have not read.\n"
    "- Then read what matters in full (read_chunk, read_section) and grep for exact "
    "terms you discovered.\n"
    "- Stop researching as soon as the evidence answers the question.\n\n")


AUTO_FOLLOWUP_PREFACE = (
    "This is a FOLLOW-UP in an ongoing thread. Earlier turns are context, not "
    "evidence, and their search results are NOT in this prompt. Run the tools "
    "again for anything you need to verify; never answer from an earlier answer "
    "alone.\n\n")


def build_auto_messages(question, dataset, datasets, followup=False, tools=None,
                        system_prompt=None):
    """Opening messages for auto-search: the question and the datasets, no references.

    ``datasets`` is ``[(name, description)]``; ``dataset`` is the user's default.
    ``followup`` is true when a thread prefix precedes this turn: the model must
    not be told "no search has been run yet" while reading an earlier dialog.
    """
    listing = "\n".join("- %s: %s%s" % (name, desc, " (default)" if name == dataset else "")
                        for name, desc in datasets or [])
    preface = AUTO_FOLLOWUP_PREFACE if followup else AUTO_PREFACE
    body = (preface + "DATASETS\n\n" + listing +
            "\n\n---\n\nQUESTION\n\n%s\n\n%s" % (question, AGENT_ANSWER_INSTRUCTION))
    system = system_prompt or agent_system_prompt(tools)
    return [{"role": "system", "content": system},
            {"role": "user", "content": body}]


def merge_tool_call_deltas(acc, deltas):
    """Accumulate the streaming ``tool_calls`` deltas into complete calls.

    A streamed call arrives in pieces: the first delta for an index carries the id
    and the function NAME, later ones carry fragments of the arguments JSON, and
    several calls are interleaved under their own ``index`` (the model routinely
    asks for two searches at once). The name is assigned once but concatenated
    here anyway, because a server that splits it would otherwise produce a
    silently truncated tool name.

    ``acc`` is ``{index: {"id":…, "name":…, "arguments":…}}``, mutated in place and
    returned. Keyed by index rather than by position because the deltas of parallel
    calls interleave.
    """
    for delta in deltas or []:
        if not isinstance(delta, dict):
            continue
        slot = acc.setdefault(int(delta.get("index") or 0),
                              {"id": "", "name": "", "arguments": ""})
        if delta.get("id"):
            slot["id"] = delta["id"]
        fn = delta.get("function") or {}
        if fn.get("name"):
            slot["name"] += fn["name"]
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]
    return acc


def parse_tool_args(raw):
    """``(args, error)`` — a model's arguments are text, and may be malformed."""
    text = (raw or "").strip()
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except ValueError as e:
        return {}, "arguments are not valid JSON (%s): %s" % (e, text[:200])
    if not isinstance(parsed, dict):
        return {}, "arguments must be a JSON object, got %s" % type(parsed).__name__
    return parsed, ""


class AgentBudget:
    """Every ceiling on one research session, as values rather than constants.

    Defaults are deliberately small: a step at a large prompt costs seconds, and a
    forced final answer on a long transcript costs more — so the difference between
    a 6-step and a 20-step ceiling is the difference between a wait and a hang.
    Each of these is a latency/faithfulness knob, not a context limit.

    Environment overrides (all optional):

        PEEKSEED_AGENT_MAX_STEPS        default 6
        PEEKSEED_AGENT_MAX_TOOL_CALLS   default 12
        PEEKSEED_AGENT_WALL_SEC         default 240
        PEEKSEED_AGENT_OBS_CHARS        default 6000
        PEEKSEED_AGENT_BARREN_LIMIT     default 2
    """

    def __init__(self, max_steps=6, max_tool_calls=12, wall_sec=240.0,
                 obs_chars=6000, barren_limit=2):
        self.max_steps = int(max_steps)
        self.max_tool_calls = int(max_tool_calls)
        self.wall_sec = float(wall_sec)
        self.obs_chars = int(obs_chars)
        self.barren_limit = int(barren_limit)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        get = lambda k, d: env[k] if env.get(k) not in (None, "") else d  # noqa: E731
        return cls(max_steps=get("PEEKSEED_AGENT_MAX_STEPS", 6),
                   max_tool_calls=get("PEEKSEED_AGENT_MAX_TOOL_CALLS", 12),
                   wall_sec=get("PEEKSEED_AGENT_WALL_SEC", 240),
                   obs_chars=get("PEEKSEED_AGENT_OBS_CHARS", 6000),
                   barren_limit=get("PEEKSEED_AGENT_BARREN_LIMIT", 2))


class Ledger:
    """The numbered evidence of one session: seed references first, then findings.

    One number per piece of evidence, shared by citations and by the links the page
    renders, so ``[4]`` means the same thing to the model, to the answer text and
    to the reader. Rows keep the shape the one-shot path already produces
    (label/section/page/chunk/pdf_url), because the page's citation renderer and
    its reference cards are keyed on exactly those fields.

    ``link_for(row)`` is an optional injected callable that turns a row into its
    PDF URL (``""`` when none). It may return a string or a ``(url, …)`` tuple.
    """

    def __init__(self, excerpts=None, link_for=None):
        self.rows = []
        self.link_for = link_for
        for e in excerpts or []:
            chunk = e.get("chunk") or e.get("chunk_file") or e.get("id")
            self.rows.append({
                "n": e.get("n") or len(self.rows) + 1,
                "label": _excerpt_label(e), "section": e.get("section") or "",
                "numbering": e.get("numbering") or "", "chapter": e.get("chapter"),
                "page_start": e.get("page_start"), "page_end": e.get("page_end"),
                "chunk": chunk or "", "source": e.get("source") or "seed",
                "kind": e.get("kind") or "seed", "dataset": e.get("dataset") or "",
                "pdf_url": e.get("pdf_url") or "",
            })

    @property
    def next_n(self):
        return (self.rows[-1]["n"] + 1) if self.rows else 1

    def add(self, row):
        """Number one evidence row and return it."""
        n = self.next_n
        row = dict(row)
        row["n"] = n
        if self.link_for is not None:
            try:
                linked = self.link_for(row)
                if isinstance(linked, (tuple, list)):
                    linked = linked[0] if linked else ""
                if linked:
                    row["pdf_url"] = linked
            except Exception:
                pass
        row.setdefault("pdf_url", "")
        self.rows.append(row)
        return row

    def chunk_numbers(self):
        return {r["chunk"]: r["n"] for r in self.rows if r.get("chunk")}

    def citations(self, text):
        """Which ``[n]`` the answer used, and which of those are NOT in the ledger.

        A model can print a plausible citation to nothing. Reporting that is the
        difference between a link that works and a link that looks like it does.
        """
        seen = []
        for n in _CITE_RE.findall(text or ""):
            try:
                value = int(n)
            except ValueError:
                continue
            if value not in seen:
                seen.append(value)
        known = {r["n"] for r in self.rows}
        return {"cited": seen, "unknown": [n for n in seen if n not in known]}

    def event_rows(self):
        """The ledger rows safe to put in a ``done`` event: no observation text."""
        return [{k: r.get(k) for k in
                 ("n", "label", "section", "numbering", "chapter", "page_start",
                  "page_end", "chunk", "source", "kind", "dataset", "pdf_url",
                  "rank", "score") if k in r}
                for r in self.rows]


class AgentSession:
    """One question's research loop, as a generator of page events.

    ``chat(messages, tools, tool_choice)`` yields ``(kind, piece)``:

        ("text", str)             assistant content delta
        ("reasoning", str)        the model's thinking, if it emits any
        ("tool_call_delta", list) raw ``delta.tool_calls`` entries
        ("usage", dict)           token counts
        ("finish", str)           finish_reason

    ``tools`` is a :class:`tools.DatasetTools` (or anything with the same
    ``schemas()`` and ``run(name, args, default_dataset=…)``). The session owns the
    policy — what is evidence, what is a repeat, when to stop — and nothing else.
    """

    def __init__(self, chat, tools, budget=None, dataset="", ledger=None,
                 model="", log=None):
        self.chat = chat
        self.tools = tools
        self.budget = budget or AgentBudget()
        self.dataset = dataset
        self.ledger = ledger or Ledger()
        self.model = model
        self.log = log or (lambda _msg: None)
        self.schemas = tools.schemas()
        self._seen_calls = {}     # canonical call signature -> observation number
        self._t0 = None
        self.steps = 0
        self.tool_calls = 0

    # --- helpers -----------------------------------------------------------
    def _timings(self):
        total = None
        if self._t0 is not None:
            total = round(time.perf_counter() - self._t0, 3)
        return {"total_sec": total, "steps": self.steps,
                "tool_calls": self.tool_calls}

    def _run_tool(self, name, args):
        """``(rows, note)`` from the tool set, tolerating a 2- or 3-tuple result."""
        try:
            outcome = self.tools.run(name, args, default_dataset=self.dataset)
        except TypeError:
            # A tool set ported without the new keyword is still usable; a
            # genuine internal TypeError is caught by the caller and reported.
            outcome = self.tools.run(name, args)
        if isinstance(outcome, dict):
            return outcome.get("rows") or [], outcome.get("note") or ""
        if isinstance(outcome, (tuple, list)):
            if len(outcome) >= 3:
                return (outcome[0] or []), (outcome[2] or "")
            if len(outcome) == 2:
                return (outcome[0] or []), (outcome[1] or "")
        return [], ""

    def _observe(self, name, args, rows, note, error=""):
        """Number this call's evidence and render the observation text.

        The header line names the call, because the model needs to know WHICH
        search produced these numbers when it looks back over a long transcript;
        each row then carries its own number, which is what an answer cites.
        """
        args_text = json.dumps(args, ensure_ascii=False, sort_keys=True)
        if len(args_text) > 300:
            args_text = args_text[:300] + "…"
        head = "%s(%s)" % (name, args_text)
        if error:
            return "[!] %s\nERROR: %s" % (head, error), []
        added = []
        lines = ["[obs] %s → %s" % (head, note or "%d result(s)" % len(rows))]
        for row in rows:
            stamped = self.ledger.add(row)
            added.append(stamped)
            # The chunk id comes FIRST and is labelled, because it is the handle
            # the model has to pass back to read_chunk. Without it the only thing
            # in front of a reader is a human label, and a live run showed exactly
            # that failure: the model passed the label as a chunk_id and lost the
            # round trip.
            where = " · ".join(p for p in (
                ("chunk " + stamped["chunk"]) if stamped.get("chunk") else "",
                stamped.get("dataset"), stamped.get("label")) if p)
            text = (stamped.get("text") or "").strip()
            lines.append("[%d] %s" % (stamped["n"], where))
            if text:
                lines.append(text)
        body = "\n".join(lines)
        if len(body) > self.budget.obs_chars:
            body = body[:self.budget.obs_chars].rstrip() + \
                "\n[… observation truncated; ask for a specific chunk or section to see more]"
        return body, added

    def _forced_answer(self, messages, reason):
        """One last call with the tools still declared but OFF, then finish.

        ``tool_choice`` is "none" rather than an empty ``tools`` array: some
        chat-completion servers answer HTTP 400 to ``"tools": []``, and a 400
        here would throw away everything the session had read.
        """
        yield {"type": "note", "text": reason}
        messages.append({"role": "user",
                         "content": agent_forced_answer_note(reason)})
        finish = None
        usage = None
        text = []
        guard = RepetitionGuard()
        looped = False
        for kind, piece in self.chat(messages, self.schemas, "none"):
            if kind == "text":
                text.append(piece)
                yield {"type": "text", "text": piece}
                if guard.feed(piece):
                    looped = True
                    break
            elif kind == "reasoning":
                yield {"type": "reasoning", "text": piece}
            elif kind == "usage":
                usage = piece
            elif kind == "finish":
                finish = piece
        body = "".join(text)
        if looped:
            yield {"type": "note", "text": LOOP_NOTE}
        # The citation audit has to run on the FORCED answer too, and that is the
        # common case for a research question: this is the answer a budget produced,
        # so leaving `unknown` off here would mean the one answer most likely to
        # contain an unresolvable [n] is the one nobody checks.
        yield {"type": "done", "forced": True, "reason": reason, "chars": len(body),
               "usage": usage, "finish_reason": finish, "repeated": looped,
               "steps": self.steps, "tool_calls": self.tool_calls,
               "truncated": finish == "length", "timings": self._timings(),
               "ledger": self.ledger.event_rows(),
               **self.ledger.citations(body)}

    # --- the loop ----------------------------------------------------------
    def run(self, messages):
        budget = self.budget
        self._t0 = time.perf_counter()
        t0 = self._t0
        barren = 0
        while self.steps < budget.max_steps:
            if time.perf_counter() - t0 > budget.wall_sec:
                for event in self._forced_answer(
                        messages, "wall-clock budget reached — answering from what was read"):
                    yield event
                return
            self.steps += 1
            step = self.steps
            t_step = time.perf_counter()
            yield {"type": "step", "n": step, "status": "start",
                   "tool_calls_left": max(0, budget.max_tool_calls - self.tool_calls)}
            acc = {}
            buffer = []
            streamed = False
            tool_seen = False
            usage = None
            finish = None
            guard = RepetitionGuard()
            looped = False
            for kind, piece in self.chat(messages, self.schemas, "auto"):
                if kind == "tool_call_delta":
                    if not tool_seen:
                        tool_seen = True
                        if streamed:
                            # The model narrated before deciding to call a tool.
                            # That text is not the answer, so it is taken back out
                            # of the answer area and re-emitted into the trace —
                            # otherwise every researched answer would begin with
                            # the model's notes about what it was about to do.
                            yield {"type": "text_retract"}
                            yield {"type": "narration", "text": "".join(buffer)}
                    merge_tool_call_deltas(acc, piece)
                elif kind == "text":
                    if tool_seen:
                        yield {"type": "narration", "text": piece}
                    else:
                        buffer.append(piece)
                        streamed = True
                        yield {"type": "text", "text": piece}
                        if guard.feed(piece):
                            looped = True
                            break
                elif kind == "reasoning":
                    yield {"type": "reasoning", "text": piece}
                elif kind == "usage":
                    usage = piece
                elif kind == "finish":
                    finish = piece
            if looped and not tool_seen:
                # An answer step that started repeating itself: the prefix is kept
                # (it is often the answer up to the point the model lost the
                # thread) and the loop is reported instead of being streamed out.
                text = "".join(buffer)
                yield {"type": "note", "text": LOOP_NOTE}
                yield {"type": "step", "n": step, "status": "done", "finish_reason": finish,
                       "usage": usage, "tool_calls": 0, "added": 0}
                yield {"type": "done", "forced": False, "chars": len(text), "usage": usage,
                       "finish_reason": finish, "truncated": False, "repeated": True,
                       "steps": self.steps, "tool_calls": self.tool_calls,
                       "timings": self._timings(),
                       "ledger": self.ledger.event_rows(),
                       **self.ledger.citations(text)}
                return
            calls = [acc[i] for i in sorted(acc)]
            if not calls:
                # No tool call means this step's text IS the answer: it has already
                # streamed, so the session is done.
                text = "".join(buffer)
                yield {"type": "step", "n": step, "status": "done", "finish_reason": finish,
                       "usage": usage, "tool_calls": 0}
                if not text.strip():
                    # An answer-shaped step with no text: the thinking budget ate it
                    # (finish_reason=length), which is invisible in the transcript
                    # and must not look like a successful empty answer.
                    yield {"type": "note", "text": "the model returned no text "
                           "(finish_reason=%s)" % (finish or "unknown")}
                yield {"type": "done", "forced": False, "chars": len(text), "usage": usage,
                       "finish_reason": finish, "truncated": finish == "length",
                       # Always present, so "no repetition" and "the field is
                       # missing" cannot be confused by a reader of this stream.
                       "repeated": False,
                       "steps": self.steps, "tool_calls": self.tool_calls,
                       "timings": self._timings(),
                       "ledger": self.ledger.event_rows(),
                       **self.ledger.citations(text)}
                return

            # The narration is NOT put back into the transcript. It is the model's
            # process, not evidence, and carrying it forward does two harms: it
            # costs prefill on every later step, and it teaches the model by example
            # that an answer may open with "let me look at…" — which a live run
            # then did. The tool_calls and their observations are the whole turn.
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": c["id"] or "call_%d" % i, "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]}}
                               for i, c in enumerate(calls)]})
            added_total = 0
            for i, call in enumerate(calls):
                call_id = call["id"] or "call_%d" % i
                name = (call["name"] or "").strip()
                args, arg_error = parse_tool_args(call["arguments"])
                yield {"type": "tool_call", "step": step, "call": call_id,
                       "name": name, "args": args, "arg_error": arg_error}
                rows, note, error = [], "", ""
                sec = None
                self.tool_calls += 1
                signature = "%s|%s" % (name, json.dumps(args, sort_keys=True))
                first = self._seen_calls.get(signature)
                if arg_error:
                    error = arg_error
                elif self.tool_calls > budget.max_tool_calls:
                    error = ("tool-call budget reached — answer from the observations "
                             "you already have")
                elif signature in self._seen_calls:
                    # The live probe's failure mode, made harmless: the model
                    # rewrites a regex into a near-identical one forever. An exact
                    # repeat is answered from the transcript instead of re-run.
                    error = ("you already ran this exact call. %s Do not repeat it — "
                             "read one of those results in full, use a different tool, "
                             "or answer." % (
                                 "Its results are at [%s] above." % first if first
                                 else "It returned no results."))
                else:
                    try:
                        # Timed so the page can say what each lookup cost. The tool
                        # is local and sub-millisecond; search may embed, so the
                        # range is real.
                        t_tool = time.perf_counter()
                        rows, note = self._run_tool(name, args)
                        sec = round(time.perf_counter() - t_tool, 4)
                    except ToolError as e:
                        error = str(e)
                    except Exception as e:      # a broken tool must not end the session
                        self.log("tool %s failed: %s: %s" % (name, type(e).__name__, e))
                        error = "the tool failed internally (%s)" % type(e).__name__
                observation, added = self._observe(name, args, rows, note, error)
                if not error:
                    # Remember where this call's evidence starts, so a repeat can be
                    # answered by pointing at it rather than by running it again.
                    self._seen_calls[signature] = added[0]["n"] if added else 0
                added_total += len(added)
                yield {"type": "tool_result", "step": step, "call": call_id, "name": name,
                       "added": len(added), "note": note or error,
                       "error": bool(error), "sec": sec, "args": args,
                       # Same row shape the recorder stores, so the live trace and
                       # the stored log cannot disagree about a row.
                       "rows": [{k: r.get(k) for k in
                                 ("n", "label", "rank", "score", "dataset", "chunk",
                                  "source", "section", "numbering", "chapter",
                                  "page_start", "page_end", "kind", "pdf_url")}
                                for r in added],
                       # Kept for stream-shape compatibility: this project never
                       # attaches rendered pages, so it is always empty.
                       "images": []}
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": observation})
            yield {"type": "step", "n": step, "status": "done", "finish_reason": finish,
                   "usage": usage, "tool_calls": len(calls), "added": added_total,
                   "sec": round(time.perf_counter() - t_step, 4)}
            if added_total == 0:
                barren += 1
                if barren >= budget.barren_limit:
                    for event in self._forced_answer(
                            messages, "%d steps added no new evidence — answering from "
                                      "what was read" % barren):
                        yield event
                    return
                messages.append({"role": "user", "content":
                                 "Those calls added no new evidence. Change approach: read "
                                 "a result you already have in full, open a section, or "
                                 "answer with what you have."})
            else:
                barren = 0
        for event in self._forced_answer(
                messages, "step budget reached (%d) — answering from what was read"
                          % budget.max_steps):
            yield event
