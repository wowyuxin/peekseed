# The workspace

A peekseed **workspace** is one directory that holds your documents and everything
peekseed derives from them. It is the unit you back up and the unit you can move
between machines. There is no database and no hidden state elsewhere: delete the
derived half and your documents are untouched; delete the whole workspace and you
have deleted exactly what you put in it.

```
workspace/                          one directory to back up
├── documents/                      YOUR documents; peekseed never writes here
│   ├── cxl/                        one top-level folder = one collection
│   │   ├── CXL_spec.pdf
│   │   └── notes.md
│   └── manual.pdf                  loose files → collection "default"
├── .peekseed/                      DERIVED; rebuildable; safe to delete
│   ├── manifest.json               scan state: paths, content hashes, statuses
│   ├── config/links.json           model endpoints (0600, keys masked in the UI)
│   ├── datasets/<collection>/      dataset.json, chunks.jsonl, index/<space>/
│   ├── extracted/<sha256>.json     extraction cache
│   ├── history/                    search and dialog history
│   ├── jobs/<id>.json              persistent, cancellable jobs
│   └── uploads/                    staging for an upload in progress
└── .gitignore                      `peekseed init` ensures "/workspace/" is ignored
```

## Where the workspace lives

Resolution order:

1. `--workspace PATH` on the command line, or `PEEKSEED_WORKSPACE` in the
   environment (an absolute path, or one starting with `~`).
2. `./workspace` in the current directory — the documented default, created by
   `peekseed init`.
3. `~/peekseed` — only when the current directory is not writable (read-only
   checkouts, containers), and printed with a warning.

`PEEKSEED_DATA` overrides only the **derived** root (`<workspace>/.peekseed`), for
advanced layouts where documents live on synced storage and derived data on local
disk. If a legacy `./data` root exists and no workspace does, peekseed keeps using
it and prints a migration hint; it never creates a second derived root silently.

## Commands

```bash
peekseed init --workspace ~/research        # create the tree, gitignore guard, print paths
peekseed init                               # same, at ./workspace
peekseed workspace path                     # print the resolved absolute root
peekseed workspace move ~/new-research      # move the tree as one unit
peekseed doctor                             # paths, gitignore/checkout check, links, schemas
peekseed status                             # last scan, collections, stale/missing files
peekseed scan                               # preview only: scan workflow ships in 0.2.0
```

The workspace is also exposed over HTTP: `GET /api/workspace` returns the served
absolute paths, last scan and per-collection state; `GET /api/collections` is the
same collection rows; `GET /api/datasets/{id}/sources` lists per-source
provenance. The UI has a Workspace panel that shows those paths with copy
buttons, the collection states, Build/Rebuild, and the persisted job records with
Cancel/Resume.

`init` prints every resolved absolute path, writes an empty link store (0600),
initialises `manifest.json`, and ensures an **anchored** entry for the workspace
path in the `.gitignore` of the enclosing git checkout: `/workspace/` for the
default `./workspace`, or `/ws/` for a custom `--workspace ws`. Anchoring matters:
the pattern only matches that exact path from the repository root, so an unrelated
`docs/workspace/` folder is not silently ignored. If the workspace itself is a
tracked path in a checkout, `doctor` warns and the guard refuses to treat it as
safe — documents must never be committed to a repository.

## Invariants

These are enforced by tests, not just documented:

1. **Documents are never modified, moved or deleted.** peekseed writes only under
   `.peekseed/` (and the `.gitignore` edit made by `init`).
2. **Derived data is rebuildable.** Deleting `.peekseed/` loses no documents; the
   next scan rebuilds the state.
3. **Every chunk traces to `source_sha256`,** and every index records the source
   hashes plus extractor, chunker and embedding identity it was built from.
4. **Scan is idempotent** (0.2.0 workflow; the engine already honours it). A second
   scan of unchanged documents makes zero embed calls.
5. **Paths are allowlisted.** Only `documents/` is scanned, served and watched;
   symlinks are resolved and rejected when they escape. `/pdf` serves only the
   dataset's own `original/` copies.
6. **One writer per collection.** A lock file with pid and timestamp prevents two
   concurrent index builds; a stale lock is detected and reported, not waited on.

## Identity is content, not paths

A file is identified by its sha256. That means:

- **moving** a document (for example into a subfolder) is not a new document: the
  manifest updates its path and keeps it `unchanged`;
- **renaming** follows the same rule;
- **editing** a document is `changed`, and only that collection is rebuilt.

This avoids the classic "move = new asset, re-embed everything" trap and makes
incremental ingestion safe, because chunk ids are derived from the source content
and the chunk's position inside that source (see [ingestion](ingestion.md)).

## Backups

Back up the workspace as one directory (`tar`, restic, Time Machine, any tool).
`.peekseed/config/links.json` holds API keys and is written 0600; `.peekseed/`
directories are 0700. A workspace restore is just the directory back in place —
the derived half can also be rebuilt from scratch if you only kept `documents/`.
