---
name: wiki-ingest
description: Turn unprocessed .wiki-raw blocks into per-project LLM-Wiki pages. Use when the user says /wiki-ingest, "обнови вики", "прогони ingest", or when the scheduled autonomous run fires.
trigger: /wiki-ingest
---

# /wiki-ingest

Ingest hook-recorded raw blocks into the per-project wiki at `<vault>`.

Schema of record: `<vault>/AGENTS.md`. It wins over this file if they ever disagree.

## Non-negotiables

- **Selectivity beats volume.** Most blocks deserve no page. A block that records "restarted the bot, ran tests" is noise — mark it processed, write nothing. Only durable facts earn a page: a decision and its reason, a diagnosis, a measured result with its sample size, an architecture change, a constraint discovered the hard way.
- **Never edit a raw block's body.** The only mutation allowed is the `status` flag, and only through `wiki_queue.py mark`.
- **Never invent.** If a block is thin, either open the referenced files or commits to verify, or write nothing. A wrong wiki page is worse than a missing one.
- **Update before you create.** Search the project's existing pages first; extending a page beats adding a fifth near-duplicate.
- **Mark everything you consumed**, including the blocks you decided were noise. An unmarked block will be re-read forever.

## Run it in session, and delegate

This skill is meant to run **inside a Claude Code session**, so the pages are written by the model
you are already working with. Two rules follow from that:

- **Delegate to a subagent.** Spawn one general-purpose agent and have it follow this procedure end
  to end, then report back in a paragraph. The queue can be dozens of blocks; reading them in the
  main thread burns the context you need for actual work. The subagent inherits the session's model.
- **Do not shell out to `wikictl ingest` when a session is available.** That entry point exists for
  unattended runs: it spawns a separate `claude -p` process with its own credentials, and if those
  are missing or rate-limited it falls back to whatever local engine is configured. Same skill, worse
  model, and no way to tell from the page afterwards except the engine name in `log.md`.

## Procedure

0. **Claim the queue.** Another run — a scheduled one, or a second session — can be working the same
   blocks; that has already produced duplicate pages once.
   ```bash
   python ~/.claude/skills/claude-code-wiki/bin/wiki_queue.py claim --who session
   ```
   If it refuses, somebody else holds it: say so and stop. Release it when you are done, including
   when you stop early:
   ```bash
   python ~/.claude/skills/claude-code-wiki/bin/wiki_queue.py release
   ```

1. **Queue.**
   ```bash
   python ~/.claude/skills/claude-code-wiki/bin/wiki_queue.py list
   ```
   Empty queue → say so and stop. Do not invent work.

2. **Orient once per project.** Read `<vault>/projects/<slug>/index.md`. If the project has no directory, copy `<vault>/projects/_template/` to `<vault>/projects/<slug>/`, fill in the placeholders, and add a row to the master `LLM-Wiki/index.md`.

3. **Read blocks oldest first.**
   ```bash
   python ~/.claude/skills/claude-code-wiki/bin/wiki_queue.py show --file <file> --id <id>
   ```
   Group related blocks: a session block and the commit blocks it produced are one story, not three.

4. **Verify what matters.** For anything you are about to state as fact, check the source: `git -C <repo> show <sha>`, or read the file at the path the block names. Numbers must come from a real artifact, never from a prompt's phrasing.

5. **Write.** For each durable finding, create or update a page under `<vault>/projects/<slug>/wiki/{analyses,concepts,entities,sources}/`:
   - one-line summary, then `## Status`, then the body
   - cite `path/to/file.js:line`, commit shas, and dates
   - label anything unverified as unverified
   - link related pages with relative markdown links

6. **Index and log.** Add new pages to `projects/<slug>/index.md`. Append one entry to `projects/<slug>/log.md`:
   ```
   ## [YYYY-MM-DD] ingest | <short subject>
   - <what was consumed: N blocks, which ids>
   - <what was written or updated>
   - <what was deliberately skipped as noise>
   ```

7. **Mark.**
   ```bash
   python ~/.claude/skills/claude-code-wiki/bin/wiki_queue.py mark --file <file> --id <id> --id <id>
   ```

8. **Release the lock**, then **report.** One short paragraph: blocks consumed, pages written or updated, blocks dropped as noise. If the queue was empty, say exactly that.

## Weekly lint

When invoked as `/wiki-ingest lint`, skip the queue and instead check, per project: contradictions between pages, claims superseded by newer blocks, orphan pages missing from `index.md`, recurring concepts with no page, blocks stuck `unprocessed` for over 7 days, and registry entries with no project directory. Write findings to `<vault>/wiki/automation/lint-<YYYY-MM-DD>.md` and link them from the master index.

## Autonomy notes

This skill runs unattended from Windows Task Scheduler via `bin/wiki-ingest-run.ps1`. In that mode:

- Never ask questions — there is nobody to answer. Decide, act, and record the decision in the log.
- Touch nothing outside `LLM-Wiki/` and the `status` flags in `.wiki-raw/`. No commits, no restarts, no config edits, no pushes.
- On a broken block or unreadable file: leave it unprocessed, note it in the run log, continue with the rest.
