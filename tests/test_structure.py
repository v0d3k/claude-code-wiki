"""Levers are the coupling an import graph cannot see: two functions that write
the same table contend even though neither imports the other."""
import argparse
import json
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


# --------------------------------------------------------------------------- is_test_file

def test_is_test_file_recognizes_test_directory_segments():
    assert wiki_structure.is_test_file("test/foo.js")
    assert wiki_structure.is_test_file("tests/foo.py")
    assert wiki_structure.is_test_file("packages/a/__tests__/foo.js")
    assert wiki_structure.is_test_file("spec/foo.js")
    assert wiki_structure.is_test_file("packages/a/test/nested/foo.js")


def test_is_test_file_recognizes_dot_test_and_dot_spec_filenames():
    assert wiki_structure.is_test_file("src/foo.test.js")
    assert wiki_structure.is_test_file("src/foo.spec.ts")
    assert not wiki_structure.is_test_file("src/footest.js")


def test_is_test_file_recognizes_python_test_prefix_and_suffix():
    assert wiki_structure.is_test_file("src/test_foo.py")
    assert wiki_structure.is_test_file("src/foo_test.py")
    assert not wiki_structure.is_test_file("src/testing_utils.py")


def test_is_test_file_does_not_flag_a_shipped_file_that_merely_contains_test_in_its_name():
    # data/_refactor-tests.cjs is a real path in the repository this predicate is
    # measured against: a one-off migration script, not a test file. A bare
    # "test" substring anywhere in the name would misclassify it, and this same
    # shape (a hyphenated "-tests" or "-test" tail with no directory segment and
    # no dot-test/dot-spec marker) recurs across that repo's stress-test and
    # backtest scripts.
    assert not wiki_structure.is_test_file("data/_refactor-tests.cjs")
    assert not wiki_structure.is_test_file("scripts/run-strategy-stress-test.cjs")
    assert not wiki_structure.is_test_file("scripts/backtest-fetch-klines.cjs")
    assert not wiki_structure.is_test_file("scripts/liquidity-ground-truth-test.cjs")


def test_is_test_file_normalizes_windows_backslashes():
    assert wiki_structure.is_test_file("packages\\a\\test\\foo.js")


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


# --------------------------------------------------------------------------- test-aware writers() / contended()

IDX_TESTAWARE = {"v": wiki_structure.INDEX_VERSION, "files": {
    "src/a.js": {"imports": [], "writes": ["positions"], "env": [], "emits": [], "is_test": False},
    "test/b.test.js": {"imports": [], "writes": ["positions"], "env": [], "emits": [], "is_test": True},
    "src/c.js": {"imports": [], "writes": ["positions"], "env": [], "emits": [], "is_test": False},
}}


def test_writers_default_includes_test_files_same_as_before_the_flag_existed():
    # The default must not silently change existing behaviour: a caller who
    # does not know about exclude_tests gets exactly the old, all-writers list.
    assert wiki_structure.writers(IDX_TESTAWARE, "positions") == ["src/a.js", "src/c.js", "test/b.test.js"]


def test_writers_exclude_tests_true_drops_test_files():
    assert wiki_structure.writers(IDX_TESTAWARE, "positions", exclude_tests=True) == ["src/a.js", "src/c.js"]


def test_contended_default_includes_test_files_same_as_before_the_flag_existed():
    rows = dict(wiki_structure.contended(IDX_TESTAWARE))
    assert rows["positions"] == ["src/a.js", "src/c.js", "test/b.test.js"]


def test_contended_exclude_tests_true_drops_test_files_and_reapplies_the_threshold():
    # Excluding tests can drop a lever below the minimum-writer threshold
    # entirely -- outcomes has 2 writers, but only 1 once tests are excluded,
    # so it must disappear from the result, not just shrink to a 1-item list.
    idx = {"v": wiki_structure.INDEX_VERSION, "files": {
        "src/a.js": {"imports": [], "writes": ["outcomes"], "env": [], "emits": [], "is_test": False},
        "test/b.test.js": {"imports": [], "writes": ["outcomes"], "env": [], "emits": [], "is_test": True},
    }}
    assert dict(wiki_structure.contended(idx))["outcomes"] == ["src/a.js", "test/b.test.js"]
    assert dict(wiki_structure.contended(idx, exclude_tests=True)) == {}


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


# --------------------------------------------------------------------------- is_test flag storage and version bump

