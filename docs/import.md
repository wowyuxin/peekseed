# Corpus import (`peekseed import`)

`peekseed import --manifest corpus.json` reproduces a corpus in a workspace from
a **versioned manifest**. It is the supported migration path between deployments
(the Mac mini's P4 cutover), between machines, and from another tool's export. It
is idempotent and is designed to move existing vectors **without re-embedding**.

## Guarantees

- **Versioned manifest.** A manifest declaring a newer schema is refused with a
  message naming both versions; older schemas are upgraded only when a future
  migration says how. The current schema is **1**.
- **Deterministic ids.** A collection's dataset id is the slug of its manifest
  `name` (`"CXL"` → `cxl`, `"PCIe 5.0"` → `pcie-5.0`), so the same manifest
  produces the same ids on every machine. Duplicate ids are rejected before any
  file is touched.
- **Idempotent.** Re-running an unchanged manifest writes nothing and embeds no
  chunks; it only re-runs the probe canary. Documents are compared by content
  hash, not path.
- **Zero re-embed for prebuilt collections/Chroma exports.** Vectors are copied
  unchanged. Before the copy is trusted, the stored probe is re-embedded through
  the configured embed link and must match at cosine ≥ **0.999**; a mismatch
  aborts the import instead of serving vectors from a different embedding space.
  Chroma exports embed the fixed probe once to bind the new space (no chunk is
  re-embedded).
- **All-or-nothing per collection.** A collection is staged in a hidden
  `.import-*` directory and renamed into place only after documents, chunks,
  index and probe verification succeed. A failed import leaves the previous
  dataset untouched.
- **Documents are copies.** Source files are never modified; they are copied to
  the dataset's `original/` directory, hashed and recorded as provenance.

The import needs an enabled **embed link** for the probe canary. It never calls
an answer or rerank model.

## Manifest schema 1

```json
{
  "schema": 1,
  "collections": [
    {
      "name": "CXL",
      "title": "CXL",
      "documents": ["../original_docs/CXL Specification.pdf"],
      "dataset": "../export/.peekseed/datasets/cxl",
      "space": "qwen3-vl-embedding-2b-8bit-mlx",
      "sha256": {"CXL Specification.pdf": "a6694b69…"}
    },
    {
      "name": "DDR5",
      "title": "DDR5",
      "documents": ["../original_docs/JEDEC_DDR5_Spec.pdf"],
      "chroma_export": "../export/ddr5-chroma.jsonl"
    }
  ],
  "queries": ["coherency protocol", "lane reversal", "…"]
}
```

Relative paths are resolved against the manifest file's directory, so a bundle
can be copied as one tree. A collection must declare `documents`, a prebuilt
`dataset`, or a `chroma_export`; a `chroma_export` additionally needs
`documents` (provenance and `/pdf` serving). `sha256` entries are optional but,
when present, a mismatch aborts the import.

### Prebuilt `dataset`

A directory holding a peekseed dataset: `dataset.json`, `chunks.jsonl`, the
original files under `original/`, and `index/<space>/` with `CURRENT` plus a
generation containing `info.json`, `vectors.npy`, `meta.json` and `probe.npy`.
If several spaces exist, set `space`; otherwise the single space is used.

### Chroma export

A deployment-side exporter (for example from smart-search's Chroma store) writes
one JSON object per line:

```json
{"id": "chroma-123", "document": "chunk text",
 "metadata": {"source": "JEDEC_DDR5_Spec.pdf", "source_sha256": "…",
               "page_start": 41, "page_end": 41, "section": "…",
               "chapter": "", "numbering": "", "doc": "breadcrumb"},
 "embedding": [0.013, -0.221, "…"]}
```

`document` is the chunk text, `metadata.source` must match a manifest document,
and `embedding` must be the vector from the model that built the store. The row
`id` is not trusted: peekseed derives content ids from the source hash and row
order, so the import is deterministic. Mixed dimensions, missing sources and
hashes that disagree with the manifest are rejected.

## CLI

```bash
peekseed import --manifest corpus.json                 # import (idempotent)
peekseed import --manifest corpus.json --dry-run       # plan only; no writes/embeds
peekseed import --manifest corpus.json --workspace PATH
```

Exit codes: `0` success (including all collections skipped), `1` a
manifest/bundle/probe failure. The report names each collection with its status,
space, chunk count and probe cosine.

## P4 acceptance record (deployment step)

The repository contract is the command above; the deployment adds the evidence:

1. **Probe canary on the mini:** `peekseed import` prints `probe cosine ≥ 0.999`
   for every copied space (CXL/PCIe) and every Chroma export (DDR5/PIPE).
2. **Zero re-embed:** the import report's chunk counts are unchanged from the
   source indexes and the embedder request log shows no per-chunk calls.
3. **20-query overlap:** run the same query list against smart-search and the
   imported peekseed service:

   ```bash
   python scripts/overlap_check.py \
       --baseline http://127.0.0.1:8030 --candidate http://127.0.0.1:8031 \
       --queries queries.json --dataset cxl --topk 10 --out overlap.json
   ```

   `--path` and `--id-key` adapt a baseline whose route or row field differs; the
   token, if needed, is passed with `--token` and never printed. The script
   records per-query overlap, the mean and errors; `--min-overlap` can turn a
   threshold into a non-zero exit. The JSON report is the acceptance evidence.
4. **Agent smoke:** ask one research question per imported collection against the
   answer link and check the citation audit has no `unknown` numbers.

## Current limitations

- Import requires the embed link that produced (or reproduces) the source space;
  without it the probe cannot be verified and the import fails closed.
- Only one index **space** is imported per collection; other spaces are left in
  the source bundle (`peekseed reindex` builds additional spaces later).
- No downgrade: importing a bundle written by a newer peekseed/schema is refused
  rather than guessed at.
- The Chroma exporter itself is deployment glue, not shipped as a peekseed
  dependency (no Chroma in the default install).
