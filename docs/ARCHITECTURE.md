# Architecture

Four layers, one cursor, and a hard rule: the cheap deterministic parts never depend on the expensive probabilistic one.

## Layers

```
1. journal   <repo>/.wiki-raw/YYYY-MM-DD.md      machine-written, immutable, in-repo
2. pages     <vault>/projects/<slug>/wiki/       model-written, curated, per project
3. shared    <vault>/wiki/                       cross-project pages and automation reports
4. schema    <vault>/AGENTS.md                   the contract every agent follows in the vault
```

Layer 1 is written by hooks with no model involved: fast, offline, and unable to fail in a way that matters. Layer 2 is written by the ingest. The split exists so that capture keeps working when the model, the network or your quota does not — a queue that grows is a recoverable state, a lost session is not.

## The block contract

```
<!-- wiki-raw:begin id=<id> kind=session|commit|history status=unprocessed -->
## [<timestamp>] <kind> <id> | branch <name> | files N | cmds M
…body…
<!-- wiki-raw:end id=<id> -->
```

- `status` is the **only** cursor. There is no database, no offset file, no "last run" timestamp that can drift out of sync with reality.
- Ingest flips `unprocessed` → `processed date=<today>` and never edits a body. A crashed run re-reads and re-decides; it cannot skip.
- A session block is rewritten in place while its session is alive (the recorder keeps a per-session transcript cursor in `~/.claude/wiki-state/<session>.json`). Once processed it is frozen — the recorder checks and returns.
- Ids are constrained to `[A-Za-z0-9][A-Za-z0-9._-]{1,63}` because they are interpolated into a regex.

## Components

| File | Role |
| --- | --- |
| `bin/wiki_record.py` | `Stop` hook. Reads the transcript from a cursor, extracts intent/files/commands/subagents, rewrites this session's block. Also home to `_safe()` and `write_block()`, used by everything that writes a block. |
| `bin/wiki_commit.py` | `post-commit` hook. One block per commit: subject, body, author, numstat. |
| `bin/wiki_bootstrap.py` | `SessionStart` hook, async. Registers the project and installs the post-commit hook — only for repos under a configured root. |
| `bin/wiki_context.py` | `SessionStart` hook, sync. Emits the project catalog as `additionalContext`. Silent when the project has no pages. |
| `bin/wiki_queue.py` | Queue interface: `list`, `show`, `mark`, `stats`. The deterministic surface the model is told to use instead of grepping markers. |
| `bin/wiki_ingest.py` | Engine selection, the JSON-verdict loop, page writing, index and log updates. |
| `bin/wiki_install_git_hooks.py` | Idempotent post-commit installer, including the "marker present but path stale" repair. |
| `bin/wiki_paths.py` | Single source of truth for paths and config. |
| `bin/wiki_structure.py` | Import and lever extraction, the graph queries behind `map`/`path`/`levers`, and the generated module-page block. |
| `wikictl.py` | The admin CLI. |

## Three indexes, three questions

| Index | Question it answers | Refreshed |
| --- | --- | --- |
| symbols | does this name, or this body, already exist | post-commit, incremental |
| structure | what will I disturb if I touch this | post-commit, incremental |
| the wiki itself | why was this decided, and what is true | ingest, on blocks that earn a page |

The first two are parsed, not inferred: no model, no daemon, no network. The third is the only one
that costs a model call.

The structure index deliberately stops at static requires and literal SQL. Runtime coupling — a
worker started by a process manager, a table reached through an ORM, a require assembled at
runtime — is invisible to it, and every command that prints from it says so. A map that quietly
omits a third of the edges is worse than one that admits its boundary.

The two parsed indexes are stored in separate files on purpose. The duplicate guard loads the
symbol index on every `Write` and must stay inside a few hundred milliseconds; the structure index
for a 1195-file repository is 233 KB and has no business on that path. Only the `SessionStart`
orientation and the `map`/`path`/`levers` commands read it.

## Ingest flow

1. `wiki_queue.collect()` lists unprocessed blocks across registered projects.
2. For each block, oldest first: build a context header (project, repo, existing page names), wrap the block in untrusted-data markers, call the engine.
3. Parse the verdict. Models wrap JSON in prose, fences and `<think>` blocks, so the parser strips those, scans for a balanced object and decodes with `strict=False` (raw newlines inside markdown strings are common). On failure, one repair retry at temperature 0. On a second failure the block stays queued.
4. `normalize()` collapses every model string to a single line. `write_page()` validates section, slug and reserved names, asserts vault containment, then writes or appends an `## Update <date>` section.
5. `update_index()` inserts the link into the right section of the project catalog. `append_log()` records what was written, what was skipped and which engine ran.
6. `mark_processed()` flips the status flag for every consumed block, including the skips — an unmarked block would be re-read forever.

Failure isolation is per block: an engine error, an unparseable verdict or an `OSError` on write leaves that one block queued and the batch continues.

## Who runs the ingest

Default is in-session. The `Stop` hook counts the queue and, past the threshold, exits 2 with a message; Claude Code treats that as a wake-up and the model already in the session runs `/wiki-ingest`. No token, no daemon, no second model, and it works identically on every platform. Guards: at least `auto_ingest_min_blocks` blocks, one nudge per session, `auto_ingest_cooldown_min` between nudges, and the state lives in `wiki-state/auto-ingest.json`.

An ingest run edits only the vault, and the recorder ignores sessions whose cwd is inside the vault, so the wake-up cannot feed itself.

## Engine selection (unattended runs only)

Unattended runs try `claude` then `ollama`. `claude` requires a credential in the environment; `ollama` requires its endpoint to answer. The runner checks the queue before starting anything, so an empty queue costs nothing. If the `claude` engine starts but reports `Not logged in`, the run falls back down the chain instead of failing.

## Why the recorder cannot loop

The ingest writes into the vault. The recorder ignores any session whose working directory is inside the vault or inside the Claude config home. Without that, every ingest would produce a session block describing the ingest.

## Worktrees

`git rev-parse --path-format=absolute --git-common-dir` resolves a worktree to its main repository, so `<repo>/.claude/worktrees/feature-x` records under the parent project instead of registering as a new one.
