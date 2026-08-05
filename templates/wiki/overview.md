# Overview

Vault-level synthesis. Per-project synthesis lives in `projects/<slug>/index.md`.

## Working Model

- `<repo>/.wiki-raw/` is the machine-written immutable source layer, one file per day.
- `projects/<slug>/wiki/` is the curated per-project knowledge layer.
- `wiki/` (this folder) is the shared layer: conventions, vault-level sources, automation reports.
- `raw/` is the human inbox for clippings and transcripts, unrelated to the hook pipeline.
- `index.md` is the first file an agent reads; `AGENTS.md` is the schema it follows.
- `log.md` per project tracks ingests, filed queries, and lint passes.

## Current State

Freshly installed. Nothing ingested yet.
