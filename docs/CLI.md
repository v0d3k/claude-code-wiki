# Command reference

All commands are `python wikictl.py <command>`. Exit code is 0 unless stated.

## install

```
install [--vault PATH] [--root PATH ...] [--replace-roots] [--schedule]
```

Writes the config, scaffolds the vault, installs the three Claude Code hooks and `post-commit` in every repository under the configured roots, and migrates a config written by an older version. Ingest is in-session by default, so no background job is registered unless you ask for one. Idempotent, and the repair command for almost everything — run it again after moving the package and every path is rewritten.

| Flag | Default | Effect |
| --- | --- | --- |
| `--vault PATH` | `~/llm-wiki` | where curated pages live |
| `--root PATH` | none | a directory containing repositories; repeatable. **Added** to the configured roots |
| `--replace-roots` | off | replace the configured roots instead of adding to them |
| `--schedule` | off | also register a background ingest (Task Scheduler on Windows, cron elsewhere) |

Existing vault files are never overwritten. `settings.json` is copied to `settings.json.<timestamp>.bak` before every write. With no roots configured, it says so and wires nothing — use `add` per repository.

## uninstall

```
uninstall [--purge] [--purge-vault --confirm NAME] [--dry-run]
```

Removes the hook entries, the `post-commit` blocks and any background schedule. **Your notes are kept.**

| Flag | Effect |
| --- | --- |
| `--purge` | additionally delete every `.wiki-raw/` journal and the whole `wiki-state` directory (config, cursors, token) |
| `--purge-vault` | additionally delete the vault. Refused unless the directory holds both `AGENTS.md` and `projects.json`, contains nothing the scaffold did not create, and `--confirm <vault directory name>` is given |
| `--dry-run` | print what would happen and change nothing |

`--purge-vault` does not imply `--purge`; they are independent.

## status

Prints the vault path and whether it exists, the config file in use, the installed hooks, how automatic ingest is wired (mode, threshold, cooldown), any background schedule, engine availability for unattended runs, then one row per registered project with its page count, queued blocks and repository, and the last line of the run log.

## doctor

Verifies: vault present and complete; `settings.json` parses; the expected number of hooks is installed and each points at a file that exists; every repository under the roots has the `post-commit` hook; no non-history block has been unprocessed for over 7 days; the Stop hook carries `asyncRewake` when `auto_ingest` is `rewake`; and something is able to ingest at all. Prints `FAIL <problem> -- <fix>` per finding, or `all checks passed`. Exit 1 when anything failed.

## schedule

```
schedule [status|install|remove]
```

Manages the optional background run: Task Scheduler on Windows, a `crontab` line tagged `# claude-code-wiki` elsewhere. Independent of in-session ingest — most people need neither this nor a token.

## ingest / lint

```
ingest [--engine auto|claude|ollama] [--limit N] [--dry-run]
lint   [--engine ...]
```

Turns queued blocks into pages now, in a **separate process with its own credentials**. When a session is available, prefer `/wiki-ingest` inside it: the pages are then written by the model you are working with, and the skill delegates to a subagent so the queue stays out of your context. `--limit` caps blocks this run (otherwise `max_blocks_per_run`). `--dry-run` prints each verdict without writing. `lint` runs the weekly consistency pass; it is implemented on the `claude` engine only and is a no-op elsewhere.

Exit 3 means no engine was available.

## backfill

```
backfill --project SLUG|all [--source git|sessions|both] [--since YYYY-MM-DD] [--max N] [--dry-run]
```

Queues historical blocks. `--source git` (default) writes one block per calendar day of commits, with subjects and files ranked by churn. `--source sessions` replays past Claude Code transcripts whose working directory resolves to that repository. `--since` defaults to `2026-01-01`, `--max` to 60 blocks per project.

Idempotent: a block id already present in the journal is skipped. Nothing is ingested — run `ingest` after, and remember each block is one model call.

## symbols / where / dupes

```
symbols [REPO] [--full]      # rebuild the index; incremental over the last commit unless --full
where NAME [--repo PATH]     # every definition of NAME, grouped by body, with aliases
dupes [--kind all|identical|diverged|renamed] [--limit N]
```

