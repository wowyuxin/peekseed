"""Generic prompt text shared by the one-shot answer path and the research loop.

Everything here is plain strings and small functions: no imports, no model or
document-domain names, so the wording can be reused with any provider and tested
by comparing strings. The system prompts stay constant across turns on purpose —
they are the cacheable prefix of every request — and the phase-specific
instructions live in separate constants that the caller appends to the user turn.
"""

# --- grounded answering ----------------------------------------------------

ASK_SYSTEM_PROMPT = (
    "You are a research assistant answering questions about a set of local "
    "documents.\n"
    "You are given numbered EXCERPTS retrieved from those documents, and a "
    "QUESTION.\n\n"
    "Rules:\n"
    "1. Answer only from the excerpts. Do not use outside knowledge, and do not "
    "invent values, names, identifiers, dates or details that the excerpts do not "
    "contain.\n"
    "2. Cite the excerpt number of every claim, like [3]. Cite several when they "
    "combine.\n"
    "3. If the excerpts do not answer the question, say so plainly and name what "
    "is missing. Do not pad the answer with unrelated excerpt content. A correct "
    "'the excerpts do not cover this' is more useful than a plausible-looking "
    "invention.\n"
    "4. Quote identifiers, names, numbers and units exactly as they appear.\n"
    "5. Be concise. Prefer short paragraphs or a list over an essay. Do not "
    "restate the question or these instructions."
)

# The closing instruction for the common case: answer with inline citations.
ASK_ANSWER_PLAIN = ("Answer the question from the excerpts above, citing them as [n].")

# A stricter variant. Use it when the caller needs every sentence attributed
# (for example when the answer will be shown beside its references): unsupported
# sentence-level citations are the failure mode that matters there, not brevity.
ASK_ANSWER_CITED = (
    "Answer the question from the excerpts above. Attach the excerpt number to "
    "every sentence that relies on one, as [n]. Do not cite an excerpt that does "
    "not support the sentence it is attached to; if a sentence needs no source, "
    "leave it uncited."
)


# --- research loop ---------------------------------------------------------

# The agent's stable protocol. Tool-specific guidance is generated from the
# declared tool list by agent_system_prompt(), so this text never has to name a
# tool that a deployment did not expose.
AGENT_SYSTEM_PROMPT = (
    "You are a research assistant working over a set of local documents. You "
    "have TOOLS that read those documents directly.\n\n"
    "How to work:\n"
    "1. The references you are given first are HINTS from a first-pass similarity "
    "search, not evidence. They may be incomplete, ranked wrongly, or cut short. "
    "Treat them as a place to start and verify anything you rely on.\n"
    "2. Use the exact-match tool for identifiers, names and numbers, and the "
    "search tool for concepts or for a second query built from what you just "
    "learned. Read the surrounding section when you know where you are, and read "
    "a full chunk whenever a hit was cut short.\n"
    "3. Work in short steps. Reading a hit in full beats running another search. "
    "Never issue two near-identical calls: if a pattern found nothing, change the "
    "wording, the target, or the tool.\n"
    "4. Every number, name, identifier and value you state must come from a "
    "reference or a tool result. Never fill a gap from memory or plausibility, "
    "even when the value seems familiar.\n"
    "5. Cite the number of every piece of evidence you use, like [7]; cite "
    "several when they combine.\n"
    "6. If the evidence does not answer the question, say so plainly and name "
    "what is missing. 'The documents do not state this' is a correct and useful "
    "answer; a plausible-looking invention is not.\n"
    "7. Quote identifiers, names, numbers and units exactly as they appear."
)

# Closing instruction for a normal step that ends in prose. Kept out of the
# system prompt so that turn is byte-identical across steps and questions.
AGENT_ANSWER_INSTRUCTION = (
    "Answer the question from the references and tool results above, citing the "
    "number of every piece of evidence you use as [n]. Write for a reader who "
    "cannot see your process: never address yourself or the question ('let me "
    "check', 'the question asks'), never restate the question, and leave your "
    "reasoning out — state the answer, its caveats, and what the evidence does "
    "not establish."
)

# Used once when a budget is reached. Ending with a blank screen is never the
# right failure: the model must answer from what it already read.
AGENT_FORCE_ANSWER = (
    "Your tool budget is exhausted. Answer NOW from the observations above, "
    "citing them as [n]. Do not ask for more searches and do not write your "
    "reasoning out: the final answer only, with the caveats it needs. Say exactly "
    "what you could not verify, and name the source that would have settled it."
)


def _tool_lines(tools) -> str:
    """One line per declared tool: its name and the first line of its description.

    Accepts the OpenAI tool schema (``{"type": "function", "function": {...}}``),
    a bare ``{"name", "description"}`` dict, or a ``(name, description)`` pair,
    so the caller can pass whatever it already builds for the API request.
    """
    lines = []
    for tool in tools or []:
        name = description = ""
        if isinstance(tool, dict):
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = str(fn.get("name") or "")
            description = str(fn.get("description") or "")
        elif isinstance(tool, (tuple, list)) and len(tool) >= 2:
            name, description = str(tool[0]), str(tool[1])
        elif isinstance(tool, str):
            name = tool
        if not name:
            continue
        first = description.strip().splitlines()[:1]
        line = "- " + name
        if first and first[0]:
            line += ": " + first[0].strip()
        lines.append(line)
    return "\n".join(lines)


def agent_system_prompt(tools=None) -> str:
    """The research-loop system prompt, with the tools it was given listed.

    ``tools`` is optional: with no tools the protocol alone is returned. Tool
    descriptions are rendered into the system turn as well as the API schema
    because a model reads a one-line list of what exists more reliably than the
    nested JSON schema it receives separately.
    """
    rendered = _tool_lines(tools)
    if not rendered:
        return AGENT_SYSTEM_PROMPT
    return AGENT_SYSTEM_PROMPT + "\n\nTOOLS AVAILABLE\n\n" + rendered


def agent_forced_answer_note(reason: str = "") -> str:
    """The forced final-answer instruction, optionally naming why the loop ended.

    ``reason`` is a short human-readable cause ("the step budget is exhausted",
    "two steps added no new evidence") appended in parentheses, so the model can
    mention the limit accurately instead of guessing.
    """
    reason = (reason or "").strip()
    if not reason:
        return AGENT_FORCE_ANSWER
    return AGENT_FORCE_ANSWER + "\n\n(" + reason + ")"
