"""The guard must say three different things, and must stay silent otherwise."""
import wiki_guard


IDX = {
    "defs": {
        "num": [{"loc": "src/a.js:3", "h": "hnum"}, {"loc": "src/b.js:7", "h": "hother"}],
        "esc": [{"loc": "src/c.js:5", "h": "hesc"}],
        "solo": [{"loc": "src/e.js:2", "h": "hsolo"}],
    },
    "dup_bodies": {"hesc": ["esc@src/c.js:5", "escHtml@src/d.js:9"]},
}


def _check(monkeypatch, source, exclude="src/new.js"):
    monkeypatch.setattr(wiki_guard, "load_index", lambda root: IDX)
    return wiki_guard.check(source, ".js", None, exclude)


def test_identical_body_is_reported_as_importable(monkeypatch):
    monkeypatch.setattr(wiki_guard, "declared", lambda t, s: [("num", "hnum")])
    out = _check(monkeypatch, "irrelevant")
    assert len(out) == 1 and "identical body already here" in out[0]


def test_same_body_under_a_new_name_is_reported(monkeypatch):
    monkeypatch.setattr(wiki_guard, "declared", lambda t, s: [("renderSafe", "hesc")])
    out = _check(monkeypatch, "irrelevant")
    assert len(out) == 1 and "already exists" in out[0] and "esc" in out[0]


def test_taken_name_with_other_behaviour_is_reported(monkeypatch):
    monkeypatch.setattr(wiki_guard, "declared", lambda t, s: [("num", "hbrandnew")])
    out = _check(monkeypatch, "irrelevant")
    assert len(out) == 1 and "name taken" in out[0]


def test_new_name_and_new_body_is_silent(monkeypatch):
    monkeypatch.setattr(wiki_guard, "declared", lambda t, s: [("brandNew", "hbrandnew")])
    assert _check(monkeypatch, "irrelevant") == []


def test_the_file_being_written_never_reports_itself(monkeypatch):
    """solo has no other name or body match anywhere in the index; writing that
    same file back (unchanged) must stay silent."""
    monkeypatch.setattr(wiki_guard, "declared", lambda t, s: [("solo", "hsolo")])
    assert _check(monkeypatch, "irrelevant", exclude="src/e.js") == []
