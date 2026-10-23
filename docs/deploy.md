# Run peekseed on a server

This guide deploys peekseed 0.1.0 as a **single-user** service on a machine that
stays on: a home server, a NAS or a Mac mini. It covers installing a pinned
release, running it as a process, exposing it over a private network, backups,
upgrades, rollback and a smoke check. It is not a guide for multi-tenant or
internet-facing use.

If you only want peekseed on your own laptop, start with [install.md](install.md).
This page assumes you can already create a workspace and configure model links.

## Topology

```
phone / laptop browser
        │  TLS (Tailscale Serve, Caddy, nginx, …)
        ▼
  127.0.0.1:8030  peekseed  ──►  model endpoints (loopback, LAN or tailnet)
        │
        ├── /srv/peekseed/workspace/   documents + .peekseed/ (local disk)
        └── /srv/peekseed/.env         0600 configuration
```

The rules that keep this deployment safe and simple:

1. **Bind loopback.** `PEEKSEED_HOST=127.0.0.1`. Never bind `0.0.0.0`: the app
   has no TLS and no accounts.
2. **Terminate TLS in front of it** — an overlay network (Tailscale Serve) or a
   reverse proxy (Caddy, nginx, …).
3. **Keep the workspace on local disk**, outside any code checkout and not on
   NFS/SMB or a file-sync service.
4. **One app process per workspace.** Two servers sharing a workspace are not
   supported (the collection lock protects one writer, not two app processes).
5. **Set the token and Host allowlist before exposing** the app beyond loopback.
6. **Models are links.** peekseed bundles no models; run your own endpoint or
   point at an API you control. Links are configured in the UI or seeded from
   environment variables ([model-links.md](model-links.md)).

## Deployment checklist

- [ ] Create the directory tree and install Python 3.11+.
- [ ] Install a pinned release into an isolated venv; record version + hash.
- [ ] Write a 0600 `.env` with an absolute workspace path, loopback bind, token
      and allowed hosts.
- [ ] `peekseed init` + `peekseed doctor`; first foreground start and `/health`.
- [ ] Run it as a background process (manual or an optional service manager).
- [ ] Configure model links; run one upload → index → search → ask cycle.
- [ ] Expose through Tailscale or a reverse proxy; re-test through that URL.
- [ ] Take a backup and **restore it into a scratch directory** as a drill.
- [ ] Record the upgrade/rollback procedure and the current pin.

## 1. Prepare the machine

- Linux or macOS, Python **3.11+** (the suite runs on 3.11–3.14), a POSIX shell.
- Disk for documents plus the derived index (roughly the extracted text, not a
  second copy of the PDFs); no database server is required.
- The default install needs no system libraries. Optional `peekseed[ocr]` needs
  Tesseract, and OCR is a 0.2.0 workflow.
- Run as your normal user or a dedicated service user. That user must own the
  venv, the workspace and the `.env`; a dedicated user without a login shell
  needs a service manager to start the app.

```bash
# Example layout; adjust paths and user to your host.
sudo mkdir -p /srv/peekseed
sudo chown "$USER" /srv/peekseed
```

Do not put the workspace on a synced or network filesystem: rename-atomic writes,
lock files and 0600 model-link keys assume a normal local filesystem.

## 2. Install a pinned release

```bash
python3 -m venv /srv/peekseed/venv
/srv/peekseed/venv/bin/pip install --upgrade pip
/srv/peekseed/venv/bin/pip install 'peekseed==0.1.0'
/srv/peekseed/venv/bin/peekseed version   # peekseed 0.1.0
```

**Pre-publish note.** Until 0.1.0 is on PyPI (the last step of
[release.md](release.md)), build the wheel or sdist from this repository and
install that file into the venv:

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

Create `/srv/peekseed/.env`, mode **0600**:

```bash
install -m 600 /dev/null /srv/peekseed/.env
```

