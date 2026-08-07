"""The guard must say three different things, and must stay silent otherwise."""
import json

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


def test_variant_count_ignores_the_file_being_written(monkeypatch):
    """The count beside the file list must describe the same set as the list."""
    monkeypatch.setattr(wiki_guard, "declared", lambda t, s: [("num", "hbrandnew")])
    out = _check(monkeypatch, "irrelevant", exclude="src/a.js")
    assert len(out) == 1
    assert "1 file(s)" in out[0]
    assert "2 different implementations" not in out[0]


# --------------------------------------------------------------------------- structure_check (contended tables)

SUMMARY = {"signal_records": [56, 17], "positions": [22, 6], "runs": [1, 1]}


def test_structure_check_warns_about_a_table_over_the_threshold():
    out = wiki_guard.structure_check(
        "db.exec('INSERT INTO signal_records (id) VALUES (1)')", ".js",
        SUMMARY, "src/new.js", old_text="")
    assert len(out) == 1
    assert "signal_records" in out[0]
    assert "56 file(s)" in out[0]
    assert "17 outside tests" in out[0]


def test_structure_check_is_silent_under_the_default_threshold():
    # "runs" has only 1 writer total -- below the default guard_min_lever_writers of 2.
    out = wiki_guard.structure_check(
        "db.exec('INSERT INTO runs (id) VALUES (1)')", ".js",
        SUMMARY, "src/new.js", old_text="")
    assert out == []


def test_structure_check_is_silent_for_a_table_the_summary_has_never_seen():
    out = wiki_guard.structure_check(
        "db.exec('INSERT INTO brand_new_table (id) VALUES (1)')", ".js",
        SUMMARY, "src/new.js", old_text="")
    assert out == []


def test_structure_check_is_silent_when_there_is_no_summary_at_all():
    # Missing or malformed summary -> load_levers_summary() already returns {}
    # upstream; this pins that an empty summary means silence, not a KeyError.
    out = wiki_guard.structure_check(
        "db.exec('INSERT INTO signal_records (id) VALUES (1)')", ".js",
        {}, "src/new.js", old_text="")
    assert out == []


def test_structure_check_is_silent_for_a_suffix_the_structure_index_never_scans():
    # .md was never walked into the structure index (iter_source() only
    # visits JS/TS/PY suffixes) -- running the same extractor on it would
    # produce a warning the index cannot actually back up.
    out = wiki_guard.structure_check(
        "```sql\nINSERT INTO signal_records (id) VALUES (1)\n```", ".md",
        SUMMARY, "docs/example.md", old_text="")
    assert out == []


def test_structure_check_does_not_subtract_when_the_file_previously_wrote_nothing():
    # Brand-new file (or an existing file that is only now adding this write) --
    # the summary count already excludes it, nothing to subtract.
    out = wiki_guard.structure_check(
        "db.exec('UPDATE positions SET qty = 1')", ".js",
        SUMMARY, "src/new.js", old_text="")
    assert "22 file(s)" in out[0]
    assert "6 outside tests" in out[0]


def test_structure_check_subtracts_the_files_own_prior_contribution():
    # This file already wrote positions before the edit -- the summary's 22
    # already counts it once, so the message must read 21, and the non-test
    # count must drop too since this file is not itself a test.
    out = wiki_guard.structure_check(
        "db.exec('UPDATE positions SET qty = 1')", ".js",
        SUMMARY, "src/existing.js",
        old_text="db.exec('UPDATE positions SET qty = 1')")
    assert "21 file(s)" in out[0]
    assert "5 outside tests" in out[0]


def test_structure_check_subtraction_leaves_the_non_test_count_alone_when_self_is_a_test():
    out = wiki_guard.structure_check(
        "db.exec('UPDATE positions SET qty = 1')", ".js",
        SUMMARY, "test/existing.test.js",
        old_text="db.exec('UPDATE positions SET qty = 1')")
    assert "21 file(s)" in out[0]
    assert "6 outside tests" in out[0]  # self was already one of the 16 test writers


def test_structure_check_subtraction_can_drop_a_table_below_the_threshold():
    # 2 total writers, both already counted; subtracting this file's own
    # prior contribution leaves 1 -- below the default threshold of 2, so the
    # table must disappear from the report entirely, not just show a lower count.
    summary = {"outcomes": [2, 2]}
    out = wiki_guard.structure_check(
        "db.exec('UPDATE outcomes SET x = 1')", ".js",
        summary, "src/existing.js",
        old_text="db.exec('UPDATE outcomes SET x = 1')")
    assert out == []


def test_structure_check_respects_the_configured_threshold(monkeypatch):
    monkeypatch.setattr(wiki_guard, "load_config", lambda: {"guard_min_lever_writers": 5})
    summary = {"outcomes": [4, 4]}
    out = wiki_guard.structure_check(
        "db.exec('UPDATE outcomes SET x = 1')", ".js", summary, "src/new.js", old_text="")
    assert out == []


