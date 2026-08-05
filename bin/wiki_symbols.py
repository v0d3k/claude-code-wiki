"""Symbol index: what is already defined in this repository, and where.

The wiki answers "why was this decided". This answers "does it already exist" --
the question that, unasked, produces the twentieth copy of the same helper.

Deliberately regex-based rather than language-server-based: it must run inside a
git hook in tens of milliseconds, with no daemon, no language server and no
network. It knows less than an LSP and is always available, which is the right
trade for a guard.

    python wiki_symbols.py build   <repo> [--full]
    python wiki_symbols.py where   <name> [--repo PATH]
    python wiki_symbols.py dupes   [--repo PATH] [--limit N] [--diverged]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_paths import STATE_DIR, load_config  # noqa: E402

INDEX_DIR = STATE_DIR / "symbols"

# One pattern per language family. Group 1 is always the name.
PATTERNS = {
    ".js": [
        r"^[ \t]*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
        r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)",
        r"^[ \t]*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)",
    ],
    ".py": [
        r"^[ \t]*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(",
        r"^[ \t]*class\s+([A-Za-z_]\w*)",
    ],
}
PATTERNS[".cjs"] = PATTERNS[".mjs"] = PATTERNS[".ts"] = PATTERNS[".tsx"] = PATTERNS[".jsx"] = PATTERNS[".js"]

SKIP_DIRS = {".git", "node_modules", "dist", "build", "vendor", "__pycache__",
             ".venv", "venv", ".next", "coverage", ".wiki-raw", ".serena"}
# Names too generic to be worth reporting, or that mean something local by nature.
IGNORE_NAMES = {"main", "run", "init", "setup", "test", "cb", "fn", "f", "e", "i"}
MIN_NAME_LEN = 3


def repo_root(start: str | None = None) -> Path | None:
    try:
        out = subprocess.run(["git", "-C", start or os.getcwd(), "rev-parse",
                              "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    p = Path(out.stdout.strip())
    return p.parent if p.name == ".git" else p


def index_path(root: Path) -> Path:
    return INDEX_DIR / f"{root.name}.json"


def scan_file(path: Path, root: Path) -> list[tuple[str, str]]:
    pats = PATTERNS.get(path.suffix)
    if not pats:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rel = str(path.relative_to(root)).replace("\\", "/")
    found = []
    for pat in pats:
        for m in re.finditer(pat, text, re.M):
            name = m.group(1)
            if len(name) < MIN_NAME_LEN or name in IGNORE_NAMES:
                continue
            found.append((name, f"{rel}:{text.count(chr(10), 0, m.start()) + 1}"))
    return found


def iter_source(root: Path):
    exclude = set(load_config().get("exclude", [])) | SKIP_DIRS
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in PATTERNS:
                yield p


def build(root: Path, changed: list[str] | None = None) -> dict:
    """Full scan, or an incremental update of the given files."""
    path = index_path(root)
    idx: dict[str, list[str]] = {}
    if changed is not None and path.exists():
        try:
            idx = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            changed = None
    if changed is None:
        for f in iter_source(root):
            for name, loc in scan_file(f, root):
                idx.setdefault(name, []).append(loc)
    else:
        touched = {c.replace("\\", "/") for c in changed}
        for name in list(idx):
            kept = [loc for loc in idx[name] if loc.rsplit(":", 1)[0] not in touched]
            if kept:
                idx[name] = kept
            else:
                del idx[name]
        for rel in touched:
            f = root / rel
            if f.exists():
                for name, loc in scan_file(f, root):
                    idx.setdefault(name, []).append(loc)
    for name in idx:
        idx[name] = sorted(set(idx[name]))
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return idx


def load_index(root: Path) -> dict:
    path = index_path(root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def declared_names(text: str, suffix: str) -> set[str]:
    """Names a piece of source declares -- used on content about to be written."""
    names = set()
    for pat in PATTERNS.get(suffix, []):
        for m in re.finditer(pat, text, re.M):
            name = m.group(1)
            if len(name) >= MIN_NAME_LEN and name not in IGNORE_NAMES:
                names.add(name)
    return names


# --------------------------------------------------------------------------- cli

def cmd_build(args) -> int:
    root = Path(args.repo).resolve() if args.repo else repo_root()
    if root is None:
        print("not a git repository", file=sys.stderr)
        return 1
    t0 = time.time()
    changed = None
    if not args.full:
        out = subprocess.run(["git", "-C", str(root), "diff-tree", "--no-commit-id",
                              "--name-only", "-r", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            changed = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    idx = build(root, changed)
    kind = "full" if changed is None else f"incremental ({len(changed)} file(s))"
    print(f"{root.name}: {len(idx)} names, {kind}, {time.time() - t0:.2f}s "
          f"-> {index_path(root)}")
    return 0


def cmd_where(args) -> int:
    root = Path(args.repo).resolve() if args.repo else repo_root()
    if root is None:
        print("not a git repository", file=sys.stderr)
        return 1
    idx = load_index(root)
    if not idx:
        print(f"no index for {root.name}; run: wikictl symbols build")
        return 1
    hits = idx.get(args.name)
    if not hits:
        near = [n for n in idx if args.name.lower() in n.lower()][:8]
        print(f"{args.name}: not defined in {root.name}"
              + (f"; similar: {', '.join(near)}" if near else ""))
        return 1
    print(f"{args.name}: {len(hits)} definition(s) in {root.name}")
    for loc in hits[:args.limit]:
        print(f"  {loc}")
    if len(hits) > args.limit:
        print(f"  ... and {len(hits) - args.limit} more")
    return 0


def cmd_dupes(args) -> int:
    root = Path(args.repo).resolve() if args.repo else repo_root()
    if root is None:
        print("not a git repository", file=sys.stderr)
        return 1
    idx = load_index(root)
    if not idx:
        print("no index; run: wikictl symbols build")
        return 1
    multi = {n: locs for n, locs in idx.items()
             if len({loc.rsplit(':', 1)[0] for loc in locs}) > 1}
    if not multi:
        print("no name is defined in more than one file")
        return 0
    print(f"{len(multi)} name(s) defined in more than one file, "
          f"{sum(len(v) - 1 for v in multi.values())} redundant definition(s)\n")
    for name, locs in sorted(multi.items(), key=lambda kv: -len(kv[1]))[:args.limit]:
        print(f"{name}: {len(locs)} definitions")
        for loc in locs[:3]:
            print(f"    {loc}")
        if len(locs) > 3:
            print(f"    ... and {len(locs) - 3} more")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="wiki_symbols", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build")
    p.add_argument("repo", nargs="?")
    p.add_argument("--full", action="store_true", help="rescan everything, not just the last commit")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("where")
    p.add_argument("name")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=12)
    p.set_defaults(func=cmd_where)

    p = sub.add_parser("dupes")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_dupes)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