def test_build_full_scan_stores_the_is_test_flag_on_each_file(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "test").mkdir(parents=True)
    (repo / "src" / "a.js").write_text(
        "db.exec('INSERT INTO runs (id) VALUES (1)');\n", encoding="utf-8")
    (repo / "test" / "a.test.js").write_text(
        "db.exec('INSERT INTO runs (id) VALUES (1)');\n", encoding="utf-8")

    idx = wiki_structure.build(repo, changed=None)

    assert idx["files"]["src/a.js"]["is_test"] is False
    assert idx["files"]["test/a.test.js"]["is_test"] is True


def test_build_incremental_scan_also_stores_the_is_test_flag(tmp_path):
    repo = tmp_path / "repo"
    (repo / "test").mkdir(parents=True)
    (repo / "test" / "b.test.js").write_text(
        "db.exec('INSERT INTO runs (id) VALUES (1)');\n", encoding="utf-8")
    wiki_structure.build(repo, changed=None)

    (repo / "test" / "b.test.js").write_text(
        "db.exec('INSERT INTO runs (id) VALUES (2)');\n", encoding="utf-8")
    idx = wiki_structure.build(repo, changed=["test/b.test.js"])

    assert idx["files"]["test/b.test.js"]["is_test"] is True


def test_build_forces_a_full_rescan_when_the_on_disk_index_predates_the_is_test_flag(tmp_path):
    # The version check in build() already forces a full rebuild whenever the
    # on-disk `v` does not match INDEX_VERSION -- this pins that bumping
    # INDEX_VERSION for the is_test flag actually exercises that existing path,
    # rather than silently patching a shape that has no is_test key at all.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.js").write_text("require('./b');\n", encoding="utf-8")
    (repo / "src" / "b.js").write_text("module.exports = {};\n", encoding="utf-8")
    (repo / "src" / "c.js").write_text("const t = process.env.SOME_KEY;\n", encoding="utf-8")

    stale = {"v": wiki_structure.INDEX_VERSION - 1,
             "files": {"src/a.js": {"imports": ["src/b.js"], "writes": [], "env": [], "emits": []}}}
    wiki_structure.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    (wiki_structure.INDEX_DIR / f"{repo.name}.json").write_text(json.dumps(stale), encoding="utf-8")

    idx = wiki_structure.build(repo, changed=["src/a.js"])

    assert idx["v"] == wiki_structure.INDEX_VERSION
    assert "src/c.js" in idx["files"]  # only reachable via a full scan
    assert idx["files"]["src/a.js"]["is_test"] is False


# --------------------------------------------------------------------------- compact levers summary (guard input)

def test_build_writes_a_levers_summary_alongside_the_index(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "test").mkdir(parents=True)
    (repo / "src" / "a.js").write_text(
        "db.exec('INSERT INTO positions (id) VALUES (1)');\n", encoding="utf-8")
    (repo / "src" / "b.js").write_text(
        "db.exec('UPDATE positions SET qty = 1');\n", encoding="utf-8")
    (repo / "test" / "c.test.js").write_text(
        "db.exec('UPDATE positions SET qty = 1');\n", encoding="utf-8")

    wiki_structure.build(repo, changed=None)
    summary = wiki_structure.load_levers_summary(repo)

    assert summary == {"positions": [3, 2]}


def test_levers_summary_covers_every_writer_not_just_the_contended_ones():
    # build_levers_summary() must not apply contended()'s >= 2 threshold --
    # the guard applies its own threshold at check time (guard_min_lever_writers),
    # so a table with a single writer still needs an entry to compare against.
    idx = {"v": wiki_structure.INDEX_VERSION, "files": {
        "src/a.js": {"imports": [], "writes": ["runs"], "env": [], "emits": [], "is_test": False},
    }}
    assert wiki_structure.build_levers_summary(idx) == {"runs": [1, 1]}


def test_levers_summary_counts_test_and_non_test_writers_separately():
    idx = {"v": wiki_structure.INDEX_VERSION, "files": {
        "src/a.js": {"imports": [], "writes": ["positions"], "env": [], "emits": [], "is_test": False},
        "test/b.test.js": {"imports": [], "writes": ["positions"], "env": [], "emits": [], "is_test": True},
        "src/c.js": {"imports": [], "writes": ["positions"], "env": [], "emits": [], "is_test": False},
    }}
    assert wiki_structure.build_levers_summary(idx) == {"positions": [3, 2]}


