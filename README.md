# claude-code-wiki

A per-project knowledge base that writes itself from your Claude Code sessions and your commits.

Claude Code forgets everything between sessions. You re-explain the same architecture, re-derive the same diagnosis, and re-discover the constraint you already hit three weeks ago. Memory files help until they turn into a 20 KB wall of bullets. This keeps a real wiki instead — one directory of markdown per project, maintained by a scheduled job, and injected as a catalog at the start of every session so the model knows what it already knows.

It is an implementation of [Karpathy's LLM wiki idea](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) with the human taken out of the capture loop.

```
your session  ─┐
your commits  ─┼─►  <repo>/.wiki-raw/2026-08-05.md  ─►  ingest  ─►  vault/projects/<slug>/wiki/*.md
                                (machine journal)      (every 6h)          (curated pages)
                                                                                  │
                       SessionStart hook ◄─────────── catalog injected ◄──────────┘
```

## What it actually does

**Records.** A `Stop` hook reads the session transcript from a per-session cursor and appends one block per session: your intent (the prompts, minus harness noise), files touched with operation counts, shell commands, subagents. A git `post-commit` hook appends one block per commit: subject, body, author, files with churn. Both write to `<repo>/.wiki-raw/YYYY-MM-DD.md`. No model, no network, ~50 ms, and the hook can never break your session — it swallows everything and exits 0.

**Ingests.** A scheduled task reads the unprocessed blocks and decides what is worth keeping. Most blocks are not: restarts, reruns, typo fixes, exploration that concluded nothing. A block earns a page only when it records a decision and its reason, a diagnosis, a measured result, an architecture change, or a constraint discovered the hard way. Pages land in `projects/<slug>/wiki/{analyses,concepts,entities,sources}/`, get linked from the project catalog, and the run is logged with the engine that produced it.

**Injects.** A `SessionStart` hook puts the current project's catalog (not the pages — the catalog, ~1 KB) into context, along with the tail of the ingest log and a count of blocks not yet folded in. The model opens what it needs.

**Never loses a block.** `status=unprocessed` in the block marker is the only cursor. Ingest flips it to `status=processed date=…` and never edits a block body. A crashed run re-reads; it does not skip.

## Requirements

- Windows 10/11 (the scheduler and the token store are Windows-specific; the hooks and the ingest itself are portable — see [Non-Windows](#non-windows))
- Python 3.10+
- Git
- Claude Code
- An ingest engine: [Ollama](https://ollama.com) running locally is enough. A Claude token is better if you have one.

## Install

```powershell
git clone https://github.com/v0d3k/claude-code-wiki "$env:USERPROFILE\.claude\skills\claude-code-wiki"
python "$env:USERPROFILE\.claude\skills\claude-code-wiki\wikictl.py" install --vault "$env:USERPROFILE\llm-wiki" --root "$env:USERPROFILE\projects"
```

`--vault` is where curated pages live (point it inside an Obsidian vault if you use one). `--root` is a directory containing your repositories; pass it more than once for several. Install is idempotent — run it again after moving the package and it repairs every path.

Check it:

```powershell
python "$env:USERPROFILE\.claude\skills\claude-code-wiki\wikictl.py" status
python "$env:USERPROFILE\.claude\skills\claude-code-wiki\wikictl.py" doctor
```

Then open a repo in Claude Code, do some work, commit. `status` will show blocks queued. The first ingest runs on schedule, or force it:

```powershell
python "$env:USERPROFILE\.claude\skills\claude-code-wiki\wikictl.py" ingest
```

## Seeding history

Nothing is retroactive — the hooks only record forward. To fold in what already happened:

```powershell
# see the cost first: one block becomes one model call at ingest time
python wikictl.py backfill --project myapp --since 2026-01-01 --dry-run

python wikictl.py backfill --project myapp --since 2026-01-01 --max 60
python wikictl.py ingest
```

`--source git` (default) groups commits by calendar day, one block per day, with subjects and files by churn. `--source sessions` replays past Claude Code transcripts whose working directory resolves to that repo. `--source both` does both. Backfill is idempotent: a block id already present is skipped.

## Commands

| Command | What it does |
| --- | --- |
| `install [--vault P] [--root P]... [--no-schedule]` | config, vault scaffold, hooks, git hooks, scheduled tasks. Idempotent; also the repair command |
| `uninstall [--purge] [--purge-vault --confirm NAME]` | removes hooks, git hooks, tasks. **Keeps your notes** unless you ask twice |
| `status` | vault, hooks, schedule, engine availability, per-project page and queue counts |
| `doctor` | verifies every invariant and names the fix for each failure |
| `ingest [--engine E] [--limit N] [--dry-run]` | turn queued blocks into pages now |
| `backfill --project SLUG\|all [--source S] [--since D] [--max N]` | queue historical blocks |
| `add PATH` / `remove SLUG` | wire or unwire one repository |
| `search "QUERY"` | grep the vault |

Full reference: [docs/CLI.md](docs/CLI.md).

## Engines

The ingest picks the first engine that answers:

| Engine | Needs | Notes |
| --- | --- | --- |
| `claude` | a token from `claude setup-token`, stored by `bin/wiki_set_token.ps1` | Best pages. Runs `claude -p` with `Read,Glob,Grep,Edit,Write` and no shell |
| `ollama` | Ollama on `127.0.0.1:11434` | Default. Free, local, weaker prose |
| `fds` | a local FreeDeepSeek gateway | **Opt-in.** Forwards block content to a third-party service — not in the default engine list |

On the local engines the model never touches your filesystem. It returns one small JSON verdict — `skip` with a reason, or `write` with section, slug, title, summary and body — and Python does every write, path check, index edit and status flip. A hallucinating local model can produce a weak page; it cannot escape the vault or lose a block.

## What it puts on your machine

| Path | Purpose |
| --- | --- |
| `<vault>/` | the wiki: `AGENTS.md`, `index.md`, `projects.json`, `projects/<slug>/…` |
| `<repo>/.wiki-raw/` | the machine journal, one file per day |
| `<repo>/.git/hooks/post-commit` | four-line stub; appended to an existing POSIX hook, never to a non-shell one |
| `<repo>/.git/info/exclude` | `.wiki-raw/` added here, so the journal never shows in `git status` and is never committed |
| `~/.claude/settings.json` | three hook entries, each tagged `--owner=claude-code-wiki` |
| `~/.claude/wiki-state/` | config, per-session cursors, run logs, the optional token |
| Task Scheduler | `LLM-Wiki Ingest` (every 6 h, catches up on missed runs) and `LLM-Wiki Lint` (weekly) |

`uninstall` reverses all of it and leaves the notes. `settings.json` is backed up with a timestamp on every install.

## Security

The journal contains your prompts and shell commands verbatim, and the ingest feeds them to a model. That is a real attack surface, and the design takes it seriously:

- Everything captured is sanitized before it is written: block markers are neutralized so a commit message cannot forge or truncate a journal entry, and obvious secrets (`API_KEY=…`, `Bearer …`, `gh*_…`, `sk-…`, `scheme://user:pass@host`) are redacted.
- `.wiki-raw/` is added to `.git/info/exclude`, so a journal never reaches a public repo by accident.
- The unattended `claude` engine runs without `bypassPermissions` and without Bash.
- Block content is framed as untrusted data in the ingest prompt, and the injected catalog carries the same warning.
- Slug and section come from a model; both are validated, and every write asserts it stays inside the vault.

Details and the full threat model: [docs/SECURITY.md](docs/SECURITY.md).

## Non-Windows

The hooks (`wiki_record.py`, `wiki_commit.py`, `wiki_bootstrap.py`, `wiki_context.py`) and the ingest are plain Python and work anywhere. What is Windows-only: the scheduled tasks (`install` registers them through PowerShell) and the DPAPI token store. On macOS or Linux use `install --no-schedule` and drive the ingest from cron:

```
7 */6 * * * python ~/.claude/skills/claude-code-wiki/bin/wiki_ingest.py --engine auto
```

## Known limits

- Page quality tracks the engine. On a small local model, pages are correct but plain, and can over-explain; the per-project `log.md` records which engine wrote each run.
- `lint` is only implemented on the `claude` engine.
- A project directory is scaffolded on its first written page, so a repo where nothing durable has happened stays absent from the vault by design.
- Local models sometimes break the JSON contract. One repair retry at temperature 0 is built in; if that fails too the block stays queued for the next run.

## License

MIT. See [LICENSE](LICENSE).
