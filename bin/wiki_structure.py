"""Structure index: who imports whom, and who writes to which shared resource.

The symbol index answers "does this name exist". This answers "what will I
disturb". They are separate files on purpose: the duplicate guard loads the
symbol index on every Write, and structure data has no business slowing that
path down.

Deliberately regex-based, like the symbol index, and for the same reason -- it
runs inside a git hook. It sees static requires, literal SQL and literal event
names. Dynamic requires, ORM calls and computed table names are invisible, and
the CLI says so rather than pretending completeness.

The guard never loads the index this module builds (a few hundred KB) -- only
the tiny levers-summary sibling written alongside it, via load_levers_summary()
and extract_writes() below. `argparse`, `subprocess` and `difflib` are CLI-only
and imported lazily inside the functions that use them for exactly that reason:
importing them at module level cost real, measured milliseconds on every guard
invocation for work the guard never does.

    python wiki_structure.py build <repo> [--full]
    python wiki_structure.py map   [--repo PATH] [--top N]
    python wiki_structure.py path  <from> <to> [--repo PATH]
    python wiki_structure.py levers <name> [--repo PATH]
"""
import json
import os
import re
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_paths import STATE_DIR, load_config  # noqa: E402
from wiki_symbols import SKIP_DIRS, repo_root  # noqa: E402

INDEX_DIR = STATE_DIR / "structure"
INDEX_VERSION = 2  # v2 adds the is_test flag per file (see is_test_file())

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

TEST_DIR_SEGMENTS = {"test", "tests", "__tests__", "spec"}


def is_test_file(rel: str) -> bool:
    """Repo-relative path -> True if it looks like a test file, not shipped code.

    Four checks, all narrow on purpose: a test/tests/__tests__/spec path segment;
    `.test.` or `.spec.` in the filename (Jest/Mocha/Vitest convention); Python's
    `test_*.py` and `*_test.py`. A bare "test" substring anywhere in the name is
    deliberately NOT enough -- `data/_refactor-tests.cjs` is a real shipped script
    in the repository this was measured against, and a hyphenated `-test`/`-tests`
    tail with no directory segment and no dot-marker is common there (stress-test
    and backtest scripts). Matching is case-sensitive on the dot-markers and the
    Python prefix/suffix (source conventions are), case-insensitive on directory
    segments (Windows paths vary in case more than filenames do).
    """
    norm = rel.replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return False
    segments, filename = parts[:-1], parts[-1]
    if any(seg.lower() in TEST_DIR_SEGMENTS for seg in segments):
        return True
    if ".test." in filename or ".spec." in filename:
        return True
    if filename.endswith(".py"):
        stem = filename[:-3]
        if stem.startswith("test_") or stem.endswith("_test"):
            return True
    return False


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


def extract_writes(text: str) -> set[str]:
    """Just the table-write facet of extract() -- the one thing the guard's
    contended-table check needs. Skipping the import/env/emit regex passes
    matters on a large file: on a ~90 KB real-world source file, extract()
    costs ~6 ms and this alone costs ~2 ms, and the guard runs it twice (new
    content and old) on every Write."""
    return {t.lower() for t in SQL_WRITE.findall(text)} - NOT_A_TABLE


def extract(text: str, suffix: str) -> dict:
    """Facts one file states about itself. Sets, so callers can union them."""
    writes = extract_writes(text)
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


def levers_summary_path(root: Path) -> Path:
    return INDEX_DIR / f"{root.name}.levers-summary.json"


def build_levers_summary(idx: dict) -> dict[str, list[int]]:
    """lever -> [total writers, non-test writers], for every table with >=1 writer.

    Unthresholded on purpose: the guard applies its own threshold
    (guard_min_lever_writers) at check time, the same way the symbol guard's
    guard_min_existing is applied at check time rather than baked into its
    index -- a table with one writer today still needs an entry to compare
    against once a second writer shows up.
    """
    counts: dict[str, list[int]] = {}
    for facts in idx.get("files", {}).values():
        is_test = facts.get("is_test", False)
        for w in facts.get("writes", []):
            row = counts.setdefault(w, [0, 0])
            row[0] += 1
            if not is_test:
                row[1] += 1
    return counts


def write_levers_summary(root: Path, idx: dict) -> dict:
    """Guard input: a few KB, never the ~68-entry version of the full index.

    Written alongside the main index by build() so the two never drift --
    the guard loads only this file, on every Write, and must never touch
    index_path()'s few-hundred-KB file to get it.
    """
    summary = {"v": INDEX_VERSION, "levers": build_levers_summary(idx)}
    path = levers_summary_path(root)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return summary


