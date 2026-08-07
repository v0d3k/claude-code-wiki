"""Duplicate guard: says "this already exists" before a copy is written.

Wired as a PreToolUse hook on Write (a new file is where a fresh copy of an old
helper usually appears) and optionally on Edit. It reads the symbol index built
by wiki_symbols.py -- no model call, no language server, ~8 ms of work on top of
interpreter start.

Also warns about contended tables: a file writing SQL into a table the compact
levers summary (built alongside the structure index by wiki_structure.build())
already shows several other writers for. That summary, not the full structure
index, is the only thing this ever reads -- the index is a few hundred KB, the
summary a few KB, and this runs on every Write.

It never blocks. It reports locations and lets the model decide, because a
same-named local helper is sometimes the right answer and a hook cannot tell.

Exit codes: always 0. A hook must never break Claude Code.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_paths import load_config  # noqa: E402
from wiki_record import _log, _read_event, is_ignored  # noqa: E402
from wiki_symbols import PATTERNS, declared, declared_names, load_index, repo_root  # noqa: E402
from wiki_structure import (JS_SUFFIXES, PY_SUFFIXES, extract_writes, is_test_file,  # noqa: E402
                             load_levers_summary)

MAX_REPORTED = 5      # names per warning
MAX_LOCATIONS = 3     # locations per name
STRUCTURE_SUFFIXES = JS_SUFFIXES | PY_SUFFIXES


def _locations(entries, exclude_file: str) -> list[str]:
    return [e["loc"] for e in entries if e["loc"].rsplit(":", 1)[0] != exclude_file]


def _brief(locs: list[str]) -> str:
    files = sorted({loc.rsplit(":", 1)[0] for loc in locs})
    shown = ", ".join(files[:MAX_LOCATIONS])
    more = f" and {len(files) - MAX_LOCATIONS} more" if len(files) > MAX_LOCATIONS else ""
    return f"{shown}{more}"


def check(text: str, suffix: str, root: Path, exclude_file: str) -> list[str]:
    """One line per finding, strongest signal first.

    Three different problems, three different answers:
      identical  the same body is already here -- import it
      renamed    the same body is here under another name -- the invisible case
      diverged   the name is taken by different behaviour -- a collision
    """
    if not text or suffix not in PATTERNS:
        return []
    idx = load_index(root)
    defs = idx.get("defs", {})
    if not defs:
        return []
    dup_bodies = idx.get("dup_bodies", {})
    threshold = int(load_config().get("guard_min_existing", 1))

    identical, renamed, diverged = [], [], []
    for name, h in declared(text, suffix):
        entries = defs.get(name, [])
        locs = _locations(entries, exclude_file)
        same_body = [e["loc"] for e in entries
                     if h and e.get("h") == h and e["loc"].rsplit(":", 1)[0] != exclude_file]

        if same_body:
            identical.append(
                f"- `{name}` — **identical body already here** ({len(same_body)} place(s)): "
                f"{_brief(same_body)}. Import it.")
            continue

        if h:
            elsewhere = [r for r in dup_bodies.get(h, [])
                         if not r.startswith(f"{name}@")
                         and r.split("@", 1)[1].rsplit(":", 1)[0] != exclude_file]
            if elsewhere:
                aliases = sorted({r.split("@", 1)[0] for r in elsewhere})
                renamed.append(
                    f"- `{name}` — **this exact body already exists** as "
                    f"{', '.join('`' + a + '`' for a in aliases[:3])} in {len(elsewhere)} place(s): "
                    f"{_brief([r.split('@', 1)[1] for r in elsewhere])}.")
                continue

        files = {loc.rsplit(":", 1)[0] for loc in locs}
        if len(files) >= threshold:
            surviving = [e for e in entries if e["loc"].rsplit(":", 1)[0] != exclude_file]
            variants = len({e.get("h") for e in surviving if e.get("h")})
            what = (f"{variants} different implementations" if variants > 1
                    else "a different implementation")
            diverged.append(
                f"- `{name}` — name taken in {len(files)} file(s) with {what}: {_brief(locs)}.")

    return (identical + renamed + diverged)[:MAX_REPORTED]


def structure_check(text: str, suffix: str, summary: dict, exclude_file: str, old_text: str) -> list[str]:
    """One line per table this write already has enough other writers for.

    `summary` is the compact lever -> [total, non_test] map from
    wiki_structure.load_levers_summary() -- NOT the full structure index,
    which this must never load. `old_text` is whatever `exclude_file` already
    contained on disk before this write (empty for a brand-new file): the
    summary reflects the last commit, so if this same file already wrote the
    table then, its own contribution is already baked into those counts and
    must be subtracted before comparing to the threshold or displaying it --
    the mechanical equivalent of how check() excludes exclude_file for
    symbols, just computed differently because the summary carries no
    per-file breakdown to filter by location.

    Suffixes outside JS/TS/PY are silent: iter_source() never scanned them
    into the structure index in the first place, so a warning here would cite
    numbers the index cannot actually back up.
    """
    if not text or not summary or suffix not in STRUCTURE_SUFFIXES:
        return []
    threshold = int(load_config().get("guard_min_lever_writers", 2))
    self_is_test = is_test_file(exclude_file)
    old_writes = extract_writes(old_text) if old_text else set()

    lines = []
    for table in sorted(extract_writes(text)):
        total, non_test = summary.get(table, [0, 0])
        if table in old_writes:
            total -= 1
            if not self_is_test:
                non_test -= 1
        if total >= threshold:
            lines.append(
                f"- `{table}` — {total} file(s) already write this table "
                f"({non_test} outside tests). Check `wikictl levers {table}` "
                "before changing its shape.")
    return lines


def emit(event_name: str, report: list[str], lever_report: list[str], target: str) -> None:
    sections = []
    if report:
        sections.append(
            f"Duplicate check for `{target}`:\n\n" + "\n".join(report) + "\n\n"
            "An identical body means import it. The same body under another name means the "
            "helper already exists and you are about to fork it. A taken name with different "
            "behaviour is a collision — pick another name or reconcile the two. "
            "`wikictl where <name>` shows every definition and which of them are the same code."
        )
    if lever_report:
        sections.append(
            f"Contended tables written by `{target}`:\n\n" + "\n".join(lever_report) + "\n\n"
            "A table with several writers is coupling an import graph cannot see -- changing "
            "its shape can break a file that never imports this one."
        )
    body = "\n\n---\n\n".join(sections)
    out = {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": body}}
    if event_name == "PreToolUse":
        out["hookSpecificOutput"]["permissionDecision"] = "allow"
        total = len(report) + len(lever_report)
        out["hookSpecificOutput"]["permissionDecisionReason"] = (
            f"{total} note(s) about `{target}`; proceeding, see context"
        )
    print(json.dumps(out, ensure_ascii=False))


def main() -> int:
    if os.environ.get("WIKI_RECORD_DISABLE") == "1":
        return 0
    try:
        cfg = load_config()
        if not cfg.get("guard_enabled", True):
            return 0
        event = _read_event()
        event_name = str(event.get("hook_event_name") or "PreToolUse")
        tool = str(event.get("tool_name") or "")
        inp = event.get("tool_input") or {}
        if not isinstance(inp, dict):
            return 0

        file_path = str(inp.get("file_path") or "")
        if not file_path:
            return 0

        # Resolve the repository from the file being written, not from the
        # session's cwd: editing another project from inside this one must not
        # be checked against this project's index.
        target = Path(file_path)
        root = repo_root(str(target.parent if target.parent.exists() else Path.cwd()))
        if root is None:
            return 0
        if is_ignored(str(root)):
            return 0
        suffix = Path(file_path).suffix
        try:
            rel = str(Path(file_path).resolve().relative_to(root)).replace("\\", "/")
        except (OSError, ValueError):
            rel = file_path.replace("\\", "/")

        if tool == "Write":
            text = str(inp.get("content") or "")
        elif tool in ("Edit", "MultiEdit"):
            # Only what this edit introduces, so an untouched old helper stays quiet.
            new = str(inp.get("new_string") or "")
            old = str(inp.get("old_string") or "")
            introduced = declared_names(new, suffix) - declared_names(old, suffix)
            if not introduced:
                return 0
            text = new
        else:
            return 0

        report = check(text, suffix, root, rel)

        summary = load_levers_summary(root)
        old_text = ""
        if summary:
            try:
                old_text = Path(file_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                old_text = ""
        lever_report = structure_check(text, suffix, summary, rel, old_text)

        if report or lever_report:
            emit(event_name, report, lever_report, rel)
            _log(f"guard {tool} {rel}: {len(report)} duplicate name(s), "
                 f"{len(lever_report)} contended lever(s)")
        return 0
    except Exception as e:  # never break Claude Code
        try:
            _log(f"guard fail {type(e).__name__}: {e}")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
