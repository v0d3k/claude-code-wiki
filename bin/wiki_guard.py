"""Duplicate guard: says "this already exists" before a copy is written.

Wired as a PreToolUse hook on Write (a new file is where a fresh copy of an old
helper usually appears) and optionally on Edit. It reads the symbol index built
by wiki_symbols.py -- no model call, no language server, ~8 ms of work on top of
interpreter start.

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
from wiki_symbols import PATTERNS, declared_names, load_index, repo_root  # noqa: E402

MAX_REPORTED = 5      # names per warning
MAX_LOCATIONS = 3     # locations per name


def check(text: str, suffix: str, root: Path, exclude_file: str) -> list[str]:
    """Return one report line per name that already exists elsewhere."""
    if not text or suffix not in PATTERNS:
        return []
    idx = load_index(root)
    if not idx:
        return []
    cfg = load_config()
    threshold = int(cfg.get("guard_min_existing", 1))

    lines = []
    for name in sorted(declared_names(text, suffix)):
        locs = [loc for loc in idx.get(name, [])
                if loc.rsplit(":", 1)[0] != exclude_file]
        files = {loc.rsplit(":", 1)[0] for loc in locs}
        if len(files) < threshold:
            continue
        shown = ", ".join(sorted(files)[:MAX_LOCATIONS])
        more = f" and {len(files) - MAX_LOCATIONS} more file(s)" if len(files) > MAX_LOCATIONS else ""
        lines.append(f"- `{name}` already defined in {len(files)} file(s): {shown}{more}")
    lines.sort(key=lambda s: -int(s.split("already defined in ")[1].split(" ")[0]))
    return lines[:MAX_REPORTED]


def emit(event_name: str, report: list[str], target: str) -> None:
    body = (
        f"Duplicate check for `{target}`: this file declares names that already exist "
        f"in this repository.\n\n" + "\n".join(report) + "\n\n"
        "Import the existing one, or keep yours and be deliberate about it — a local "
        "helper with the same name is sometimes correct, a third copy of the same "
        "logic usually is not. `wikictl where <name>` lists every definition."
    )
    out = {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": body}}
    if event_name == "PreToolUse":
        out["hookSpecificOutput"]["permissionDecision"] = "allow"
        out["hookSpecificOutput"]["permissionDecisionReason"] = (
            f"{len(report)} name(s) already defined elsewhere; proceeding, see context"
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

        cwd = str(event.get("cwd") or os.getcwd())
        if is_ignored(cwd):
            return 0
        root = repo_root(cwd)
        if root is None:
            return 0

        file_path = str(inp.get("file_path") or "")
        if not file_path:
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
        if report:
            emit(event_name, report, rel)
            _log(f"guard {tool} {rel}: {len(report)} duplicate name(s)")
        return 0
    except Exception as e:  # never break Claude Code
        try:
            _log(f"guard fail {type(e).__name__}: {e}")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