def load_levers_summary(root: Path) -> dict[str, list[int]]:
    """The guard's structure-index read. Missing, unversioned or malformed
    all come back as {} -- same fail-open shape as load_index()."""
    path = levers_summary_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if data.get("v") != INDEX_VERSION:
        return {}
    return data.get("levers", {})


def build(root: Path, changed: list[str] | None = None) -> dict:
    path = index_path(root)
    files: dict[str, dict] = {}
    if changed is not None:
        if not path.exists():
            # No index on disk yet -- there is nothing to apply an incremental
            # patch on top of. Without this, the very first call (typically
            # the first post-commit hook run in a repo) would seed a stub
            # index containing only that one commit's files, and every
            # subsequent incremental update would keep building on top of
            # that partial base -- silently and permanently missing every
            # file that existed before the index did. Fall back to a full
            # scan instead, same as if the caller had passed changed=None.
            changed = None
        else:
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
                facts["is_test"] = is_test_file(rel)
                files[rel] = facts
    else:
        # diff-tree lists deletions and renames-away too, not just edits: a
        # `rel` that no longer exists (or moved out of the tracked suffixes)
        # must both drop its own entry AND stop being a valid import target
        # for every file that pointed at it -- otherwise those edges go stale
        # and stay wrong until the next --full rebuild. A file that still
        # exists but currently extracts to zero facts is NOT "gone": it is an
        # ordinary factless leaf, same as any import target that was never a
        # `files` key, so edges pointing at it are left alone.
        gone: set[str] = set()
        for rel in {c.replace("\\", "/") for c in changed}:
            files.pop(rel, None)
            f = root / rel
            if f.is_file() and f.suffix in JS_SUFFIXES | PY_SUFFIXES:
                facts = scan_file(f, root)
                if any(facts.values()):
                    facts["is_test"] = is_test_file(rel)
                    files[rel] = facts
            else:
                gone.add(rel)
        if gone:
            for facts in files.values():
                imports = facts.get("imports")
                if imports:
                    pruned = [i for i in imports if i not in gone]
                    if len(pruned) != len(imports):
                        facts["imports"] = pruned

    idx = {"v": INDEX_VERSION, "files": files}
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    write_levers_summary(root, idx)
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


def fan_in(idx: dict) -> dict:
    """How many files import each file."""
    counts: dict[str, int] = defaultdict(int)
    for facts in idx.get("files", {}).values():
        for target in facts.get("imports", []):
            counts[target] += 1
    return dict(counts)


def fan_out(idx: dict) -> dict:
    return {rel: len(f.get("imports", [])) for rel, f in idx.get("files", {}).items()}