def test_load_levers_summary_is_empty_when_no_summary_has_ever_been_written(tmp_path):
    assert wiki_structure.load_levers_summary(tmp_path) == {}


def test_load_levers_summary_is_empty_on_a_version_mismatch(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.js").write_text(
        "db.exec('INSERT INTO positions (id) VALUES (1)');\n", encoding="utf-8")
    wiki_structure.build(repo, changed=None)

    stale = {"v": wiki_structure.INDEX_VERSION - 1, "levers": {"positions": [1, 1]}}
    wiki_structure.levers_summary_path(repo).write_text(json.dumps(stale), encoding="utf-8")

    assert wiki_structure.load_levers_summary(repo) == {}


def test_load_levers_summary_is_empty_on_malformed_json(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    wiki_structure.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    wiki_structure.levers_summary_path(repo).write_text("{not json", encoding="utf-8")

    assert wiki_structure.load_levers_summary(repo) == {}


def test_levers_summary_is_much_smaller_than_the_full_index(tmp_path):
    # The whole point: the guard must be able to load this on every Write
    # without paying for the full index's size.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    for i in range(30):
        (repo / "src" / f"f{i}.js").write_text(
            f"db.exec('INSERT INTO t{i} (id) VALUES (1)');\n"
            "db.exec('INSERT INTO shared (id) VALUES (1)');\n", encoding="utf-8")
    wiki_structure.build(repo, changed=None)

    index_bytes = wiki_structure.index_path(repo).stat().st_size
    summary_bytes = wiki_structure.levers_summary_path(repo).stat().st_size
    assert summary_bytes < index_bytes / 3


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


# --------------------------------------------------------------------------- module pages (`map --write`)

def test_slug_component_splits_camelcase_to_kebab_case():
    # The bug this guards against: without a case-transition split,
    # `orderRouter` has no separators at all for a `[^a-z0-9]+` regex to
    # bite on, so it reduces to `orderrouter` -- a near-miss of the
    # hand-written `order-router.md` that duplicates instead of merging.
    assert wiki_structure._slug_component("orderRouter") == "order-router"
    assert wiki_structure._slug_component("buildRequestContext") == "build-request-context"
    assert wiki_structure._slug_component("wiki_structure") == "wiki-structure"  # unaffected, unchanged behavior
    assert wiki_structure._slug_component("HTTPServer") == "http-server"  # run of capitals -> one split


def test_module_slug_matches_the_real_hand_written_position_steward_page():
    # This is the one hand-written entity page (of the four in the real
    # example-repo vault) that genuinely corresponds to a single source module
    # -- src/execution/orderRouter.js -- so the slug the generator picks
    # for it must equal the page that already exists: order-router.md.
    assert wiki_structure.module_slug("src/execution/orderRouter.js", set()) == "order-router"


def test_module_slug_can_reach_the_other_three_hand_written_names_but_no_real_file_produces_them():
    # request-advisor.md, llm-stack.md and live-db.md describe a subsystem, a
    # cross-cutting concept, and a SQLite data file respectively -- none of
    # them is one JS/PY module. The slug algorithm CAN land on these names
    # (proven here against synthetic paths shaped like they would be), but
    # in the real example-repo structure index no file is named requestAdvisor.js,
    # llmStack.js or liveDb.js/live.db (verified separately against the real
    # index: 1195 files, none matching), so write_module_pages() will not
    # accidentally collide with these three hand-written pages -- it will
    # only ever create brand-new stub pages if it ranks a related-but-
    # differently-named file into the top N.
    assert wiki_structure.module_slug("src/somewhere/requestAdvisor.js", set()) == "request-advisor"
    assert wiki_structure.module_slug("src/somewhere/llmStack.js", set()) == "llm-stack"
    assert wiki_structure.module_slug("src/somewhere/liveDb.js", set()) == "live-db"
    # The real files nearest to these three concepts do NOT reduce to the
    # hand-written slugs -- confirming the near-miss is not silently forced:
    assert wiki_structure.module_slug("src/analysis/buildRequestContext.js", set()) != "request-advisor"
    assert wiki_structure.module_slug(
        "packages/ai-signals/src/providers/openaiProvider.js", set()) != "llm-stack"


def test_module_slug_disambiguates_a_collision_within_one_batch():
    # Six different index.js files exist in the real example-repo repo; two
    # is enough to prove the escalation. First claim wins the short slug,
    # the second must qualify with a parent directory rather than overwrite it.
    taken = set()
    first = wiki_structure.module_slug("packages/a/src/index.js", taken)
    taken.add(first)
    second = wiki_structure.module_slug("packages/b/src/index.js", taken)
    assert first == "index"
    assert second == "src-index"
    assert first != second


def test_module_slug_returns_none_when_the_whole_path_is_already_taken():
    # A single-segment path (no parent directory left to qualify with)
    # whose only possible slug is already claimed must refuse rather than
    # silently overwrite whatever claimed it first.
    assert wiki_structure.module_slug("index.js", {"index"}) is None


def test_merge_structure_block_appends_when_no_markers_are_present():
    # First write for a page, or a page that is purely hand-written prose:
    # the block is appended, nothing existing is touched.
    text = "# a\n\nHand-written notes.\n"
    merged = wiki_structure._merge_structure_block(text, "BLOCK")
    assert merged == "# a\n\nHand-written notes.\n\nBLOCK\n"


def test_merge_structure_block_replaces_only_the_marked_region():
    text = ("# a\n\nProse above.\n\n"
            + wiki_structure.STRUCT_BEGIN + "\nold block\n" + wiki_structure.STRUCT_END
            + "\n\nProse below.\n")
    new_block = wiki_structure.STRUCT_BEGIN + "\nNEW BLOCK\n" + wiki_structure.STRUCT_END
    merged = wiki_structure._merge_structure_block(text, new_block)
    assert "Prose above." in merged
    assert "Prose below." in merged
    assert "old block" not in merged
    assert "NEW BLOCK" in merged
    assert merged.count(wiki_structure.STRUCT_BEGIN) == 1


def test_merge_structure_block_refuses_when_begin_has_no_matching_end():
    # The shape a crashed write leaves behind: BEGIN written, END never
    # reached. Guessing here (e.g. via a bare partition()) risks discarding
    # everything after BEGIN; refusing is the only safe move on a vault page.
    text = "# a\n\nProse.\n\n" + wiki_structure.STRUCT_BEGIN + "\nhalf-written\n"
    assert wiki_structure._merge_structure_block(text, "NEW BLOCK") is None


def test_merge_structure_block_refuses_on_duplicated_markers():
    text = (wiki_structure.STRUCT_BEGIN + "\nold\n" + wiki_structure.STRUCT_END + "\n"
            + wiki_structure.STRUCT_BEGIN + "\nstray\n")
    assert wiki_structure._merge_structure_block(text, "NEW BLOCK") is None


def test_merge_structure_block_refuses_when_end_precedes_begin():
    text = wiki_structure.STRUCT_END + "\nstray\n" + wiki_structure.STRUCT_BEGIN + "\nunterminated\n"
    assert wiki_structure._merge_structure_block(text, "NEW BLOCK") is None


def test_atomic_write_leaves_no_tmp_file_and_writes_the_content(tmp_path):
    target = tmp_path / "page.md"
    wiki_structure._atomic_write(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert not target.with_suffix(target.suffix + ".tmp").exists()


IDX_WM = {"v": wiki_structure.INDEX_VERSION, "files": {
    "src/a.js": {"imports": [], "writes": ["t1"], "env": [], "emits": []},
    "src/b.js": {"imports": ["src/a.js"], "writes": [], "env": [], "emits": []},
}}


def test_write_module_pages_creates_stub_pages_for_new_modules(tmp_path):
    # fan_in only ranks files that something else imports -- src/b.js is the
    # importer here, not an import target, so it has zero fan-in and is not
    # itself a candidate for a page (matching what `map`'s own "most
    # depended on" ranking already means).
    created, merged, warnings = wiki_structure.write_module_pages(IDX_WM, tmp_path, top=10)
    assert warnings == []
    assert merged == []
    assert created == [("wiki/entities/a.md", "src/a.js")]
    page = (tmp_path / "wiki" / "entities" / "a.md").read_text(encoding="utf-8")
    assert "What this module is for: not written yet." in page
    assert "**Path:** `src/a.js`" in page
    assert "`t1`" in page
    assert wiki_structure.STRUCT_BEGIN in page and wiki_structure.STRUCT_END in page
    assert not (tmp_path / "wiki" / "entities" / "b.md").exists()


def test_write_module_pages_preserves_hand_written_prose_across_regeneration(tmp_path):
    # This is the plan's Step 3, run against a scratch vault directory
    # (tmp_path here), never the real Obsidian vault.
    idx_v1 = {"v": 1, "files": {
        "src/a.js": {"imports": [], "writes": ["t1"], "env": [], "emits": []},
        "src/b.js": {"imports": ["src/a.js"], "writes": [], "env": [], "emits": []},
    }}
    wiki_structure.write_module_pages(idx_v1, tmp_path, top=10)
    page = tmp_path / "wiki" / "entities" / "a.md"

    # A human edits the page: replaces the placeholder with real prose above
    # the machine block.
    hand_written = page.read_text(encoding="utf-8").replace(
        "What this module is for: not written yet.",
        "Hand-written summary sentence that must survive regeneration.")
    page.write_text(hand_written, encoding="utf-8")

    # The module gains a fact (e.g. a new table write) and the page is regenerated.
    idx_v2 = {"v": 1, "files": {
        "src/a.js": {"imports": [], "writes": ["t1", "t2"], "env": [], "emits": []},
        "src/b.js": {"imports": ["src/a.js"], "writes": [], "env": [], "emits": []},
    }}
    created, merged, warnings = wiki_structure.write_module_pages(idx_v2, tmp_path, top=10)

    final = page.read_text(encoding="utf-8")
    assert warnings == []
    assert created == []                                        # page already existed -> merged, not created
    assert merged == [("wiki/entities/a.md", "src/a.js")]
    assert "Hand-written summary sentence that must survive regeneration." in final
    assert "`t2`" in final                                  # block was refreshed
    assert final.count(wiki_structure.STRUCT_BEGIN) == 1     # not duplicated


def test_write_module_pages_disambiguates_a_real_collision_shape(tmp_path):
    # Two distinct index.js files, equal fan-in (one importer each), tied
    # and broken by rel-path order -- packages/a/... sorts before packages/b/....
    idx = {"v": 1, "files": {
        "packages/a/src/index.js": {"imports": [], "writes": [], "env": [], "emits": []},
        "packages/b/src/index.js": {"imports": [], "writes": [], "env": [], "emits": []},
        "src/c.js": {"imports": ["packages/a/src/index.js", "packages/b/src/index.js"],
                     "writes": [], "env": [], "emits": []},
    }}
    created, merged, warnings = wiki_structure.write_module_pages(idx, tmp_path, top=10)
    assert warnings == []
    assert merged == []
    assert {p for p, _rel in created} == {"wiki/entities/index.md", "wiki/entities/src-index.md"}
    a_page = (tmp_path / "wiki" / "entities" / "index.md").read_text(encoding="utf-8")
    b_page = (tmp_path / "wiki" / "entities" / "src-index.md").read_text(encoding="utf-8")
    assert "**Path:** `packages/a/src/index.js`" in a_page
    assert "**Path:** `packages/b/src/index.js`" in b_page


def test_write_module_pages_refuses_and_preserves_a_page_with_malformed_markers(tmp_path):
    out_dir = tmp_path / "wiki" / "entities"
    out_dir.mkdir(parents=True)
    broken = "# a\n\nHand notes.\n\n" + wiki_structure.STRUCT_BEGIN + "\nhalf-written, crashed here\n"
    (out_dir / "a.md").write_text(broken, encoding="utf-8")

    created, merged, warnings = wiki_structure.write_module_pages(IDX_WM, tmp_path, top=10)

    touched = {p for p, _rel in created + merged}
    assert "wiki/entities/a.md" not in touched
    assert any("malformed" in w for w in warnings)
    assert (out_dir / "a.md").read_text(encoding="utf-8") == broken  # untouched, nothing lost


def test_cmd_map_write_reports_missing_vault_project_without_crashing(tmp_path, monkeypatch):
    # No `projects/<name>` directory has ever been scaffolded for this repo
    # -- cmd_map --write must say so and exit non-zero, not raise.
    repo = tmp_path / "repo"
    repo.mkdir()
    wiki_structure.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    (wiki_structure.INDEX_DIR / f"{repo.name}.json").write_text(json.dumps(IDX_WM), encoding="utf-8")
    args = argparse.Namespace(repo=str(repo), top=10, write=True)
    assert wiki_structure.cmd_map(args) == 1


def test_cmd_map_write_creates_pages_under_the_isolated_vault(tmp_path, monkeypatch, capsys):
    # isolated_home (autouse, see conftest.py) already points wiki_paths at a
    # scratch config+vault under this test's own tmp_path -- this exercises
    # the real vault()-resolution path in cmd_map without touching the real
    # Obsidian vault. No index.md is scaffolded here on purpose: cmd_map must
    # still write the pages and just note the catalog could not be filed,
    # not fail the whole command (see test_cmd_map_write_also_files_the_catalog
    # for the index.md-present path).
    import wiki_paths
    repo = tmp_path / "repo"
    repo.mkdir()
    wiki_structure.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    (wiki_structure.INDEX_DIR / f"{repo.name}.json").write_text(json.dumps(IDX_WM), encoding="utf-8")
    project_dir = wiki_paths.vault() / "projects" / repo.name
    (project_dir / "wiki" / "entities").mkdir(parents=True)

    args = argparse.Namespace(repo=str(repo), top=10, write=True)
    assert wiki_structure.cmd_map(args) == 0
    assert (project_dir / "wiki" / "entities" / "a.md").exists()
    assert not (project_dir / "wiki" / "entities" / "b.md").exists()  # zero fan-in, not ranked
    out = capsys.readouterr().out
    assert "catalog missing" in out
    assert "1 new page(s) not filed" in out


def test_cmd_map_write_also_files_the_catalog(tmp_path, capsys):
    # The happy path end to end: an index.md with a real ## Entities section
    # picks up a line for the newly created page.
    import wiki_paths
    repo = tmp_path / "repo"
    repo.mkdir()
    wiki_structure.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    (wiki_structure.INDEX_DIR / f"{repo.name}.json").write_text(json.dumps(IDX_WM), encoding="utf-8")
    project_dir = wiki_paths.vault() / "projects" / repo.name
    (project_dir / "wiki" / "entities").mkdir(parents=True)
    (project_dir / "index.md").write_text(
        "# repo — Index\n\n## Analyses\n\nNone yet.\n\n## Entities\n\nNone yet.\n\n"
        "## Concepts\n\nNone yet.\n\n## Sources\n\nNone yet.\n", encoding="utf-8")

    args = argparse.Namespace(repo=str(repo), top=10, write=True)
    assert wiki_structure.cmd_map(args) == 0
    catalog = (project_dir / "index.md").read_text(encoding="utf-8")
    assert "[wiki/entities/a.md](wiki/entities/a.md)" in catalog
    assert "imported by 1" in catalog
    assert "`t1`" in catalog
    out = capsys.readouterr().out
    assert "catalog missing" not in out


# --------------------------------------------------------------------------- --no-tests (cmd_levers / cmd_map)

def _write_index(repo, idx: dict) -> None:
    wiki_structure.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    (wiki_structure.INDEX_DIR / f"{repo.name}.json").write_text(json.dumps(idx), encoding="utf-8")


def test_cmd_levers_default_output_always_shows_the_split(tmp_path, capsys):
    # "positions: 22 file(s) (6 outside tests)" is more useful than either
    # number alone -- the header carries both, regardless of --no-tests.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_index(repo, IDX_TESTAWARE)

    args = argparse.Namespace(repo=str(repo), name="positions", limit=20, no_tests=False)
    assert wiki_structure.cmd_levers(args) == 0
    out = capsys.readouterr().out
    assert "positions: 3 file(s) (2 outside tests)" in out
    assert "test/b.test.js" in out  # default listing still includes the test writer


def test_cmd_levers_no_tests_filters_the_listed_files_but_keeps_the_split_header(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_index(repo, IDX_TESTAWARE)

    args = argparse.Namespace(repo=str(repo), name="positions", limit=20, no_tests=True)
    assert wiki_structure.cmd_levers(args) == 0
    out = capsys.readouterr().out
    assert "positions: 3 file(s) (2 outside tests)" in out
    assert "test/b.test.js" not in out
    assert "src/a.js" in out and "src/c.js" in out


IDX_MAP_TESTAWARE = {"v": wiki_structure.INDEX_VERSION, "files": {
    "src/a.js": {"imports": [], "writes": ["outcomes"], "env": [], "emits": [], "is_test": False},
    "test/b.test.js": {"imports": [], "writes": ["outcomes"], "env": [], "emits": [], "is_test": True},
    "src/c.js": {"imports": [], "writes": ["positions"], "env": [], "emits": [], "is_test": False},
    "src/d.js": {"imports": [], "writes": ["positions"], "env": [], "emits": [], "is_test": False},
    "test/e.test.js": {"imports": [], "writes": ["positions"], "env": [], "emits": [], "is_test": True},
}}


def test_cmd_map_default_shows_the_split_for_every_contended_lever(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_index(repo, IDX_MAP_TESTAWARE)

    args = argparse.Namespace(repo=str(repo), top=10, write=False, no_tests=False)
    assert wiki_structure.cmd_map(args) == 0
    out = capsys.readouterr().out
    assert "outcomes (1 outside tests)" in out
    assert "positions (2 outside tests)" in out


def test_cmd_map_no_tests_reapplies_the_threshold_after_excluding_tests(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_index(repo, IDX_MAP_TESTAWARE)

    args = argparse.Namespace(repo=str(repo), top=10, write=False, no_tests=True)
    assert wiki_structure.cmd_map(args) == 0
    out = capsys.readouterr().out
    levers_section = out.split("shared levers")[1]
    assert "outcomes" not in levers_section  # only 1 non-test writer left, below the minimum-2 threshold
    assert "positions" in levers_section
    assert "test/e.test.js" not in levers_section  # test writer dropped from the listed files too


# --------------------------------------------------------------------------- module_catalog_summary

def test_module_catalog_summary_reports_imported_by_count_and_writes():
    idx = {"v": wiki_structure.INDEX_VERSION, "files": {
        "src/order-router.js": {"imports": [], "writes": ["orders", "positions"], "env": [], "emits": []},
        "src/caller-one.js": {"imports": ["src/order-router.js"], "writes": [], "env": [], "emits": []},
        "src/caller-two.js": {"imports": ["src/order-router.js"], "writes": [], "env": [], "emits": []},
    }}
    summary = wiki_structure.module_catalog_summary(idx, "src/order-router.js")
    assert summary == "imported by 2; writes `orders`, `positions`."


def test_module_catalog_summary_omits_writes_clause_when_the_module_writes_nothing():
    idx = {"v": wiki_structure.INDEX_VERSION, "files": {
        "src/reader.js": {"imports": [], "writes": [], "env": [], "emits": []},
    }}
    assert wiki_structure.module_catalog_summary(idx, "src/reader.js") == "imported by 0."


def test_module_catalog_summary_never_guesses_a_purpose():
    # The honest material is imported-by count and writes -- nothing that reads
    # like an assessment of what the module is *for*.
    idx = {"v": wiki_structure.INDEX_VERSION, "files": {
        "src/a.js": {"imports": [], "writes": ["positions"], "env": [], "emits": []},
    }}
    summary = wiki_structure.module_catalog_summary(idx, "src/a.js")
    for word in ("handles", "responsible", "manages", "is a", "provides"):
        assert word not in summary.lower()


# --------------------------------------------------------------------------- update_module_catalog

def _catalog(tmp_path, body: str) -> Path:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "index.md").write_text(body, encoding="utf-8")
    return project_dir


CATALOG_WITH_ENTITIES = (
    "# repo — Index\n\n"
    "## Analyses\n\nNone yet.\n\n"
    "## Entities\n\n"
    "- [wiki/entities/order-router.md](wiki/entities/order-router.md) - "
    "post-entry management, hand-written.\n\n"
    "## Concepts\n\nNone yet.\n\n"
    "## Sources\n\nNone yet.\n"
)

IDX_CATALOG = {"v": wiki_structure.INDEX_VERSION, "files": {
    "src/a.js": {"imports": [], "writes": ["t1"], "env": [], "emits": []},
}}


def test_update_module_catalog_does_nothing_when_nothing_was_created(tmp_path):
    project_dir = _catalog(tmp_path, CATALOG_WITH_ENTITIES)
    before = (project_dir / "index.md").read_text(encoding="utf-8")
    note = wiki_structure.update_module_catalog(IDX_CATALOG, project_dir, [])
    assert note is None
    assert (project_dir / "index.md").read_text(encoding="utf-8") == before


def test_update_module_catalog_warns_when_index_missing(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    note = wiki_structure.update_module_catalog(
        IDX_CATALOG, project_dir, [("wiki/entities/a.md", "src/a.js")])
    assert note is not None
    assert "catalog missing" in note
    assert not (project_dir / "index.md").exists()


def test_update_module_catalog_appends_a_line_without_touching_the_hand_written_one(tmp_path):
    project_dir = _catalog(tmp_path, CATALOG_WITH_ENTITIES)
    note = wiki_structure.update_module_catalog(
        IDX_CATALOG, project_dir, [("wiki/entities/a.md", "src/a.js")])
    assert note is None
    text = (project_dir / "index.md").read_text(encoding="utf-8")
    assert "[wiki/entities/order-router.md](wiki/entities/order-router.md) - " \
           "post-entry management, hand-written." in text          # untouched
    assert "[wiki/entities/a.md](wiki/entities/a.md) - imported by 0; writes `t1`." in text
    assert text.count("## Entities") == 1


def test_update_module_catalog_is_idempotent_on_a_second_call(tmp_path):
    project_dir = _catalog(tmp_path, CATALOG_WITH_ENTITIES)
    created = [("wiki/entities/a.md", "src/a.js")]
    wiki_structure.update_module_catalog(IDX_CATALOG, project_dir, created)
    once = (project_dir / "index.md").read_text(encoding="utf-8")

    note = wiki_structure.update_module_catalog(IDX_CATALOG, project_dir, created)

    assert note is None
    twice = (project_dir / "index.md").read_text(encoding="utf-8")
    assert once == twice
    assert twice.count("wiki/entities/a.md") == 2  # one link: appears in [text](target) once each


def test_update_module_catalog_checks_the_whole_file_not_just_the_entities_section(tmp_path):
    # A human filed the link under a different section already -- the tool
    # must not add a second line for the same page inside Entities.
    body = (
        "# repo — Index\n\n"
        "## Analyses\n\n"
        "- [wiki/entities/a.md](wiki/entities/a.md) - filed here by a human for now.\n\n"
        "## Entities\n\nNone yet.\n\n"
        "## Concepts\n\nNone yet.\n\n"
        "## Sources\n\nNone yet.\n"
    )
    project_dir = _catalog(tmp_path, body)
    note = wiki_structure.update_module_catalog(
        IDX_CATALOG, project_dir, [("wiki/entities/a.md", "src/a.js")])
    assert note is None
    text = (project_dir / "index.md").read_text(encoding="utf-8")
    assert text.count("wiki/entities/a.md") == 2  # still only the one link, under Analyses
    assert "None yet." in text                    # Entities section left alone


def test_update_module_catalog_creates_the_entities_section_before_concepts_when_missing(tmp_path):
    body = (
        "# repo — Index\n\n"
        "## Analyses\n\nNone yet.\n\n"
        "## Concepts\n\nNone yet.\n\n"
        "## Sources\n\nNone yet.\n"
    )
    project_dir = _catalog(tmp_path, body)
    note = wiki_structure.update_module_catalog(
        IDX_CATALOG, project_dir, [("wiki/entities/a.md", "src/a.js")])
    assert note is None
    text = (project_dir / "index.md").read_text(encoding="utf-8")
    assert text.index("## Entities") < text.index("## Concepts") < text.index("## Sources")
    assert "[wiki/entities/a.md](wiki/entities/a.md)" in text


def test_update_module_catalog_creates_the_entities_section_before_sources_when_concepts_is_also_missing(tmp_path):
    body = "# repo — Index\n\n## Analyses\n\nNone yet.\n\n## Sources\n\nNone yet.\n"
    project_dir = _catalog(tmp_path, body)
    note = wiki_structure.update_module_catalog(
        IDX_CATALOG, project_dir, [("wiki/entities/a.md", "src/a.js")])
    assert note is None
    text = (project_dir / "index.md").read_text(encoding="utf-8")
    assert text.index("## Entities") < text.index("## Sources")
    assert "[wiki/entities/a.md](wiki/entities/a.md)" in text


def test_update_module_catalog_refuses_when_there_is_no_anchor_at_all(tmp_path):
    # No ## Entities, no ## Concepts, no ## Sources -- the catalog does not
    # follow the known schema closely enough to guess where a new section
    # belongs. Nothing is written.
    body = "# repo — Index\n\n## Analyses\n\nNone yet.\n"
    project_dir = _catalog(tmp_path, body)
    before = (project_dir / "index.md").read_text(encoding="utf-8")
    note = wiki_structure.update_module_catalog(
        IDX_CATALOG, project_dir, [("wiki/entities/a.md", "src/a.js")])
    assert note is not None
    assert "no anchor" in note or "no ## Concepts" in note or "no ## Entities" in note
    assert (project_dir / "index.md").read_text(encoding="utf-8") == before
