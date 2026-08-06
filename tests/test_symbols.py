"""The slug rule already regressed once: an ASCII-only pattern silently dropped
every repository with a non-Latin name."""
import wiki_queue
import wiki_symbols


def test_slug_ok_accepts_any_alphabet():
    assert wiki_queue.slug_ok("example-repo")
    assert wiki_queue.slug_ok("Генеалогия")
    assert wiki_queue.slug_ok("プロジェクト")


def test_slug_ok_rejects_path_tricks():
    for bad in ("..", ".", "a/b", "a\\b", "c:evil", "-leading", ".hidden", ""):
        assert not wiki_queue.slug_ok(bad), bad


def test_declared_finds_functions_consts_and_classes():
    src = """
function alpha(a) { return a + 1; }
const beta = (b) => { return b * 2; };
class Gamma {}
"""
    names = {n for n, _ in wiki_symbols.declared(src, ".js")}
    assert {"alpha", "beta", "Gamma"} <= names


def test_body_hash_ignores_comments_and_whitespace():
    a = wiki_symbols.body_hash("{ const n = Number(v); return Number.isFinite(n) ? n : null; }", False)
    b = wiki_symbols.body_hash(
        "{\n  // parse\n  const n = Number(v);\n  return Number.isFinite(n) ? n : null;\n}", False)
    assert a is not None and a == b


def test_body_hash_skips_boilerplate():
    assert wiki_symbols.body_hash("{ return null; }", False) is None


def test_classify_separates_the_three_kinds():
    idx = {
        "defs": {
            "same": [{"loc": "a.js:1", "h": "h1"}, {"loc": "b.js:1", "h": "h1"}],
            "drift": [{"loc": "a.js:9", "h": "h2"}, {"loc": "b.js:9", "h": "h3"}],
        },
        "dup_bodies": {"h1": ["same@a.js:1", "same@b.js:1"],
                       "h4": ["one@a.js:20", "other@b.js:20"]},
    }
    k = wiki_symbols.classify(idx)
    assert "same" in k["identical"]
    assert "drift" in k["diverged"]
    assert "h4" in k["renamed"] and "h1" not in k["renamed"]