def shortest_path(idx: dict, src: str, dst: str) -> list[str] | None:
    """Breadth-first over import edges. Direction matters: a imports b, not back.

    src == dst returns the trivial one-node path [src] rather than None --
    None means unreachable, and a file always reaches itself.
    """
    files = idx.get("files", {})
    if src not in files:
        return None
    queue, seen = deque([[src]]), {src}
    while queue:
        route = queue.popleft()
        if route[-1] == dst:
            return route
        for nxt in files.get(route[-1], {}).get("imports", []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(route + [nxt])
    return None


def writers(idx: dict, lever: str, exclude_tests: bool = False) -> list[str]:
    """Files that write the named table, read the named env key, or emit the event.

    One CLI verb for three different kinds of coupling, on purpose -- callers
    ask "who touches this" without caring which kind it is. Matching is NOT
    uniformly case-insensitive: table names are lowercased at extraction time
    (see extract()), so table lookups match any case; env keys and event names
    are stored exactly as written in the source, so those two match only the
    exact case given here.

    exclude_tests defaults to False -- a caller who does not pass it gets
    exactly the old, all-writers list. A count that silently dropped test
    files by default would be a different (if arguably more "honest") number
    than every caller of this function has relied on so far; opt in instead.
    """
    key = lever.lower()
    out = []
    for rel, facts in sorted(idx.get("files", {}).items()):
        if exclude_tests and facts.get("is_test"):
            continue
        if (key in [w.lower() for w in facts.get("writes", [])]
                or lever in facts.get("env", [])
                or lever in facts.get("emits", [])):
            out.append(rel)
    return out


def contended(idx: dict, minimum: int = 2, exclude_tests: bool = False) -> list[tuple[str, list[str]]]:
    """Levers with more than one writer, worst first.

    exclude_tests defaults to False, same reasoning as writers(). When True,
    the threshold is reapplied AFTER dropping test writers -- a lever with two
    writers, one of them a test, no longer counts as contended once tests are
    excluded; it must disappear from the result rather than shrink to a
    single-file list that still passed the >= minimum check on the old count.
    """
    by_lever: dict[str, list[str]] = defaultdict(list)
    for rel, facts in idx.get("files", {}).items():
        if exclude_tests and facts.get("is_test"):
            continue
        for w in facts.get("writes", []):
            by_lever[w].append(rel)
    rows = [(k, sorted(v)) for k, v in by_lever.items() if len(v) >= minimum]
    return sorted(rows, key=lambda kv: -len(kv[1]))


def _norm_rel(p: str) -> str:
    """User-typed path -> index-key shape.

    Index keys are always forward-slashed (see build()/scan_file()). A path
    pasted from an editor or from `where` output on Windows commonly carries
    backslashes; without this, a perfectly real file looks unknown to `path`
    and `levers` just because the separator does not match.
    """
    return p.replace("\\", "/")


def describe_path(idx: dict, source: str, target: str) -> tuple[int, list[str]]:
    """Answer for the `path` verb, without touching stdio.

    Distinguishes "one of these files isn't in the index" (probably a typo --
    with 1000+ files that is the likelier case) from "both are indexed but no
    import connects them" (a real, useful negative). Both endpoints get
    checked even when only one is unknown, so a single run surfaces every
    typo instead of making the caller fix-and-rerun once per bad name.
    """
    files = idx.get("files", {})
    src, dst = _norm_rel(source), _norm_rel(target)
    unknown = [p for p in (src, dst) if p not in files]
    if unknown:
        from difflib import get_close_matches  # CLI-path only, see module docstring
        lines = []
        for p in unknown:
            near = get_close_matches(p, files.keys(), n=3, cutoff=0.6)
            msg = f"{p}: not in the structure index"
            if near:
                msg += f" -- did you mean: {', '.join(near)}?"
            lines.append(msg)
        return 1, lines
    route = shortest_path(idx, src, dst)
    if route is None:
        return 1, [f"no import path from {src} to {dst} "
                    "(both files are indexed, they just don't connect)"]
    return 0, [("  " * i) + ("-> " if i else "") + step for i, step in enumerate(route)]


def cmd_build(args) -> int:
    import subprocess  # CLI-path only, see module docstring
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
    edges = sum(len(f["imports"]) for f in idx["files"].values())
    kind = "full" if changed is None else f"incremental ({len(changed)} file(s))"
    print(f"{root.name}: {len(idx['files'])} files, {edges} import edges, "
          f"{len(contended(idx))} contended levers, {kind}, {time.time() - t0:.2f}s")
    return 0


def cmd_map(args) -> int:
    root = Path(args.repo).resolve() if args.repo else repo_root()
    idx = load_index(root) if root else {}
    if not idx.get("files"):
        print("no structure index; run: wikictl map --rebuild")
        return 1
    fi, fo = fan_in(idx), fan_out(idx)
    print(f"{len(idx['files'])} files, {sum(fo.values())} import edges\n")
    print("most depended on (fan-in):")
    for rel, c in sorted(fi.items(), key=lambda kv: -kv[1])[:args.top]:
        print(f"  {c:4}  {rel}")
    print("\nmost dependent (fan-out):")
    for rel, c in sorted(fo.items(), key=lambda kv: -kv[1])[:args.top]:
        print(f"  {c:4}  {rel}")
    no_tests = getattr(args, "no_tests", False)
    rows = contended(idx, exclude_tests=no_tests)
    if rows:
        files_idx = idx.get("files", {})
        header = ("\nshared levers with more than one writer outside tests:" if no_tests else
                   "\nshared levers with more than one writer:")
        print(header)
        for lever, files in rows[:args.top]:
            # Already excluded and re-thresholded on non-test writers alone
            # when no_tests -- the parenthetical would just repeat len(files).
            suffix = "" if no_tests else (
                f" ({sum(1 for rel in files if not files_idx.get(rel, {}).get('is_test'))} outside tests)")
            print(f"  {len(files):4}  {lever}{suffix}: {', '.join(files[:3])}"
                  f"{' …' if len(files) > 3 else ''}")
    if getattr(args, "write", False):
        from wiki_paths import vault
        project_dir = vault() / "projects" / root.name
        if not project_dir.exists():
            print(f"no vault directory for {root.name}; ingest one block first")
            return 1
        created, merged, page_warnings = write_module_pages(idx, project_dir, args.top)
        pages = sorted(created + merged)
        print(f"\nwrote {len(pages)} module page(s) ({len(created)} new):")
        for p, _rel in pages:
            print(f"  {p}")
        for w in page_warnings:
            print(f"  warning: {w}")
        catalog_note = update_module_catalog(idx, project_dir, created)
        if catalog_note:
            print(f"\n{catalog_note}")
    print("\nStatic requires and literal SQL only. Dynamic requires, ORM calls and "
          "computed table names are invisible to this index.")
    return 0


def cmd_path(args) -> int:
    root = Path(args.repo).resolve() if args.repo else repo_root()
    idx = load_index(root) if root else {}
    code, lines = describe_path(idx, args.source, args.target)
    for line in lines:
        print(line)
    return code


def cmd_levers(args) -> int:
    root = Path(args.repo).resolve() if args.repo else repo_root()
    idx = load_index(root) if root else {}
    name = _norm_rel(args.name)
    files = writers(idx, name)
    if not files:
        print(f"{args.name}: no file writes, reads or emits this")
        return 1
    non_test_files = writers(idx, name, exclude_tests=True)
    shown = non_test_files if getattr(args, "no_tests", False) else files
    print(f"{args.name}: {len(files)} file(s) ({len(non_test_files)} outside tests)")
    for rel in shown[:args.limit]:
        print(f"  {rel}")
    if len(shown) > args.limit:
        print(f"  ... and {len(shown) - args.limit} more")
    return 0


# --------------------------------------------------------------------------- module pages (`map --write`)

STRUCT_BEGIN = "<!-- structure:begin - regenerated by wikictl map --write, do not edit -->"
STRUCT_END = "<!-- structure:end -->"


def module_page(idx: dict, rel: str) -> str:
    """The machine-owned half of an entity page."""
    facts = idx["files"].get(rel, {})
    importers = sorted(r for r, f in idx["files"].items() if rel in f.get("imports", []))
    lines = [STRUCT_BEGIN, "",
             f"**Path:** `{rel}`", "",
             f"**Imported by ({len(importers)}):** "
             + (", ".join(f"`{i}`" for i in importers[:12]) or "nothing")
             + (" …" if len(importers) > 12 else ""), ""]
    if facts.get("imports"):
        lines += [f"**Imports ({len(facts['imports'])}):** "
                  + ", ".join(f"`{i}`" for i in facts["imports"][:12])
                  + (" …" if len(facts["imports"]) > 12 else ""), ""]
    for label, key in (("Writes", "writes"), ("Reads env", "env"), ("Emits", "emits")):
        if facts.get(key):
            lines += [f"**{label}:** " + ", ".join(f"`{v}`" for v in facts[key]), ""]
    lines += ["Generated from static requires and literal SQL. Anything dynamic is missing.",
              "", STRUCT_END]
    return "\n".join(lines)


def _slug_component(name: str) -> str:
    """kebab-case one path segment, splitting camelCase/PascalCase first.

    Without the case-transition split, `orderRouter.js` reduces to
    `orderrouter` -- no separators for the later regex to bite on -- and
    duplicates the hand-written `order-router.md` instead of landing on
    it. `HTTPServer`-style runs of capitals get one split too, so `HTTPServer`
    -> `http-server` rather than `h-t-t-p-server` or `httpserver`.
    """
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "-", s)
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()