```dotenv
# --- service ---------------------------------------------------------------
PEEKSEED_WORKSPACE=/srv/peekseed/workspace
PEEKSEED_HOST=127.0.0.1
PEEKSEED_PORT=8030
PEEKSEED_TOKEN=<paste the generated token>
PEEKSEED_ALLOWED_HOSTS=peekseed.example.com,localhost,127.0.0.1
# PEEKSEED_ENV_FILE=/srv/peekseed/.env   # only needed if cwd is elsewhere

# --- optional: research mode ----------------------------------------------
# PEEKSEED_AGENT=1

# --- optional: seed model links on first start ----------------------------
# The UI under Settings is the normal way; these are for headless first runs.
# PEEKSEED_EMBED_BASE=http://127.0.0.1:8000/v1
# PEEKSEED_EMBED_PROTOCOL=openai
# PEEKSEED_EMBED_MODEL=nomic-embed-text
# PEEKSEED_EMBED_API_KEY=
# PEEKSEED_ANSWER_BASE=http://127.0.0.1:11434/v1
# PEEKSEED_ANSWER_MODEL=llama3.1
# PEEKSEED_ANSWER_API_KEY=
```

Notes:

- A real environment variable wins over `.env`. The CLI reads `.env` from the
  current working directory, then the project root, or from `PEEKSEED_ENV_FILE`
  when set.
- For a systemd `EnvironmentFile=`, keep the file to `KEY=VALUE` lines and
  full-line `#` comments (no inline comments after values).
- `.env` holds the service token; `workspace/.peekseed/config/links.json` (0600)
  holds model API keys. Neither belongs in git or in chat/logs.
- The UI asks for the token once: open `/#token=<token>` (the fragment never
  reaches the server) or paste it in **Settings → Service token**. A reverse
  proxy can instead inject an `Authorization: Bearer …` header server-side.

## 4. Initialize and first start

```bash
cd /srv/peekseed
/srv/peekseed/venv/bin/peekseed init --workspace /srv/peekseed/workspace
/srv/peekseed/venv/bin/peekseed doctor --workspace /srv/peekseed/workspace
```

`init` prints every resolved path and writes the empty 0600 link store; `doctor`
checks paths, the gitignore guard, links and schemas. Then start it in the
foreground for the first check:

```bash
/srv/peekseed/venv/bin/peekseed serve --workspace /srv/peekseed/workspace
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
mkdir -p logs
nohup venv/bin/peekseed serve >>logs/serve.log 2>&1 &
echo $! > serve.pid
curl -fsS http://127.0.0.1:8030/health

# stop
kill "$(cat serve.pid)" && rm -f serve.pid
# status
curl -fsS http://127.0.0.1:8030/health || echo "not running"
```

Nothing restarts it after a crash, logout, reboot or sleep. That is acceptable
for a single-user service if you start it when you need it. The repository's
`scripts/serve.sh start|stop|restart|status|logs` is a small convenience wrapper
around the same pattern for a source checkout (pidfile + health wait); it is not
required for a pip-installed deployment.

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
EnvironmentFile=/srv/peekseed/.env
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
manual starts; set it to `true` for restart-on-failure. `WorkingDirectory` is what
lets the CLI find `.env`.

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
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>/srv/peekseed/logs/serve.log</string>
  <key>StandardErrorPath</key><string>/srv/peekseed/logs/serve.log</string>
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
TOKEN='<the token from .env>'
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

