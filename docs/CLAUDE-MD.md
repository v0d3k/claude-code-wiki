# Telling your assistant the wiki exists

The hooks deliver data: the catalog arrives at session start, the guard warns before a duplicate is written. What hooks cannot do is change *behaviour* — consult the wiki before re-deriving, check a name before declaring it, file a finding while it is still fresh. That belongs in `CLAUDE.md`.

Paste this into `~/.claude/CLAUDE.md` (applies everywhere) or into a project's `CLAUDE.md` (that project only). Adjust the vault path if you moved it.

---

## The block

```markdown
# claude-code-wiki

Long-term memory for this project: a wiki of decisions plus a symbol index.
CLI: `~/.claude/skills/claude-code-wiki/wikictl.py`. Vault: `~/llm-wiki`
(`AGENTS.md` is the schema, `projects/<slug>/index.md` the catalog, `log.md` the history).

**Read before re-deriving.** The current repository's catalog is injected at session
start. Before working out the architecture again, reproducing an old diagnosis,
recomputing a metric or proposing something already rejected, open the relevant page.
The wiki outranks re-reading code and logs; read code to verify when the wiki is
silent or contradicted. Cite the pages you used. No catalog in context means the
project has no pages yet — that is normal.

**Check before writing code.** Before declaring a function or helper, run
`wikictl.py where <name>`. In a large repository the thing you need usually exists
already. The guard will warn on `Write` anyway, but not reaching that point is better.
`wikictl.py dupes` shows the overall picture.

**Look at the structure before changing it.** `wikictl.py map` names the modules
everything depends on and the resources several files write to; `wikictl.py levers
<table>` answers "who else touches this" before you change a schema or a write path;
`wikictl.py path <a> <b>` shows how two files connect. The orientation injected at
session start is a summary of the first of those, not a substitute for any of them.

**File durable findings immediately.** A decision with its reason, a diagnosis, a
measurement with its sample size, an architecture change, a trap someone will hit
again — write the page under `projects/<slug>/wiki/` and link it from that project's
`index.md`. Do not wait for the next ingest.

**Never hand-edit:** `<repo>/.wiki-raw/` (machine journal; only the `status` flag may
change, only via `bin/wiki_queue.py mark`), the hooks in `settings.json`, this system's
git hooks, or the symbol index. Everything goes through `wikictl.py`.
```

---

## Why each rule is there

**"Read before re-deriving"** is the one that pays for the whole system. Without it a model treats the injected catalog as decoration and re-analyses from scratch anyway — the catalog is a list of titles, and nothing compels opening them. Naming the specific failure modes (architecture, old diagnoses, metrics, rejected options) works better than a general "use the wiki", because those are the four things assistants actually redo.

**"Look at the structure before changing it"** covers the coupling a name check cannot see. On the repository these docs quote, the ledger module is imported by 11 non-test files, while 21 other non-test files write to the ten tables it owns — with no overlap at all. Every one of those 21 depends on its schema and none of them can be found by following imports. `levers` is the cheap way to ask that before a migration.

**"Check before writing code"** exists because the guard is a net, not a plan. It fires when a file is being written, which is already late: the model has composed the helper and now has to undo it. A `where` call before writing costs one tool call and avoids the rework. Keep both — the instruction for the disciplined path, the guard for when discipline slips.

**"File durable findings immediately"** counteracts a real bias. Left alone, an assistant reports a finding in chat and moves on, and the finding dies with the session. The ingest catches what the journal recorded, but a conclusion you reached by reasoning — not by editing a file — may leave no trace in the journal at all. Writing it as a page at the time is the only reliable capture.

**"Never hand-edit"** protects the invariants. The `status` flag is the ingest's only cursor; an assistant that "helpfully" tidies the journal or rewrites a hook path breaks either the queue or the wiring, and the failure is silent. Routing everything through the CLI keeps the marker contract intact.

---

## Optional additions

**If your team writes pages in a language other than English**, say so explicitly — the schema in `AGENTS.md` is written in English and models follow it by default:

```markdown
Wiki pages are written in <language>. Code, paths, commands and commit messages stay untranslated.
```

**If you want the assistant to be stricter about the guard**, promote the warning to a rule:

```markdown
When the duplicate guard reports an existing definition, do not proceed with a second
copy without saying why the existing one does not fit.
```

**If you keep the wiki inside an Obsidian vault** shared across machines, warn about sync races:

```markdown
The vault is synced. Never assume a page you wrote is already on another device;
re-read before appending.
```

**If Serena (or another language-server MCP) is available**, say which tool answers which
question, so the assistant does not grep when it could resolve references:

```markdown
For "where is this defined" use `wikictl.py where <name>` — it is instant and always available.
For "who calls this", "which overload is in scope" or a symbol-precise edit, use the Serena tools.
The duplicate guard stays the safety net either way.
```

**If you use the unattended ingest** (`schedule install`), remember it runs with no human present. Nothing in `CLAUDE.md` reaches that run — it follows `SKILL.md` and the schema only. Keep policy that matters for unattended behaviour in `AGENTS.md`, not here.

---

## What not to put in

Do not restate the CLI surface. Every subcommand is already in the skill description that Claude Code loads, and duplicating it costs context on every session while going stale the moment a flag changes.

Do not describe the pipeline mechanics (which hook fires when, how blocks are marked). The model does not need it to behave correctly, and `docs/ARCHITECTURE.md` is one read away when it does.

Keep the block short. It is loaded into every session of every project, so each line should change what the assistant does — not explain what the system is.
