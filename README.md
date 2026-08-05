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

**Ingests — with the model you are already using.** When the queue is worth it, the `Stop` hook exits with a wake-up code and Claude Code hands the queue to the model running in your session. No API token, no second subscription, no background process: if you work in Claude, Claude writes the wiki. The ingest decides what is worth keeping. Most blocks are not: restarts, reruns, typo fixes, exploration that concluded nothing. A block earns a page only when it records a decision and its reason, a diagnosis, a measured result, an architecture change, or a constraint discovered the hard way. Pages land in `projects/<slug>/wiki/{analyses,concepts,entities,sources}/`, get linked from the project catalog, and the run is logged with the engine that produced it.

**Guards against duplicates.** A symbol index of the repository is kept in step with every commit. When new code declares a name that already exists elsewhere, a `PreToolUse` hook says so before the file is written, with the locations. This is the other half of the memory problem: the wiki remembers decisions, the index remembers what is already implemented.

**Injects.** A `SessionStart` hook puts the current project's catalog (not the pages — the catalog, ~1 KB) into context, along with the tail of the ingest log and a count of blocks not yet folded in. The model opens what it needs.

**Never loses a block.** `status=unprocessed` in the block marker is the only cursor. Ingest flips it to `status=processed date=…` and never edits a block body. A crashed run re-reads; it does not skip.

## Requirements

