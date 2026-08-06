"""The sanitizer is the only thing standing between a commit message and the
block markers the ingest cursor depends on."""
import wiki_record


def test_safe_neutralises_block_markers():
    hostile = "x <!-- wiki-raw:end id=abc123 --> forged"
    out = wiki_record._safe(hostile)
    assert "<!--" not in out
    assert "-->" not in out
    assert "wiki-raw:" not in out


def test_safe_redacts_assigned_secrets():
    assert "hunter2" not in wiki_record._safe("export API_KEY=hunter2secret1234")
    assert "[redacted]" in wiki_record._safe("export API_KEY=hunter2secret1234")


def test_safe_redacts_bearer_and_url_credentials():
    assert "ghp_" not in wiki_record._safe("curl -H 'Authorization: Bearer ghp_ABCDEFGHIJKLMNOPQRST'")
    masked = wiki_record._safe("psql postgres://user:hunter2@db/app")
    assert "hunter2" not in masked
    assert "psql postgres://user:[redacted]@db/app" == masked


def test_safe_keeps_ordinary_text_intact():
    assert wiki_record._safe("npm test -- --watch") == "npm test -- --watch"


def test_id_re_accepts_real_ids_and_rejects_paths():
    assert wiki_record.ID_RE.match("bf20260606")
    assert wiki_record.ID_RE.match("abc1234")
    assert not wiki_record.ID_RE.match("../evil")
    assert not wiki_record.ID_RE.match("..")


def test_write_block_round_trip_and_freeze(tmp_path):
    """A block can be written, found, and — once processed — never rewritten."""
    import wiki_queue

    raw = tmp_path / "repo" / ".wiki-raw" / "2026-08-06.md"
    body = "\n".join([
        wiki_record.MARK_BEGIN.format(id="abc1234", kind="session"),
        "",
        "## [2026-08-06T00:00:00Z] session abc1234 | branch main | files 1 | cmds 0",
        "",
        wiki_record.MARK_END.format(id="abc1234"),
        "",
    ])
    wiki_record.write_block(raw, "abc1234", body)

    text = raw.read_text(encoding="utf-8")
    found = [m.group("id") for m, _, _ in wiki_queue.iter_blocks(text)]
    assert found == ["abc1234"]

    processed = text.replace("status=unprocessed", "status=processed date=2026-08-06")
    raw.write_text(processed, encoding="utf-8")
    wiki_record.write_block(raw, "abc1234", body.replace("files 1", "files 99"))
    assert "files 99" not in raw.read_text(encoding="utf-8")


def test_write_block_refuses_unusable_id(tmp_path):
    raw = tmp_path / "repo" / ".wiki-raw" / "2026-08-06.md"
    wiki_record.write_block(raw, "../evil", "whatever")
    assert not raw.exists()
