# Data folders

peekseed keeps every changeable runtime file under **two required folders**,
named by environment variables. Both must be absolute paths that already exist;
there are no fallbacks (`./workspace`, `~/peekseed` or a sibling folder), and a
missing setting stops the program with a clear error instead of guessing.

```
$PEEKSEED_HOME/                  private runtime data (0700)
├── config/links.json            model endpoints (0600; keys are env names only)
├── config/settings.json         optional UI title/tagline/examples
├── datasets/<dataset>/          dataset.json, chunks.jsonl, index/<space>/
├── history/                     search and dialog history
├── jobs/<id>.json               persistent, cancellable jobs
├── uploads/                     staging for an upload in progress
├── logs/                        serve.log, serve.pid (deploy scripts)
└── env/                         venv/, uv-cache/, requirements.lock/freeze

$PEEKSEED_SHARED_DIR/            shared with peelfeed (0700)
├── sources/<sha256>/<name>.pdf  the ONE stored copy of each uploaded PDF
└── peels/<peel_id>/             peelfeed corpora (source, extract/corpus, .peel)
```

`PEEKSEED_SHARED_DIR` is the same folder as peelfeed's `PEELFEED_SHARED_DIR`.
peelfeed writes `peels/`; peekseed writes `sources/` and reads `peels/` only.
A peel's `source/<name>.pdf` is a **hardlink** to the shared `sources/` copy, so
an uploaded PDF exists once on disk.

## Where the folders live

The deployment sets both in its environment file, mode 0600 (see
[deploy.md](deploy.md)). For a checkout at `<ROOT>` that keeps runtime data under
`livefield/`:

    PEEKSEED_HOME=<ROOT>/livefield/.peekseed
    PEEKSEED_SHARED_DIR=<ROOT>/livefield/shared

`peekseed init` creates the subfolders, hardens them to 0700 and writes an empty
0600 link store; it never overwrites an existing link store or dataset.

## Commands

```bash
peekseed init      # create the home/shared tree and print the paths
peekseed status    # paths, datasets, peelfeed handshake
peekseed doctor    # paths, permissions, link store, schemas, peelfeed
```

`GET /api/workspace` returns the served absolute paths (`root`, `shared_dir`,
`sources_dir`, `peels_dir`) and per-dataset state; `GET /api/collections` is the
same dataset rows; `GET /api/datasets/{id}/sources` lists per-source provenance.
The UI Workspace panel shows the paths with copy buttons, the dataset states,
Build/Rebuild and the persisted job records with Cancel/Resume.

## Invariants

These are enforced by tests, not just documented:

1. **Uploads are storage only.** A file is stored once at
   `<shared>/sources/<sha256>/<name>.pdf` (0600); peekseed never parses it.
2. **Extraction is peelfeed's job.** A peel lives at `<shared>/peels/<peel_id>/`;
   peekseed only reads it, realpath-checked against the peel root.
3. **Derived data is rebuildable.** Deleting the home loses no uploaded bytes
   (they live in the shared sources), but it does lose model links: back that
   file up.
4. **One writer per dataset.** A lock file with pid and timestamp prevents two
   concurrent index builds; a stale lock is detected and reported.
5. **Content is identity.** A PDF is identified by its sha256; re-uploading the
   same bytes is idempotent, and two datasets share the one stored file.

## Backups

Back up `$PEEKSEED_HOME` (except `env/venv`, `env/uv-cache`, `logs/` and
`uploads/`) and `$PEEKSEED_SHARED_DIR/sources`. Peels (`shared/peels`) are not
backed up: they can be rebuilt by extracting again. `deploy/backup.sh`
implements exactly this policy.
