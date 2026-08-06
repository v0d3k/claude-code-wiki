"""Levers are the coupling an import graph cannot see: two functions that write
the same table contend even though neither imports the other."""
import subprocess

import wiki_structure


JS = """
const { open } = require('./db');
function insertPosition(db, row) {
  db.prepare('INSERT INTO positions (symbol) VALUES (?)').run(row.symbol);
}
function updatePosition(db, row) {
  db.prepare('UPDATE positions SET qty = ? WHERE id = ?').run(row.qty, row.id);
}
function readOnly(db) {
  return db.prepare('SELECT * FROM positions').all();
}
const timeout = process.env.DB_BUSY_TIMEOUT_MS;
bus.emit('position_opened', row);
"""


def test_sql_writes_are_extracted_and_reads_are_not():
    facts = wiki_structure.extract(JS, ".js")
    assert facts["writes"] == {"positions"}


def test_env_keys_are_extracted():
    assert wiki_structure.extract(JS, ".js")["env"] == {"DB_BUSY_TIMEOUT_MS"}


def test_events_are_extracted():
    assert wiki_structure.extract(JS, ".js")["emits"] == {"position_opened"}


def test_relative_imports_are_extracted():
    assert wiki_structure.extract(JS, ".js")["imports"] == {"./db"}


def test_python_levers():
    py = """
import os
cur.execute("INSERT INTO runs (id) VALUES (?)", (1,))
token = os.environ["OZON_TOKEN"]
"""
    facts = wiki_structure.extract(py, ".py")
    assert facts["writes"] == {"runs"}
    assert facts["env"] == {"OZON_TOKEN"}


# The JS fixture above only passes the import assertion by luck: its SQL says
# uppercase FROM against an unquoted table, and the import pattern is
# case-sensitive. Lowercase SQL against a quoted identifier is the same clause
# and must still not be mistaken for an import specifier.
SQL_NOT_IMPORTS = """
const q1 = 'select * from "runs"';
const q2 = `
  select *
  from "orders"
`;
db.exec('delete from "positions" where id = 1');
export const q3 = `select * from "ledger"`;
"""


def test_sql_from_clause_is_not_an_import():
    assert wiki_structure.extract(SQL_NOT_IMPORTS, ".js")["imports"] == set()


def test_side_effect_and_dynamic_imports_are_extracted():
    src = "import './register.js';\nconst m = import('./lazy.js');\n"
    assert wiki_structure.extract(src, ".js")["imports"] == {"./register.js", "./lazy.js"}


IDX = {"v": 1, "files": {
    "a.js": {"imports": ["b.js"], "writes": ["positions"], "env": [], "emits": []},
    "b.js": {"imports": ["c.js"], "writes": [], "env": [], "emits": []},
    "c.js": {"imports": [], "writes": ["positions"], "env": [], "emits": []},
    "d.js": {"imports": ["c.js"], "writes": [], "env": [], "emits": []},
}}


def test_fan_in_counts_importers():
    assert wiki_structure.fan_in(IDX)["c.js"] == 2


def test_shortest_path_follows_imports():
    assert wiki_structure.shortest_path(IDX, "a.js", "c.js") == ["a.js", "b.js", "c.js"]


def test_shortest_path_returns_none_when_unreachable():
    assert wiki_structure.shortest_path(IDX, "c.js", "a.js") is None


def test_levers_lists_every_writer_of_a_table():
    assert wiki_structure.writers(IDX, "positions") == ["a.js", "c.js"]


def test_shortest_path_source_equals_target_is_the_trivial_one_node_path():
    # A caller asking "how does a.js relate to a.js" should get a single-node
    # path, not None -- None means unreachable, and a file always reaches itself.
    assert wiki_structure.shortest_path(IDX, "a.js", "a.js") == ["a.js"]


