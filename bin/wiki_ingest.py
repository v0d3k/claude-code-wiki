"""Autonomous ingest dispatcher for the LLM Wiki.

Picks the best available engine and turns unprocessed `.wiki-raw` blocks into
wiki pages. No user interaction, no credentials required beyond what is already
running on this machine.

Engine order (first reachable wins):
  1. claude   -- `claude -p /wiki-ingest`, needs CLAUDE_CODE_OAUTH_TOKEN or the
                 DPAPI token file. Best quality; used when available.
  2. fds      -- FreeDeepSeek gateway on 127.0.0.1:9655 (deepseek-chat).
  3. ollama   -- local Ollama on 127.0.0.1:11434.

Engines 2 and 3 run the deterministic loop in this file: the model only returns
a small JSON verdict, and Python does every file write, path check, index update
and status flip. A hallucinating model can produce a bad page; it cannot write
outside the vault or lose a raw block.

Usage:
  python wiki_ingest.py [--engine auto|claude|fds|ollama] [--limit N] [--dry-run]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_queue import VAULT, collect, iter_blocks, load_projects  # noqa: E402

from wiki_paths import RUN_LOG, STATE_DIR, load_config as _load_config  # noqa: E402


SECTIONS = {"analyses", "concepts", "entities", "sources"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")

SYSTEM_PROMPT = """You maintain a per-project engineering wiki.

You receive one raw journal block: what a coding session or a commit actually did.
Decide whether it contains anything worth keeping for months, then answer with ONE
JSON object and nothing else.

Keep a block only if it records at least one of:
  - a decision and the reason behind it
  - a diagnosis or root cause
  - a measured result with its sample size
  - an architecture or interface change
  - a constraint discovered the hard way

Skip (verdict "skip") anything that is routine work: restarts, reruns, formatting,
exploration that concluded nothing, dependency bumps, or a commit whose subject
already says everything. Most blocks are skips. Skipping is the correct, expected
answer -- never invent significance to justify a page.

JSON shape:
{"verdict":"skip","reason":"<one short sentence>"}
or
{"verdict":"write","section":"analyses|concepts|entities|sources",
 "slug":"kebab-case-file-name-without-extension",
 "title":"Short human title",
 "summary":"One sentence for the project index.",
 "markdown":"Full page body in markdown, English."}

Rules for the page body:
  - open with a one-line summary, then "## Status", then the substance
  - state only what the block supports; never invent numbers, dates or file names
  - do not speculate about causes, consequences, or mechanisms the block does not
    state. If a detail would be a guess, leave it out or mark it "unverified"
  - reference code as `path/to/file.js:line`, never paste code
  - no front matter, no H1 heading (the title is added for you)
"""


def log(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with RUN_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass
    print(msg, flush=True)


def load_config() -> dict:
    return _load_config()


# --------------------------------------------------------------------------- engines

def have_claude_token() -> bool:
    """True only when this process can actually authenticate.

    The DPAPI file deliberately does not count: only wiki-ingest-run.ps1 can
    decrypt it, and it exports CLAUDE_CODE_OAUTH_TOKEN before calling us. A
    direct `wikictl ingest` must not pick the claude engine on its strength.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"):
        return True
    from wiki_paths import CONFIG_HOME
    return (CONFIG_HOME / ".credentials.json").exists()


def endpoint_alive(url: str, timeout: float = 3.0) -> bool:
    base = url.split("/v1/")[0]
    for probe in (f"{base}/v1/models", base):
        try:
            with urllib.request.urlopen(probe, timeout=timeout) as r:
                if r.status < 500:
                    return True
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return True
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False


def pick_engine(cfg: dict, want: str) -> str | None:
    order = [want] if want != "auto" else cfg["engines"]
    for name in order:
        if name == "claude" and have_claude_token():
            return "claude"
        if name in ("fds", "ollama"):
            conf = cfg.get(name) or {}
            if conf.get("url") and endpoint_alive(conf["url"]):
                return name
    return None