# 8. workspace health and dataset list
/srv/peekseed/venv/bin/peekseed doctor --workspace /srv/peekseed/workspace
curl -sS "${AUTH[@]}" "$BASE/api/datasets"
```

Delete the smoke dataset afterwards (`DELETE /api/datasets/smoke` with the
token). The expected status codes are part of the contract in
[api.md](api.md); search and ask also run a probe canary and return `409` instead
of comparing vectors from a different embedding space.

Also verify the same checks **through the exposed URL** (Tailscale or proxy),
because Host/Origin and TLS mistakes only show up there.

## 8. Back up

The backup unit is small: the **workspace** (documents plus derived state,
including the 0600 link keys), the **`.env`** (service token) and your recorded
version/lock files. The venv and code are rebuildable; derived indexes are
rebuildable but keeping them makes a restore instant.

Take a **consistent** copy: stop the app first (single-user, so the downtime is
short), archive, verify, then start again.

```bash
systemctl stop peekseed      # or: kill "$(cat /srv/peekseed/serve.pid)"
cd /srv/peekseed
mkdir -p backups
ts=$(date +%Y%m%d-%H%M%S)
tar -czf "backups/peekseed-$ts.tar.gz" workspace .env requirements.lock
sha256sum "backups/peekseed-$ts.tar.gz" > "backups/peekseed-$ts.sha256"
tar -tzf "backups/peekseed-$ts.tar.gz" >/dev/null   # archive is readable
systemctl start peekseed    # or: start it the way you normally do
```

- Keep several generations locally (for example 7 daily, 4 weekly) and copy at
  least one **off the machine** (rsync to another host, restic/object storage).
  A backup that only exists on the same disk is not disaster recovery.
- Encrypt off-site copies (`gpg -c`, restic, …): the archive contains documents
  and API keys.
- **Restore drill** (do this once, and after large changes):

```bash
mkdir -p /tmp/peekseed-restore && cd /tmp/peekseed-restore
tar -xzf /srv/peekseed/backups/peekseed-<ts>.tar.gz
PEEKSEED_PORT=8031 /srv/peekseed/venv/bin/peekseed serve \
  --workspace /tmp/peekseed-restore/workspace
curl -fsS http://127.0.0.1:8031/health
# check the dataset list, one search and one PDF through :8031, then stop it
```

- **Live restore:** stop the app, move the live workspace aside (never delete it
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
/srv/peekseed/venv/bin/peekseed migrate --workspace /srv/peekseed/workspace
/srv/peekseed/venv/bin/peekseed migrate --apply --workspace /srv/peekseed/workspace

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
`<data_root>/config/local_models.json`, no command can run.

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
- [ ] `PEEKSEED_TOKEN` set, unique, and only in the 0600 `.env`; rotate by
      replacing it and restarting (browsers need the new value).
- [ ] `PEEKSEED_ALLOWED_HOSTS` contains only names you actually use.
- [ ] TLS terminates at Tailscale or the reverse proxy; port 8030 is not exposed.
- [ ] Firewall allows only the proxy/overlay port; the box is single-user.
- [ ] Workspace directories 0700, `links.json` and `.env` 0600, backups
      encrypted off-site.
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
- **PDF-only upload path.** Folder scanning (`scan`) and OCR are 0.2.0 features;
  the office/tables/pymupdf extras exist but belong to the 0.2.0 scan workflow.
  `peekseed scan` is a preview and not part of the 0.1.0 promise.
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
| `401` after a token rotation | Restart the app after editing `.env`, and update every browser/proxy that stores the token. |
| `403` through the proxy/tailnet | The `Host` does not match `PEEKSEED_ALLOWED_HOSTS` (or `Origin` is hostile). Add the exact `host[:port]`, make the proxy forward the original `Host`, then restart. |
| Streams stop mid-answer | The proxy buffers or times out. Disable buffering (`proxy_buffering off`, Caddy `flush_interval -1`) and raise the read timeout for `/api/ask|search|agent/stream`. |
| `409` from search/ask | Embedding space changed (link id/model) or the endpoint no longer reproduces the probe. Rebuild the index with the current embed link. |
| `409` from Build index | Another job holds the writer lock; wait, cancel it, or resume an interrupted job. Stale locks are detected. |
| Server is up but data looks missing | A schema migration was not applied after an upgrade. Run `peekseed migrate` (dry-run), then `--apply`, with a backup taken first. |
| `Operation not permitted` on local-model start/stop | The app was started from a restricted shell and cannot write model pid files. Start it as the owning user from a normal shell or via the service manager. |
| `peekseed doctor` reports workspace problems | Check ownership/modes (0700/0600), that the path is not tracked by a checkout, and that it is on local disk. |
| Slow or inconsistent I/O | The workspace is probably on a network/synced filesystem. Move it to local disk (`peekseed workspace move`). |

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
