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


IDX_WM = {"v": 1, "files": {
    "src/a.js": {"imports": [], "writes": ["t1"], "env": [], "emits": []},
    "src/b.js": {"imports": ["src/a.js"], "writes": [], "env": [], "emits": []},
}}


def test_write_module_pages_creates_stub_pages_for_new_modules(tmp_path):
    # fan_in only ranks files that something else imports -- src/b.js is the
    # importer here, not an import target, so it has zero fan-in and is not
    # itself a candidate for a page (matching what `map`'s own "most
    # depended on" ranking already means).
    written, warnings = wiki_structure.write_module_pages(IDX_WM, tmp_path, top=10)
    assert warnings == []
    assert written == ["wiki/entities/a.md"]
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
    written, warnings = wiki_structure.write_module_pages(idx_v2, tmp_path, top=10)

    final = page.read_text(encoding="utf-8")
    assert warnings == []
    assert "wiki/entities/a.md" in written
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
    written, warnings = wiki_structure.write_module_pages(idx, tmp_path, top=10)
    assert warnings == []
    assert set(written) == {"wiki/entities/index.md", "wiki/entities/src-index.md"}
    a_page = (tmp_path / "wiki" / "entities" / "index.md").read_text(encoding="utf-8")
    b_page = (tmp_path / "wiki" / "entities" / "src-index.md").read_text(encoding="utf-8")
    assert "**Path:** `packages/a/src/index.js`" in a_page
    assert "**Path:** `packages/b/src/index.js`" in b_page


def test_write_module_pages_refuses_and_preserves_a_page_with_malformed_markers(tmp_path):
    out_dir = tmp_path / "wiki" / "entities"
    out_dir.mkdir(parents=True)
    broken = "# a\n\nHand notes.\n\n" + wiki_structure.STRUCT_BEGIN + "\nhalf-written, crashed here\n"
    (out_dir / "a.md").write_text(broken, encoding="utf-8")

    written, warnings = wiki_structure.write_module_pages(IDX_WM, tmp_path, top=10)

    assert "wiki/entities/a.md" not in written
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


def test_cmd_map_write_creates_pages_under_the_isolated_vault(tmp_path, monkeypatch):
    # isolated_home (autouse, see conftest.py) already points wiki_paths at a
    # scratch config+vault under this test's own tmp_path -- this exercises
    # the real vault()-resolution path in cmd_map without touching the real
    # Obsidian vault.
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
