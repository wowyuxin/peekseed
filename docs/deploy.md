# Run peekseed on a server

This guide deploys peekseed 0.1.0 as a **single-user** service on a machine that
stays on: a home server, a NAS or a Mac mini. It covers installing a pinned
release, running it as a process, exposing it over a private network, backups,
upgrades, rollback and a smoke check. It is not a guide for multi-tenant or
internet-facing use.

If you only want peekseed on your own laptop, start with [install.md](install.md).
This page assumes you can already create the two data folders and configure model
links.

## Topology

```
phone / laptop browser
        │  TLS (Tailscale Serve, Caddy, nginx, …)
        ▼
  127.0.0.1:8030  peekseed  ──►  model endpoints (loopback, LAN or tailnet)
        │
        ├── /srv/peekseed/livefield/.peekseed/   private data (local disk)
        ├── /srv/peekseed/livefield/shared/      sources/ + peels/ (local disk)
        └── /srv/peekseed/deploy/env/peekseed.env  0600 configuration
```

The rules that keep this deployment safe and simple:

1. **Bind loopback.** `PEEKSEED_HOST=127.0.0.1`. Never bind `0.0.0.0`: the app
   has no TLS and no accounts.
2. **Terminate TLS in front of it** — an overlay network (Tailscale Serve) or a
   reverse proxy (Caddy, nginx, …).
3. **Keep the data folders on local disk**, outside any code checkout and not on
   NFS/SMB or a file-sync service.
4. **One app process per home/shared pair.** Two servers sharing the same folders
   are not supported (the dataset lock protects one writer, not two app
   processes).
5. **Set the token and Host allowlist before exposing** the app beyond loopback.
6. **Models are links.** peekseed bundles no models; run your own endpoint or
   point at an API you control. Links are configured in the UI; a link names the
   environment variable that holds its key ([model-links.md](model-links.md)).

## Deployment checklist

- [ ] Create the directory tree and install Python 3.11+.
- [ ] Install a pinned release into an isolated venv; record version + hash.
- [ ] Write a 0600 env file with absolute `PEEKSEED_HOME`/`PEEKSEED_SHARED_DIR`,
      loopback bind, token and allowed hosts.
- [ ] `peekseed init` + `peekseed doctor`; first foreground start and `/health`.
- [ ] Run it as a background process (manual or an optional service manager).
- [ ] Configure model links; run one upload → index → search → ask cycle.
- [ ] Expose through Tailscale or a reverse proxy; re-test through that URL.
- [ ] Take a backup and **restore it into a scratch directory** as a drill.
- [ ] Record the upgrade/rollback procedure and the current pin.

## 1. Prepare the machine

- Linux or macOS, Python **3.11+** (the suite runs on 3.11–3.14), a POSIX shell.
- Disk for the uploaded PDFs (stored once in the shared sources pool) plus the
  derived index (roughly the extracted text, not a second copy of the PDFs); no
  database server is required.
- The default install needs no system libraries. Optional `peekseed[ocr]` needs
  Tesseract, and OCR is a 0.2.0 workflow.
- Run as your normal user or a dedicated service user. That user must own the
  venv, the data folders and the env file; a dedicated user without a login shell
  needs a service manager to start the app.

```bash
# Example layout; adjust paths and user to your host.
sudo mkdir -p /srv/peekseed/livefield/.peekseed /srv/peekseed/livefield/shared
sudo chown "$USER" /srv/peekseed
```

Do not put the data folders on a synced or network filesystem: rename-atomic
writes, lock files and 0600 stored sources assume a normal local filesystem.

## 2. Install a pinned release

```bash
python3 -m venv /srv/peekseed/venv
/srv/peekseed/venv/bin/pip install --upgrade pip
/srv/peekseed/venv/bin/pip install 'peekseed==0.1.0'
/srv/peekseed/venv/bin/peekseed version   # peekseed 0.1.0
```

**Release artifact.** Install the exact released version (`pip install
'peekseed==0.1.0'`). If the host cannot reach PyPI, build the same commit and
install the wheel or sdist from this repository:

```bash
python -m build --wheel             # or: pip wheel . --no-deps -w dist
/srv/peekseed/venv/bin/pip install dist/peekseed-*.whl
```

