"""Structure index: who imports whom, and who writes to which shared resource.

The symbol index answers "does this name exist". This answers "what will I
disturb". They are separate files on purpose: the duplicate guard loads the
symbol index on every Write, and structure data has no business slowing that
path down.

Deliberately regex-based, like the symbol index, and for the same reason -- it
runs inside a git hook. It sees static requires, literal SQL and literal event
names. Dynamic requires, ORM calls and computed table names are invisible, and
the CLI says so rather than pretending completeness.

    python wiki_structure.py build <repo> [--full]
    python wiki_structure.py map   [--repo PATH] [--top N]
    python wiki_structure.py path  <from> <to> [--repo PATH]
    python wiki_structure.py levers <name> [--repo PATH]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_paths import STATE_DIR, load_config  # noqa: E402
from wiki_symbols import SKIP_DIRS, repo_root  # noqa: E402

INDEX_DIR = STATE_DIR / "structure"
INDEX_VERSION = 1

JS_SUFFIXES = {".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx"}
PY_SUFFIXES = {".py"}

SQL_WRITE = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+[`\"\[]?([A-Za-z_][\w]*)",
    re.I)
# `from` is anchored to an import/export statement rather than accepted bare:
# SQL says `select ... from "runs"` and a bare-`from` pattern reads that quoted
# identifier as a module specifier. Backticks stop the scan too, because
# template literals are where multi-line SQL usually lives.
JS_IMPORT = re.compile(
    r"require\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"|\bimport\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"|^\s*(?:import|export)\b[^'\"`]*?\bfrom\s+['\"]([^'\"]+)['\"]"
    r"|^\s*import\s+['\"]([^'\"]+)['\"]", re.M)
JS_ENV = re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)|process\.env\[\s*['\"]([A-Z_][A-Z0-9_]*)")
PY_ENV = re.compile(r"os\.environ(?:\.get)?\(?\s*\[?\s*['\"]([A-Z_][A-Z0-9_]*)")
PY_IMPORT = re.compile(r"^\s*(?:from\s+(\.[\w.]*)\s+import|import\s+(\.[\w.]*))", re.M)
EMIT = re.compile(r"\.emit\(\s*['\"]([A-Za-z_][\w.:-]*)['\"]")

# UPDATE ... SET on its own line makes "SET" look like a table name.
NOT_A_TABLE = {"set", "from", "where", "select", "values", "into", "table"}


def _fired(pattern: re.Pattern, text: str) -> set[str]:
    """Collapse findall over a multi-alternative pattern to the groups that fired.

    findall yields one tuple per match with an empty string for every branch
    that did not participate, so the arity is the pattern's business, not the
    caller's -- adding an alternative must not break the unpacking here.
    """
    out = set()
    for m in pattern.findall(text):
        g = next((x for x in (m if isinstance(m, tuple) else (m,)) if x), None)
        if g:
            out.add(g)
    return out


def extract(text: str, suffix: str) -> dict:
    """Facts one file states about itself. Sets, so callers can union them."""
    writes = {t.lower() for t in SQL_WRITE.findall(text)} - NOT_A_TABLE
    emits = set(EMIT.findall(text))
    if suffix in PY_SUFFIXES:
        env = set(PY_ENV.findall(text))
        imports = _fired(PY_IMPORT, text)
    else:
        env = _fired(JS_ENV, text)
        imports = _fired(JS_IMPORT, text)
    return {"writes": writes, "env": env, "emits": emits, "imports": imports}


def resolve_import(spec: str, source: Path, root: Path) -> str | None:
    """Relative specifier -> repo-relative path, or None for a package."""
    if not spec.startswith("."):
        return None
    base = (source.parent / spec).resolve()
    for cand in (base, *(Path(str(base) + e) for e in (".js", ".cjs", ".mjs", ".ts", ".py")),
                 base / "index.js", base / "index.cjs", base / "__init__.py"):
        if cand.is_file():
            try:
                return str(cand.relative_to(root)).replace("\\", "/")
            except ValueError:
                return None
    return None


def iter_source(root: Path):
    exclude = set(load_config().get("exclude", [])) | SKIP_DIRS
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in JS_SUFFIXES | PY_SUFFIXES:
                yield p


def scan_file(path: Path, root: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    facts = extract(text, path.suffix)
    resolved = {r for r in (resolve_import(s, path, root) for s in facts["imports"]) if r}
    return {"imports": sorted(resolved), "writes": sorted(facts["writes"]),
            "env": sorted(facts["env"]), "emits": sorted(facts["emits"])}


def index_path(root: Path) -> Path:
    return INDEX_DIR / f"{root.name}.json"


def build(root: Path, changed: list[str] | None = None) -> dict:
    path = index_path(root)
    files: dict[str, dict] = {}
    if changed is not None and path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if loaded.get("v") == INDEX_VERSION:
                files = loaded.get("files", {})
            else:
                changed = None
        except (OSError, json.JSONDecodeError):
            changed = None

    if changed is None:
        files = {}
        for f in iter_source(root):
            rel = str(f.relative_to(root)).replace("\\", "/")
            facts = scan_file(f, root)
            if any(facts.values()):
                files[rel] = facts
    else:
        for rel in {c.replace("\\", "/") for c in changed}:
            files.pop(rel, None)
            f = root / rel
            if f.is_file() and f.suffix in JS_SUFFIXES | PY_SUFFIXES:
                facts = scan_file(f, root)
                if any(facts.values()):
                    files[rel] = facts

    idx = {"v": INDEX_VERSION, "files": files}
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return idx


def load_index(root: Path) -> dict:
    p = index_path(root)
    if not p.exists():
        return {"v": INDEX_VERSION, "files": {}}
    try:
        idx = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"v": INDEX_VERSION, "files": {}}
    return idx if idx.get("v") == INDEX_VERSION else {"v": INDEX_VERSION, "files": {}}
