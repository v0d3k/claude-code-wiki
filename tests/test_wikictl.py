"""wikictl.py's map/path/levers verbs are thin wrappers that shell out to
bin/wiki_structure.py. These tests pin the argv each one builds by faking
subprocess.run, so a dropped flag (like --limit) fails a test instead of only
showing up as a silent truncation on a 1000+ file repo."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wikictl  # noqa: E402


class _Result:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _fake_run(monkeypatch):
    calls = []

    def run(cmd):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(wikictl.subprocess, "run", run)
    return calls


def test_levers_cli_forwards_the_argparse_default_limit(monkeypatch):
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["wikictl.py", "levers", "signal_records"])
    assert wikictl.main() == 0
    cmd = calls[-1]
    assert "--limit" in cmd
    assert cmd[cmd.index("--limit") + 1] == "20"  # matches wiki_structure's own default


def test_levers_cli_forwards_a_custom_limit(monkeypatch):
    # On the real repo, signal_records has 56 writers -- with the
    # default limit that is only reachable if --limit actually gets through.
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr(sys, "argv",
                        ["wikictl.py", "levers", "signal_records", "--limit", "100"])
    assert wikictl.main() == 0
    cmd = calls[-1]
    assert cmd[cmd.index("--limit") + 1] == "100"


def test_map_rebuild_runs_a_full_build_before_the_map_call(monkeypatch):
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["wikictl.py", "map", "--rebuild", "--top", "5", "--repo", "R"])
    assert wikictl.main() == 0
    assert len(calls) == 2
    build_cmd, map_cmd = calls
    assert build_cmd[-3:] == ["build", "R", "--full"]
    assert map_cmd[-5:] == ["map", "--top", "5", "--repo", "R"]


def test_map_write_forwards_alongside_repo(monkeypatch):
    # Task 6 already made cmd_map forward --repo; adding --write must not
    # regress that combination (they are independent conditionals in
    # cmd_map, but only a test pins the combination itself).
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr(sys, "argv",
                        ["wikictl.py", "map", "--write", "--top", "3", "--repo", "R"])
    assert wikictl.main() == 0
    cmd = calls[-1]
    assert "--repo" in cmd and cmd[cmd.index("--repo") + 1] == "R"
    assert "--write" in cmd


def test_map_without_write_does_not_forward_the_flag(monkeypatch):
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["wikictl.py", "map", "--top", "5", "--repo", "R"])
    assert wikictl.main() == 0
    assert "--write" not in calls[-1]


def test_path_cli_forwards_source_and_target_positionally(monkeypatch):
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["wikictl.py", "path", "src\\a.js", "src\\b.js"])
    assert wikictl.main() == 0
    cmd = calls[-1]
    assert cmd[-2:] == ["src\\a.js", "src\\b.js"]


def test_levers_cli_forwards_no_tests(monkeypatch):
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["wikictl.py", "levers", "signal_records", "--no-tests"])
    assert wikictl.main() == 0
    assert "--no-tests" in calls[-1]


def test_levers_cli_without_no_tests_does_not_forward_the_flag(monkeypatch):
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["wikictl.py", "levers", "signal_records"])
    assert wikictl.main() == 0
    assert "--no-tests" not in calls[-1]


def test_map_cli_forwards_no_tests_alongside_repo(monkeypatch):
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr(sys, "argv",
                        ["wikictl.py", "map", "--no-tests", "--top", "5", "--repo", "R"])
    assert wikictl.main() == 0
    cmd = calls[-1]
    assert "--no-tests" in cmd
    assert "--repo" in cmd and cmd[cmd.index("--repo") + 1] == "R"


def test_map_cli_without_no_tests_does_not_forward_the_flag(monkeypatch):
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["wikictl.py", "map", "--top", "5", "--repo", "R"])
    assert wikictl.main() == 0
    assert "--no-tests" not in calls[-1]