The index holds, per definition, the location and a hash of the normalised body (comments and
whitespace stripped, bodies under 40 characters ignored as boilerplate). That is what lets `where`
group definitions into variants and report that the same body also goes by other names, and what
lets `dupes` separate three different problems:

- `identical` — same name, same body. Mechanical: consolidate and import.
- `diverged` — same name, different bodies. A name collision; the source of bugs where one module's
  `num('')` is `0` and the next one's is `null`.
- `renamed` — same body under different names. Invisible to any name-based check.

The index lives at `wiki-state/symbols/<repo>.json` and is refreshed by the post-commit hook, which
re-parses only the files in that commit. A format change triggers a full rebuild automatically.
`where` exits 1 when the name is unknown, and suggests similar names.

## map / path / levers

```
map [--repo PATH] [--top N] [--rebuild] [--write] [--no-tests]   # fan-in, fan-out, contended levers
path SOURCE TARGET [--repo PATH]                                 # import route between two repo-relative files
levers NAME [--repo PATH] [--limit N] [--no-tests]                # who writes this table, reads this env key,
                                                                  #   or emits this event
```

`--rebuild` rescans before printing; normally the post-commit hook keeps the index current.
`--write` creates or refreshes an entity page per top module, replacing only the block between
`<!-- structure:begin -->` and `<!-- structure:end -->`, so hand-written prose above it survives
regeneration. A page whose markers are malformed is left untouched rather than rewritten. A
module's own file name decides its page slug (`orderRouter.js` -> `order-router.md`), splitting
camelCase first and qualifying with parent directories only on a collision within the same run --
this is deliberate: it lets generated facts land on an existing hand-written page of the same name
instead of creating a near-duplicate beside it.

A page that did not exist before the run also earns one line in `projects/<slug>/index.md` under
`## Entities`, e.g.:

```
- [wiki/entities/order-router.md](wiki/entities/order-router.md) - imported by 6; writes `positions`.
```

The description is synthesised only from what the structure index actually knows (imported-by
count, what the module writes), never a guess at what the module is for. A page `--write` merely
merged into (hand-written, or already filed by an earlier run) never gets a second line. The check
for an existing link covers the whole file, not just the `## Entities` section, so a page a human
already filed somewhere else is never duplicated. Missing `## Entities` heading but `## Concepts`
or `## Sources` present: a new `## Entities` section is created right before whichever comes first,
matching the catalog's normal section order. No index.md, or no section to anchor a missing
`## Entities` heading on at all: nothing is written, and the run says so instead of guessing.

A writer count on its own conflates two different facts: a table with 22 writers where 16 are
tests means 6 places actually ship code against its shape, and the other 16 just fail loudly in
CI. `levers` always prints both -- `positions: 22 file(s) (6 outside tests)` -- and `--no-tests`
narrows the listed paths below that header to the 6. `map`'s "shared levers" section shows the
same split per lever; with `--no-tests` the contention threshold (>= 2 writers) is reapplied to
the non-test count alone, so a lever that only looked contended because of its test suite drops
out of the list entirely rather than shrinking to one file. A file counts as a test by the same
rule everywhere in this index: a `test/`, `tests/`, `__tests__/` or `spec/` path segment, a
`.test.` or `.spec.` filename marker, or Python's `test_*.py` / `*_test.py` -- a bare "test"
substring elsewhere in a name (a backtest script, a one-off `*-tests.cjs` migration) does not
count.

`path` distinguishes the two ways it can fail: an endpoint that is not in the index at all (with
near-match suggestions, since a typo is the likelier case in a large repo) versus two files that
are both indexed and simply do not connect. Windows-style backslashes in either argument are
normalised.

The index lives at `wiki-state/structure/<repo>.json`, kept separate from the symbol index because
the duplicate guard loads that one on every `Write` and must stay under a few hundred milliseconds.

Static requires and literal SQL only. A worker started by a process manager, a table reached
through an ORM, a require assembled at runtime — none of it is here, and every command that prints
from the index repeats that caveat.

## add / remove

```
add PATH        # install the hook, exclude the journal, register the project
remove SLUG     # remove the hook, unregister; pages under projects/<slug>/ are kept
```

## search

```
search "QUERY" [--limit N]
```

Case-insensitive substring search across every markdown file in the vault. `--limit` defaults to 40 hits.

## Configuration

