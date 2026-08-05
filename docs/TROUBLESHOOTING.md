# Troubleshooting

Three files answer most questions:

| File | Tells you |
| --- | --- |
| `wiki-state/wiki_record.log` | whether the recorder ran and what it decided |
| `wiki-state/ingest-runs.log` | one line per scheduled run: engine, counts, or why it stopped |
| `wiki-state/ingest-last-run.txt` / `.err` | full output of the last run |

Start with `wikictl.py status` and `wikictl.py doctor`. Nearly every structural problem is fixed by re-running `install`.

## Nothing is being recorded

`status` shows `queued 0` everywhere and no journal appears in the repo.

- **Hooks not installed.** `doctor` says so. Fix: `install`.
- **Hooks installed but the app has not reloaded them.** Claude Code reads `settings.json` at start; restart the app or open a new session.
- **Working directory is ignored on purpose.** Sessions inside the vault, inside the Claude config home, or exactly at your home directory never produce a journal — that is what keeps the ingest from recording itself.
- **Nothing durable happened.** A session with no file writes and no commands is skipped by design.
- **The repository is outside the configured roots.** `wiki_bootstrap` refuses to touch repositories you merely visited. Fix: `add <path>`, or add the parent directory with `install --root`.

Confirm the recorder itself works:

```powershell
type wiki-state\wiki_record.log
```

## Commits are recorded but sessions are not (or the reverse)

They are two independent hooks. `.git/hooks/post-commit` should contain the `# llm-wiki-record` marker; `settings.json` should contain three commands ending in `--owner=claude-code-wiki`. `doctor` checks both.

If `install` reports `skip (post-commit is not a POSIX shell script…)`, your repository already has a hook written in another language. Nothing was touched; call the recorder from your own hook instead:

```sh
python "<package>/bin/wiki_commit.py" >/dev/null 2>&1 || true
```

## Blocks stay unprocessed

`status` shows a growing `queued` count.

- **No engine available.** `status` prints each engine's state. Exit code 3 and `abort: no engine available` in the run log mean the same. Start Ollama, or store a Claude token.
- **The model broke the JSON contract twice.** The run log says `no usable verdict, left unprocessed`. One repair retry at temperature 0 is automatic; the block waits for the next run. A larger model fixes this.
- **The verdict was structurally invalid.** `rejected verdict: section=… slug=… body=…` means the model returned a section outside `analyses|concepts|entities|sources`, a slug that fails `^[a-z0-9][a-z0-9-]{2,79}$` or a Windows reserved name, or a body under 80 characters.
- **The write failed.** `write failed (…)` — a read-only vault, a path over the Windows limit, or the file open in another program.
- **Blocks arrive faster than `max_blocks_per_run`.** Raise it, or run `ingest` by hand.

Blocks are never lost: an unprocessed block is retried on every run until it succeeds.

## The scheduled task does not fire

```powershell
Get-ScheduledTask -TaskName 'LLM-Wiki Ingest' | Get-ScheduledTaskInfo
```

- `LastTaskResult` non-zero with no ingest-run log line: the runner could not start. Check that Python is on `PATH` for your account.
- The task runs but the log only says `skip: queue empty` — nothing to do, which is the normal state.
- `abort: engine script missing` — the package moved. Fix: `install`.
- Machine was asleep at 04:07: the trigger has `StartWhenAvailable`, so it catches up after wake. If it never does, re-register with `install`.

## The vault or a project catalog stays empty

- A project directory is scaffolded **on its first written page**. A repository where the ingest only ever skipped blocks has no directory, and the SessionStart hook stays silent for it. That is intended, not a fault.
- `status` shows pages `0` and queued `0` for every project: nothing durable has happened yet. Seed history with `backfill`.
- Pages exist but the master `index.md` has no row: `ensure_project` adds it when it scaffolds. If you created the directory by hand, add the row yourself.

## Hooks point at a stale path after moving the package

`doctor` says `hook points at a missing file`, or commits stop being recorded after you moved or renamed the directory.

Fix: `install` from the new location. It rewrites the `settings.json` entries and repairs every `post-commit` whose marker is present but whose command points elsewhere (reported as `path updated`).

## `claude=no token` in status

Not a fault — the strongest engine is optional. To enable it:

```powershell
claude setup-token
powershell -ExecutionPolicy Bypass -File <package>\bin\wiki_set_token.ps1
```

The token is injected by the scheduled runner. A direct `wikictl ingest` only uses the `claude` engine when `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` is in the environment, or the CLI itself is logged in.

## Ollama returns 404

The configured model is not pulled. `ollama list` shows what you have; set `ollama.model` in `wiki-config.json` to one of them, or `ollama pull llama3.1`.

## A page says something wrong

The engine that wrote it is recorded in `projects/<slug>/log.md`. Small local models stay factual about *what* changed but can over-explain *why*. Edit the page — it is yours; the ingest only appends `## Update <date>` sections to existing pages and never rewrites your text.

To re-ingest a block after fixing the cause, flip its status back:

```powershell
# in the journal file, change status=processed date=… back to status=unprocessed
python bin\wiki_queue.py list
```

## What `doctor` checks

| Check | Failure means |
| --- | --- |
| vault exists, has `AGENTS.md`, `index.md`, `projects.json` | never installed, or the vault was moved/deleted — run `install` |
| `settings.json` parses | hand-edited into invalid JSON; fix it before anything else |
| three hooks installed, each pointing at an existing file | package moved or partially installed — run `install` |
| every repository under the roots has the post-commit marker | a new repository appeared — run `install` (the scheduled run also sweeps) |
| no non-history block unprocessed for over 7 days | the ingest has been failing; read `ingest-runs.log` |
| scheduled task registered | installed with `--no-schedule`, or the task was removed |

## Starting over

`uninstall` then `install` is safe and keeps every page. To also drop the journals and cursors, `uninstall --purge`. The vault is only deleted with `--purge-vault --confirm <vault directory name>`, and only if it looks like a vault this system created.
