"""structure_orientation() and its wiring into SessionStart's main()."""
import json

import wiki_context
import wiki_paths
import wiki_structure


def _seed_index(repo):
    """A tiny repo whose fan-in and contended-lever facts are easy to assert on."""
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "a.js").write_text(
        "require('./db');\ndb.exec('INSERT INTO positions (id) VALUES (1)');\n", encoding="utf-8")
    (repo / "src" / "b.js").write_text(
        "require('./db');\ndb.exec('UPDATE positions SET qty = 1');\n", encoding="utf-8")
    (repo / "src" / "db.js").write_text("module.exports = {};\n", encoding="utf-8")
    return wiki_structure.build(repo, changed=None)


def test_structure_orientation_is_empty_when_no_index_exists_yet(tmp_path):
    # A fresh install, or a repo that has never been committed to since Task 7
    # shipped: load_index() must come back falsy, not raise, and the caller
    # must turn that into silence rather than an empty-but-present section.
    assert wiki_context.structure_orientation(tmp_path) == ""


def test_structure_orientation_disabled_returns_empty(tmp_path):
    repo = tmp_path / "repo"
    _seed_index(repo)
    wiki_paths.write_config({**wiki_paths.load_config(), "orient_enabled": False})

    assert wiki_context.structure_orientation(repo) == ""


def test_structure_orientation_lists_fan_in_and_contended_levers(tmp_path):
    repo = tmp_path / "repo"
    _seed_index(repo)

    text = wiki_context.structure_orientation(repo)

    assert "## Structure (regenerated on every commit)" in text
    assert "Most depended on:" in text
    assert "src/db.js" in text  # imported by both a.js and b.js
    assert "Shared state with more than one writer:" in text
    assert "positions" in text  # written by both a.js and b.js
    assert "written from 2 file(s)" in text
    assert "wikictl map" in text and "wikictl levers" in text and "wikictl path" in text


def test_structure_orientation_respects_orient_modules_and_orient_levers_limits(tmp_path):
    repo = tmp_path / "repo"
    _seed_index(repo)
    wiki_paths.write_config({**wiki_paths.load_config(), "orient_modules": 0, "orient_levers": 0})

    text = wiki_context.structure_orientation(repo)

    # The pointer footer is unconditional, but with both limits at zero there
    # is nothing to rank, so neither ranked section should appear.
    assert "Most depended on:" not in text
    assert "Shared state with more than one writer:" not in text


def test_main_injects_the_structure_section_when_catalog_and_index_both_exist(tmp_path, monkeypatch, isolated_home, git_repo):
    repo = git_repo
    _seed_index(repo)

    vault = isolated_home["vault"]
    pdir = vault / "projects" / repo.name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "index.md").write_text("# catalog\n", encoding="utf-8")

    monkeypatch.setattr(wiki_context, "_read_event", lambda: {"cwd": str(repo)})
    monkeypatch.chdir(repo)

    import io
    import sys
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert wiki_context.main() == 0

    payload = json.loads(out.getvalue())
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "## Structure (regenerated on every commit)" in ctx
    assert ctx.rstrip().endswith("dynamic requires and ORM calls are invisible to it.")


def test_main_omits_the_structure_section_when_orient_disabled(tmp_path, monkeypatch, isolated_home, git_repo):
    repo = git_repo
    _seed_index(repo)
    wiki_paths.write_config({**wiki_paths.load_config(), "orient_enabled": False})

    vault = isolated_home["vault"]
    pdir = vault / "projects" / repo.name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "index.md").write_text("# catalog\n", encoding="utf-8")

    monkeypatch.setattr(wiki_context, "_read_event", lambda: {"cwd": str(repo)})
    monkeypatch.chdir(repo)

    import io
    import sys
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert wiki_context.main() == 0

    payload = json.loads(out.getvalue())
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "## Structure" not in ctx
