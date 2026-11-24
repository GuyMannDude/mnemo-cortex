"""cc-sync per-session offset tests (clean-room review M-group).

The old state was a single {session_id, byte_offset}: with two live sessions
alternating as "newest", every flip reset the offset to 0 (duplicate floods)
and the non-newest session's tail was skipped. Now every active file syncs
against its own offset, torn final lines wait for their newline (watcher-H2
sibling), and upgrade/migration seeds existing files at EOF so the already-
posted backlog can't re-flood.
"""
import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "integrations/claude-code/mnemo-cc-sync.py"


def user_line(text):
    return json.dumps({"type": "user", "timestamp": "t",
                       "message": {"role": "user", "content": text}})


def assistant_line(text):
    return json.dumps({"type": "assistant", "timestamp": "t",
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": text}]}})


def batch(tag, n=6):
    """n alternating messages, each tagged so duplicates are detectable."""
    return "".join(
        (user_line(f"{tag}-u{i}") if i % 2 == 0 else assistant_line(f"{tag}-a{i}")) + "\n"
        for i in range(n)
    )


@pytest.fixture
def ccsync(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("ccsync_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sessions = tmp_path / "projects"
    sessions.mkdir()
    monkeypatch.setattr(mod, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(mod, "OFFSET_FILE", tmp_path / "state" / "offset.json")

    mod.posts = []

    def fake_post(session_id, summary, key_facts, wire=None, *, bare=False):
        mod.posts.append({"session_id": session_id, "summary": summary})
        return {"memory_id": f"m{len(mod.posts)}"}

    monkeypatch.setattr(mod, "post_to_mnemo", fake_post)
    monkeypatch.setattr(mod, "read_windows", lambda since=None: [])  # item 18c': no server
    return mod


def all_summaries(mod):
    return "\n".join(p["summary"] for p in mod.posts)


def test_two_live_sessions_no_flood_no_skipped_tail(ccsync):
    mod = ccsync
    mod.main()  # fresh install on an empty dir → seeds, from here new files start at 0

    a = mod.SESSIONS_DIR / "aaaa1111.jsonl"
    b = mod.SESSIONS_DIR / "bbbb2222.jsonl"

    a.write_text(batch("a1"))
    assert mod.main() == 0
    assert len(mod.posts) == 1

    # Second session goes live — the old code would flip to it and reset a's offset.
    b.write_text(batch("b1"))
    mod.main()
    assert len(mod.posts) == 2

    # First session speaks again: only its NEW turns post, nothing re-floods.
    with a.open("a") as fh:
        fh.write(batch("a2"))
    mod.main()
    assert len(mod.posts) == 3
    assert "a2-u0" in mod.posts[2]["summary"]
    assert "a1-u0" not in mod.posts[2]["summary"]  # no duplicate flood
    assert all_summaries(mod).count("b1-u0") == 1


def test_torn_final_line_waits_for_newline(ccsync):
    mod = ccsync
    mod.main()
    f = mod.SESSIONS_DIR / "cccc3333.jsonl"
    torn = user_line("torn-message")
    f.write_text(batch("c1") + torn[:25])  # mid-append, no newline

    mod.main(force=True)
    assert "torn-message" not in all_summaries(mod)

    with f.open("a") as fh:
        fh.write(torn[25:] + "\n" + assistant_line("torn-reply") + "\n")
    mod.main(force=True)
    assert all_summaries(mod).count("torn-message") == 1  # once the line completes


def test_failed_post_retries_without_loss(ccsync, monkeypatch):
    mod = ccsync
    mod.main()
    f = mod.SESSIONS_DIR / "dddd4444.jsonl"
    f.write_text(batch("d1"))

    def broken_post(session_id, summary, key_facts, wire=None, *, bare=False):
        raise urllib.error.URLError("mnemo down")

    real_post = mod.post_to_mnemo
    monkeypatch.setattr(mod, "post_to_mnemo", broken_post)
    assert mod.main() == 1
    assert mod.posts == []

    monkeypatch.setattr(mod, "post_to_mnemo", real_post)
    assert mod.main() == 0
    assert all_summaries(mod).count("d1-u0") == 1  # retried, exactly once


def test_legacy_state_migrates_offset_and_seeds_others(ccsync):
    mod = ccsync
    tracked = mod.SESSIONS_DIR / "eeee5555.jsonl"
    other = mod.SESSIONS_DIR / "ffff6666.jsonl"

    already_synced = batch("old")
    tracked.write_text(already_synced)
    other.write_text(batch("other-history"))

    # Legacy single-session state pointing mid-file at the tracked session.
    mod.OFFSET_FILE.parent.mkdir(parents=True)
    mod.OFFSET_FILE.write_text(json.dumps(
        {"session_id": "eeee5555", "byte_offset": len(already_synced.encode())}
    ))

    with tracked.open("a") as fh:
        fh.write(batch("new"))
    mod.main()

    summaries = all_summaries(mod)
    assert "new-u0" in summaries          # carried offset: only the tail posts
    assert "old-u0" not in summaries      # no re-flood of what legacy already synced
    assert "other-history" not in summaries  # untracked backlog seeded at EOF


def test_small_batch_defers_without_advancing(ccsync):
    mod = ccsync
    mod.main()
    f = mod.SESSIONS_DIR / "gggg7777.jsonl"
    f.write_text(batch("g1", n=2))  # below MIN_TURNS_PER_BATCH

    mod.main()
    assert mod.posts == []

    with f.open("a") as fh:
        fh.write(batch("g2", n=4))  # now 6 total
    mod.main()
    assert all_summaries(mod).count("g1-u0") == 1  # deferred turns not lost


def test_unicode_line_separator_content_survives(ccsync):
    # Claude Code (a JS app) emits raw U+2028/U+2029 inside JSON strings.
    # splitlines() would fragment the record and silently drop the message.
    mod = ccsync
    mod.main()
    f = mod.SESSIONS_DIR / "iiii9999.jsonl"
    line = json.dumps({"type": "user", "timestamp": "t",
                       "message": {"role": "user",
                                   "content": "before\u2028after-separator"}},
                      ensure_ascii=False)
    f.write_text(batch("i1", n=4) + line + "\n", encoding="utf-8")

    mod.main(force=True)
    assert all_summaries(mod).count("after-separator") == 1


def test_vanished_file_does_not_abort_tick(ccsync, monkeypatch):
    mod = ccsync
    mod.main()
    gone = mod.SESSIONS_DIR / "aaaa0000-gone.jsonl"   # older mtime — syncs first
    ok = mod.SESSIONS_DIR / "bbbb0000-ok.jsonl"
    gone.write_text(batch("gone1"))
    ok.write_text(batch("ok1"))

    real_sync = mod.sync_file

    def flaky(jsonl, entry, force, *rest):
        if "gone" in jsonl.name:
            raise FileNotFoundError(jsonl)  # deleted between walk and read
        return real_sync(jsonl, entry, force, *rest)

    monkeypatch.setattr(mod, "sync_file", flaky)
    assert mod.main() == 1                     # failure reported...
    assert "ok1-u0" in all_summaries(mod)      # ...but other sessions still sync
    # and the successful post's offset was persisted despite the failure
    saved = json.loads(mod.OFFSET_FILE.read_text())
    assert saved["files"]["bbbb0000-ok.jsonl"]["byte_offset"] > 0


def test_top_level_last_post_at_mirror_for_watchdog(ccsync):
    mod = ccsync
    mod.main()
    f = mod.SESSIONS_DIR / "jjjj0000.jsonl"
    f.write_text(batch("j1"))
    mod.main()

    saved = json.loads(mod.OFFSET_FILE.read_text())
    assert saved.get("last_post_at")  # watchdog reads this top-level key


def test_corrupt_offset_file_recovers(ccsync):
    mod = ccsync
    mod.OFFSET_FILE.parent.mkdir(parents=True)
    mod.OFFSET_FILE.write_text("{not json")
    f = mod.SESSIONS_DIR / "kkkk0000.jsonl"
    f.write_text(batch("k1"))

    assert mod.main() == 0  # treated as fresh install: reseeds, no crash


def test_state_pruned_for_deleted_files(ccsync):
    mod = ccsync
    mod.main()
    f = mod.SESSIONS_DIR / "hhhh8888.jsonl"
    f.write_text(batch("h1"))
    mod.main()
    assert "hhhh8888.jsonl" in json.loads(mod.OFFSET_FILE.read_text())["files"]

    f.unlink()
    mod.main()
    assert "hhhh8888.jsonl" not in json.loads(mod.OFFSET_FILE.read_text())["files"]
