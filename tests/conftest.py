"""Fixtures. Every test runs against a throwaway config home, vault and repo.

The package writes to ~/.claude and to an Obsidian vault by default. A test that
forgets to redirect those would edit the developer's real notes, so the
redirection lives in an autouse fixture rather than in individual tests.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point config home and vault at tmp_path, and reset the config cache."""
    config_home = tmp_path / "config"
    vault = tmp_path / "vault"
    (config_home / "wiki-state").mkdir(parents=True)
    (vault / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_home))

    import wiki_paths
    monkeypatch.setattr(wiki_paths, "CONFIG_HOME", config_home)
    monkeypatch.setattr(wiki_paths, "STATE_DIR", config_home / "wiki-state")
    monkeypatch.setattr(wiki_paths, "CONFIG_PATH", config_home / "wiki-state" / "wiki-config.json")
    monkeypatch.setattr(wiki_paths, "RUN_LOG", config_home / "wiki-state" / "ingest-runs.log")
    wiki_paths._cache = None
    (config_home / "wiki-state" / "wiki-config.json").write_text(
        json.dumps({"vault": str(vault).replace("\\", "/"), "roots": []}), encoding="utf-8")

    # wiki_record binds its own copies of these at import time (module-level
    # constants, imported by value), so patching wiki_paths alone does not
    # reach them -- without this, its _log() calls and VAULT-based ignore
    # checks still hit the real ~/.claude install.
    import wiki_record
    monkeypatch.setattr(wiki_record, "STATE_DIR", config_home / "wiki-state")
    monkeypatch.setattr(wiki_record, "LOG_PATH", config_home / "wiki-state" / "wiki_record.log")
    monkeypatch.setattr(wiki_record, "CONFIG_HOME", config_home)
    monkeypatch.setattr(wiki_record, "VAULT", vault)
    monkeypatch.setattr(wiki_record, "AUTO_STATE", config_home / "wiki-state" / "auto-ingest.json")

    # wiki_structure binds INDEX_DIR = STATE_DIR / "structure" at import time,
    # same reason as above: patching wiki_paths.STATE_DIR alone does not move
    # it, and without this every `build()` in a test would write into the
    # developer's real C:\Users\user\.claude\wiki-state\structure\.
    import wiki_structure
    monkeypatch.setattr(wiki_structure, "INDEX_DIR", config_home / "wiki-state" / "structure")

    # wiki_context.py imports VAULT by value too (`from wiki_record import
    # VAULT`), same reason as above -- without this, SessionStart's main()
    # would look for the project catalog under the developer's real vault
    # instead of this test's, and silently no-op (return 0, no stdout).
    import wiki_context
    monkeypatch.setattr(wiki_context, "VAULT", vault)

    yield {"config_home": config_home, "vault": vault}
    wiki_paths._cache = None


@pytest.fixture
def git_repo(tmp_path):
    """A real git repository, because repo_root walks for .git."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo
