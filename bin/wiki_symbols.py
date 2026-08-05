"""Symbol index: what is already defined in this repository, and where.

The wiki answers "why was this decided". This answers "does it already exist" --
the question that, unasked, produces the twentieth copy of the same helper.

It indexes two things per definition: the name, and a hash of the normalised
body. That second one matters, because the three ways code duplicates are
genuinely different problems:

  identical  -- same body, same or different name. Mechanical: import it.
  diverged   -- same name, different bodies. A name collision, and the source
                of bugs where num('') is 0 in one module and null in the next.
  renamed    -- same body under a new name. Invisible to any name-based check;
                measured at 132 groups and 931 copies in one real repository.

Deliberately regex-based rather than language-server-based: it must run inside a
git hook in tens of milliseconds, with no daemon, no language server and no
network. It knows less than an LSP and is always available, which is the right
trade for a guard.

    python wiki_symbols.py build   <repo> [--full]
    python wiki_symbols.py where   <name> [--repo PATH]
    python wiki_symbols.py dupes   [--repo PATH] [--kind all|identical|diverged|renamed]
"""
import argparse
import hashlib
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
INDEX_VERSION = 2

# One declaration pattern per language family. Group 1 is always the name.
JS_PATTERNS = [
    r"^[ \t]*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
    r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)",
    r"^[ \t]*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)",
]
PY_PATTERNS = [
    r"^([ \t]*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(",
    r"^([ \t]*)class\s+([A-Za-z_]\w*)",
]
JS_SUFFIXES = {".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx"}
PY_SUFFIXES = {".py"}
PATTERNS = {s: JS_PATTERNS for s in JS_SUFFIXES}
PATTERNS.update({s: PY_PATTERNS for s in PY_SUFFIXES})

SKIP_DIRS = {".git", "node_modules", "dist", "build", "vendor", "__pycache__",
             ".venv", "venv", ".next", "coverage", ".wiki-raw", ".serena"}
IGNORE_NAMES = {"main", "run", "init", "setup", "test", "cb", "fn", "f", "e", "i"}
MIN_NAME_LEN = 3
# Below this a body is boilerplate -- `return null;` would tie half a repository
# together. 40 characters is where the measurements stopped producing noise.
MIN_BODY_CHARS = 40

COMMENTS_JS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
COMMENTS_PY = re.compile(r"#[^\n]*")


