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
