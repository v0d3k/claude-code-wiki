"""Install the LLM-Wiki post-commit hook into every git repo under the given roots.

Usage:
  python wiki_install_git_hooks.py [--dry-run] [--root DIR ...] [--uninstall]

Idempotent. Never clobbers an existing post-commit hook: if one exists without
our marker, our line is appended to it. Worktrees share the main repo's hooks
directory, so installing once per repo covers all of its worktrees.
"""
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_paths import BIN_DIR, load_config, roots as _roots  # noqa: E402

POSIX_SHEBANGS = ("#!/bin/sh", "#!/usr/bin/env sh", "#!/bin/bash", "#!/usr/bin/env bash")

DEFAULT_ROOTS = _roots()
MARKER = "# llm-wiki-record"
# Vendored upstream clones: their commits are not our project history.
EXCLUDE = set(load_config()["exclude"])
HOOK_LINES = [
    MARKER,
    f'python "{(BIN_DIR / "wiki_commit.py").as_posix()}" >/dev/null 2>&1 || true',
    "# /llm-wiki-record",
]


def git(cwd: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def find_repos(roots: list[Path]) -> list[Path]:
    repos = []
    for root in roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name in EXCLUDE:
                continue
            if (child / ".git").exists():
                repos.append(child)
    return repos


def hooks_dir(repo: Path) -> Path | None:
    rel = git(repo, "rev-parse", "--git-path", "hooks")
    if not rel:
        return None
    p = Path(rel)
    return p if p.is_absolute() else (repo / p)


EXCLUDE_LINE = ".wiki-raw/"


def exclude_journal(repo: Path, dry: bool) -> bool:
    """Add .wiki-raw/ to .git/info/exclude: local, uncommitted, invisible to others."""
    if not load_config().get("git_exclude_raw", True):
        return False
    info = repo / ".git" / "info"
    target = info / "exclude"
    try:
        text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        if EXCLUDE_LINE in text.split():
            return False
        if not dry:
            info.mkdir(parents=True, exist_ok=True)
            lead = "" if (not text or text.endswith("\n")) else "\n"
            with target.open("a", encoding="utf-8") as f:
                f.write(lead + "# claude-code-wiki journal, kept out of the index\n"
                        + EXCLUDE_LINE + "\n")
        return True
    except OSError:
        return False


def install(repo: Path, dry: bool, uninstall: bool) -> str:
    hd = hooks_dir(repo)
    if hd is None:
        return "skip (not a git repo)"
    hook = hd / "post-commit"
    if not uninstall:
        # Runs on every install path, including "already installed": a repo that
        # was wired before this feature existed still gets its journal excluded.
        exclude_journal(repo, dry)
    existing = hook.read_text(encoding="utf-8", errors="replace") if hook.exists() else ""
    first_line = existing.splitlines()[0].strip() if existing.strip() else ""
    # "Ours" means the whole file is the four-line stub we install, nothing else.
    created_by_us = (first_line == "#!/bin/sh" and MARKER in existing
                     and len([ln for ln in existing.splitlines() if ln.strip()]) <= 4)

    if uninstall:
        if MARKER not in existing:
            return "absent"
        lines = existing.splitlines()
        kept, drop = [], False
        for ln in lines:
            if ln.strip() == MARKER:
                drop = True
                continue
            if ln.strip() == "# /llm-wiki-record":
                drop = False
                continue
            if not drop:
                kept.append(ln)
        body = "\n".join(kept).strip()
        if not dry:
            if body in ("#!/bin/sh", ""):
                hook.unlink(missing_ok=True)
            else:
                hook.write_text(body + "\n", encoding="utf-8", newline="\n")
        return "removed"

    if MARKER in existing:
        if HOOK_LINES[1] in existing:
            return "already installed"
        # Marker present but the command points somewhere else (the package
        # moved). Replace our block in place, leave the rest of the hook alone.
        kept, drop = [], False
        for ln in existing.splitlines():
            if ln.strip() == MARKER:
                drop = True
                kept.extend(HOOK_LINES)
                continue
            if ln.strip() == "# /llm-wiki-record":
                drop = False
                continue
            if not drop:
                kept.append(ln)
        if not dry:
            hook.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8", newline="\n")
        return "path updated"

    if existing.strip():
        if first_line not in POSIX_SHEBANGS:
            return f"skip (post-commit is not a POSIX shell script: {first_line or 'no shebang'})"
        body = existing.rstrip("\n") + "\n\n" + "\n".join(HOOK_LINES) + "\n"
        verb = "appended to existing hook"
    else:
        body = "#!/bin/sh\n" + "\n".join(HOOK_LINES) + "\n"
        verb = "installed"

    if not dry:
        hd.mkdir(parents=True, exist_ok=True)
        hook.write_text(body, encoding="utf-8", newline="\n")
        try:
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass
    return verb


def main() -> int:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    uninstall = "--uninstall" in args
    roots = []
    i = 0
    while i < len(args):
        if args[i] == "--root" and i + 1 < len(args):
            roots.append(Path(args[i + 1]))
            i += 2
            continue
        i += 1
    repos = find_repos(roots or DEFAULT_ROOTS)
    if not repos:
        print("no git repos found")
        return 1
    for repo in repos:
        print(f"{repo.name:28} {install(repo, dry, uninstall)}")
    print(f"\n{len(repos)} repos" + (" (dry run)" if dry else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