def module_slug(rel: str, taken: set[str]) -> str | None:
    """Vault-page slug for a module, disambiguated against a batch.

    Starts from just the file's own name (`src/execution/orderRouter.js`
    -> `order-router`) and only qualifies with parent directories when
    that collides with something already claimed in this same
    write_module_pages() call -- e.g. the six different `index.js` files in
    this repo would otherwise all reduce to `index.md` and silently
    overwrite each other's page one by one. Escalates one path segment at a
    time (`index` -> `src-index` -> `ai-signals-src-index` -> ...) until it
    finds a free slug. Returns None if the whole path is exhausted and still
    taken -- that would mean two distinct repo-relative paths agree on every
    single segment, which cannot happen, but refusing beats guessing.
    """
    segments = [seg for seg in Path(rel).with_suffix("").parts if seg not in (".", "..")]
    slugged = [s for s in (_slug_component(seg) for seg in segments) if s]
    for n in range(1, len(slugged) + 1):
        candidate = "-".join(slugged[-n:])
        if candidate and candidate not in taken:
            return candidate
    return None


def _merge_structure_block(text: str, block: str) -> str | None:
    """Splice the machine block into `text` between the structure markers.

    Returns the new page text, or None when the existing markers are
    malformed and it would be unsafe to guess: duplicated markers, one
    present without its pair (the shape a write left mid-crash would take,
    since a torn write truncates mid-block rather than removing whichever
    marker it hasn't reached yet), or END sitting before BEGIN. Neither
    marker present at all (first write for this page, or a purely
    hand-written page) is the only other shape, and appends rather than
    splices. Refusing on anything else means a malformed page stays exactly
    as it was found rather than silently losing whatever text used to
    follow the markers.
    """
    n_begin, n_end = text.count(STRUCT_BEGIN), text.count(STRUCT_END)
    if n_begin == 0 and n_end == 0:
        return text.rstrip() + "\n\n" + block + "\n"
    if n_begin != 1 or n_end != 1:
        return None
    i, j = text.index(STRUCT_BEGIN), text.index(STRUCT_END)
    if i > j:
        return None
    return text[:i] + block + text[j + len(STRUCT_END):]


