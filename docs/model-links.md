# Model links

peekseed ships no model. A *link* is one configured endpoint. Links are stored
in `$PEEKSEED_HOME/config/links.json` and edited from **Settings** in the UI.
A link never stores an API key: it names the environment variable that holds one
(`api_key_env`), and the key itself lives only in `deploy/env/peekseed.env`.

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
      "api_key_env": "",
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
- `api_key_env` is optional. When set it must match
  `^PEEKSEED_[A-Z0-9_]+_API_KEY$`; anything else (for example `PEEKSEED_TOKEN`
  or `HOME`) is rejected.
- At most one `embed` link and one `rerank` link can be enabled. Any number of
  `answer` links can be enabled; the UI treats them as a picker.

Validation is strict on purpose: a bad base URL, a non-matching `api_key_env`,
an unknown field, or a duplicate id is rejected before it is written. A corrupt
file is reported (HTTP 500) and the service treats "no model" as the normal off
state instead of falling back to a guess.

## API keys

Keys are never stored in `links.json` and never entered in the UI. Put the key
in `deploy/env/peekseed.env` as `PEEKSEED_<NAME>_API_KEY` and set the link's
`api_key_env` to that name. The key is read from the environment at request
time, so rotating it only needs a service restart.

`GET /api/links` returns `api_key_env` and `api_key_set: true|false`, never the
value. `POST`/`PUT /api/links` with a non-empty `api_key` is rejected with a
400 that says to use the env file. The links file is written atomically with
mode 0600.

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

