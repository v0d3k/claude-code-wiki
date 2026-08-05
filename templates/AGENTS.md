# LLM Wiki

Karpathy-style persistent wiki, split per project, fed automatically by Claude Code hooks.

## Layers

1. `<repo>/.wiki-raw/` — immutable, machine-written source layer. Hooks append here. Never edit by hand, never rewrite past entries.
2. `LLM-Wiki/projects/<slug>/wiki/` — LLM-maintained curated pages for one project. This is the primary working memory.
3. `LLM-Wiki/wiki/` — shared, cross-project layer (conventions, vault-level sources, automation reports).
4. `AGENTS.md` (this file) — the schema. Defines structure, conventions, and the ingest/query/lint workflows.

## Layout

```
LLM-Wiki/
  AGENTS.md              schema (this file)
  index.md               master catalog: projects + shared pages
  log.md                 vault-level log
  projects.json          registry: slug -> repo path, written by the hooks
  projects/
    <slug>/
      index.md           project catalog — read this first for that project
      log.md             append-only ingest/query/lint log for that project
      wiki/
        analyses/        decision memos, diagnostics, roadmaps, comparisons
        concepts/        recurring ideas and mechanisms
        entities/        modules, services, files, people, external systems
        sources/         capture pages for raw material
      raw-inbox/         fallback raw for non-git working dirs
    _template/           scaffold copied when a new project appears
  raw/                   shared human inbox (URL clippings, transcripts, notes)
  wiki/                  shared layer (see above)
<repo>/.wiki-raw/
  YYYY-MM-DD.md          one file per day, session and commit blocks
```

## Raw block format

Hooks write blocks that the ingest step parses. Contract:

```
<!-- wiki-raw:begin id=<session-or-commit-id> kind=session|commit status=unprocessed -->
## [<ISO timestamp>] <kind> <short-id> | branch <name>
...body...
<!-- wiki-raw:end id=<same-id> -->
```

- `status=unprocessed` is the ingest queue. Ingest flips it to `status=processed date=<YYYY-MM-DD>` and never deletes the block.
- A session block is rewritten in place while the session is alive; once `status=processed`, it is frozen.
- Anything outside these markers is human-written and must be preserved.

## Ingest rules

Run per project, oldest unprocessed block first.

1. Resolve projects from `projects.json`. For each, scan `<repo>/.wiki-raw/*.md` for `status=unprocessed`.
2. Read the blocks. Read the actual code or diff only when the block is not self-explanatory.
3. Decide what is durable. Most sessions produce nothing wiki-worthy — that is the expected outcome, not a failure. File a page only when a block records a decision, a diagnosis, a measured result, an architecture change, or a fact that would otherwise be re-derived.
4. Write or update pages under `projects/<slug>/wiki/`. Prefer updating an existing page over adding a near-duplicate.
5. Update `projects/<slug>/index.md`.
6. Append one entry to `projects/<slug>/log.md`.
7. Flip every block you consumed to `status=processed date=<today>`.
8. If a project in `projects.json` has no `projects/<slug>/` yet, copy `projects/_template/` and add a row to the master `index.md`.

Idempotence: re-running ingest must not duplicate pages or log entries. The `status` marker is the only cursor — trust it.

## Query rules

1. Read `index.md`, then the relevant `projects/<slug>/index.md`.
2. Read only the pages you need under `wiki/`.
3. Read `.wiki-raw/` or the code only when the wiki is missing, stale, or contradicted.
4. Cite the pages used.
5. If the answer is a useful synthesis, file it under `projects/<slug>/wiki/analyses/` and add it to that project's `index.md`.

## Lint rules

Periodically, per project:

- contradictions between pages
- stale claims superseded by newer blocks
- orphan pages not linked from any `index.md`
- concepts that recur across analyses but lack a dedicated page
- `.wiki-raw` blocks stuck `unprocessed` for more than 7 days
- projects in `projects.json` with no `projects/<slug>/` directory

Write lint findings to `wiki/automation/` and link them from the master `index.md`.

## Writing conventions

- One page, one subject. Filename is kebab-case, dated when the page is a point-in-time finding (`*-2026-08-04.md`).
- Every page opens with a one-line summary, then `Status`, then body.
- State measured numbers with their source and sample size. An unverified claim is labelled as such.
- Link between pages with relative markdown links so Obsidian resolves them.
- Do not copy code into the wiki. Reference `path/to/file.js:line`.
- Include a `## Sources` section when a page draws on specific raw material.

## Token discipline

- Read `index.md` and existing wiki pages before opening raw files.
- Open raw files only for genuinely new material, verification, or contradiction checks.
- Reuse and update prior syntheses instead of rewriting them from scratch.

## Shared capture rules

These govern the vault-level `raw/` inbox, which is human-fed and unrelated to the hook pipeline.

- Web clips land in `raw/web/`, local notes and exports in `raw/notes/`, transcripts in `raw/transcripts/`, unknown or mixed input in `raw/incoming/`.
- `raw/` is immutable except for moving a file into the correct subfolder.
- Queued URLs live in `raw/url-inbox.md`. Each bullet under `## Queue` is a fetch job unless marked processed: fetch it, save a markdown copy into `raw/web/` under a stable name derived from title or domain plus date, ingest it into `wiki/sources/`, then mark the queue item processed.
- Each page in `wiki/sources/` carries YAML frontmatter: `title`, `source_path`, `source_type`, `ingested_at`, `status`. Use `status: active` unless there is a reason not to.
