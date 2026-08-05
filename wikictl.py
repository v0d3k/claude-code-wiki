"""wikictl - install, inspect, feed and remove claude-code-wiki.

    python wikictl.py status
    python wikictl.py doctor
    python wikictl.py install   [--vault PATH] [--root PATH]... [--replace-roots] [--no-schedule]
    python wikictl.py uninstall [--purge] [--purge-vault --confirm NAME] [--dry-run]
    python wikictl.py ingest    [--engine auto|claude|fds|ollama] [--limit N] [--dry-run]
    python wikictl.py lint      [--engine ...]
    python wikictl.py backfill  --project SLUG|all [--source git|sessions|both]
                                [--since YYYY-MM-DD] [--max N] [--dry-run]
    python wikictl.py add PATH | remove SLUG
    python wikictl.py search QUERY [--limit N]

Everything is idempotent: install twice changes nothing the second time, and
uninstall keeps your notes unless you explicitly ask to lose them.

Full reference: docs/CLI.md
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BIN = Path(__file__).resolve().parent / "bin"
TEMPLATES = Path(__file__).resolve().parent / "templates"
sys.path.insert(0, str(BIN))

from wiki_paths import (  # noqa: E402
    CONFIG_HOME, CONFIG_PATH, DEFAULTS, OWNER, RUN_LOG, STATE_DIR,
    load_config, registry, roots, vault, write_config,
)

SETTINGS = CONFIG_HOME / "settings.json"
TASK_INGEST = "LLM-Wiki Ingest"
TASK_LINT = "LLM-Wiki Lint"

# event, script, sync/async. Sync hooks can inject context; async ones cannot.
HOOK_SPECS = [
    ("SessionStart", "wiki_bootstrap.py", True, 20),
    ("SessionStart", "wiki_context.py", False, 10),
    ("Stop", "wiki_record.py", True, 15),
]
OWNER_FLAG = f"--owner={OWNER}"   # written into every command we install
OUR_SCRIPTS = {"wiki_bootstrap.py", "wiki_context.py", "wiki_record.py"}


def out(msg: str = "") -> None:
    print(msg, flush=True)


def ps(script: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def git(repo: Path, *args: str, timeout: int = 60) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


# --------------------------------------------------------------------------- settings.json

def read_settings() -> dict:
    if not SETTINGS.exists():
        return {}
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"settings.json is not valid JSON ({e}); fix it before installing")


def write_settings(data: dict) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = SETTINGS.with_suffix(f".json.{stamp}.bak")
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, backup)
    tmp = SETTINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SETTINGS)


def script_of(command: str) -> str:
    """The .py path inside a hook command, whatever the quoting."""
    for token in shlex.split(command, posix=False):
        clean = token.strip('"').strip("'")
        if clean.lower().endswith(".py"):
            return clean
    return ""


def is_ours(command: str) -> bool:
    """Ownership is explicit. A path substring would claim other people's hooks
    and would miss ours whenever the package is cloned under another name."""
    c = command.replace("\\", "/")
    if OWNER_FLAG in c:
        return True
    # Pre-1.0 installs had no owner flag: match the exact script names we ship.
    return any(c.endswith(s) or f"/{s} " in c or c.endswith(f"/{s}") for s in OUR_SCRIPTS)


def strip_hooks(data: dict) -> int:
    """Remove every hook entry this system owns. Returns how many were removed."""
    removed = 0
    hooks = data.get("hooks", {})
    for event in list(hooks):
        groups = hooks[event]
        for group in list(groups):
            keep = [h for h in group.get("hooks", []) if not is_ours(str(h.get("command", "")))]
            removed += len(group.get("hooks", [])) - len(keep)
            group["hooks"] = keep
        hooks[event] = [g for g in groups if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]
    return removed


def install_hooks(data: dict) -> list[str]:
    strip_hooks(data)
    hooks = data.setdefault("hooks", {})
    added = []
    for event, script, is_async, timeout in HOOK_SPECS:
        entry = {"type": "command",
                 "command": f'"{sys.executable}" "{(BIN / script).as_posix()}" {OWNER_FLAG}',
                 "timeout": timeout}
        if is_async:
            entry["async"] = True
        groups = hooks.setdefault(event, [])
        target = next((g for g in groups if "matcher" not in g), None)
        if target is None:
            target = {"hooks": []}
            groups.append(target)
        target.setdefault("hooks", []).append(entry)
        added.append(f"{event}:{script}")
    return added


# --------------------------------------------------------------------------- vault

def scaffold_vault(v: Path) -> list[str]:
    created = []
    for rel in ("projects/_template/wiki/analyses", "projects/_template/wiki/entities",
                "projects/_template/wiki/concepts", "projects/_template/wiki/sources",
                "wiki/analyses", "wiki/entities", "wiki/concepts", "wiki/sources", "wiki/automation",
                "raw"):
        (v / rel).mkdir(parents=True, exist_ok=True)
    for src in TEMPLATES.rglob("*.md"):
        dst = v / src.relative_to(TEMPLATES)
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        created.append(str(dst.relative_to(v)).replace("\\", "/"))
    reg = registry()
    if not reg.exists():
        reg.write_text(json.dumps({
            "version": 1,
            "comment": "Registry written by the wiki hooks. slug -> repo.",
            "projects": {},
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        created.append("projects.json")
    return created


# --------------------------------------------------------------------------- schedule

WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


def schedule_install(cfg: dict) -> str:
    s = cfg["schedule"]
    if s.get("lint_weekday") not in WEEKDAYS:
        return f"failed: schedule.lint_weekday must be one of {sorted(WEEKDAYS)}"
    hh, mm = s["ingest_at"].split(":")
    lh, lm = s["lint_at"].split(":")
    runner = (BIN / "wiki-ingest-run.ps1").as_posix().replace("/", "\\").replace("'", "''")
    script = f"""
