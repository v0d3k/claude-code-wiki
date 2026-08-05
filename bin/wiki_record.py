"""Wiki record hook for Claude Code sessions.

Triggered on: Stop. Reads the session transcript from a per-session cursor,
extracts what actually happened (intent, files touched, commands run, subagents),
and rewrites this session's block in `<repo>/.wiki-raw/YYYY-MM-DD.md`.

Deterministic and cheap: no LLM, no network. The scheduled ingest turns these
raw blocks into wiki pages (see LLM-Wiki/AGENTS.md).

Exit codes: always 0. A hook must never break Claude Code.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from wiki_paths import CONFIG_HOME, STATE_DIR, vault  # noqa: E402

VAULT = vault()
LOG_PATH = STATE_DIR / "wiki_record.log"
RAW_DIRNAME = ".wiki-raw"

# Working dirs that must never produce a raw journal.
HOME = Path.home()
IGNORE_ROOTS = [
    VAULT,
    CONFIG_HOME,
    HOME,
]

FILE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

MAX_PROMPTS = 25
MAX_FILES = 120
MAX_COMMANDS = 60
MAX_AGENTS = 20
PROMPT_CHARS = 400
COMMAND_CHARS = 220

SECRET_RE = re.compile(
    r"(?i)"
    r"((?:api[_-]?key|secret|passwd|password|token)\s*[=:]\s*)\S+"
    r"|(bearer\s+)[A-Za-z0-9._\-]{8,}"
    r"|(gh[pousr]_[A-Za-z0-9]{16,})"
    r"|(sk-[A-Za-z0-9]{16,})"
    r"|([a-z][a-z0-9+.\-]*://[^\s:/@]+):[^\s@]+@"
)

def _safe(text: str) -> str:
    """Make arbitrary captured text safe to embed in a journal block.

    Two jobs: keep a prompt or commit message from forging the block markers
    that the ingest cursor depends on, and keep obvious secrets out of a file
    that lives inside the repository.
    """
    if not text:
        return ""
    def _mask(m: "re.Match") -> str:
        if m.group(1):          # key = value
            return m.group(1) + "[redacted]"
        if m.group(2):          # bearer <token>
            return m.group(2) + "[redacted]"
        if m.group(5):          # scheme://user:password@host
            return m.group(5) + ":[redacted]@"
        return "[redacted]"     # bare token shapes

    s = SECRET_RE.sub(_mask, text)
    s = s.replace("<!--", "<!‑-").replace("-->", "--›").replace("wiki-raw:", "wiki‑raw:")
    s = "".join(ch for ch in s if ch >= " " or ch in "	")
    return s


MARK_BEGIN = "<!-- wiki-raw:begin id={id} kind={kind} status=unprocessed -->"
MARK_END = "<!-- wiki-raw:end id={id} -->"
BLOCK_RE = "<!-- wiki-raw:begin id={id} [^\n]*?-->.*?<!-- wiki-raw:end id={id} -->\n?"
# Block ids become part of a regex and of the queue cursor: keep them boring.
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")


def _log(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except OSError:
        pass


def _read_event() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _git(cwd: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    val = out.stdout.strip()
    return val or None


def resolve_repo(cwd: str) -> tuple[str, Path, str | None]:
    """Return (slug, raw_dir, branch).

    Worktrees resolve to the main repository via --git-common-dir, so
    `<repo>/.claude/worktrees/foo` records under the parent repo's slug.
    """
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    root = None
    if common:
        p = Path(common)
        root = p.parent if p.name == ".git" else p
    if root is None:
        top = _git(cwd, "rev-parse", "--show-toplevel")
        if top:
            root = Path(top)
    if root is not None:
        return root.name, root / RAW_DIRNAME, branch
    # Not a git repo: fall back to a vault-side inbox keyed by directory name.
    try:
        d = Path(cwd).resolve()
    except OSError:
        d = Path(cwd)
    return d.name, VAULT / "projects" / d.name / "raw-inbox", branch


def is_ignored(cwd: str) -> bool:
    try:
        c = Path(cwd).resolve()
    except OSError:
        return True
    for root in IGNORE_ROOTS:
        try:
            r = root.resolve()
        except OSError:
            continue
        if c == r:
            return True
        # Home itself is only ignored exactly; everything else ignores subtrees,
        # so editing the vault or your own Claude config never makes a journal.
        if r != HOME and _is_within(c, r):
            return True
    return False


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "")))
        return "\n".join(parts)
    return ""


# Harness-injected pseudo-prompts: not user intent, pure noise for the wiki.
NOISE_PREFIXES = (
    "<",
    "[",
    "Caveat:",
    "Stop hook feedback",
    "A session-scoped Stop hook",
    "This session is being continued",
    "Your task is to create a detailed summary",
    "API Error",
    "Base directory for this skill",
    "Skill /",
    "Launching skill",
    "Tool loaded.",
)


def _is_noise_prompt(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if t.startswith(NOISE_PREFIXES):
        return True
    # Skill bodies and other injected documents arrive as user turns.
    return t.startswith("#") and len(t) > 200


def scan_transcript(path: str, start_line: int, state: dict) -> int:
    """Merge new transcript lines into state. Returns the new cursor."""
    p = Path(path)
    if not p.exists():
        return start_line
    with p.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    for line in lines[start_line:]:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = e.get("type")
        sidechain = bool(e.get("isSidechain"))
        msg = e.get("message") or {}

        if etype == "user" and not sidechain:
            text = _text_of(msg.get("content", e.get("content", "")))
            if not _is_noise_prompt(text):
                clean = _safe(text[:PROMPT_CHARS].strip())
                # The same prompt re-appears on retries and continuations.
                if not state["prompts"] or state["prompts"][-1] != clean:
                    state["prompts"].append(clean)
            continue

        if etype != "assistant":
            continue

        if e.get("gitBranch"):
            state["branch"] = e["gitBranch"]

        for blk in msg.get("content", []) or []:
            if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                continue
            name = blk.get("name", "")
            inp = blk.get("input") or {}
            if not isinstance(inp, dict):
                continue

            if name in FILE_TOOLS:
                fp = inp.get("file_path") or inp.get("notebook_path") or ""
                if fp:
                    ops = state["files"].setdefault(str(fp), {})
                    ops[name] = ops.get(name, 0) + 1
            elif name in SHELL_TOOLS:
                cmd = str(inp.get("command", "")).strip().replace("\n", " ")
                if cmd:
                    state["commands"].append(_safe(cmd[:COMMAND_CHARS]))
            elif name == "Agent":
                desc = str(inp.get("description") or inp.get("subagent_type") or "").strip()
                if desc:
                    state["agents"].append(_safe(desc[:120]))

    return len(lines)


def _rel(path: str, root: str) -> str:
    """Relative to the repo root when the file is inside it, absolute otherwise.

    Files outside the repo (the vault, ~/.claude) stay absolute on purpose —
    that is exactly the signal that the session reached outside the project.
    """
    s = path.replace("\\", "/")
    r = (root or "").replace("\\", "/").rstrip("/")
    if r and s.lower().startswith(r.lower() + "/"):
        return s[len(r) + 1:]
    return s


def render_block(state: dict, sid: str, root: str) -> str:
    files = list(state["files"].items())[:MAX_FILES]
    cmds = []
    for c in state["commands"]:
        if c not in cmds:
            cmds.append(c)
    cmds = cmds[:MAX_COMMANDS]
    agents = state["agents"][-MAX_AGENTS:]
    prompts = state["prompts"][-MAX_PROMPTS:]

    head = (
        f"## [{state['updated']}] session {sid} | branch {state.get('branch') or '?'}"
        f" | files {len(state['files'])} | cmds {len(cmds)}"
    )
    out = [
        MARK_BEGIN.format(id=sid, kind="session"), "", head, "",
        f"Started {state['started']}. Cwd `{state['cwd']}`.", "",
    ]

    if prompts:
        out.append("**Intent**")
        out.append("")
        for t in prompts:
            flat = " ".join(t.split())
            out.append(f"- {flat}")
        out.append("")

    if files:
        out.append("**Files**")
        out.append("")
        for fp, ops in files:
            detail = ", ".join(f"{k} x{v}" for k, v in sorted(ops.items()))
            out.append(f"- `{_safe(_rel(fp, root))}` ({detail})")
        out.append("")

    if cmds:
        out.append("**Commands**")
        out.append("")
        for c in cmds:
            out.append(f"- `{c}`")
        out.append("")

    if agents:
        out.append("**Subagents**")
        out.append("")
        for a in agents:
            out.append(f"- {a}")
        out.append("")

    out.append(MARK_END.format(id=sid))
    out.append("")
    return "\n".join(out)


def write_block(raw_file: Path, sid: str, block: str) -> None:
    if not ID_RE.match(sid):
        _log(f"refusing block with unusable id {sid!r}")
        return
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    lock = raw_file.with_suffix(".lock")
    acquired = False
    for _ in range(40):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            # A crashed writer must not wedge recording forever.
            try:
                if time.time() - lock.stat().st_mtime > 60:
                    lock.unlink()
                    continue
            except OSError:
                pass
            time.sleep(0.05)
        except OSError as e:
            _log(f"lock error on {lock.name}: {e}")
            break
    if not acquired:
        # Skipping is safe: the session block is rebuilt from the cursor on the
        # next Stop event, so nothing is lost by not writing now.
        _log(f"could not lock {raw_file.name}, skipping this write")
        return
    try:
        existing = raw_file.read_text(encoding="utf-8") if raw_file.exists() else ""
        if not existing:
            day = raw_file.stem
            existing = (
                f"# .wiki-raw — {raw_file.parent.parent.name} — {day}\n\n"
                "Machine-written journal. Do not edit blocks by hand; the ingest "
                "flips `status=unprocessed` to `status=processed` in place. "
                "Schema: `LLM-Wiki/AGENTS.md`.\n\n"
            )
        pattern = re.compile(BLOCK_RE.format(id=re.escape(sid)), re.DOTALL)
        if pattern.search(existing):
            # Frozen once ingested: never touch a processed block.
            if "status=processed" in pattern.search(existing).group(0):
                return
            merged = pattern.sub(lambda _m: block, existing, count=1)
        else:
            merged = existing.rstrip("\n") + "\n\n" + block
        tmp = raw_file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(merged, encoding="utf-8")
        tmp.replace(raw_file)
    finally:
        if acquired:
            try:
                lock.unlink()
            except OSError:
                pass


def register_project(slug: str, raw_dir: Path, cwd: str) -> None:
    reg = VAULT / "projects.json"
    if not reg.parent.exists():
        return
    from wiki_paths import load_config
    if slug in set(load_config().get("exclude_repos", [])):
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        data = json.loads(reg.read_text(encoding="utf-8")) if reg.exists() else {}
    except (json.JSONDecodeError, OSError):
        return
    projects = data.setdefault("projects", {})
    entry = projects.get(slug)
    if entry is None:
        repo = str(raw_dir.parent) if raw_dir.name == RAW_DIRNAME else cwd
        projects[slug] = {
            "repo": repo.replace("\\", "/"),
            "raw_dir": str(raw_dir).replace("\\", "/"),
            "active": True,
            "first_seen": today,
            "last_seen": today,
        }
    elif entry.get("last_seen") == today:
        return
    else:
        entry["last_seen"] = today
    data.setdefault("version", 1)
    tmp = reg.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(reg)


AUTO_STATE = STATE_DIR / "auto-ingest.json"


def pending_blocks() -> int:
    """Cheap count of unprocessed blocks across registered projects."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from wiki_queue import collect, load_projects
        return len(collect(load_projects(None), None))
    except Exception:
        return 0