Record what you installed, and keep the lock next to it for reproducibility:

```bash
/srv/peekseed/venv/bin/pip freeze > /srv/peekseed/requirements.lock
sha256sum dist/peekseed-*.whl > dist/SHA256SUMS
```

A production install is a **released artifact**, never an editable checkout. Fix
bugs upstream, cut a release, then bump the pin ([release.md](release.md)).

## 3. Configure the environment

Generate a token (mutating API calls require it; reads stay open):

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Create `/srv/peekseed/deploy/env/peekseed.env`, mode **0600**:

```bash
install -d -m 700 /srv/peekseed/deploy/env
install -m 600 /dev/null /srv/peekseed/deploy/env/peekseed.env
```

```dotenv
# --- data folders ----------------------------------------------------------
PEEKSEED_HOME=/srv/peekseed/livefield/.peekseed
PEEKSEED_SHARED_DIR=/srv/peekseed/livefield/shared

# --- service ---------------------------------------------------------------
PEEKSEED_HOST=127.0.0.1
PEEKSEED_PORT=8030
PEEKSEED_TOKEN=<paste the generated token>
PEEKSEED_ALLOWED_HOSTS=peekseed.example.com,localhost,127.0.0.1
# PEEKSEED_ENV_FILE=/srv/peekseed/deploy/env/peekseed.env   # if cwd is elsewhere

# --- optional: research mode ----------------------------------------------
# PEEKSEED_AGENT=1

# --- optional: peelfeed, the extraction engine ----------------------------
# PEELFEED_HOME=/srv/peekseed/livefield/.peelfeed
# PEELFEED_SHARED_DIR=/srv/peekseed/livefield/shared   # must equal PEEKSEED_SHARED_DIR
# PEEKSEED_PEELFEED_PYTHON=/srv/peekseed/livefield/.peelfeed/env/venv/bin/python
# PEEKSEED_PEELFEED_REPO=/srv/peekseed/peelfeed

# --- optional: local model control ----------------------------------------
# PEEKSEED_LOCAL_MODELS_FILE=/srv/peekseed/deploy/env/local_models.json
```

Notes:

- A real environment variable wins over the env file. The CLI loads
  `PEEKSEED_ENV_FILE` when set, otherwise `.env` from the working directory and
  then the project root.
- For a systemd `EnvironmentFile=`, keep the file to `KEY=VALUE` lines and
  full-line `#` comments (no inline comments after values).
- The env file holds the service token **and every model API key**. A link names
  the variable (`api_key_env`); `links.json` (0600) holds endpoints only. Neither
  the env file nor `links.json` belongs in git or in chat/logs.
- `PEELFEED_HOME` and `PEELFEED_SHARED_DIR` are required by peelfeed itself;
  peekseed forces `PEELFEED_SHARED_DIR` to its own `PEEKSEED_SHARED_DIR` when it
  calls the CLI, so both sides see the same `sources/` and `peels/`.
- The UI asks for the token once: open `/#token=<token>` (the fragment never
  reaches the server) or paste it in **Settings → Service token**. A reverse
  proxy can instead inject an `Authorization: Bearer …` header server-side.

## 4. Initialize and first start

The CLI reads the env file as defaults; export it for manual commands (a service
manager loads it for the background process):

```bash
cd /srv/peekseed
set -a; . deploy/env/peekseed.env; set +a
/srv/peekseed/venv/bin/peekseed init      # create the home/shared tree
/srv/peekseed/venv/bin/peekseed doctor    # paths, permissions, links, schemas
```

`init` prints every resolved path and writes the empty 0600 link store; `doctor`
checks the two folders, their modes, the link store, the data schemas and the
peelfeed handshake. Then start it in the foreground for the first check:

```bash
/srv/peekseed/venv/bin/peekseed serve
curl -fsS http://127.0.0.1:8030/health    # {"ok":true,"datasets":0}
```

Open <http://127.0.0.1:8030>, configure an embedding link (and a chat link if you
want Ask/Research) in **Settings**, and run one upload → index → search cycle.
Stop the foreground server with Ctrl-C before starting the background process.

## 5. Run it as a background process