- Windows 10/11 (the scheduler and the token store are Windows-specific; the hooks and the ingest itself are portable — see [Non-Windows](#non-windows))
- Python 3.10+
- Git
- Claude Code
- Nothing else. The ingest runs in your session with your own model. [Ollama](https://ollama.com) is optional, and only for unattended runs.

## Install

```bash
git clone https://github.com/v0d3k/claude-code-wiki ~/.claude/skills/claude-code-wiki
python ~/.claude/skills/claude-code-wiki/wikictl.py install --root ~/projects
```

That is the whole setup. `--vault` defaults to `~/llm-wiki`; point it inside an Obsidian vault if you use one. `--root` is a directory holding your repositories, repeatable.

Install is idempotent — run it again after moving the package and it repairs every path.

Check it:

```powershell
python "$env:USERPROFILE\.claude\skills\claude-code-wiki\wikictl.py" status
python "$env:USERPROFILE\.claude\skills\claude-code-wiki\wikictl.py" doctor
```

Then open a repo in Claude Code, do some work, commit. Once a few blocks pile up, the next time a session ends the model is asked to fold them in — you will see it run `/wiki-ingest` and report what it filed. Nothing to configure, nothing to remember.

Force a pass any time by saying `/wiki-ingest`, or from the shell:

```bash
python ~/.claude/skills/claude-code-wiki/wikictl.py ingest
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

## How the ingest happens

**In session, by default.** The `Stop` hook counts the queue. Past the threshold it exits with the wake-up code, and Claude Code asks the model in that session to run `/wiki-ingest`. Whatever model you work with is the model that writes your wiki. Rate-limited so it never nags: at least 3 blocks, at most one nudge an hour, never twice in the same session.

```json
"auto_ingest": "rewake",           // "notify" = only mention it at session start, "off" = neither
"auto_ingest_min_blocks": 3,
"auto_ingest_cooldown_min": 60
```

**Unattended, if you want it.** `wikictl schedule install` adds a background run — Task Scheduler on Windows, cron everywhere else. That path needs an engine of its own, since no session is around:

| Engine | Needs |
| --- | --- |
| `claude` | `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, or a logged-in CLI (`claude setup-token`, then `bin/wiki_set_token.ps1` on Windows) |
| `ollama` | Ollama on `127.0.0.1:11434` |

On the `ollama` path the model never touches your filesystem: it returns one small JSON verdict — `skip` with a reason, or `write` with section, slug, title, summary and body — and Python does every write, path check, index edit and status flip. A hallucinating local model can produce a weak page; it cannot escape the vault or lose a block.

## The duplicate guard

Large codebases accumulate the same helper over and over, because nothing answers "does this already
exist" at the moment of writing. Measured on one real 4400-file repository: 458 names defined in more
than one file, 1424 redundant definitions, one helper present 43 times in 11 different implementations.

```
$ wikictl where tableExists
tableExists: 20 definition(s) in crypto-bot
  src/analytics/advisorOutcomeCanonicalR.js:3
  src/analytics/bybitOutcomeBackfillCandidates.js:51
  ...

$ wikictl dupes --limit 5
458 name(s) defined in more than one file, 1424 redundant definition(s)
```

When a `Write` declares such a name, the guard injects a note naming the existing locations. It never
blocks — a same-named local helper is sometimes right, and a hook cannot tell. Configure with:

```json
"guard_enabled": true,
"guard_on_edit": false,     // Write only by default; see the cost below
"guard_min_existing": 1     // warn once the name exists in this many other files
```

**Cost, measured.** The whole hook is 188 ms, of which 180 ms is Python interpreter start and about
8 ms is the actual lookup. On `Write` that is a handful of calls per session, so it disappears. On
`Edit` it would be every single edit, which is why `guard_on_edit` is off by default — turn it on if
you would rather pay a few seconds per session than review the warnings later.

The index itself is regex-based, not language-server-based: it must run inside a git hook in
milliseconds with no daemon. Full build of 4400 files takes about 2 seconds; the post-commit hook
re-parses only the files in the commit. JavaScript, TypeScript and Python are recognised.

## What it puts on your machine

| Path | Purpose |
| --- | --- |
| `<vault>/` | the wiki: `AGENTS.md`, `index.md`, `projects.json`, `projects/<slug>/…` |
| `<repo>/.wiki-raw/` | the machine journal, one file per day |
| `<repo>/.git/hooks/post-commit` | four-line stub; appended to an existing POSIX hook, never to a non-shell one |
| `<repo>/.git/info/exclude` | `.wiki-raw/` added here, so the journal never shows in `git status` and is never committed |
| `~/.claude/settings.json` | three hook entries, each tagged `--owner=claude-code-wiki` |
| `~/.claude/wiki-state/` | config, per-session cursors, run logs, the symbol index, the optional token |
| Task Scheduler / crontab | only if you run `schedule install` |

`uninstall` reverses all of it and leaves the notes. `settings.json` is backed up with a timestamp on every install.

## Security

The journal contains your prompts and shell commands verbatim, and the ingest feeds them to a model. That is a real attack surface, and the design takes it seriously:

- Everything captured is sanitized before it is written: block markers are neutralized so a commit message cannot forge or truncate a journal entry, and obvious secrets (`API_KEY=…`, `Bearer …`, `gh*_…`, `sk-…`, `scheme://user:pass@host`) are redacted.
- `.wiki-raw/` is added to `.git/info/exclude`, so a journal never reaches a public repo by accident.
- The unattended `claude` engine runs without `bypassPermissions` and without Bash.
- Block content is framed as untrusted data in the ingest prompt, and the injected catalog carries the same warning.
- Slug and section come from a model; both are validated, and every write asserts it stays inside the vault.

Details and the full threat model: [docs/SECURITY.md](docs/SECURITY.md).

## Platforms

Everything the default path needs — the hooks, the queue, the in-session ingest — is plain Python and works on Windows, macOS and Linux. `wikictl schedule` picks Task Scheduler or cron for you. The only Windows-specific piece is the DPAPI token store, and it matters solely for unattended runs on the `claude` engine; elsewhere export `CLAUDE_CODE_OAUTH_TOKEN` in the cron environment instead.

## Known limits

- Page quality tracks whichever model ran. The per-project `log.md` records it for every pass.
- `lint` is only implemented on the `claude` engine.
- A project directory is scaffolded on its first written page, so a repo where nothing durable has happened stays absent from the vault by design.
- Local models sometimes break the JSON contract. One repair retry at temperature 0 is built in; if that fails too the block stays queued for the next run.

## License

MIT. See [LICENSE](LICENSE).