def maybe_rewake(session_id: str) -> str | None:
    """Decide whether to hand the queue to the model that is already running.

    Returning a message makes the hook exit 2, which Claude Code surfaces as a
    wake-up: the ingest then happens in this session, with this model, with no
    external process, no API token and no scheduler. Rate-limited so it never
    turns into a nag.
    """
    from wiki_paths import load_config
    cfg = load_config()
    mode = cfg.get("auto_ingest", "rewake")
    if mode != "rewake":
        return None

    now = time.time()
    try:
        state = json.loads(AUTO_STATE.read_text(encoding="utf-8")) if AUTO_STATE.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("last_session") == session_id:
        return None  # one nudge per session, whatever else happens
    cooldown = float(cfg.get("auto_ingest_cooldown_min", 60)) * 60
    if now - float(state.get("last_ts", 0)) < cooldown:
        return None

    pending = pending_blocks()
    if pending < int(cfg.get("auto_ingest_min_blocks", 3)):
        return None

    try:
        AUTO_STATE.parent.mkdir(parents=True, exist_ok=True)
        AUTO_STATE.write_text(json.dumps({"last_ts": now, "last_session": session_id}),
                              encoding="utf-8")
    except OSError:
        return None

    return (f"{pending} wiki blocks are waiting to be ingested. Do it now, with this session's "
            "model, by delegating to a subagent: spawn one general-purpose agent and tell it to "
            "follow the /wiki-ingest skill end to end -- read the queue, file anything durable as "
            "pages, mark every block it consumed, and report back in a short paragraph. "
            "Delegating keeps the queue out of your own context; the subagent inherits this "
            "session's model, so the pages are written by it and not by a local fallback. "
            "Do not shell out to `wikictl ingest` for this -- that path is for unattended runs and "
            "uses a separate process with its own credentials. "
            "Set auto_ingest to \"notify\" or \"off\" in wiki-config.json to stop this.")


