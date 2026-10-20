# Model links

peekseed ships no model. A *link* is one configured endpoint. Links are stored
in `<data root>/config/links.json`, edited from **Settings** in the UI (or seeded
from `PEEKSEED_*` environment variables on first run).

## Schema

```json
{
  "version": 1,
  "links": [
    {
      "id": "my-embed",
      "role": "embed",
      "label": "My embedder",
      "protocol": "openai",
      "base_url": "http://127.0.0.1:11434/v1",
      "api_key": "",
      "model": "nomic-embed-text",
      "enabled": true
    }
  ]
}
```

- `role` is `embed`, `rerank` or `answer`; it cannot change after creation.
- `id` is a slug (`^[a-z0-9][a-z0-9._-]{0,39}$`), generated from the label when
  omitted, and cannot change.
- `protocol` is role-specific: `embed` accepts `openai` or `custom`; `rerank`
  accepts `custom`; `answer` accepts `openai`.
- `model` is required for `embed` links because it identifies the vector space.
  For `answer` it is an optional default; the UI can also pick a model from the
  provider's `/models` list per question.
- At most one `embed` link and one `rerank` link can be enabled. Any number of
  `answer` links can be enabled; the UI treats them as a picker.

Validation is strict on purpose: a bad base URL, a key containing whitespace or
control characters, an unknown field, or a duplicate id is rejected before it is
written. A corrupt file is reported (HTTP 500) and the service treats "no model"
as the normal off state instead of falling back to a guess.

## API keys

Keys are write-only. `GET /api/links` returns `api_key_set: true|false` and never
the value. `PUT /api/links/{id}` with the key omitted keeps the stored key; an
empty string clears it. The file is written atomically with mode 0600.

## Endpoint contracts

### OpenAI-compatible embedding

```
POST {base_url}/embeddings
{"model": "<link model>", "input": ["text one", "text two"]}

200 {"data": [{"embedding": [0.01, ...]}, ...], "model": "..."}
```

Set `base_url` to the API root that `/embeddings` hangs off. Many local servers
and gateways use a `/v1` suffix; the link stores exactly what you enter.

### Custom embedding

```
POST {base_url}/embed
{"inputs": [{"text": "text one"}, {"text": "text two"}]}

200 {"embeddings": [[0.01, ...], ...]}
```

This covers servers that offer a simple text-array contract (including
multimodal servers that accept a text-only input). The link's `model` still
names the model for index identity.

### Reranker

```
POST {base_url}/rerank
{"query": {"text": "the query"},
 "documents": [{"text": "doc one"}, {"text": "doc two"}]}

200 {"scores": [0.9, 0.2]}
```

Scores align with `documents`; higher is better. A reranker is optional: with no
enabled rerank link, retrieval uses reciprocal-rank fusion alone.

### Answer model

```
POST {base_url}/chat/completions
{"model": "...", "messages": [...], "stream": true,
 "temperature": 0.2, "max_tokens": 4096,
 "stream_options": {"include_usage": true}}
```

The client understands standard OpenAI SSE (`choices[0].delta.content`), optional
`reasoning_content`, tool-call deltas and a final usage chunk. Any mistakes are
reported as a short, non-secret detail string.

## Seeding from the environment

Optional, for headless setups; the UI is the normal path.

```bash
PEEKSEED_EMBED_BASE=http://host:port/v1
PEEKSEED_EMBED_PROTOCOL=openai          # openai | custom
PEEKSEED_EMBED_MODEL=nomic-embed-text
PEEKSEED_EMBED_API_KEY=...

PEEKSEED_RERANK_BASE=http://host:port   # protocol is always custom
PEEKSEED_RERANK_MODEL=
PEEKSEED_RERANK_API_KEY=...

PEEKSEED_ANSWER_BASE=http://host:port/v1
PEEKSEED_ANSWER_MODEL=llama3.1
PEEKSEED_ANSWER_API_KEY=...
```

Bad values are not written: the server logs a warning and starts with no seeded
link for that role, so the user can fix it in Settings.
