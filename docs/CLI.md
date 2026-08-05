# Command reference

All commands are `python wikictl.py <command>`. Exit code is 0 unless stated.

## install

```
install [--vault PATH] [--root PATH ...] [--replace-roots] [--no-schedule]
```

Writes the config, scaffolds the vault, installs the three Claude Code hooks, installs `post-commit` in every repository under the configured roots, and registers the scheduled tasks. Idempotent, and the repair command for almost everything — run it again after moving the package and every path is rewritten.

| Flag | Default | Effect |
| --- | --- | --- |
| `--vault PATH` | `~/llm-wiki` | where curated pages live |
| `--root PATH` | none | a directory containing repositories; repeatable. **Added** to the configured roots |
| `--replace-roots` | off | replace the configured roots instead of adding to them |
| `--no-schedule` | off | skip Task Scheduler registration (use on non-Windows, or drive ingest from cron) |

Existing vault files are never overwritten. `settings.json` is copied to `settings.json.<timestamp>.bak` before every write. With no roots configured, it says so and wires nothing — use `add` per repository.

## uninstall

```
uninstall [--purge] [--purge-vault --confirm NAME] [--dry-run]
```

Removes the hook entries, the `post-commit` blocks and the scheduled tasks. **Your notes are kept.**

| Flag | Effect |
| --- | --- |
| `--purge` | additionally delete every `.wiki-raw/` journal and the whole `wiki-state` directory (config, cursors, token) |
| `--purge-vault` | additionally delete the vault. Refused unless the directory holds both `AGENTS.md` and `projects.json`, contains nothing the scaffold did not create, and `--confirm <vault directory name>` is given |
| `--dry-run` | print what would happen and change nothing |

`--purge-vault` does not imply `--purge`; they are independent.

## status

Prints the vault path and whether it exists, the config file in use, the installed hooks, the scheduled task state and next run, engine availability, then one row per registered project with its page count, queued blocks and repository, and the last line of the run log.

## doctor

Verifies: vault present and complete; `settings.json` parses; the expected number of hooks is installed and each points at a file that exists; every repository under the roots has the `post-commit` hook; no non-history block has been unprocessed for over 7 days; the scheduled task exists. Prints `FAIL <problem> -- <fix>` per finding, or `all checks passed`. Exit 1 when anything failed.

## ingest / lint

```
ingest [--engine auto|claude|fds|ollama] [--limit N] [--dry-run]
lint   [--engine ...]
```

Turns queued blocks into pages now. `--limit` caps blocks this run (otherwise `max_blocks_per_run`). `--dry-run` prints each verdict without writing. `lint` runs the weekly consistency pass; it is implemented on the `claude` engine only and is a no-op elsewhere.

Exit 3 means no engine was available.

## backfill

```
backfill --project SLUG|all [--source git|sessions|both] [--since YYYY-MM-DD] [--max N] [--dry-run]
```

Queues historical blocks. `--source git` (default) writes one block per calendar day of commits, with subjects and files ranked by churn. `--source sessions` replays past Claude Code transcripts whose working directory resolves to that repository. `--since` defaults to `2026-01-01`, `--max` to 60 blocks per project.

Idempotent: a block id already present in the journal is skipped. Nothing is ingested — run `ingest` after, and remember each block is one model call.

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
| `engines` | `["claude", "ollama"]` | tried in order, first reachable wins |
| `claude.timeout_min` | `30` | kill the headless run after this long |
| `ollama.url` / `ollama.model` | `http://127.0.0.1:11434/v1/chat/completions`, `llama3.1` | local engine |
| `fds.url` / `fds.model` | `http://127.0.0.1:9655/…`, `deepseek-chat` | third-party gateway; add `"fds"` to `engines` to enable |
| `inter_call_ms` | `1000` | pause between model calls |
| `block_chars` | `6000` | block text sent to the model per call |
| `max_blocks_per_run` | `20` | cap per ingest run |
| `git_exclude_raw` | `true` | add `.wiki-raw/` to `.git/info/exclude` |
| `auto_install_git_hooks` | `true` | let the SessionStart hook wire repos under the roots |
| `schedule.*` | 6 h from 04:07, lint Sunday 05:07 | used at install time |

## State files

| Path | Contents |
| --- | --- |
| `wiki-state/wiki-config.json` | the config above |
| `wiki-state/<session-id>.json` | per-session transcript cursor and accumulated block |
| `wiki-state/ingest-runs.log` | one line per scheduled run |
| `wiki-state/ingest-last-run.txt` / `.err` | stdout and stderr of the last run |
| `wiki-state/wiki_record.log` | recorder diagnostics |
| `wiki-state/oauth-token.dpapi` | optional Claude token, DPAPI-encrypted |

## PowerShell entry points

```
bin/wiki-ingest-run.ps1 [-Mode ingest|lint] [-Engine auto|claude|fds|ollama] [-TimeoutMinutes 45]
bin/wiki_set_token.ps1
```

The runner is what Task Scheduler calls: it locates the package from its own path, loads the optional token, runs the ingest and logs the outcome. `wiki_set_token.ps1` stores a token from `claude setup-token` and verifies it can be read back.

The token is only injected by the runner. A direct `wikictl ingest` uses the `claude` engine only if `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` or a logged-in CLI credential file is present.

## Supporting CLIs

`bin/wiki_queue.py list|show|mark|stats` is the deterministic queue interface used by the ingest skill. `bin/wiki_install_git_hooks.py [--dry-run] [--uninstall] [--root DIR]` manages `post-commit` hooks alone. `bin/wiki_ingest.py --mode --engine --limit --dry-run` is the engine loop that `wikictl ingest` wraps.
