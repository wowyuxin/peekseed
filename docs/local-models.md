# Local model processes (optional)

peekseed does not ship or supervise a model: every model is a *link* the user
configures (see [model-links.md](model-links.md)). On a machine that runs its
embedder or reranker as a local process, though, it is convenient to start and
stop that process from the same UI that shows whether it is running. This page
documents that **optional** feature.

It is off by default. Without a config file, no command can run, and every
endpoint below behaves as "no local models on this machine".

## The config file

Machine-local, read-only, never committed and never written by the API:

```
<data_root>/config/local_models.json        # 0600
```

`PEEKSEED_LOCAL_MODELS_FILE` overrides the path. The data root is the same one
that owns `links.json` (see [workspace.md](workspace.md)). A missing file is a
supported state; a malformed one is reported as HTTP 500 rather than silently
ignored.

Schema (version 1):

```json
{
  "version": 1,
  "stages": {
    "embed": {
      "label": "Embedding model",
      "script": "/abs/path/serve-embedder.sh",
      "port_env": "PORT",
      "health_url": "http://127.0.0.1:8000/health",
      "models": [
        {"id": "Qwen3-VL-Embedding-2B-8bit-mlx", "label": "Qwen3-VL 2B - 8bit"},
        {"id": "Qwen3-VL-Embedding-8B-8bit-mlx", "label": "Qwen3-VL 8B - 8bit"}
      ],
      "start_timeout": 300,
      "stop_timeout": 60
    },
    "rerank": {
      "label": "Reranker",
      "script": "/abs/path/serve-reranker.sh",
      "port_env": "RERANK_PORT",
      "health_url": "http://127.0.0.1:8001/health"
    }
  }
}
```

| Field | Required | Meaning |
|---|---|---|
| `script` | yes | Absolute path to an executable serve script. It is called with one action word: `start` or `stop`; for the embed stage, a model id may follow `start`. The script decides how to daemonize, write its pidfile and wait for health. |
| `health_url` | yes | The exact loopback `http(s)` URL probed (with an explicit port). It is used as-is -- a link whose `base_url` carries a path (e.g. `/v1`) still probes this URL. The process is considered running when `GET <health_url>` succeeds. |
| `port_env` | no | Environment variable the script reads its port from (default `PORT` for embed, `RERANK_PORT` for rerank). It is set to the port of the link's `base_url`. |
| `label` | no | Display label (default "Embedding model" / "Reranker"). |
| `models` | embed only | The embedding models the script can serve: `{id, label}`. `id` is passed as the model argument and must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`. |
| `start_timeout` / `stop_timeout` | no | Seconds, 10-900 / 5-300. Defaults 300 / 60. |

Trust boundary: the file must be a **regular file owned by the service user**,
not writable by group or others (mode `0600` is the norm), and must live in a
directory that is not writable by group or others. It is opened with
`O_NOFOLLOW` and re-checked with `fstat`, so swapping it for a symlink (or a
different file) between the check and the read is refused. The serve script
gets the same check (regular file, service-user owner, not group/other
writable, executable, safe parent directory) at load time **and again before
every execution**; a script whose inode changed since load is refused. A
config that exists but cannot be trusted is reported on the control-status
stages (`start_stop: false`, detail explains) instead of failing the poll;
start/stop calls return `500`.

## Which links are locally controllable

A link is controllable when *all* of these hold:

- its role is `embed` or `rerank` (answer links are remote endpoints);
- its role has an entry in `local_models.json`;
- its `base_url` host is loopback (`127.0.0.1`, `localhost` or `::1`) and its
  port equals the stage's `health_url` port.

That last rule is what keeps a remote link -- or a local link pointed at a
different port -- from being handed a command. `GET /api/control-status`
reports `capabilities.start_stop` from this same check, so a UI never offers a
button that would be rejected.

## API

- `GET /api/control-status` -- one poll for a control panel: stage readouts
  (`running`, `model`, `label`, `detail`, `link_id`, `base_url`, `enabled`,
  `capabilities`), the configured embedding models, masked links and the answer
  state.
- `POST /api/links/{id}/start` -- run the stage's script with `start`
  (body may be `{"model": "<id>"}` for embed). Returns the resulting stage
  readout; `400` when the link is not locally controllable or the model is not
  configured, `502` when the script fails (last output line, with the service
  token redacted) or when a stop script exits 0 but the health URL still
  answers.
- `POST /api/links/{id}/stop` -- run the script with `stop` and wait briefly for
  health to drop.

Both POSTs are mutating `/api/*` requests, so they need the service token when
`PEEKSEED_TOKEN` is set (see [security.md](security.md)). Only scripts named in
the config file are ever invoked, always as an argument vector -- never through
a shell -- so link data cannot become a command. The child gets a minimal
environment (PATH/HOME/LANG/TMPDIR plus the port), never the service's own
variables, so script output cannot echo service secrets into an API error. The child gets a minimal
environment (PATH/HOME/LANG/TMPDIR plus the port), never the service's own
variables, so script output cannot echo service secrets into an API error.

## Notes

- The config is re-read per request (cached by path + mtime + size + inode),
  so edits apply without restarting the service.
- Starting a model is a blocking admin action: the request stays open until the
  script exits (bounded by `start_timeout`).
- "Off" is a supported state, exactly like an unset link: a stopped stage is
  skipped, not an error.
- peekseed does not start models at boot. If you need that, manage it outside
  peekseed (launchd, a shell profile, a manual start) and use this feature only
  for on-demand control.