def repo_root(start: str | None = None) -> Path | None:
    """Repository root, resolved on the filesystem rather than by asking git.

    The guard calls this on every Write, and spawning `git rev-parse` there costs
    more than everything else the guard does put together. Walking up for `.git`
    is microseconds and works without git on PATH; git is only consulted if the
    walk finds nothing.
    """
    try:
        here = Path(start or os.getcwd()).resolve()
    except OSError:
        return None
    for d in [here, *here.parents]:
        dot = d / ".git"
        if dot.is_dir():
            return d
        if dot.is_file():
            # A worktree: .git holds "gitdir: <main>/.git/worktrees/<name>".
            try:
                target = dot.read_text(encoding="utf-8", errors="replace").split(":", 1)[1].strip()
            except (OSError, IndexError):
                return d
            marker = "/.git/worktrees/"
            norm = target.replace("\\", "/")
            return Path(norm.split(marker)[0]) if marker in norm else d
    try:
        out = subprocess.run(["git", "-C", str(here), "rev-parse",
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


# --------------------------------------------------------------------------- parsing

def normalise(body: str, is_py: bool) -> str:
    body = (COMMENTS_PY if is_py else COMMENTS_JS).sub("", body)
    return re.sub(r"\s+", " ", body).strip()


def body_hash(body: str, is_py: bool) -> str | None:
    norm = normalise(body, is_py)
    if len(norm) < MIN_BODY_CHARS:
        return None
    return hashlib.blake2b(norm.encode("utf-8"), digest_size=6).hexdigest()


def _js_body(text: str, start: int) -> str:
    """From the declaration to the matching closing brace."""
    i = text.find("{", start)
    if i < 0:
        return ""
    depth, j, in_str, quote, esc = 0, i, False, "", False
    while j < len(text):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        elif c in "\"'`":
            in_str, quote = True, c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
        j += 1
    return text[i:]


def _py_body(text: str, decl_start: int, indent: str) -> str:
    """From the def line to the first line indented no deeper than the def."""
    lines = text[decl_start:].split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        if line.strip() and not line.startswith(indent + " ") and not line.startswith(indent + "\t"):
            break
        out.append(line)
    return "\n".join(out)


def scan_file(path: Path, root: Path) -> list[tuple[str, str, str | None]]:
    """[(name, "rel:line", body_hash_or_None)] for one file."""
    pats = PATTERNS.get(path.suffix)
    if not pats:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    is_py = path.suffix in PY_SUFFIXES
    rel = str(path.relative_to(root)).replace("\\", "/")
    found = []
    for pat in pats:
        for m in re.finditer(pat, text, re.M):
            if is_py:
                indent, name = m.group(1), m.group(2)
                body = _py_body(text, m.start(), indent)
            else:
                name = m.group(1)
                body = _js_body(text, m.end())
            if len(name) < MIN_NAME_LEN or name in IGNORE_NAMES:
                continue
            line = text.count("\n", 0, m.start()) + 1
            found.append((name, f"{rel}:{line}", body_hash(body, is_py) if body else None))
    return found


def iter_source(root: Path):
    exclude = set(load_config().get("exclude", [])) | SKIP_DIRS
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in PATTERNS:
                yield p


# --------------------------------------------------------------------------- index

def _empty() -> dict:
    return {"v": INDEX_VERSION, "defs": {}, "dup_bodies": {}}


def _rebuild_dup_bodies(defs: dict) -> dict:
    """hash -> ["name@loc", ...], kept only for hashes seen more than once.

    Storing just the collisions keeps the index small while answering both
    questions the guard asks: is this body already here, and under what name.
    """
    seen: dict[str, list[str]] = {}
    for name, entries in defs.items():
        for e in entries:
            h = e.get("h")
            if h:
                seen.setdefault(h, []).append(f"{name}@{e['loc']}")
    return {h: v for h, v in seen.items() if len(v) > 1}


def build(root: Path, changed: list[str] | None = None) -> dict:
    """Full scan, or an incremental update of the given files."""
    path = index_path(root)
    idx = _empty()
    if changed is not None and path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if loaded.get("v") == INDEX_VERSION:
                idx = loaded
            else:
                changed = None   # format changed: full rebuild
        except (OSError, json.JSONDecodeError):
            changed = None
    defs = idx["defs"]

    if changed is None:
        defs = {}
        for f in iter_source(root):
            for name, loc, h in scan_file(f, root):
                defs.setdefault(name, []).append({"loc": loc, "h": h} if h else {"loc": loc})
    else:
        touched = {c.replace("\\", "/") for c in changed}
        for name in list(defs):
            kept = [e for e in defs[name] if e["loc"].rsplit(":", 1)[0] not in touched]
            if kept:
                defs[name] = kept
            else:
                del defs[name]
        for rel in touched:
            f = root / rel
            if f.exists():
                for name, loc, h in scan_file(f, root):
                    defs.setdefault(name, []).append({"loc": loc, "h": h} if h else {"loc": loc})

    for name in defs:
        defs[name] = sorted({(e["loc"], e.get("h")) for e in defs[name]})
        defs[name] = [{"loc": loc, "h": h} if h else {"loc": loc} for loc, h in defs[name]]

    idx = {"v": INDEX_VERSION, "defs": defs, "dup_bodies": _rebuild_dup_bodies(defs)}
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return idx


def load_index(root: Path) -> dict:
    path = index_path(root)
    if not path.exists():
        return _empty()
    try:
        idx = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    return idx if idx.get("v") == INDEX_VERSION else _empty()


def declared(text: str, suffix: str) -> list[tuple[str, str | None]]:
    """[(name, body_hash)] for source that is about to be written."""
    pats = PATTERNS.get(suffix)
    if not pats:
        return []
    is_py = suffix in PY_SUFFIXES
    out = []
    for pat in pats:
        for m in re.finditer(pat, text, re.M):
            if is_py:
                indent, name = m.group(1), m.group(2)
                body = _py_body(text, m.start(), indent)
            else:
                name = m.group(1)
                body = _js_body(text, m.end())
            if len(name) < MIN_NAME_LEN or name in IGNORE_NAMES:
                continue
            out.append((name, body_hash(body, is_py) if body else None))
    return out


def declared_names(text: str, suffix: str) -> set[str]:
    return {n for n, _ in declared(text, suffix)}


# --------------------------------------------------------------------------- analysis

def classify(idx: dict) -> dict:
    """Split duplication into the three kinds that need different answers."""
    defs = idx.get("defs", {})
    identical, diverged = {}, {}
    for name, entries in defs.items():
        files = {e["loc"].rsplit(":", 1)[0] for e in entries}
        if len(files) < 2:
            continue
        hashes = {e.get("h") for e in entries if e.get("h")}
        if len(hashes) == 1 and len(entries) > 1:
            identical[name] = entries
        elif len(hashes) > 1:
            diverged[name] = entries
    renamed = {}
    for h, refs in idx.get("dup_bodies", {}).items():
        names = {r.split("@", 1)[0] for r in refs}
        if len(names) > 1:
            renamed[h] = refs
    return {"identical": identical, "diverged": diverged, "renamed": renamed}


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
    print(f"{root.name}: {len(idx['defs'])} names, {len(idx['dup_bodies'])} repeated bodies, "
          f"{kind}, {time.time() - t0:.2f}s -> {index_path(root)}")
    return 0


def cmd_where(args) -> int:
    root = Path(args.repo).resolve() if args.repo else repo_root()
    if root is None:
        print("not a git repository", file=sys.stderr)
        return 1
    idx = load_index(root)
    defs = idx.get("defs", {})
    if not defs:
        print(f"no index for {root.name}; run: wikictl symbols build")
        return 1
    hits = defs.get(args.name)
    if not hits:
        near = [n for n in defs if args.name.lower() in n.lower()][:8]
        print(f"{args.name}: not defined in {root.name}"
              + (f"; similar: {', '.join(near)}" if near else ""))
        return 1
    groups: dict[str, list[str]] = {}
    for e in hits:
        groups.setdefault(e.get("h") or "-", []).append(e["loc"])
    print(f"{args.name}: {len(hits)} definition(s) in {len(groups)} variant(s)")
    for h, locs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        label = "identical group" if h != "-" else "body too small to compare"
        print(f"  [{len(locs)}x {label}]")
        for loc in locs[:args.limit]:
            print(f"    {loc}")
        if len(locs) > args.limit:
            print(f"    ... and {len(locs) - args.limit} more")
        aliases = {r.split("@", 1)[0] for r in idx.get("dup_bodies", {}).get(h, [])} - {args.name}
        if aliases:
            print(f"    same body also named: {', '.join(sorted(aliases))}")
    return 0


def cmd_dupes(args) -> int:
    root = Path(args.repo).resolve() if args.repo else repo_root()
    if root is None:
        print("not a git repository", file=sys.stderr)
        return 1
    idx = load_index(root)
    if not idx.get("defs"):
        print("no index; run: wikictl symbols build")
        return 1
    k = classify(idx)
    show = args.kind

    if show in ("all", "identical"):
        print(f"IDENTICAL -- same name, same body, {len(k['identical'])} name(s). "
              f"Consolidate mechanically.")
        for name, entries in sorted(k["identical"].items(), key=lambda kv: -len(kv[1]))[:args.limit]:
            print(f"  {name}: {len(entries)} copies -- {entries[0]['loc']} …")
        print()

    if show in ("all", "diverged"):
        print(f"DIVERGED -- same name, different bodies, {len(k['diverged'])} name(s). "
              f"A name collision: one name, several behaviours.")
        for name, entries in sorted(k["diverged"].items(), key=lambda kv: -len(kv[1]))[:args.limit]:
            variants = len({e.get("h") for e in entries if e.get("h")})
            print(f"  {name}: {len(entries)} definitions, {variants} different implementations")
        print()

    if show in ("all", "renamed"):
        print(f"RENAMED -- same body under different names, {len(k['renamed'])} group(s). "
              f"Invisible to any name-based check.")
        for h, refs in sorted(k["renamed"].items(), key=lambda kv: -len(kv[1]))[:args.limit]:
            names: dict[str, int] = {}
            for r in refs:
                n = r.split("@", 1)[0]
                names[n] = names.get(n, 0) + 1
            spread = ", ".join(f"{n} x{c}" for n, c in sorted(names.items(), key=lambda kv: -kv[1]))
            print(f"  {len(refs)} copies as {spread}")
            print(f"      e.g. {refs[0].split('@', 1)[1]}")
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
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_where)

    p = sub.add_parser("dupes")
    p.add_argument("--repo")
    p.add_argument("--kind", default="all", choices=["all", "identical", "diverged", "renamed"])
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_dupes)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
