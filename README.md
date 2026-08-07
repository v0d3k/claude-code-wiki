# claude-code-wiki

**A per-project memory that writes itself — and a guard that stops your assistant from rebuilding what already exists.**

*English · [Русский](README.ru.md)*

Claude Code forgets everything between sessions. You re-explain the same architecture, re-derive the same diagnosis, and re-discover the constraint you already hit three weeks ago. Then, inside a session, it writes a helper that already exists in twenty other files — because nothing answered *"does this exist?"* at the moment it mattered.

This fixes both, from the same set of hooks, using the model you are already paying for.

```
your session  ─┐
your commits  ─┼─►  <repo>/.wiki-raw/2026-08-05.md  ─►  ingest  ─►  vault/projects/<slug>/wiki/*.md
                                (machine journal)     (in-session)       (curated pages)
                                                                                │
                     SessionStart hook ◄────────────  catalog injected ◄────────┘

your commits  ────►  symbol index  ────►  PreToolUse guard: "num already exists in 43 files"
your commits  ────►  structure index ──►  wikictl map / path / levers, and ~1 KB of orientation
                                          at session start: what everything depends on, and
                                          which tables have more than one writer
```

---

## Contents

| | |
| --- | --- |
| [Two problems, measured](#two-problems-measured) | what goes wrong, in numbers from a real repository |
| [What it does](#what-it-does) | the four moving parts: record, ingest, guard, map |
| [Install](#install) | two commands, plus how to seed history |
| [What a page looks like](#what-a-page-looks-like) | a real generated page and the vault layout |
| [Commands](#commands) | every verb in one table |
| [Design notes](#design-notes) | why capture never depends on a model |
| [Works alongside a language server (Serena)](#works-alongside-a-language-server-serena) | what each one can and cannot answer |
| [Configuration](#configuration) | the config file and what each key changes |
| [What it puts on your machine](#what-it-puts-on-your-machine) | every path it touches, and how to undo it |
| [Security](#security) | the journal is an attack surface; what is done about it |
| [Honest limits](#honest-limits) | what it does not do |

---

## Two problems, measured

These are numbers from one real repository — 1231 commits, 4400 files — not hypotheticals. Every
count and ratio below was measured. Identifiers are renamed throughout, because the repository is
private; nothing else about the examples is altered.

**Knowledge evaporates.** A hand-maintained wiki covered phases 187–267 and then stopped for seven weeks while the project kept moving. Nobody forgot on purpose; updating it simply depended on someone remembering to ask. Meanwhile the things that mattered — which PnL number is trustworthy, why a feature was built and then disabled, which measurement turned out to be self-fulfilling — lived only in commit messages and in one person's head.

**Code duplicates.** In the same repository:

```
IDENTICAL  109 names   same name, same body -- consolidate mechanically
DIVERGED   337 names   same name, different bodies -- one name, several behaviours
RENAMED     66 groups  same body under different names -- invisible to any name check

num          43 definitions, 13 different implementations
tableExists  20 definitions, 12 different implementations
```

Thirteen implementations of `num` is not a style problem. Three of them disagree about the empty string: one returns `0`, another returns `null`. Half the `tableExists` copies swallow a database error and return `false`, so "table missing" and "database unreachable" become indistinguishable. That is where the quiet bugs live.

Both problems have the same shape: **the information exists, and nothing puts it in front of the model at the moment of the decision.**

---

## What it does

**Records, deterministically.** A `Stop` hook reads the session transcript from a per-session cursor and appends one block per session: your intent (the prompts, minus harness noise), files touched with operation counts, shell commands, subagents. A git `post-commit` hook appends one block per commit. No model, no network, ~50 ms, and the hook can never break your session — it swallows everything and exits 0.

**Ingests with your own model.** When the queue is worth it, the `Stop` hook exits with a wake-up code and Claude Code hands the queue to the model in your session. No API token, no second subscription, no background daemon. If you work in Claude, Claude writes your wiki. Most blocks deserve no page at all — restarts, reruns, typo fixes — and the ingest is told to say so and move on.

**Injects the catalog, not the pages.** A `SessionStart` hook puts the project's table of contents (~1 KB) into context along with the tail of the ingest log. The model opens what it needs instead of carrying 200 KB it mostly does not.

**Guards against duplicates.** A symbol index stays in step with every commit, holding both the
name and a hash of the normalised body of every definition. That second half matters, because
duplication comes in three kinds that need three different answers:

| | What the guard says |
| --- | --- |
| **identical** — same body already here | "identical body already here (8 places) … Import it." |
| **renamed** — same body, new name | "this exact body already exists as `esc`, `escHtml` in 12 places" |
| **diverged** — name taken, different behaviour | "name taken in 20 files with 12 different implementations" |

The renamed case is the one no name-based check can see. Measured on one repository: 66 groups
of bodies living under several names, including a single `escHtml` body spread across `esc`,
`escapeHtml` and `escHtmlLite`, and one `num` body also called `finiteOrNull`, `finiteNumber`
and `finite`. Writing `renderSafeText` with a body that already exists as `esc` is caught before
the file lands:

```
- `renderSafeText` — this exact body already exists as `esc`, `escHtml`,
  `escapeAlertHtml` in 12 place(s): src/report/dailyReportHtml.js,
  src/delivery/notifyHub.js and 9 more.
```

It warns and never blocks. A same-named local helper is sometimes correct, and a hook cannot tell.

**Maps the structure.** A second index records who imports whom, and who writes to each shared
resource — a SQL table, an env key, an emitted event. It refreshes on every commit, so it cannot
drift the way a generated snapshot does. `wikictl map` prints the shape, `wikictl path A B` shows
how two files connect, `wikictl levers positions` lists every writer of one table.

The second half is the half an import graph cannot give you. On the same repository, the ledger
module is imported by 11 non-test files. Twenty-one other non-test files write to the ten tables it
owns — and **not one of them imports it**. The import graph shows you the eleven; the twenty-one
that break when you change a column are invisible to it.

```
signal_records   56 writers   (17 outside the test suite)
outcomes         23
positions        22
```

Counts include tests, which is usually what you want before a migration and occasionally not —
`levers` and `map` always print the split (`signal_records: 56 file(s) (17 outside tests)`), and
`--no-tests` narrows the listing itself to the non-test writers.

Static requires and literal SQL only. Dynamic requires, ORM calls and computed table names are
invisible to this index, and every command that prints from it says so.

---

## Install

```bash
git clone https://github.com/v0d3k/claude-code-wiki ~/.claude/skills/claude-code-wiki
python ~/.claude/skills/claude-code-wiki/wikictl.py install --root ~/projects
```

That is the whole setup. `--vault` defaults to `~/llm-wiki` — point it inside an Obsidian vault if you use one. `--root` is a directory holding your repositories, repeatable. Install is idempotent and doubles as the repair command: run it again after moving the package and every path is rewritten.

```bash
python ~/.claude/skills/claude-code-wiki/wikictl.py status
python ~/.claude/skills/claude-code-wiki/wikictl.py doctor
```

Then work normally. After a few sessions the queue fills, the model is asked to fold it in, and pages appear. Nothing is retroactive — to seed history:

```bash
wikictl.py backfill --project myapp --since 2026-01-01 --dry-run   # see the cost first
wikictl.py backfill --project myapp --since 2026-01-01 --max 60
wikictl.py ingest
```

`--source git` groups commits by calendar day, one block per day. `--source sessions` replays past Claude Code transcripts. Each queued block becomes one model call at ingest time, so check the dry-run count before choosing a large window.

**Tell your assistant the wiki exists.** The hooks deliver the data; the behaviour — consult before re-deriving, check before writing a helper, file durable findings — is policy, and policy belongs in `CLAUDE.md`. A ready-to-paste block: [docs/CLAUDE-MD.md](docs/CLAUDE-MD.md).

---

## What a page looks like

Pages are short, sourced, and honest about uncertainty. This one was written by a local model from a single commit:

```markdown
# Respecting Retry-After and aborting cross-account auto-continuation

The server now honors Retry-After headers on 429 responses and aborts
auto-continuation across accounts.

## Status
Logic added in `server.js` checks for the header and pauses until it elapses;
if the error originates from a different account, auto-continuation is aborted.

## Sources
- `.wiki-raw` block `bf20260609`, ingested 2026-08-05.
```

The vault is plain markdown with relative links — it renders as a graph in Obsidian and reads fine in any editor.

```
<vault>/
  AGENTS.md              the schema every agent follows here
  index.md               master catalog
  projects.json          registry, written by the hooks
  projects/<slug>/
    index.md             the project's table of contents — this is what gets injected
    log.md               every ingest: what was written, what was skipped, which model ran
    wiki/{analyses,concepts,entities,sources}/
```

---

## Commands

| Command | What it does |
| --- | --- |
| `install [--vault P] [--root P]... [--schedule]` | config, vault scaffold, hooks, git hooks. Idempotent; also the repair command |
| `uninstall [--purge] [--purge-vault --confirm NAME]` | removes hooks and git hooks. **Keeps your notes** unless you ask twice |
| `status` | vault, hooks, ingest wiring, engines, per-project page and queue counts |
| `doctor` | verifies every invariant and names the fix for each failure |
| `ingest [--engine E] [--limit N] [--dry-run]` | turn queued blocks into pages now |
| `backfill --project SLUG\|all [--source S] [--since D]` | queue historical blocks from git or past transcripts |
| `where NAME` | every definition of a name, with `file:line` |
| `dupes [--kind identical\|diverged\|renamed]` | duplication split by kind, worst first |
| `symbols [--full]` | rebuild the symbol index |
| `map [--top N] [--rebuild] [--write]` | fan-in, fan-out, and the resources several files write to |
| `path A B` | the import route between two files, or why there isn't one |
| `levers NAME` | every file that writes this table, reads this env key, or emits this event |
| `search "QUERY"` | grep the vault |
| `schedule [install\|remove\|status]` | optional background ingest: Task Scheduler or cron |
| `add PATH` / `remove SLUG` | wire or unwire one repository |

Full reference: [docs/CLI.md](docs/CLI.md). Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Threat model: [docs/SECURITY.md](docs/SECURITY.md). When something misbehaves: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

---

## Design notes

**Capture never depends on inference.** The hooks are plain Python: no model, no network, no daemon. If your quota is gone, the network is down, or the model is confused, recording still works and the queue simply grows. A queue that grows is recoverable; a session that vanished is not.

**One cursor, and it lives in the data.** A block is `status=unprocessed` until the ingest flips it to `status=processed date=…`. There is no database, no offset file, no "last run" timestamp that can drift out of sync with reality. A crashed run re-reads and re-decides; it cannot silently skip.

**Blocks are append-only and frozen once processed.** A session block is rewritten in place while its session is alive, then never again. Every captured string is sanitized first, so a commit message containing the marker syntax cannot forge or truncate a journal entry.

**The index is regex-based on purpose.** A language server knows more, but it needs a daemon and a warm-up and cannot be queried from a git hook. This has to answer during a `PreToolUse` call. Full build of 4400 files: ~2 s. Incremental update after a commit: only the files in that commit. JavaScript, TypeScript and Python are recognised.

**Latency is measured, not assumed.** On Windows, against a 4400-file repository, the guard costs ~115 ms per `Write` — of which 82 ms is Python interpreter start and 32 ms is the actual index work. Resolving the repository by walking up for `.git` rather than spawning `git rev-parse` saved 30 ms of that. Adding the structure index did not move this number, because the guard never loads it. On `Write` this is a handful of calls per session; on `Edit` it would be every edit, which is why `guard_on_edit` defaults to off.

**The ingest cannot feed itself.** It writes into the vault, and the recorder ignores any session whose working directory is inside the vault or the Claude config home. Without that, every ingest would produce a block describing the ingest.

---

## Works alongside a language server (Serena)

This ships its own symbol index because a guard has to run inside a `PreToolUse` hook, in
milliseconds, with no daemon. That constraint costs accuracy: the index knows names and
locations, nothing else. It cannot tell you who calls a function, which of two same-named
symbols is in scope, or what a type resolves to.

[Serena](https://github.com/oraios/serena) answers exactly those questions, over a real
language server, and the two do not overlap:

| | this index | Serena |
| --- | --- | --- |
| Reachable from a hook or a script | yes | **no** — MCP tools only, inside a session |
| Lookup cost | ~8 ms | language-server round trip |
| Knows | names, `file:line`, imports, SQL writes | symbols, references, scope, types |
| "Who calls this?" | no | yes |
| "Who else writes this table?" | yes | no — a language server does not read SQL strings |
| Symbol-precise edits | no | yes |
| Can power an automatic guard | yes | no |

Read it as prevention versus investigation. The index is the tripwire that fires whether or
not anyone thought to ask; Serena is the scalpel once the model is already looking. Neither
replaces the other: a language server cannot be queried from a git hook, and a regex index
cannot resolve a reference.

If you want both, register Serena with Claude Code and keep the guard as-is:

```bash
claude mcp add serena -s user -- serena start-mcp-server --project-from-cwd
```

## Configuration

`<config home>/wiki-state/wiki-config.json` — config home is `$CLAUDE_CONFIG_DIR` or `~/.claude`. Any missing key falls back to its default.

```json
{
  "vault": "~/llm-wiki",
  "roots": ["~/projects"],
  "auto_ingest": "rewake",
  "auto_ingest_min_blocks": 3,
  "auto_ingest_cooldown_min": 60,
  "guard_enabled": true,
  "guard_on_edit": false,
  "guard_min_existing": 1,
  "engines": ["claude", "ollama"]
}
```

`auto_ingest` accepts `rewake` (the running model is asked to ingest), `notify` (the pending count is mentioned at session start) or `off`. `engines` matters only for unattended runs started by `schedule install` — there is no session to borrow a model from, so it needs a Claude credential in the environment or Ollama on `127.0.0.1:11434`. On the Ollama path the model never touches your filesystem: it returns one small JSON verdict and Python does every write, path check and status flip.

---

## What it puts on your machine

| Path | Purpose |
| --- | --- |
| `<vault>/` | the wiki |
| `<repo>/.wiki-raw/` | the machine journal, one file per day |
| `<repo>/.git/hooks/post-commit` | four-line stub, appended to an existing POSIX hook, never to a non-shell one |
| `<repo>/.git/info/exclude` | `.wiki-raw/` added here, so the journal never shows in `git status` or reaches a public repo |
| `~/.claude/settings.json` | four hook entries, each tagged `--owner=claude-code-wiki` |
| `~/.claude/wiki-state/` | config, session cursors, run logs, the symbol and structure indexes |

`uninstall` reverses all of it and leaves the notes. `settings.json` is backed up with a timestamp on every write, and only entries carrying the owner flag are ever removed — your other hooks are untouched.

---

## Security

The journal holds your prompts and shell commands verbatim, and the ingest shows them to a model. That is a real attack surface and the design treats it as one: block markers are neutralized so a commit message cannot forge a journal entry, obvious secrets are redacted, `.wiki-raw/` is added to `.git/info/exclude`, the unattended engine runs without a shell and without permission bypass, block content is framed as untrusted data, and every model-supplied path is validated before a write. Details and residual risk: [docs/SECURITY.md](docs/SECURITY.md).

---

## Honest limits

- **The pages are not a code map.** They record decisions and findings. "Where is the function that does X" is answered by `where`; "what depends on this" by `map` and `levers` — not by the prose.
- **The structure index is a parser, not a compiler.** It sees static requires and literal SQL. A table name built at runtime, an ORM call, a worker started by a process manager — all invisible. Treat a lever count as a floor, never a total.
- **Page quality tracks whichever model ran.** A small local model stays factual about what changed but can over-explain why. The per-project `log.md` names the model for every pass, so weak pages are attributable.
- **A project appears in the vault only when its first page is written.** A repository where nothing durable has happened stays absent by design.
- **The guard compares bodies textually, not semantically.** Two functions that do the same thing with different code read as different. Normalisation strips comments and whitespace only; renaming a parameter is enough to hide a copy. `dupes --kind renamed` shows what it does catch.
- **`lint` is implemented on the `claude` engine only.**
- Requires Python 3.10+, Git and Claude Code. Scheduling and the DPAPI token store are the only Windows-specific parts; everything on the default path is cross-platform.

---

## License

MIT. See [LICENSE](LICENSE).
