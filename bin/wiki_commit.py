"""Git post-commit recorder for the LLM Wiki.

Installed as `<repo>/.git/hooks/post-commit` by wiki_install_git_hooks.py.
Appends one commit block to `<repo>/.wiki-raw/YYYY-MM-DD.md`, so changes made
outside Claude Code (by hand, by another agent, by an IDE) still reach the wiki.

Exit codes: always 0. A hook must never break a commit.
"""
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_record import (  # noqa: E402
    MARK_BEGIN, MARK_END, _log, _safe, is_ignored, register_project, resolve_repo, write_block,
)

MAX_FILES = 80
BODY_CHARS = 1200


def _git(cwd: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def main() -> int:
    if os.environ.get("WIKI_RECORD_DISABLE") == "1":
        return 0
    try:
        cwd = os.getcwd()
        if is_ignored(cwd):
            return 0
        sha = _git(cwd, "rev-parse", "--short", "HEAD")
        if not sha:
            return 0
        # Amends and rebases replay the same work; the block id is the new sha,
        # so a rewritten commit is recorded as its own block. That is intended.
        subject = _git(cwd, "log", "-1", "--pretty=%s")
        body = _git(cwd, "log", "-1", "--pretty=%b")
        author = _git(cwd, "log", "-1", "--pretty=%an")
        stat = _git(cwd, "show", "--numstat", "--format=", "HEAD")

        slug, raw_dir, branch = resolve_repo(cwd)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        day = datetime.now().strftime("%Y-%m-%d")

        files = []
        changed_paths = []
        for line in stat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, dele, path = parts
            files.append(f"- `{_safe(path)}` (+{add} -{dele})")
            changed_paths.append(path)

        out = [
            MARK_BEGIN.format(id=sha, kind="commit"), "",
            f"## [{now}] commit {sha} | branch {branch or '?'} | files {len(files)}", "",
            f"**{subject}**", "",
        ]
        if body.strip():
            out += [body.strip()[:BODY_CHARS], ""]
        out += [f"Author {author}.", ""]
        if files:
            out += ["**Files**", ""] + files[:MAX_FILES]
            if len(files) > MAX_FILES:
                out.append(f"- ... and {len(files) - MAX_FILES} more")
            out.append("")
        out += [MARK_END.format(id=sha), ""]

        write_block(raw_dir / f"{day}.md", sha, "\n".join(out))
        register_project(slug, raw_dir, cwd)

        # Keep the symbol index in step with the commit. Only the files this
        # commit touched are re-parsed, so it stays in the tens of milliseconds.
        try:
            from wiki_symbols import build as build_symbols
            if changed_paths:
                build_symbols(raw_dir.parent, changed_paths)
        except Exception as e:
            _log(f"symbol index update skipped: {e}")

        _log(f"commit slug={slug} sha={sha} files={len(files)}")
        return 0
    except Exception as e:  # never break a commit
        try:
            _log(f"commit fail {type(e).__name__}: {e}")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