def _atomic_write(path: Path, content: str) -> None:
    """Write-then-rename so a crash mid-write can never leave a torn page on
    disk. This writes into a hand-curated knowledge vault; a partially
    written file there is strictly worse than the write simply not
    happening, and the next run's malformed-marker check in
    _merge_structure_block() only works if a completed write is always
    either fully old or fully new, never half of each.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def write_module_pages(idx: dict, vault_project: Path,
                        top: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """Create or refresh the machine block; never touch prose outside it.

    Returns (created, merged, warnings). `created` and `merged` are both
    lists of (vault-relative page path, source module path) pairs -- the
    distinction is which branch below wrote the page:

      - `created`: no file sat at that page path before this call. This is
        the only case update_module_catalog() should ever add a catalog
        line for.
      - `merged`: a file was already there and its structure block was
        spliced in place. That pre-existing file is either hand-written
        prose a human already catalogued (the whole point of module_slug()
        landing generated facts on an existing page instead of a
        near-duplicate), or a page an earlier run of this tool created --
        and therefore already catalogued on that earlier run. Either way, a
        page in `merged` must never earn a second catalog line.

    `warnings` is unchanged from before: an unresolvable slug collision, or
    a page whose markers are malformed.
    """
    out_dir = vault_project / "wiki" / "entities"
    out_dir.mkdir(parents=True, exist_ok=True)
    created: list[tuple[str, str]] = []
    merged: list[tuple[str, str]] = []
    warnings: list[str] = []
    taken: set[str] = set()
    # -kv[1] ranks by fan-in descending; the kv[0] tiebreaker makes the
    # ranking (and therefore which module wins a short slug on a collision)
    # deterministic when two files tie on fan-in, rather than depending on
    # dict iteration order from the underlying file scan.
    ranked = sorted(fan_in(idx).items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    for rel, _count in ranked:
        slug = module_slug(rel, taken)
        if slug is None:
            warnings.append(f"{rel}: no free slug in this batch (every ancestor directory "
                            "collides with another module) -- skipped")
            continue
        taken.add(slug)
        page = out_dir / f"{slug}.md"
        page_rel = str(page.relative_to(vault_project)).replace("\\", "/")
        block = module_page(idx, rel)
        if page.exists():
            text = page.read_text(encoding="utf-8")
            merged_text = _merge_structure_block(text, block)
            if merged_text is None:
                warnings.append(f"{page.relative_to(vault_project).as_posix()}: structure "
                                "markers are malformed (duplicated, one-sided, or reversed) -- "
                                "refused to write; fix the page by hand first")
                continue
            _atomic_write(page, merged_text)
            merged.append((page_rel, rel))
        else:
            _atomic_write(page, f"# {Path(rel).stem}\n\nWhat this module is for: not written yet.\n\n"
                                f"{block}\n")
            created.append((page_rel, rel))
    return created, merged, warnings


# --------------------------------------------------------------------------- project catalog (index.md)

CATALOG_SECTIONS_AFTER_ENTITIES = ("Concepts", "Sources")


def module_catalog_summary(idx: dict, rel: str) -> str:
    """Factual, assessment-free blurb for a generated page's catalog line.

    Only imported-by count and what the module writes -- the honest material
    a structure index actually has. The index has no idea what a module is
    *for*; a line that guessed at one would be exactly the kind of confident
    nonsense a hand-curated catalog must not carry.
    """
    facts = idx.get("files", {}).get(rel, {})
    importers = sorted(r for r, f in idx.get("files", {}).items() if rel in f.get("imports", []))
    bits = [f"imported by {len(importers)}"]
    writes = facts.get("writes") or []
    if writes:
        shown = ", ".join(f"`{w}`" for w in writes[:5])
        bits.append(f"writes {shown}" + (" …" if len(writes) > 5 else ""))
    return "; ".join(bits) + "."


def _section_heading_lines(lines: list[str]) -> dict[str, int]:
    return {ln[3:].strip(): i for i, ln in enumerate(lines) if ln.startswith("## ")}


def update_module_catalog(idx: dict, project_dir: Path, created: list[tuple[str, str]]) -> str | None:
    """File one `## Entities` catalog line per newly CREATED page (see
    write_module_pages) into `project_dir/index.md`.

    Only `created` pages are ever considered -- a page write_module_pages
    merged into already has a human-written description (it existed before
    this run) or was already filed on an earlier run; either way a second
    line would duplicate the entry or clobber good prose with a generated
    stub.

    Idempotent two ways: nothing to do at all when `created` is empty (the
    common case -- a second `map --write` run in a row), and a link already
    present ANYWHERE in the file -- not only inside the `## Entities`
    section, a human may have filed it elsewhere -- is skipped rather than
    duplicated.

    When `## Entities` itself is missing, a new heading is created directly
    before whichever of `## Concepts` / `## Sources` appears first -- both
    are defined (see templates/projects/_template/index.md) to come after
    Entities, so either is a safe anchor. If neither is present the catalog
    does not follow the known schema closely enough to guess at a location,
    and nothing is written.

    Returns a human-readable note when nothing could be filed (index.md
    missing, or no anchor for a missing `## Entities` heading), else None.
    Writes atomically: either every new link lands, or the file is left
    exactly as it was found.
    """
    if not created:
        return None
    index_path = project_dir / "index.md"
    if not index_path.exists():
        return (f"{index_path}: catalog missing -- {len(created)} new page(s) not filed")

    text = index_path.read_text(encoding="utf-8")
    to_add = [(page_rel, mod_rel) for page_rel, mod_rel in created if f"]({page_rel})" not in text]
    if not to_add:
        return None

    lines = text.split("\n")
    headings = _section_heading_lines(lines)
    if "Entities" not in headings:
        anchor = next((h for h in CATALOG_SECTIONS_AFTER_ENTITIES if h in headings), None)
        if anchor is None:
            return (f"{index_path.name} has no ## Entities section and no ## Concepts / "
                    f"## Sources heading to anchor a new one before -- {len(to_add)} new "
                    "page(s) not filed (add the section by hand first)")
        i = headings[anchor]
        prefix = lines[:i]
        if prefix and prefix[-1].strip() != "":
            prefix = prefix + [""]
        lines = prefix + ["## Entities", ""] + lines[i:]
        headings = _section_heading_lines(lines)

    start = headings["Entities"] + 1
    end = start
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    # Keep any hand-written lines already inside the section, drop the
    # template's "None yet." placeholder, then append the new links.
    body = [ln for ln in lines[start:end] if ln.strip() != "None yet."]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    for page_rel, mod_rel in to_add:
        body.append(f"- [{page_rel}]({page_rel}) - {module_catalog_summary(idx, mod_rel)}")

    lines[start:end] = ["", *body, ""]
    _atomic_write(index_path, "\n".join(lines))
    return None


def main() -> int:
    import argparse  # CLI-path only, see module docstring
    ap = argparse.ArgumentParser(prog="wiki_structure", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build")
    p.add_argument("repo", nargs="?")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("map")
    p.add_argument("--repo")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--write", action="store_true",
                   help="create or refresh entity pages for the top modules")
    p.add_argument("--no-tests", dest="no_tests", action="store_true",
                   help="rank and list contended levers by non-test writers only")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("path")
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_path)

    p = sub.add_parser("levers")
    p.add_argument("name")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--no-tests", dest="no_tests", action="store_true",
                   help="list non-test writers only (the header count always shows both)")
    p.set_defaults(func=cmd_levers)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