# --------------------------------------------------------------------------- main() end to end

def _run_main(monkeypatch, event):
    import io
    import sys
    monkeypatch.setattr(wiki_guard, "_read_event", lambda: event)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = wiki_guard.main()
    return rc, out.getvalue()


def _seed_contended_repo(repo):
    """Two non-test files and one test file all write `positions` -- a real
    structure index and its levers summary, built the same way the
    post-commit hook builds them."""
    import wiki_structure
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "test").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "a.js").write_text(
        "db.exec(\"INSERT INTO positions (id) VALUES (1)\");\n", encoding="utf-8")
    (repo / "src" / "b.js").write_text(
        "db.exec(\"UPDATE positions SET qty = 1\");\n", encoding="utf-8")
    (repo / "test" / "c.test.js").write_text(
        "db.exec(\"UPDATE positions SET qty = 1\");\n", encoding="utf-8")
    return wiki_structure.build(repo, changed=None)


def test_main_warns_about_a_contended_table_when_writing_a_new_file(monkeypatch, git_repo):
    repo = git_repo
    _seed_contended_repo(repo)
    monkeypatch.chdir(repo)

    event = {"hook_event_name": "PreToolUse", "tool_name": "Write",
             "tool_input": {"file_path": str(repo / "src" / "new.js"),
                             "content": "db.exec(\"UPDATE positions SET qty = 2\");\n"}}
    rc, out = _run_main(monkeypatch, event)

    assert rc == 0
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "Contended tables written by `src/new.js`" in ctx
    assert "`positions` — 3 file(s) already write this table (2 outside tests)" in ctx
    assert "wikictl levers positions" in ctx


def test_main_does_not_double_count_an_existing_writer_rewritten_via_write(monkeypatch, git_repo):
    # src/a.js is already one of the 3 writers the index/summary just recorded
    # -- rewriting it with the same statement must report 2, not 3.
    repo = git_repo
    _seed_contended_repo(repo)
    monkeypatch.chdir(repo)

    event = {"hook_event_name": "PreToolUse", "tool_name": "Write",
             "tool_input": {"file_path": str(repo / "src" / "a.js"),
                             "content": "db.exec(\"INSERT INTO positions (id) VALUES (1)\");\n"
                                        "// unrelated comment\n"}}
    rc, out = _run_main(monkeypatch, event)

    assert rc == 0
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "`positions` — 2 file(s) already write this table (1 outside tests)" in ctx


def test_main_is_silent_when_the_levers_summary_does_not_exist(monkeypatch, git_repo):
    # No build() has ever run for this repo -- load_levers_summary() must come
    # back {}, and the guard must neither crash nor print a lever section.
    repo = git_repo
    (repo / "src").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(repo)

    event = {"hook_event_name": "PreToolUse", "tool_name": "Write",
             "tool_input": {"file_path": str(repo / "src" / "new.js"),
                             "content": "db.exec(\"UPDATE positions SET qty = 2\");\n"}}
    rc, out = _run_main(monkeypatch, event)

    assert rc == 0
    assert out == ""  # nothing to report at all -- no duplicate, no contended lever


def test_main_is_silent_when_the_levers_summary_file_is_malformed(monkeypatch, git_repo):
    import wiki_structure
    repo = git_repo
    idx = _seed_contended_repo(repo)
    wiki_structure.levers_summary_path(repo).write_text("{not json", encoding="utf-8")
    monkeypatch.chdir(repo)

    event = {"hook_event_name": "PreToolUse", "tool_name": "Write",
             "tool_input": {"file_path": str(repo / "src" / "new.js"),
                             "content": "db.exec(\"UPDATE positions SET qty = 2\");\n"}}
    rc, out = _run_main(monkeypatch, event)

    assert rc == 0
    assert out == ""


def test_main_never_raises_when_the_target_path_cannot_be_read_as_text(monkeypatch, git_repo):
    # file_path pointing at a directory: Path.read_text() raises IsADirectoryError
    # (a subclass of OSError) -- old_text must fall back to "" rather than
    # propagate and take the whole guard down with it.
    repo = git_repo
    _seed_contended_repo(repo)
    monkeypatch.chdir(repo)
    a_dir = repo / "src" / "a.js"  # exists as a *file* from _seed_contended_repo;
    # shadow it with a directory of the same name to force the read to fail.
    a_dir.unlink()
    a_dir.mkdir()

    event = {"hook_event_name": "PreToolUse", "tool_name": "Write",
             "tool_input": {"file_path": str(a_dir),
                             "content": "db.exec(\"UPDATE positions SET qty = 2\");\n"}}
    rc, out = _run_main(monkeypatch, event)

    assert rc == 0  # must not raise
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    # old_text came back "" (read failed) -- no subtraction, full count shown.
    assert "`positions` — 3 file(s) already write this table (2 outside tests)" in ctx
