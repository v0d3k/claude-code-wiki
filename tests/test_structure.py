"""Levers are the coupling an import graph cannot see: two functions that write
the same table contend even though neither imports the other."""
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
