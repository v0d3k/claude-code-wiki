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
from wiki_symbols import PATTERNS, declared, declared_names, load_index, repo_root  # noqa: E402

MAX_REPORTED = 5      # names per warning
MAX_LOCATIONS = 3     # locations per name


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
            variants = len({e.get("h") for e in entries if e.get("h")})
            what = (f"{variants} different implementations" if variants > 1
                    else "a different implementation")
            diverged.append(
                f"- `{name}` — name taken in {len(files)} file(s) with {what}: {_brief(locs)}.")

    return (identical + renamed + diverged)[:MAX_REPORTED]


def emit(event_name: str, report: list[str], target: str) -> None:
    body = (
        f"Duplicate check for `{target}`:\n\n" + "\n".join(report) + "\n\n"
        "An identical body means import it. The same body under another name means the "
        "helper already exists and you are about to fork it. A taken name with different "
        "behaviour is a collision — pick another name or reconcile the two. "
        "`wikictl where <name>` shows every definition and which of them are the same code."
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