`<config home>/wiki-state/wiki-config.json`, where config home is `$CLAUDE_CONFIG_DIR` or `~/.claude`. Any missing key falls back to the default.

| Key | Default | Meaning |
| --- | --- | --- |
| `vault` | `~/llm-wiki` | root of the curated wiki |
| `roots` | `[]` | directories scanned for repositories |
| `exclude` | `["node_modules", "vendor"]` | directory names skipped when scanning roots |
| `engines` | `["claude"]` | unattended runs only; tried in order, first reachable wins. Add `"ollama"` to allow a local fallback |
| `auto_ingest` | `"rewake"` | `rewake` wakes the running model to ingest, `notify` only mentions the count at session start, `off` does neither |
| `auto_ingest_min_blocks` | `3` | do not interrupt for fewer blocks than this |
| `auto_ingest_cooldown_min` | `60` | minimum minutes between nudges |
| `claude.timeout_min` | `30` | kill the headless run after this long |
| `ollama.url` / `ollama.model` | `http://127.0.0.1:11434/v1/chat/completions`, `llama3.1` | local engine |
| `inter_call_ms` | `1000` | pause between model calls |
| `block_chars` | `6000` | block text sent to the model per call |
| `max_blocks_per_run` | `20` | cap per ingest run |
| `git_exclude_raw` | `true` | add `.wiki-raw/` to `.git/info/exclude` |
| `guard_enabled` | `true` | warn when new code re-declares an existing name |
| `guard_on_edit` | `false` | also guard `Edit`; costs ~190 ms per edit |
| `guard_min_existing` | `1` | warn once the name exists in this many other files |
| `guard_min_lever_writers` | `2` | warn once a table already has this many OTHER non-test writers (this file's own prior contribution is subtracted first) |
| `orient_enabled` | `true` | inject a structure orientation at session start (~1 KB) |
| `orient_modules` | `8` | how many modules the orientation names |
| `orient_levers` | `5` | how many contended resources it names |
| `exclude_repos` | `[]` | repository names that must never become projects (e.g. a parent directory that is itself a repo) |
| `auto_install_git_hooks` | `true` | let the SessionStart hook wire repos under the roots |
| `schedule.*` | 6 h from 04:07, lint Sunday 05:07 | used at install time |

## State files

| Path | Contents |
| --- | --- |
| `wiki-state/wiki-config.json` | the config above |
| `wiki-state/symbols/<repo>.json` | the symbol index behind `where` and the guard |
| `wiki-state/structure/<repo>.json` | the import and lever index behind `map`, `path`, `levers` and the session orientation |
| `wiki-state/structure/<repo>.levers-summary.json` | lever -> [total writers, non-test writers], regenerated alongside the index above; the only structure file the guard ever loads |
| `wiki-state/<session-id>.json` | per-session transcript cursor and accumulated block |
| `wiki-state/ingest-runs.log` | one line per scheduled run |
| `wiki-state/ingest-last-run.txt` / `.err` | stdout and stderr of the last run |
| `wiki-state/wiki_record.log` | recorder diagnostics |
| `wiki-state/oauth-token.dpapi` | optional Claude token, DPAPI-encrypted |

## PowerShell entry points

```
bin/wiki-ingest-run.ps1 [-Mode ingest|lint] [-Engine auto|claude|ollama] [-TimeoutMinutes 45]
bin/wiki_set_token.ps1
```

The runner is what Task Scheduler calls: it locates the package from its own path, loads the optional token, runs the ingest and logs the outcome. `wiki_set_token.ps1` stores a token from `claude setup-token` and verifies it can be read back.

The token is only injected by the runner. A direct `wikictl ingest` uses the `claude` engine only if `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` or a logged-in CLI credential file is present.

## Supporting CLIs

`bin/wiki_queue.py list|show|mark|stats|claim|release` is the deterministic queue interface used by the ingest skill. `claim` takes an advisory lock so a scheduled run and a session cannot process the same blocks — it has happened, and the result was duplicate pages. The lock is written to `wiki-state/ingest.lock` and goes stale after its TTL, so a crashed run cannot wedge the queue. `bin/wiki_install_git_hooks.py [--dry-run] [--uninstall] [--root DIR]` manages `post-commit` hooks alone. `bin/wiki_ingest.py --mode --engine --limit --dry-run` is the engine loop that `wikictl ingest` wraps.
