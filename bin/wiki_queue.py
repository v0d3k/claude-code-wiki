"""Queue helper for the LLM-Wiki ingest.

  python wiki_queue.py list [--json] [--project SLUG] [--limit N]
  python wiki_queue.py show --file F --id ID
  python wiki_queue.py mark --file F --id ID [--id ID ...]
  python wiki_queue.py stats

`list` reports every `status=unprocessed` block across the registered projects.
`mark` flips those blocks to `status=processed date=<today>` in place, which is
the only ingest cursor. Blocks are never deleted or rewritten otherwise.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_paths import registry as _registry, vault as _vault  # noqa: E402

VAULT = _vault()
REGISTRY = _registry()

# The id charset is deliberately narrow: a forged id from a commit message must
# not be able to enter the queue or reach a regex built from it.
BEGIN_RE = re.compile(
    r"<!--\s*wiki-raw:begin\s+id=(?P<id>[A-Za-z0-9][A-Za-z0-9._-]{1,63})"
    r"\s+kind=(?P<kind>[a-z-]{1,32})\s+status=(?P<status>[a-z]+)(?P<rest>[^>]*)-->"
)
# A slug becomes a directory name. Reject what breaks paths -- not what is
# simply not ASCII: repositories are legitimately named in any language.
BAD_SLUG_CHARS = set(r'/\:*?"<>|')


def slug_ok(slug: str) -> bool:
    return (
        bool(slug)
        and len(slug) <= 64
        and slug not in (".", "..")
        and not slug.startswith(("-", "."))
        and not (BAD_SLUG_CHARS & set(slug))
        and all(ch >= " " for ch in slug)
    )


def load_projects(only: str | None) -> dict:
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"registry unreadable: {e}", file=sys.stderr)
        return {}
    out = {}
    for slug, meta in (data.get("projects") or {}).items():
        if only and slug != only:
            continue
        if not meta.get("active", True):
            continue
        if not slug_ok(slug):
            print(f"skipping project with unusable name: {slug!r}", file=sys.stderr)
            continue
        out[slug] = meta
    return out


def iter_blocks(text: str):
    """Yield (match, header, body_end) for each block begin marker."""
    for m in BEGIN_RE.finditer(text):
        bid = m.group("id")
        end = text.find(f"<!-- wiki-raw:end id={bid} -->", m.end())
        if end < 0:
            continue
        yield m, text[m.end():end].strip(), end


def collect(projects: dict, limit: int | None):
    rows = []
    for slug, meta in projects.items():
        raw_dir = Path(meta.get("raw_dir", ""))
        if not raw_dir.is_dir():
            continue
        for f in sorted(raw_dir.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m, body, _ in iter_blocks(text):
                if m.group("status") != "unprocessed":
                    continue
                header = next((ln for ln in body.splitlines() if ln.startswith("## ")), "")
                rows.append({
                    "project": slug,
                    "file": str(f).replace("\\", "/"),
                    "id": m.group("id"),
                    "kind": m.group("kind"),
                    "header": header.lstrip("# ").strip(),
                    "chars": len(body),
                })
    rows.sort(key=lambda r: (r["file"], r["id"]))
    return rows[:limit] if limit else rows


def cmd_list(args) -> int:
    rows = collect(load_projects(args.project), args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("queue empty")
        return 0
    for r in rows:
        print(f"{r['project']:16} {r['kind']:8} {r['id']:10} {r['chars']:6}c  {r['file']}")
        print(f"    {r['header']}")
    print(f"\n{len(rows)} unprocessed blocks")
    return 0


def cmd_show(args) -> int:
    text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    for m, body, _ in iter_blocks(text):
        if m.group("id") == args.id:
            print(body)
            return 0
    print(f"block {args.id} not found in {args.file}", file=sys.stderr)
    return 1


def cmd_mark(args) -> int:
    path = Path(args.file)
    text = path.read_text(encoding="utf-8", errors="replace")
    today = datetime.now().strftime("%Y-%m-%d")
    changed = 0
    for bid in args.id:
        pattern = re.compile(
            r"(<!--\s*wiki-raw:begin\s+id=" + re.escape(bid) + r"\s+kind=\S+\s+status=)unprocessed"
        )
        text, n = pattern.subn(rf"\1processed date={today}", text, count=1)
        changed += n
        if n == 0:
            print(f"warn: {bid} not unprocessed in {path.name}", file=sys.stderr)
    if changed:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    print(f"marked {changed} block(s) processed in {path.name}")
    return 0 if changed else 1


def cmd_stats(args) -> int:
    projects = load_projects(None)
    total_u = 0
    for slug, meta in projects.items():
        raw_dir = Path(meta.get("raw_dir", ""))
        u = p = 0
        if raw_dir.is_dir():
            for f in raw_dir.glob("*.md"):
                text = f.read_text(encoding="utf-8", errors="replace")
                for m, _, _ in iter_blocks(text):
                    if m.group("status") == "unprocessed":
                        u += 1
                    else:
                        p += 1
        total_u += u
        print(f"{slug:20} unprocessed {u:4}  processed {p:4}  {raw_dir}")
    print(f"\ntotal unprocessed: {total_u}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--project")
    p_list.add_argument("--limit", type=int)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show")
    p_show.add_argument("--file", required=True)
    p_show.add_argument("--id", required=True)
    p_show.set_defaults(func=cmd_show)

    p_mark = sub.add_parser("mark")
    p_mark.add_argument("--file", required=True)
    p_mark.add_argument("--id", required=True, action="append")
    p_mark.set_defaults(func=cmd_mark)

    p_stats = sub.add_parser("stats")
    p_stats.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
