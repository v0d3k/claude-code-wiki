---
name: claude-code-wiki
description: Install, inspect, feed, back-fill or remove the per-project wiki (claude-code-wiki). Use for /wiki and phrases like "install the wiki", "wiki status", "backfill project history", "why is the wiki empty", "remove the wiki", and their equivalents in other languages.
trigger: /wiki
---

# /wiki

One CLI drives everything: `python ~/.claude/skills/claude-code-wiki/wikictl.py <command>`.

Run the command, then report what it printed in plain language. Do not re-implement any of this by hand — the CLI is the interface, and it is idempotent.

## Commands

| Ask | Command |
| --- | --- |
| поставь / установи / включи | `install` |
| статус / что стоит / сколько страниц | `status` |
| проверь / что сломалось | `doctor` |
| прогони / обнови вики сейчас | `ingest` |
| еженедельная проверка целостности | `lint` (только на claude-движке) |
| залей историю / backfill | `backfill --project <slug\|all> [--source git\|sessions\|both] [--since YYYY-MM-DD] [--max N]` |
| добавь проект | `add <path>` |
| убери проект | `remove <slug>` |
| найди в вики | `search "<query>"` |
| снеси / удали систему | `uninstall` |

Useful flags: `--dry-run` on `uninstall`, `backfill` and `ingest`; `--engine claude|fds|ollama` on `ingest`; `--vault PATH` and `--root PATH` on `install` (`--root` adds to the configured roots, `--replace-roots` overwrites them); `--limit N` on `search` (default 40).

## Rules

- **Always `--dry-run` first** for `uninstall` and for a `backfill` wider than one project. Show the user what would happen, then run it for real.
- **`uninstall` keeps the notes.** It removes hooks, git hooks and scheduled tasks. `--purge` additionally deletes the `.wiki-raw` journals and hook state; `--purge-vault` deletes the vault itself. Never pass either without the user asking for that specific loss, in those words.
- **`backfill` is not free.** Every queued block becomes one model call at ingest time. Estimate first: `--dry-run` prints the block count. Anything above ~50 blocks, tell the user the number and let them narrow `--since` or `--max` before running the ingest.
- **`install` is safe to re-run** and is the fix for most `doctor` failures, including a moved package or a stale hook path.
- **Never hand-edit** `settings.json` hooks, `.git/hooks/post-commit`, or `.wiki-raw` blocks for this system. Use the CLI so the marker contract stays intact.
- Report engine honestly: pages written by `ollama` or `fds` are weaker than `claude` ones, and `status` shows which engines are available. The per-project `log.md` records the engine for every run.

## What the system is

Four layers, described fully in `<vault>/AGENTS.md`:

1. `<repo>/.wiki-raw/YYYY-MM-DD.md` — machine-written journal. A `Stop` hook records each Claude session, a git `post-commit` hook records each commit. Never edited by hand.
2. `<vault>/projects/<slug>/wiki/` — curated pages, written by the ingest.
3. `<vault>/wiki/` — shared, cross-project pages and automation reports.
4. `<vault>/AGENTS.md` — the schema every agent follows inside the vault.

A `SessionStart` hook injects the current project's catalog into context, so an agent knows what already exists before re-deriving it. A scheduled task ingests every 6 hours, picking the first available engine: `claude -p` (needs a token), FreeDeepSeek `:9655`, or Ollama `:11434`.

## Files

```
~/.claude/skills/llm-wiki/
  wikictl.py            the CLI described above
  bin/                  hooks and engine modules (settings.json points here)
  templates/            vault skeleton copied on install
~/.claude/wiki-state/
  wiki-config.json      vault path, repo roots, engines, schedule
  ingest-runs.log       one line per scheduled run
  <session>.json        per-session recording cursors
```

## Troubleshooting

- **Wiki not filling up.** `status` first. `queued 0` everywhere means nothing durable happened yet, or hooks are not installed — `doctor` says which.
- **Blocks stuck unprocessed.** All engines down (`status` shows it), the local model failed the JSON contract twice, or the verdict failed validation (`rejected verdict:` in the run log — bad section, bad slug, or a body under 80 characters). The block stays queued and retries next run.
- **Nothing in a project's catalog.** Its directory is only scaffolded when the first page is written. Until then `SessionStart` stays silent for that repo, by design.
- **`claude=no token`.** Optional upgrade, not a fault: `claude setup-token`, then `bin/wiki_set_token.ps1`. That token is injected by the scheduled runner; a hand-run `ingest` picks the claude engine only when `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` is set or the CLI is logged in. The system runs on local engines without any of it.