def _call(conf: dict, messages: list, temperature: float) -> str | None:
    payload = {"model": conf["model"], "stream": False, "temperature": temperature, "messages": messages}
    req = urllib.request.Request(
        conf["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
        log(f"  engine error: {e}")
        return None
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log("  engine returned no message")
        return None


def chat(cfg: dict, engine: str, block: str, context: str) -> dict | None:
    conf = cfg[engine]
    user = f"{context}\n\n--- RAW BLOCK ---\n{block[:cfg['block_chars']]}"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    text = _call(conf, messages, 0.2)
    verdict = parse_verdict(text) if text else None
    if verdict is not None:
        return verdict
    if text is None:
        return None
    # One repair attempt: local models drift out of the JSON contract often
    # enough that a retry is cheaper than losing the block.
    time.sleep(cfg["inter_call_ms"] / 1000.0)
    messages += [
        {"role": "assistant", "content": text[:2000]},
        {"role": "user", "content": "That was not a single valid JSON object. "
                                    "Reply again with ONLY the JSON object, no prose, no code fence."},
    ]
    text = _call(conf, messages, 0.0)
    return parse_verdict(text) if text else None


def parse_verdict(text: str) -> dict | None:
    """Models wrap JSON in prose, fences, or <think> blocks. Dig it out."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    start = t.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            c = t[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        # strict=False tolerates the raw newlines models leave
                        # inside markdown string values.
                        return json.JSONDecoder(strict=False).decode(t[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = t.find("{", start + 1)
    return None


# --------------------------------------------------------------------------- writing

def project_dir(slug: str) -> Path:
    return VAULT / "projects" / slug


def ensure_project(slug: str, repo: str) -> Path:
    pdir = project_dir(slug)
    if pdir.exists():
        return pdir
    template = VAULT / "projects" / "_template"
    for sub in ("analyses", "concepts", "entities", "sources"):
        (pdir / "wiki" / sub).mkdir(parents=True, exist_ok=True)
    for name in ("index.md", "log.md"):
        src = template / name
        text = src.read_text(encoding="utf-8") if src.exists() else f"# {slug}\n"
        (pdir / name).write_text(
            text.replace("<PROJECT>", slug).replace("<REPO_PATH>", repo), encoding="utf-8"
        )
    master = VAULT / "index.md"
    try:
        t = master.read_text(encoding="utf-8")
        row = f"| {slug} | [index](projects/{slug}/index.md) | [log](projects/{slug}/log.md) | `{repo}` |\n"
        if f"projects/{slug}/index.md" not in t:
            t = t.replace("\nNew projects are registered", row + "\nNew projects are registered", 1)
            master.write_text(t, encoding="utf-8")
    except OSError as e:
        log(f"  warn: master index not updated ({e})")
    log(f"  scaffolded project {slug}")
    return pdir


def one_line(value, limit: int = 200) -> str:
    """Model output lands in index.md and log.md, which are re-injected into
    every future session. Never let it carry newlines or control characters."""
    return " ".join(str(value or "").split())[:limit]


# Windows refuses these as file names whatever the extension.
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10))}


def normalize(verdict: dict) -> dict:
    verdict["section"] = str(verdict.get("section", "")).strip().lower()
    verdict["slug"] = str(verdict.get("slug", "")).strip().lower()
    verdict["title"] = one_line(verdict.get("title"), 120)
    verdict["summary"] = one_line(verdict.get("summary"), 200)
    verdict["reason"] = one_line(verdict.get("reason"), 200)
    return verdict


def write_page(slug: str, repo: str, verdict: dict, block_id: str) -> Path | None:
    section = verdict["section"]
    page = verdict["slug"]
    title = verdict["title"]
    body = str(verdict.get("markdown", "")).strip()
    if (section not in SECTIONS or not SLUG_RE.match(page) or page in RESERVED
            or not title or len(body) < 80):
        log(f"  rejected verdict: section={section!r} slug={page!r} body={len(body)}c")
        return None

    pdir = ensure_project(slug, repo)
    target = pdir / "wiki" / section / f"{page}.md"
    # Belt and braces: whatever the model returned, the write stays in the vault.
    try:
        target.resolve().relative_to(VAULT.resolve())
    except ValueError:
        log(f"  refused write outside the vault: {target}")
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    header = f"# {title}\n\n"
    footer = f"\n\n## Sources\n\n- `.wiki-raw` block `{block_id}`, ingested {stamp}.\n"
    if target.exists():
        prev = target.read_text(encoding="utf-8").rstrip()
        target.write_text(f"{prev}\n\n---\n\n## Update {stamp}\n\n{body}{footer}", encoding="utf-8")
    else:
        target.write_text(header + body + footer, encoding="utf-8")
    return target


def update_index(slug: str, section: str, page: str, summary: str) -> None:
    idx = project_dir(slug) / "index.md"
    if not idx.exists():
        return
    link = f"wiki/{section}/{page}.md"
    text = idx.read_text(encoding="utf-8")
    if link in text:
        return
    line = f"- [{link}]({link}) - {summary}"
    heading = f"## {section.capitalize()}"
    lines = text.split("\n")
    if heading not in lines:
        lines += ["", heading, "", line, ""]
        idx.write_text("\n".join(lines), encoding="utf-8")
        return
    start = lines.index(heading)
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    # Keep the link inside its own section, and drop the template placeholder.
    body = [ln for ln in lines[start + 1:end] if ln.strip() != "None yet."]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    lines[start:end] = [heading, ""] + body + [line, ""]
    idx.write_text("\n".join(lines), encoding="utf-8")


def append_log(slug: str, written: list[str], skipped: list[tuple[str, str]], engine: str) -> None:
    logp = project_dir(slug) / "log.md"
    if not logp.exists():
        return
    stamp = datetime.now().strftime("%Y-%m-%d")
    lines = [f"\n## [{stamp}] ingest | {len(written)} page(s), {len(skipped)} skipped ({engine})\n"]
    for w in written:
        lines.append(f"- Wrote `{w}`.")
    for bid, reason in skipped:
        lines.append(f"- Skipped block `{bid}`: {reason}")
    with logp.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def mark_processed(file_path: str, block_id: str) -> bool:
    p = Path(file_path)
    text = p.read_text(encoding="utf-8", errors="replace")
    today = datetime.now().strftime("%Y-%m-%d")
    pattern = re.compile(
        r"(<!--\s*wiki-raw:begin\s+id=" + re.escape(block_id) + r"\s+kind=\S+\s+status=)unprocessed"
    )
    text, n = pattern.subn(rf"\1processed date={today}", text, count=1)
    if n:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)
    return bool(n)


def block_body(file_path: str, block_id: str) -> str | None:
    text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    for m, body, _ in iter_blocks(text):
        if m.group("id") == block_id:
            return body
    return None


def existing_pages(slug: str) -> str:
    pdir = project_dir(slug)
    if not pdir.exists():
        return "(new project, no pages yet)"
    names = []
    for sub in sorted(SECTIONS):
        for f in sorted((pdir / "wiki" / sub).glob("*.md")):
            names.append(f"{sub}/{f.stem}")
    return ", ".join(names) if names else "(no pages yet)"


# --------------------------------------------------------------------------- runs

def run_claude(cfg: dict, mode: str) -> int:
    prompt = "/wiki-ingest lint" if mode == "lint" else "/wiki-ingest"
    cfg_all = _load_config()
    # Deliberately no bypassPermissions and no Bash: this run is unattended and
    # its input is attacker-controllable text from commit messages.
    args = ["claude.cmd" if os.name == "nt" else "claude", "-p", prompt,
            "--allowedTools", "Read,Glob,Grep,Edit,Write",
            "--add-dir", str(VAULT)]
    for root in cfg_all.get("roots", [])[:1]:
        args += ["--add-dir", str(root)]
    try:
        res = subprocess.run(args, cwd=str(VAULT), capture_output=True, text=True,
                             timeout=cfg["claude"]["timeout_min"] * 60)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"claude engine failed to start: {e}")
        return 1
    out = (res.stdout or "").strip()
    if re.search(r"Not logged in|Invalid API key|authentication", out, re.I):
        log("claude engine unauthenticated -- falling back")
        return 2
    log(f"claude engine done exit={res.returncode} :: {out[-300:]}")
    return res.returncode


def run_local(cfg: dict, engine: str, limit: int | None, dry: bool) -> int:
    projects = load_projects(None)
    rows = collect(projects, limit or cfg["max_blocks_per_run"])
    if not rows:
        log("queue empty")
        return 0

    per_project: dict[str, dict] = {}
    for row in rows:
        slug = row["project"]
        repo = projects.get(slug, {}).get("repo", "")
        body = block_body(row["file"], row["id"])
        if body is None:
            log(f"  block {row['id']} unreadable, left unprocessed")
            continue

        context = (
            f"Project: {slug}\nRepository: {repo}\n"
            f"Existing pages: {existing_pages(slug)}\n"
            "If this block extends one of those pages, reuse its exact slug and section."
        )
        verdict = chat(cfg, engine, body, context)
        time.sleep(cfg["inter_call_ms"] / 1000.0)  # FDS is only stable serialized

        acc = per_project.setdefault(slug, {"written": [], "skipped": [], "marks": []})
        if verdict is None:
            log(f"  {row['id']}: no usable verdict, left unprocessed")
            continue

        verdict = normalize(verdict)
        if verdict.get("verdict") == "write":
            if dry:
                log(f"  {row['id']}: WOULD WRITE {verdict.get('section')}/{verdict.get('slug')}")
                continue
            try:
                page = write_page(slug, repo, verdict, row["id"])
            except OSError as e:
                log(f"  {row['id']}: write failed ({e}), left unprocessed")
                continue
            if page is None:
                log(f"  {row['id']}: invalid page verdict, left unprocessed")
                continue
            update_index(slug, verdict["section"], verdict["slug"],
                         verdict["summary"] or verdict["title"])
            acc["written"].append(f"wiki/{verdict['section']}/{verdict['slug']}.md")
            acc["marks"].append((row["file"], row["id"]))
            log(f"  {row['id']}: wrote {verdict['section']}/{verdict['slug']}.md")
        else:
            reason = verdict["reason"] or "no durable content"
            if dry:
                log(f"  {row['id']}: WOULD SKIP -- {reason}")
                continue
            acc["skipped"].append((row["id"], reason))
            acc["marks"].append((row["file"], row["id"]))
            log(f"  {row['id']}: skip -- {reason}")

    if dry:
        return 0
    for slug, acc in per_project.items():
        if acc["written"] or acc["skipped"]:
            append_log(slug, acc["written"], acc["skipped"], engine)
        for file_path, bid in acc["marks"]:
            if not mark_processed(file_path, bid):
                log(f"  warn: could not mark {bid} processed")
    return 0


def sweep_repos() -> None:
    """Wire any repo that appeared since the last run. Idempotent, ~1s."""
    try:
        from wiki_install_git_hooks import DEFAULT_ROOTS, find_repos, install
        from wiki_record import RAW_DIRNAME, register_project
        for repo in find_repos(DEFAULT_ROOTS):
            result = install(repo, False, False)
            if result not in ("already installed", "skip (not a git repo)"):
                log(f"sweep: {repo.name} post-commit {result}")
            register_project(repo.name, repo / RAW_DIRNAME, str(repo))
    except Exception as e:
        log(f"sweep failed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="auto", choices=["auto", "claude", "fds", "ollama"])
    ap.add_argument("--mode", default="ingest", choices=["ingest", "lint"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    sweep_repos()
    if args.mode == "ingest" and not collect(load_projects(None), None):
        log("skip: queue empty")
        return 0

    engine = pick_engine(cfg, args.engine)
    if engine is None:
        log("abort: no engine available (no claude token, :9655 and :11434 both down)")
        return 3

    log(f"start: engine={engine} mode={args.mode}")
    if engine == "claude":
        code = run_claude(cfg, args.mode)
        if code != 2:
            return code
        # Token turned out to be missing or stale: continue down the chain.
        engine = pick_engine(cfg, "fds") or pick_engine(cfg, "ollama")
        if engine is None:
            return 3
        log(f"fallback engine={engine}")
    if args.mode == "lint":
        log("lint is only implemented on the claude engine; nothing to do")
        return 0
    return run_local(cfg, engine, args.limit, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
