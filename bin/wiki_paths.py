"""Shared paths and configuration for claude-code-wiki.

Every module reads its locations from here, so the whole system can be pointed
at a different vault or set of repository roots by editing one JSON file:

    <config home>/wiki-state/wiki-config.json

Config home is $CLAUDE_CONFIG_DIR when set, else ~/.claude. Missing keys fall
back to DEFAULTS, so a partial config is valid.
"""
import json
import os
import sys
from pathlib import Path

if sys.version_info < (3, 10):  # union type hints are used throughout
    raise SystemExit("claude-code-wiki needs Python 3.10 or newer "
                     f"(running {sys.version.split()[0]})")

BIN_DIR = Path(__file__).resolve().parent
PKG_DIR = BIN_DIR.parent
CONFIG_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
STATE_DIR = CONFIG_HOME / "wiki-state"
CONFIG_PATH = STATE_DIR / "wiki-config.json"
LEGACY_CONFIG = STATE_DIR / "ingest-config.json"
RUN_LOG = STATE_DIR / "ingest-runs.log"

OWNER = "claude-code-wiki"  # marks the hook entries this package owns

DEFAULTS = {
    # Where curated pages live. Point it inside an Obsidian vault if you use one.
    "vault": str(Path.home() / "llm-wiki").replace("\\", "/"),
    # Directories that contain your repositories. Empty means "only the repos you
    # add explicitly" -- `install` fills this from --root.
    "roots": [],
    "exclude": ["node_modules", "vendor"],
    # How the queue gets turned into pages.
    #   "rewake" -- the model you are already working with does it, in session
    #   "notify" -- only mention the pending count at session start
    #   "off"    -- never nudge; use `wikictl ingest` or a scheduled run
    "auto_ingest": "rewake",
    "auto_ingest_min_blocks": 3,      # do not interrupt for a single trivial block
    "auto_ingest_cooldown_min": 60,   # at most one nudge per hour
    # Engines for unattended runs only (scheduled or CI). In-session ingest uses
    # whatever model you are already running and needs none of this.
    "engines": ["claude", "ollama"],
    "claude": {"timeout_min": 30},
    "ollama": {"url": "http://127.0.0.1:11434/v1/chat/completions", "model": "llama3.1"},
    "inter_call_ms": 1000,
    "block_chars": 6000,
    "max_blocks_per_run": 20,
    # Keep the journal out of `git status` by adding it to .git/info/exclude
    # (local, never committed, invisible to collaborators).
    "git_exclude_raw": True,
    # Install the post-commit hook automatically for repos under `roots`.
    "auto_install_git_hooks": True,
    "schedule": {"ingest_every_hours": 6, "ingest_at": "04:07",
                 "lint_weekday": "Sunday", "lint_at": "05:07"},
}

_cache: dict | None = None


def load_config(refresh: bool = False) -> dict:
    global _cache
    if _cache is not None and not refresh:
        return _cache
    cfg = json.loads(json.dumps(DEFAULTS))
    for path in (LEGACY_CONFIG, CONFIG_PATH):  # legacy first, real config wins
        if not path.exists():
            continue
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    _cache = cfg
    return cfg


def vault() -> Path:
    return Path(load_config()["vault"])


def registry() -> Path:
    return vault() / "projects.json"


def roots() -> list[Path]:
    return [Path(r) for r in load_config()["roots"]]


def write_config(cfg: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    load_config(refresh=True)