peekseed does **not** register itself with the OS. You can run it manually — the
simplest option for a single-user box — or under a service manager if you want
boot survival and restart-on-failure.

### Manual mode

```bash
cd /srv/peekseed
set -a; . deploy/env/peekseed.env; set +a
mkdir -p "$PEEKSEED_HOME/logs"
nohup venv/bin/peekseed serve >>"$PEEKSEED_HOME/logs/serve.log" 2>&1 &
echo $! > "$PEEKSEED_HOME/logs/serve.pid"
curl -fsS http://127.0.0.1:8030/health

# stop
kill "$(cat "$PEEKSEED_HOME/logs/serve.pid")" && rm -f "$PEEKSEED_HOME/logs/serve.pid"
# status
curl -fsS http://127.0.0.1:8030/health || echo "not running"
```

Nothing restarts it after a crash, logout, reboot or sleep. That is acceptable
for a single-user service if you start it when you need it. The repository's
`scripts/serve.sh start|stop|restart|status|logs` is a small convenience wrapper
around the same pattern for a source checkout (pidfile + health wait); it writes
`$PEEKSEED_HOME/logs/peekseed.{pid,log}` and refuses to start when
`PEEKSEED_HOME` is unset.

### Optional: systemd (Linux)

```ini
# /etc/systemd/system/peekseed.service
[Unit]
Description=peekseed
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<the user that owns /srv/peekseed>
Group=<same>
WorkingDirectory=/srv/peekseed
EnvironmentFile=/srv/peekseed/deploy/env/peekseed.env
ExecStart=/srv/peekseed/venv/bin/peekseed serve
Restart=on-failure
RestartSec=5
# Hardening: tighten per host. A strict ProtectSystem= needs ReadWritePaths for
# every directory a local-model serve script writes to (pid files, logs, …).
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now peekseed
systemctl status peekseed
journalctl -u peekseed -f
```

### Optional: launchd (macOS)

A per-user LaunchAgent is the macOS equivalent. Keep `KeepAlive` off if you want
manual starts; set it to `true` for restart-on-failure. `WorkingDirectory` lets
the CLI find a `.env`; the deployment's settings live in
`deploy/env/peekseed.env`, so point `PEEKSEED_ENV_FILE` at it:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.peekseed</string>
  <key>ProgramArguments</key>
  <array>
    <string>/srv/peekseed/venv/bin/peekseed</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>/srv/peekseed</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PEEKSEED_ENV_FILE</key>
    <string>/srv/peekseed/deploy/env/peekseed.env</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>/srv/peekseed/livefield/.peekseed/logs/serve.log</string>
  <key>StandardErrorPath</key><string>/srv/peekseed/livefield/.peekseed/logs/serve.log</string>
</dict>
</plist>
```

Install it as
`~/Library/LaunchAgents/com.example.peekseed.plist` and load it with
`launchctl bootstrap gui/"$(id -u)" ~/Library/LaunchAgents/com.example.peekseed.plist`.
These examples are optional starting points, not a required setup; adjust paths,
user and hardening to your host.

## 6. Expose it safely

### Tailscale Serve (recommended for a private box)

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:8030
tailscale serve status
curl -fsS https://box.example-tailnet.ts.net:8443/health
```

- Add the MagicDNS name (`box.example-tailnet.ts.net`) to
  `PEEKSEED_ALLOWED_HOSTS`, then restart the app.
- Tailnet ACLs decide who can reach the port; keep the ACL narrow.
- Do **not** use `--set-path`: the UI uses root-relative `/api/...` URLs.
- The mapping persists, but the URL returns `502` whenever no process is
  running — that is the expected manual-mode behaviour.

### Reverse proxy with TLS (Caddy)

```caddyfile
peekseed.example.com {
    reverse_proxy 127.0.0.1:8030 {
        flush_interval -1    # do not buffer the NDJSON streams
    }
}
```

### Reverse proxy with TLS (nginx)

```nginx
server {
    listen 443 ssl;
    server_name peekseed.example.com;
    # ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:8030;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;          # SSE/NDJSON: /api/ask|search|agent/stream
        proxy_read_timeout 300s;      # long answers and agent runs
    }
}
```

Then set `PEEKSEED_ALLOWED_HOSTS=peekseed.example.com`, keep
`PEEKSEED_HOST=127.0.0.1`, and restart. Never forward port 8030 directly from
the internet: there is no TLS, no account system and no rate limiting in
peekseed. The token gates writes, not reads; treat the whole app as
single-user-private.

## 7. Verify the deployment (smoke)

Set `BASE` and `TOKEN`, and use any small text PDF as `sample.pdf`.

```bash
BASE=http://127.0.0.1:8030
TOKEN='<the token from the env file>'
AUTH=(-H "Authorization: Bearer $TOKEN")

# 1. liveness
curl -fsS "$BASE/health"

# 2. gates: 401 without a token, 403 for a hostile Origin/Host
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/api/datasets" -F 'name=x'
curl -s -o /dev/null -w '%{http_code}\n' "${AUTH[@]}" \
  -H 'Origin: https://evil.example' -X POST "$BASE/api/datasets" -F 'name=x'
curl -s -o /dev/null -w '%{http_code}\n' "${AUTH[@]}" \
  -H 'Host: evil.example' -X POST "$BASE/api/datasets" -F 'name=x'

# 3. upload a throwaway dataset
curl -sS "${AUTH[@]}" -F 'name=smoke' \
  -F 'files=@sample.pdf;type=application/pdf' "$BASE/api/datasets"
# -> 201 {"dataset": {"id": "smoke", …}}

# 4. build its index, then poll the persisted job record
curl -sS "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{}' "$BASE/api/datasets/smoke/index"
# -> 202 {"job": "job-…", "state": "queued"}
curl -sS "${AUTH[@]}" "$BASE/api/jobs/job-…"
# -> {"state": "succeeded", "progress": {…}} when done

# 5. search
curl -sS "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"dataset":"smoke","query":"green button","topk":5}' "$BASE/api/search"

# 6. citation opens the stored PDF inline (GET; the route has no HEAD)
curl -sS -D - -o /dev/null "$BASE/pdf/smoke/sample.pdf" \
  | grep -i '^content-disposition:'

# 7. grounded answer (needs an answer link); NDJSON, last line is done
curl -sS -N "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"dataset":"smoke","query":"What starts the smoke pump?"}' \
  "$BASE/api/ask/stream"

# 8. data-folder health, dataset list and served paths
/srv/peekseed/venv/bin/peekseed doctor
curl -sS "${AUTH[@]}" "$BASE/api/datasets"
curl -sS "${AUTH[@]}" "$BASE/api/workspace" | head -c 400
```

Delete the smoke dataset afterwards (`DELETE /api/datasets/smoke` with the
token). The expected status codes are part of the contract in
[api.md](api.md); search and ask also run a probe canary and return `409` instead
of comparing vectors from a different embedding space.

Also verify the same checks **through the exposed URL** (Tailscale or proxy),
because Host/Origin and TLS mistakes only show up there.

## 8. Back up

The backup unit is small: the **private home** (`$PEEKSEED_HOME`, without the
venv/caches, logs and upload staging), the **shared sources pool**
(`$PEEKSEED_SHARED_DIR/sources`) and the **env file**
(`deploy/env/peekseed.env`, including model API keys). Peels (`shared/peels`) are
rebuildable by extracting again and are not backed up; the venvs and model caches
are rebuildable too. Keeping the datasets and indexes makes a restore instant.

Take a **consistent** copy: stop the app first (single-user, so the downtime is
short), archive, verify, then start again.

```bash
systemctl stop peekseed      # or: kill "$(cat /srv/peekseed/livefield/.peekseed/logs/serve.pid)"
cd /srv/peekseed
mkdir -p backups
ts=$(date +%Y%m%d-%H%M%S)
tar -czf "backups/peekseed-$ts.tar.gz" \
  --exclude='livefield/.peekseed/env' \
  --exclude='livefield/.peekseed/logs' \
  --exclude='livefield/.peekseed/uploads' \
  livefield/.peekseed livefield/shared/sources deploy/env
sha256sum "backups/peekseed-$ts.tar.gz" > "backups/peekseed-$ts.sha256"
tar -tzf "backups/peekseed-$ts.tar.gz" >/dev/null   # archive is readable
systemctl start peekseed    # or: start it the way you normally do
```