$act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File {runner}'
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date -Hour {int(hh)} -Minute {int(mm)} -Second 0) -RepetitionInterval (New-TimeSpan -Hours {int(s['ingest_every_hours'])})
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$prn = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{TASK_INGEST}' -Action $act -Trigger $trg -Settings $set -Principal $prn -Force | Out-Null
$actL = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File {runner} -Mode lint'
$trgL = New-ScheduledTaskTrigger -Weekly -DaysOfWeek {s['lint_weekday']} -At (Get-Date -Hour {int(lh)} -Minute {int(lm)} -Second 0)
Register-ScheduledTask -TaskName '{TASK_LINT}' -Action $actL -Trigger $trgL -Settings $set -Principal $prn -Force | Out-Null
'OK'
"""
    code, text = ps(script)
    return "registered" if code == 0 and "OK" in text else f"failed: {text.strip()[:200]}"


def schedule_remove() -> str:
    code, text = ps(f"Unregister-ScheduledTask -TaskName '{TASK_INGEST}','{TASK_LINT}' "
                    f"-Confirm:$false -ErrorAction SilentlyContinue; 'OK'")
    return "removed" if "OK" in text else f"failed: {text.strip()[:200]}"


def schedule_state() -> str:
    code, text = ps(f"Get-ScheduledTask -TaskName '{TASK_INGEST}' -ErrorAction SilentlyContinue | "
                    f"ForEach-Object {{ $i=$_|Get-ScheduledTaskInfo; \"$($_.State) next=$($i.NextRunTime)\" }}")
    return text.strip() or "not registered"


# --------------------------------------------------------------------------- commands

def cmd_install(args) -> int:
    cfg = load_config()
    if args.vault:
        cfg["vault"] = str(Path(args.vault)).replace("\\", "/")
    if args.root:
        new = [str(Path(r)).replace("\\", "/") for r in args.root]
        cfg["roots"] = new if args.replace_roots else sorted(set(cfg.get("roots", []) + new))
    if not cfg["roots"]:
        out("note       no repository roots configured; pass --root PATH or use `add PATH` per repo")
    write_config(cfg)
    v = Path(cfg["vault"])

    out(f"vault      {v}")
    created = scaffold_vault(v)
    out(f"  scaffold  {len(created)} file(s) created" if created else "  scaffold  already present")

    data = read_settings()
    added = install_hooks(data)
    write_settings(data)
    out(f"hooks      {', '.join(added)}")
    out(f"  backup    {SETTINGS.parent}/settings.json.<timestamp>.bak")

    from wiki_install_git_hooks import find_repos, install as install_hook
    from wiki_record import RAW_DIRNAME, register_project
    repos = find_repos(roots())
    states = defaultdict(int)
    for repo in repos:
        states[install_hook(repo, False, False)] += 1
        register_project(repo.name, repo / RAW_DIRNAME, str(repo))
    out(f"git hooks  {len(repos)} repo(s): " + ", ".join(f"{k} x{v}" for k, v in states.items()))

    if args.no_schedule:
        out("schedule   skipped (--no-schedule)")
    else:
        out(f"schedule   {schedule_install(cfg)}")

    out("")
    out("Installed. New sessions record automatically; the next scheduled run ingests.")
    out("Nothing is retroactive -- run `backfill` to seed history.")
    return 0


def cmd_uninstall(args) -> int:
    v = vault()
    if args.dry_run:
        out("dry run -- nothing will be changed")

    data = read_settings()
    n = strip_hooks(data)
    if not args.dry_run:
        write_settings(data)
    out(f"hooks      {n} entry(ies) removed from settings.json")

    from wiki_install_git_hooks import find_repos, install as install_hook
    repos = find_repos(roots())
    states = defaultdict(int)
    for repo in repos:
        states[install_hook(repo, args.dry_run, True)] += 1
    out(f"git hooks  {len(repos)} repo(s): " + ", ".join(f"{k} x{val}" for k, val in states.items()))

    out(f"schedule   {'would remove' if args.dry_run else schedule_remove()}")

    if args.purge:
        raw_dirs = [repo / ".wiki-raw" for repo in repos if (repo / ".wiki-raw").is_dir()]
        for d in raw_dirs:
            out(f"  purge     {d}")
            if not args.dry_run:
                shutil.rmtree(d, ignore_errors=True)
        for f in (STATE_DIR,):
            out(f"  purge     {f}")
            if not args.dry_run:
                shutil.rmtree(f, ignore_errors=True)
    else:
        out("raw+state  kept (pass --purge to delete .wiki-raw journals and hook state)")

    if args.purge_vault:
        looks_like_ours = (v / "AGENTS.md").exists() and (v / "projects.json").exists()
        if not looks_like_ours:
            out(f"REFUSED   {v} does not look like a wiki vault "
                "(no AGENTS.md + projects.json). Delete it yourself if you meant to.")
        elif args.confirm != v.name:
            out(f"REFUSED   deleting the vault needs --confirm {v.name}")
        else:
            strangers = sorted(x.name for x in v.iterdir()
                               if x.name not in {"AGENTS.md", "index.md", "log.md",
                                                 "projects.json", "projects", "wiki", "raw"})
            if strangers:
                out(f"REFUSED   {v} holds files this system never created: "
                    f"{', '.join(strangers[:8])}")
            else:
                out(f"  purge     {v}")
                if not args.dry_run:
                    shutil.rmtree(v)
    else:
        out(f"vault      kept at {v}")
    return 0


def project_rows() -> list[dict]:
    from wiki_queue import collect, load_projects
    projects = load_projects(None)
    pending = defaultdict(int)
    for row in collect(projects, None):
        pending[row["project"]] += 1
    rows = []
    v = vault()
    for slug, meta in sorted(projects.items()):
        pdir = v / "projects" / slug
        pages = len(list(pdir.rglob("*.md"))) - 2 if pdir.exists() else 0
        rows.append({"slug": slug, "pages": max(0, pages), "pending": pending.get(slug, 0),
                     "repo": meta.get("repo", "")})
    return rows


def cmd_status(args) -> int:
    cfg = load_config()
    v = Path(cfg["vault"])
    data = read_settings()
    installed = [f"{e}:{Path(script_of(str(h.get('command', '')))).name or '?'}"
                 for e, gs in data.get("hooks", {}).items()
                 for g in gs for h in g.get("hooks", []) if is_ours(str(h.get("command", "")))]

    out(f"vault      {v}  {'ok' if v.exists() else 'MISSING'}")
    out(f"config     {CONFIG_PATH if CONFIG_PATH.exists() else '(defaults)'}")
    out(f"hooks      {', '.join(installed) if installed else 'NOT INSTALLED'}")
    out(f"schedule   {schedule_state()}")

    sys.path.insert(0, str(BIN))
    from wiki_ingest import endpoint_alive, have_claude_token
    engines = []
    for name in cfg["engines"]:
        if name == "claude":
            engines.append(f"claude={'ready' if have_claude_token() else 'no token'}")
        else:
            url = (cfg.get(name) or {}).get("url")
            if not url:
                engines.append(f"{name}=misconfigured (no url)")
            else:
                engines.append(f"{name}={'up' if endpoint_alive(url, 2.0) else 'down'}")
    out(f"engines    {', '.join(engines)}")

    rows = project_rows()
    out("")
    out(f"{'project':22} {'pages':>6} {'queued':>7}  repo")
    for r in rows:
        out(f"{r['slug'][:22]:22} {r['pages']:>6} {r['pending']:>7}  {r['repo']}")
    out("")
    out(f"{len(rows)} project(s), {sum(r['pages'] for r in rows)} page(s), "
        f"{sum(r['pending'] for r in rows)} block(s) waiting to be ingested")
    if RUN_LOG.exists():
        tail = RUN_LOG.read_text(encoding="utf-8", errors="replace").rstrip().split("\n")[-1]
        out(f"last run   {tail}")
    return 0


def cmd_doctor(args) -> int:
    problems = []
    cfg = load_config()
    v = Path(cfg["vault"])

    if not v.exists():
        problems.append(f"vault missing: {v} -- run `install`")
    else:
        for must in ("AGENTS.md", "index.md", "projects.json"):
            if not (v / must).exists():
                problems.append(f"vault incomplete: {must} missing -- run `install`")

    data = {}
    try:
        data = read_settings()
    except SystemExit as e:
        problems.append(str(e))
    ours = [h for e, gs in data.get("hooks", {}).items() for g in gs
            for h in g.get("hooks", []) if is_ours(str(h.get("command", "")))]
    if len(ours) != len(HOOK_SPECS):
        problems.append(f"expected {len(HOOK_SPECS)} hooks, found {len(ours)} -- run `install`")
    for h in ours:
        path = script_of(str(h.get("command", "")))
        if path and not Path(path).exists():
            problems.append(f"hook points at a missing file: {path} -- run `install`")

    from wiki_install_git_hooks import MARKER, find_repos, hooks_dir
    for repo in find_repos(roots()):
        hd = hooks_dir(repo)
        hook = hd / "post-commit" if hd else None
        if hook is None or not hook.exists() or MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
            problems.append(f"{repo.name}: post-commit hook not installed -- run `install`")

    stale = 0
    from wiki_queue import collect, load_projects
    today = datetime.now()
    for row in collect(load_projects(None), None):
        if row["kind"] == "history":
            continue  # backfilled days are old by definition, not stale
        m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(row["file"]).stem)
        if m and (today - datetime.strptime(m.group(1), "%Y-%m-%d")).days > 7:
            stale += 1
    if stale:
        problems.append(f"{stale} block(s) unprocessed for more than 7 days -- run `ingest`")

    if "not registered" in schedule_state():
        problems.append("scheduled task missing -- run `install` (or `install --no-schedule` deliberately)")

    if problems:
        for p in problems:
            out(f"FAIL  {p}")
        out(f"\n{len(problems)} problem(s)")
        return 1
    out("all checks passed")
    return 0


def cmd_ingest(args) -> int:
    cmd = [sys.executable, str(BIN / "wiki_ingest.py"), "--engine", args.engine,
           "--mode", getattr(args, "mode", "ingest")]
    if args.limit is not None:
        cmd += ["--limit", str(args.limit)]
    if args.dry_run:
        cmd.append("--dry-run")
    return subprocess.run(cmd).returncode


def cmd_add(args) -> int:
    from wiki_install_git_hooks import install as install_hook
    from wiki_record import RAW_DIRNAME, register_project
    repo = Path(args.path).resolve()
    if not repo.is_dir():
        out(f"not a directory: {repo}")
        return 1
    out(f"git hook   {install_hook(repo, False, False)}")
    register_project(repo.name, repo / RAW_DIRNAME, str(repo))
    out(f"registered {repo.name} -> {repo}")
    return 0


def cmd_remove(args) -> int:
    reg = registry()
    data = json.loads(reg.read_text(encoding="utf-8"))
    meta = data.get("projects", {}).pop(args.slug, None)
    if meta is None:
        out(f"not registered: {args.slug}")
        return 1
    reg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from wiki_install_git_hooks import install as install_hook
    out(f"git hook   {install_hook(Path(meta['repo']), False, True)}")
    out(f"unregistered {args.slug} (pages under projects/{args.slug}/ kept)")
    return 0


def cmd_search(args) -> int:
    v = vault()
    needle = args.query.lower()
    hits = 0
    for f in sorted(v.rglob("*.md")):
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
                if needle in line.lower():
                    out(f"{f.relative_to(v).as_posix()}:{i}: {line.strip()[:160]}")
                    hits += 1
                    if hits >= args.limit:
                        out(f"... stopped at {args.limit} hits")
                        return 0
        except OSError:
            continue
    out(f"{hits} hit(s)")
    return 0


# --------------------------------------------------------------------------- backfill

def backfill_git(repo: Path, slug: str, since: str, max_blocks: int, dry: bool) -> int:
    """One block per calendar day of commits, oldest first. Idempotent by block id."""
    sys.path.insert(0, str(BIN))
    from wiki_record import MARK_BEGIN, MARK_END, write_block

    fmt = "@@%h|%ad|%an|%s"
    raw = git(repo, "log", f"--since={since}", "--date=short", "--numstat", f"--pretty=format:{fmt}",
              "--no-merges", timeout=180)
    if not raw.strip():
        out(f"  {slug}: no commits since {since}")
        return 0

    days: dict[str, list[dict]] = defaultdict(list)
    cur = None
    for line in raw.split("\n"):
        if line.startswith("@@"):
            sha, date, author, subject = (line[2:].split("|", 3) + ["", "", ""])[:4]
            cur = {"sha": sha, "date": date, "author": author, "subject": subject, "files": []}
            days[date].append(cur)
        elif line.strip() and cur is not None:
            parts = line.split("\t")
            if len(parts) == 3:
                cur["files"].append((parts[2], parts[0], parts[1]))

    written = 0
    for date in sorted(days):
        if written >= max_blocks:
            out(f"  {slug}: stopped at --max {max_blocks}")
            break
        commits = days[date]
        bid = f"bf{date.replace('-', '')}"
        body = [
            MARK_BEGIN.format(id=bid, kind="history"), "",
            f"## [{date}] history {bid} | {len(commits)} commit(s) | backfilled from git", "",
        ]
        touched: dict[str, int] = defaultdict(int)
        for c in commits:
            body.append(f"- `{c['sha']}` {c['subject']} ({c['author']})")
            for path, add, dele in c["files"]:
                try:
                    touched[path] += int(add) + int(dele)
                except ValueError:
                    touched[path] += 0
        if touched:
            body += ["", "**Files (by churn)**", ""]
            for path, churn in sorted(touched.items(), key=lambda kv: -kv[1])[:40]:
                body.append(f"- `{path}` ({churn} lines)")
        body += ["", MARK_END.format(id=bid), ""]

        target = repo / ".wiki-raw" / f"{date}.md"
        if target.exists() and f"id={bid} " in target.read_text(encoding="utf-8", errors="replace"):
            continue
        out(f"  {slug} {date}: {len(commits)} commit(s), {len(touched)} file(s)"
            + (" [dry]" if dry else ""))
        if not dry:
            write_block(target, bid, "\n".join(body))
        written += 1
    return written


def backfill_sessions(repo: Path, slug: str, since: str, max_blocks: int, dry: bool) -> int:
    """Replay past Claude transcripts whose cwd resolves to this repo."""
    sys.path.insert(0, str(BIN))
    from wiki_record import render_block, resolve_repo, scan_transcript, write_block

    proj_root = Path.home() / ".claude" / "projects"
    if not proj_root.is_dir():
        return 0
    since_dt = datetime.strptime(since, "%Y-%m-%d")
    written = 0
    for jsonl in sorted(proj_root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime):
        if written >= max_blocks:
            break
        if datetime.fromtimestamp(jsonl.stat().st_mtime) < since_dt:
            continue
        state = {"cursor": 0, "prompts": [], "files": {}, "commands": [], "agents": [],
                 "started": "", "cwd": "", "updated": ""}
        try:
            first = json.loads(jsonl.read_text(encoding="utf-8", errors="replace").split("\n")[0] or "{}")
        except (OSError, json.JSONDecodeError):
            continue
        cwd = str(first.get("cwd") or "")
        if not cwd:
            continue
        try:
            other_slug, _raw_dir, _branch = resolve_repo(cwd)
        except Exception:
            continue
        if other_slug != slug:
            continue

        ts = str(first.get("timestamp") or "")[:19] or datetime.fromtimestamp(jsonl.stat().st_mtime).isoformat()[:19]
        state["started"] = state["updated"] = ts.replace("T", "T") + "Z" if len(ts) == 19 else ts
        state["cwd"] = cwd.replace("\\", "/")
        scan_transcript(str(jsonl), 0, state)
        if not (state["files"] or state["commands"]):
            continue
        sid = jsonl.stem[:8]
        day = ts[:10] or datetime.now().strftime("%Y-%m-%d")
        target = repo / ".wiki-raw" / f"{day}.md"
        if target.exists() and f"id={sid} " in target.read_text(encoding="utf-8", errors="replace"):
            continue
        out(f"  {slug} session {sid} ({day}): {len(state['files'])} file(s), "
            f"{len(state['commands'])} cmd(s)" + (" [dry]" if dry else ""))
        if not dry:
            write_block(target, sid, render_block(state, sid, str(repo)))
        written += 1
    return written


def cmd_backfill(args) -> int:
    from wiki_queue import load_projects
    projects = load_projects(None if args.project == "all" else args.project)
    if not projects:
        out(f"no such registered project: {args.project}")
        return 1
    total = 0
    for slug, meta in projects.items():
        repo = Path(meta["repo"])
        if not (repo / ".git").exists():
            continue
        out(f"{slug}:")
        if args.source in ("git", "both"):
            total += backfill_git(repo, slug, args.since, args.max, args.dry_run)
        if args.source in ("sessions", "both"):
            total += backfill_sessions(repo, slug, args.since, args.max, args.dry_run)
    out("")
    out(f"{total} block(s) {'would be ' if args.dry_run else ''}queued. "
        f"Run `wikictl.py ingest` to turn them into pages.")
    return 0


# --------------------------------------------------------------------------- entry

def main() -> int:
    ap = argparse.ArgumentParser(prog="wikictl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install")
    p.add_argument("--vault")
    p.add_argument("--root", action="append", help="repo root; repeatable, added to existing roots")
    p.add_argument("--replace-roots", action="store_true", help="replace configured roots instead of adding")
    p.add_argument("--no-schedule", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall")
    p.add_argument("--purge", action="store_true", help="also delete .wiki-raw journals and hook state")
    p.add_argument("--purge-vault", action="store_true", help="also delete the vault itself")
    p.add_argument("--confirm", help="vault directory name, required by --purge-vault")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    p = sub.add_parser("ingest")
    p.add_argument("--engine", default="auto", choices=["auto", "claude", "fds", "ollama"])
    p.add_argument("--limit", type=int, help="stop after N blocks")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ingest, mode="ingest")

    # Lint is a claude-engine-only pass; exposed so the weekly task is reachable by hand.
    p = sub.add_parser("lint")
    p.add_argument("--engine", default="auto", choices=["auto", "claude", "fds", "ollama"])
    p.set_defaults(func=cmd_ingest, mode="lint", limit=None, dry_run=False)

    p = sub.add_parser("backfill")
    p.add_argument("--project", required=True)
    p.add_argument("--source", default="git", choices=["git", "sessions", "both"])
    p.add_argument("--since", default="2026-01-01")
    p.add_argument("--max", type=int, default=60)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("add")
    p.add_argument("path")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove")
    p.add_argument("slug")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_search)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
