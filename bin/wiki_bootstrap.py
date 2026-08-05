"""SessionStart bootstrap for the LLM Wiki.

Whatever repo you open — desktop app or CLI, first time or hundredth — this
makes sure it is wired: post-commit hook installed, project registered in the
vault registry. No one-time setup step, no per-repo action.

Exit codes: always 0. A hook must never break Claude Code.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_paths import load_config, roots  # noqa: E402
from wiki_record import (  # noqa: E402
    RAW_DIRNAME, _log, _read_event, is_ignored, register_project, resolve_repo,
)
from wiki_install_git_hooks import MARKER, hooks_dir, install  # noqa: E402


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def main() -> int:
    if os.environ.get("WIKI_RECORD_DISABLE") == "1":
        return 0
    try:
        event = _read_event()
        cwd = str(event.get("cwd") or os.getcwd())
        if is_ignored(cwd):
            return 0

        slug, raw_dir, _branch = resolve_repo(cwd)
        is_git = raw_dir.name == RAW_DIRNAME
        register_project(slug, raw_dir, cwd)

        if is_git and load_config().get("auto_install_git_hooks", True):
            repo = raw_dir.parent
            # Only repos under a configured root are wired automatically: opening
            # somebody else's clone must not rewrite its hooks.
            if not any(_within(repo, r) for r in roots()):
                _log(f"bootstrap {slug}: outside configured roots, hook not installed")
                return 0
            hd = hooks_dir(repo)
            hook = hd / "post-commit" if hd else None
            if hook is not None and MARKER not in (
                hook.read_text(encoding="utf-8", errors="replace") if hook.exists() else ""
            ):
                _log(f"bootstrap {slug}: post-commit {install(repo, False, False)}")
        return 0
    except Exception as e:  # never break Claude Code
        try:
            _log(f"bootstrap fail {type(e).__name__}: {e}")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