- Keep several generations locally (for example 7 daily, 4 weekly) and copy at
  least one **off the machine** (rsync to another host, restic/object storage).
  A backup that only exists on the same disk is not disaster recovery.
- Encrypt off-site copies (`gpg -c`, restic, …): the archive contains uploaded
  PDFs and API keys.
- **Restore drill** (do this once, and after large changes):

```bash
mkdir -p /tmp/peekseed-restore && cd /tmp/peekseed-restore
tar -xzf /srv/peekseed/backups/peekseed-<ts>.tar.gz
PEEKSEED_PORT=8031 \
PEEKSEED_HOME=/tmp/peekseed-restore/livefield/.peekseed \
PEEKSEED_SHARED_DIR=/tmp/peekseed-restore/livefield/shared \
  /srv/peekseed/venv/bin/peekseed serve
curl -fsS http://127.0.0.1:8031/health
# check the dataset list, one search and one PDF through :8031, then stop it
```

- **Live restore:** stop the app, move the live folders aside (never delete them
  before verifying), extract the archive, fix ownership (`chown -R`), start, run
  the smoke check from section 7.

## 9. Upgrade and rollback

Upgrade procedure (forward-only data migrations):

```bash
# 1. back up first (section 8)
systemctl stop peekseed

# 2. install the new pinned release (or build a new venv alongside and switch)
/srv/peekseed/venv/bin/pip install --upgrade 'peekseed==<new-version>'
/srv/peekseed/venv/bin/peekseed version

# 3. upgrade the data schemas (dry-run first; --apply writes them)
set -a; . deploy/env/peekseed.env; set +a
/srv/peekseed/venv/bin/peekseed migrate
/srv/peekseed/venv/bin/peekseed migrate --apply

# 4. start and smoke
systemctl start peekseed
curl -fsS http://127.0.0.1:8030/health
```

Read [CHANGELOG.md](../CHANGELOG.md) before upgrading and keep the previous
version installed (a separate venv, or the previous wheel) so a code-only
rollback is one command.

**Rollback:**

- **Code-only** (no data-schema change): reinstall the previous version into the
  venv, restart, smoke. Data is untouched.
- **Across a schema change:** restore the pre-upgrade backup from section 8.
  Data schemas are forward-only, so a code downgrade alone would leave newer
  data unreadable; always restore data with the old code.

## 10. Local model processes (optional)

If your embedding or reranking model runs as a local process on the same
machine, peekseed can start and stop it from the UI through its optional local
model control ([local-models.md](local-models.md)). It is off by default: without
the file named by `PEEKSEED_LOCAL_MODELS_FILE`, no command can run. A deployment
keeps it at `deploy/env/local_models.json`:

```dotenv
PEEKSEED_LOCAL_MODELS_FILE=/srv/peekseed/deploy/env/local_models.json
```

Requirements, in short: the config is a 0600 regular file owned by the service
user in a directory that is not group/other-writable; each referenced serve
script passes the same checks, is executable, and is re-validated before every
execution; the link's `base_url` must be loopback and its port must match the
stage's `health_url`. Start/stop are mutating API calls, so the service token
applies. peekseed does not start models at boot — for that, use a service manager
or a login hook outside peekseed.

If starting a local model fails with `Operation not permitted`, the peekseed
process was started from a restricted/sandboxed shell and cannot write the model
process's pid files. Start peekseed from a normal shell as the user that owns the
model scripts, or from the service manager configured with that user.

## 11. Security checklist

- [ ] `PEEKSEED_HOST=127.0.0.1`; no `0.0.0.0` listener.
- [ ] `PEEKSEED_TOKEN` set, unique, and only in the 0600 env file; rotate by
      replacing it and restarting (browsers need the new value).
- [ ] `PEEKSEED_ALLOWED_HOSTS` contains only names you actually use.
- [ ] TLS terminates at Tailscale or the reverse proxy; port 8030 is not exposed.
- [ ] Firewall allows only the proxy/overlay port; the box is single-user.
- [ ] Data folders 0700; `links.json`, stored PDFs and the env file
      0600; backups encrypted off-site.