def test_shortest_path_terminates_on_a_cycle():
    cyclic = {"v": 1, "files": {
        "a.js": {"imports": ["b.js"], "writes": [], "env": [], "emits": []},
        "b.js": {"imports": ["a.js"], "writes": [], "env": [], "emits": []},
    }}
    assert wiki_structure.shortest_path(cyclic, "a.js", "b.js") == ["a.js", "b.js"]
    # And an unreachable target in the same cyclic graph must still come back
    # (not hang) rather than looping the a<->b edge forever.
    assert wiki_structure.shortest_path(cyclic, "b.js", "z.js") is None


def test_writers_matches_table_writes_case_insensitively():
    # writes are lowercased at extraction time (see extract()), so a query in
    # any case must match.
    idx = {"v": 1, "files": {"w.js": {"imports": [], "writes": ["positions"],
                                       "env": [], "emits": []}}}
    assert wiki_structure.writers(idx, "POSITIONS") == ["w.js"]


def test_writers_env_lookup_is_case_sensitive():
    # env keys are NOT lowercased at extraction (JS_ENV/PY_ENV keep the
    # original, usually-uppercase, spelling) -- so unlike table writes, an env
    # lookup must match the stored case exactly.
    idx = {"v": 1, "files": {"e.js": {"imports": [], "writes": [],
                                       "env": ["DB_BUSY_TIMEOUT_MS"], "emits": []}}}
    assert wiki_structure.writers(idx, "ledger_busy_timeout_ms") == []
    assert wiki_structure.writers(idx, "DB_BUSY_TIMEOUT_MS") == ["e.js"]


def test_duplicate_import_specifiers_resolve_to_one_edge(tmp_path):
    # fan_in counts entries in each file's "imports" list, so the ranking is
    # only sound if that list can never hold the same resolved target twice.
    # scan_file builds it from a set comprehension over resolved paths -- this
    # pins that guarantee at the source rather than assuming it.
    (tmp_path / "db.js").write_text("module.exports = {};", encoding="utf-8")
    src = tmp_path / "a.js"
    src.write_text("const x = require('./db');\nconst y = require('./db');\n",
                    encoding="utf-8")
    facts = wiki_structure.scan_file(src, tmp_path)
    assert facts["imports"] == ["db.js"]


# --------------------------------------------------------------------------- describe_path (CLI `path` verb)

IDX_PATH = {"v": 1, "files": {
    "src/a.js": {"imports": ["src/b.js"], "writes": [], "env": [], "emits": []},
    "src/b.js": {"imports": [], "writes": [], "env": [], "emits": []},
    "src/c.js": {"imports": [], "writes": [], "env": [], "emits": []},  # real, but unconnected
}}


def test_describe_path_normalizes_windows_backslashes():
    # A path pasted from an editor or from `where` output on Windows commonly
    # carries backslashes; index keys are always forward-slashed (build()/
    # scan_file()), so the CLI boundary has to normalize before lookup.
    code, lines = wiki_structure.describe_path(IDX_PATH, "src\\a.js", "src\\b.js")
    assert code == 0
    assert lines == ["src/a.js", "  -> src/b.js"]


def test_describe_path_unknown_endpoint_is_not_confused_with_unreachable():
    # Typo case: says the file isn't indexed at all.
    code, lines = wiki_structure.describe_path(IDX_PATH, "src/a.js", "src/nope.js")
    assert code == 1
    assert "not in the structure index" in lines[0]

    # Unreachable case: both files are real and indexed, they just don't
    # connect -- a distinct message so a real gap doesn't send the reader
    # typo-hunting for a name that was always correct.
    code2, lines2 = wiki_structure.describe_path(IDX_PATH, "src/a.js", "src/c.js")
    assert code2 == 1
    assert "not in the structure index" not in lines2[0]
    assert "no import path" in lines2[0]


def test_describe_path_suggests_near_matches_for_unknown_endpoints():
    code, lines = wiki_structure.describe_path(IDX_PATH, "src/aa.js", "src/c.js")
    assert code == 1
    assert "did you mean" in lines[0]
    assert "src/a.js" in lines[0]


def test_describe_path_reports_every_unknown_endpoint_in_one_run():
    # With 1000+ files a typo is the likelier failure than "unconnected", and
    # a caller who mistyped both ends should not have to fix-and-rerun twice.
    code, lines = wiki_structure.describe_path(IDX_PATH, "nope1.js", "nope2.js")
    assert code == 1
    assert len(lines) == 2


