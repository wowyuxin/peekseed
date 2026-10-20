# Architecture

peekseed is one Python package and one configurable data directory. There is no
database, no message broker and no external service: a single FastAPI process
serves the UI and the API, reads datasets from disk, and calls whatever model
endpoints the user configured.

```
            browser (single-file UI)
                     |
                     v
   +----------------- app.py (FastAPI) -----------------+
   |  search | ask | agent | datasets | links | history |
   +----+---------+---------+----------+---------+------+
        |         |         |          |         |
        v         v         v          v         v
    engine.py  answer.py  agent.py   ingest/    links.py
        |         |         |          |         |
        +---------+---------+----------+---------+----> httpx --> user models
                                |
                                v
                         config.py -> <PEEKSEED_DATA>
```

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Resolve the data root and every path under it; settings; bind host/port. |
| `links.py` | Model-link config store: schema, validation, masking, atomic 0600 write. |
| `engine.py` | Pure retrieval: tokenizer, BM25, cosine, reciprocal-rank fusion, optional rerank hook, `DatasetIndex`/`IndexStore`. |
| `answer.py` | OpenAI-compatible streaming chat client and generic grounded-answer prompt builders. |
| `prompts.py` | Generic prompt strings used by the answer and research paths. |
| `agent.py` | Transport-free research loop: budgets, observation ledger, repetition guard, forced answer. |
| `tools.py` | Dataset tools the agent calls: list, outline, search, grep, read chunk/section. |
| `history.py` | Persistent search/dialog history (per-entry JSON + index). |
| `ingest/` | PDF extraction, chunking, embedding and index writing. |
| `app.py` | The HTTP layer that wires all of the above together. |
| `web/index.html` | The single-file UI. |

The dependency direction is intentionally acyclic: `app.py` imports everything;
`engine.py`, `agent.py`, `tools.py`, `history.py` and `ingest/` import no web
framework and no config, so they can be tested with plain files and fakes.
`links.py` and `config.py` are small stdlib-only stores.

## One data root

`PEEKSEED_DATA` (default `./data`) owns every mutable path:

```
<root>/config/links.json       model endpoints + masked keys
<root>/config/settings.json    optional UI copy
<root>/datasets/<id>/          sources, chunks, indexes
<root>/history/                history entries + index
<root>/uploads/                upload staging
```

Nothing outside that root is written at runtime. Code updates, a new clone, or
`pip install --upgrade` cannot touch user data.

## Request lifecycle

**Search.** The API resolves the enabled embed link, finds the `DatasetIndex`
whose `space` equals the link's embedding space, embeds the query, runs cosine +
BM25 + RRF, optionally reranks through the enabled rerank link, and maps the
result rows to citations. If no index exists for the active embedding space the
request fails with a clear "build an index" message rather than comparing
vectors from two different spaces.

**Ask.** The browser sends the question plus the result rows it is looking at
(or none, in which case the server searches first). The server selects an answer
link, builds a grounded prompt with numbered excerpts, streams the model's
response, and records the run in history. Citations are validated against the
ledger: numbers the model invents are reported, not rendered as links.

**Research.** The same model gets tools over the dataset (`search`, `grep`,
`read_chunk`, `read_section`, `outline`, `list_datasets`). The deterministic
search result set is the seed, not the ceiling. Every observation is numbered in
one ledger and the answer cites those numbers.

**Index build.** `POST /api/datasets/{id}/index` starts a background job. The
worker extracts PDF text (or reuses `chunks.jsonl`), chunks it, embeds in
batches through the enabled embed link, and atomically writes
`vectors.npy`/`meta.json`/`info.json`. The index store is reloaded when the job
finishes, so a new dataset becomes searchable without restarting the process.

## Index format

One index per embedding space, written directly (no Chroma, no SQLite):

```
datasets/<id>/index/<space>/
  vectors.npy   float32 [n, dim], unnormalised (normalised at load)
  meta.json     array of chunk metadata, row-aligned with vectors
  info.json     embedding-space identity + provenance
```

The `space` key is `"<link id>__<model id>"` (sanitised). The engine only
compares vectors inside one index, so mixing model dimensions is structurally
impossible; the UI shows a dataset as "needs index" when the active space has no
index yet.

## Security

- Loopback bind by default; no authentication.
- Keys live only in `config/links.json`, mode 0600, masked in every response.
- Uploads are size-capped, type-checked, and served with a path-traversal guard.
- The server never executes shell scripts or starts model processes.