- [ ] No untrusted users on the machine; no public internet exposure.
- [ ] You have read [security.md](security.md) (threat model, upload limits,
      SSRF notes, `/pdf` inline).

## 12. Known limitations (0.1.0)

- **Single user.** No accounts, tenants, roles or audit log; no rate limiting.
- **No built-in TLS.** Loopback bind plus an overlay/proxy is the supported
  pattern.
- **Manual mode is the default.** Without a service manager there is no
  crash recovery, no restart after logout/reboot, and macOS sleep pauses the
  process.
- **PDF-only upload path.** Folder scanning (`POST /api/scan` and a CLI scan UX)
  and OCR are 0.2.0 features; uploads are the only ingestion path in 0.1.0.
  The office/tables/pymupdf extras exist but belong to the 0.2.0 workflow.
- **No page-image evidence layer.** Figure/table questions use extracted text
  only; page images and table grids are planned for 0.2.0.
- **Ask citations are not ledger-validated.** The `[n]` numbers in the model's
  text are not verified; the numbered excerpt chips are the authoritative links.
  Research mode does audit citations against its ledger.
- **No model lifecycle at boot.** Links are configured endpoints; local model
  start/stop is an on-demand optional feature, not a supervisor.
- **Windows is untested.** The suite and helper script assume POSIX.

## 13. Troubleshooting

| Symptom | Check / fix |
|---|---|
| Tailnet/proxy returns `502` | No process is running (manual mode). Start it or use a service manager; check `/health` on loopback. |
| `401` on upload/search/ask | The service token is set but the browser has none. Provision it once with `/#token=…` or **Settings → Service token**; scripts must send `Authorization: Bearer …`. |
| `401` after a token rotation | Restart the app after editing the env file, and update every browser/proxy that stores the token. |
| `403` through the proxy/tailnet | The `Host` does not match `PEEKSEED_ALLOWED_HOSTS` (or `Origin` is hostile). Add the exact `host[:port]`, make the proxy forward the original `Host`, then restart. |
| Streams stop mid-answer | The proxy buffers or times out. Disable buffering (`proxy_buffering off`, Caddy `flush_interval -1`) and raise the read timeout for `/api/ask|search|agent/stream`. |
| `409` from search/ask | Embedding space changed (link id/model) or the endpoint no longer reproduces the probe. Rebuild the index with the current embed link. |
| `409` from Build index | Another job holds the writer lock; wait, cancel it, or resume an interrupted job. Stale locks are detected. |
| Server is up but data looks missing | A schema migration was not applied after an upgrade. Run `peekseed migrate` (dry-run), then `--apply`, with a backup taken first. |
| `Operation not permitted` on local-model start/stop | The app was started from a restricted shell and cannot write model pid files. Start it as the owning user from a normal shell or via the service manager. |
| `peekseed doctor` reports data-folder problems | Check that `PEEKSEED_HOME` and `PEEKSEED_SHARED_DIR` are absolute existing directories, owned by the service user, mode 0700, on local disk; then check the link store (0600) and the data schemas. |
| `PEEKSEED_HOME is not set` (or `PEEKSEED_SHARED_DIR`) | The env file is not loaded: pass `PEEKSEED_ENV_FILE=…` or source `deploy/env/peekseed.env` before the command; the service manager's `EnvironmentFile=` must point at it. |
| Slow or inconsistent I/O | The data folders are probably on a network/synced filesystem. Stop the service, copy them to local disk, update `PEEKSEED_HOME`/`PEEKSEED_SHARED_DIR`, and start again. |

## 14. Where to go next

- [install.md](install.md) — installation on a laptop and the first dataset.
- [api.md](api.md) — routes, stream events and job records used by the smoke
  check above.
- [security.md](security.md), [local-models.md](local-models.md),
  [workspace.md](workspace.md) — the contracts behind this guide.
- [release.md](release.md), [CHANGELOG.md](../CHANGELOG.md) — version policy,
  upgrade and no-downgrade rules.

The repository ships documentation, not a parameterised deployment script
bundle; `scripts/serve.sh` is a convenience helper for a source checkout.
Parameterised deployment templates are a 0.2.0 consideration.