# --------------------------------------------------------------------------- incremental build vs. deletions

def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _commit_all(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-qm", message)


def _diff_tree_head(repo):
    out = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def test_incremental_build_falls_back_to_full_scan_when_no_index_exists_yet(tmp_path):
    # This is what the post-commit hook does on a repo's first commit: call
    # build(root, changed) with only the files that one commit touched, on a
    # repo where no structure index has ever been written. A true incremental
    # apply on top of an empty base would silently seed a stub index holding
    # only those files -- src/c.js below sits on disk already but is not part
    # of `changed`, so it only ends up in the index if build() detects the
    # missing index file and falls back to a full scan.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.js").write_text("require('./b');\n", encoding="utf-8")
    (repo / "src" / "b.js").write_text("module.exports = {};\n", encoding="utf-8")  # zero facts
    (repo / "src" / "c.js").write_text("const t = process.env.SOME_KEY;\n", encoding="utf-8")

    idx = wiki_structure.build(repo, changed=["src/a.js"])

    assert "src/c.js" in idx["files"]  # only reachable via a full scan
    assert "src/a.js" in idx["files"]


def test_incremental_build_removes_the_deleted_files_own_entry(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "src" / "b.js").write_text("const t = process.env.SOME_KEY;\n", encoding="utf-8")
    _commit_all(repo, "init")
    idx = wiki_structure.build(repo, changed=None)
    assert "src/b.js" in idx["files"]

    (repo / "src" / "b.js").unlink()
    _commit_all(repo, "delete b")
    changed = _diff_tree_head(repo)
    assert changed == ["src/b.js"]

    idx2 = wiki_structure.build(repo, changed)  # must not raise on a vanished path
    assert "src/b.js" not in idx2["files"]


def test_incremental_build_prunes_dangling_edges_in_files_diff_tree_never_mentions(tmp_path):
    # diff-tree HEAD only reports the file that was actually deleted, not
    # everyone who imports it, so an unrelated importer that build() never
    # rescans would otherwise keep pointing at a file that no longer exists.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "src" / "a.js").write_text("require('./b');\n", encoding="utf-8")
    (repo / "src" / "b.js").write_text("const t = process.env.SOME_KEY;\n", encoding="utf-8")
    _commit_all(repo, "init")
    idx = wiki_structure.build(repo, changed=None)
    assert idx["files"]["src/a.js"]["imports"] == ["src/b.js"]

    (repo / "src" / "b.js").unlink()
    _commit_all(repo, "delete b")
    changed = _diff_tree_head(repo)
    assert changed == ["src/b.js"]  # a.js itself was NOT touched by this commit

    idx2 = wiki_structure.build(repo, changed)
    assert idx2["files"]["src/a.js"]["imports"] == []


def test_incremental_build_keeps_edges_to_files_that_still_exist_but_lost_their_facts(tmp_path):
    # Losing facts is not the same as being deleted: a file that still exists
    # on disk, just currently without SQL writes/env/emits/imports of its own,
    # is an ordinary factless leaf -- same as any import target that was never
    # a `files` key -- so edges pointing at it must survive the sweep.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "src" / "a.js").write_text("require('./b');\n", encoding="utf-8")
    (repo / "src" / "b.js").write_text("const t = process.env.SOME_KEY;\n", encoding="utf-8")
    _commit_all(repo, "init")
    wiki_structure.build(repo, changed=None)

    (repo / "src" / "b.js").write_text("module.exports = {};\n", encoding="utf-8")  # -> zero facts
    _commit_all(repo, "strip facts from b")
    changed = _diff_tree_head(repo)
    assert changed == ["src/b.js"]

    idx2 = wiki_structure.build(repo, changed)
    assert "src/b.js" not in idx2["files"]                        # factless, so not its own key ...
    assert idx2["files"]["src/a.js"]["imports"] == ["src/b.js"]   # ... but the edge survives