def main() -> int:
    if os.environ.get("WIKI_RECORD_DISABLE") == "1":
        return 0
    try:
        event = _read_event()
        session_id = str(event.get("session_id") or "")
        cwd = str(event.get("cwd") or "")
        transcript_path = event.get("transcript_path")
        if not session_id or not cwd or not transcript_path:
            return 0
        if is_ignored(cwd):
            return 0

        slug, raw_dir, branch = resolve_repo(cwd)
        sid = session_id[:8]
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_path = STATE_DIR / f"{session_id}.json"

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                state = {}
        else:
            state = {}
        state.setdefault("cursor", 0)
        state.setdefault("prompts", [])
        state.setdefault("files", {})
        state.setdefault("commands", [])
        state.setdefault("agents", [])
        state.setdefault("started", now)
        state.setdefault("day", datetime.now().strftime("%Y-%m-%d"))
        state["cwd"] = cwd.replace("\\", "/")
        state["updated"] = now

        state["cursor"] = scan_transcript(transcript_path, int(state["cursor"]), state)
        if branch:  # live git wins over whatever the transcript recorded earlier
            state["branch"] = branch

        if not (state["prompts"] or state["files"] or state["commands"]):
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            return 0

        root = str(raw_dir.parent) if raw_dir.name == RAW_DIRNAME else cwd
        state["repo_root"] = root.replace("\\", "/")
        raw_file = raw_dir / f"{state['day']}.md"
        write_block(raw_file, sid, render_block(state, sid, root))
        register_project(slug, raw_dir, cwd)
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        _log(f"ok slug={slug} sid={sid} files={len(state['files'])} cmds={len(state['commands'])}")

        nudge = maybe_rewake(session_id)
        if nudge:
            print(nudge, flush=True)
            _log(f"rewake requested: {nudge[:60]}")
            return 2   # asyncRewake: hands the queue to the running model
        return 0
    except Exception as e:  # never break Claude Code
        _log(f"fail {type(e).__name__}: {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
