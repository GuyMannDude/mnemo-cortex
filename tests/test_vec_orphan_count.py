"""/vec/status counts the index rows recall drops for a missing record
(item 14 B).

Item 14 A stopped recall serving a vec row whose record file is gone. Those
rows still sit in the index, and nothing counted them: `indexed` included
them. Now /vec/status says how many (`orphans`) and how many rows carry no
source_file at all (`source_unknown`, served as they are). The count uses
recall's own rule (record_file), so the two never disagree: a data dir that
moved is not orphaned, a record kept outside the folder is not orphaned.

The Mnemo host is Windows, so the folder compare folds case there; the
compare takes names, not the disk, and is pinned here with ntpath and
posixpath on any platform.
"""
from __future__ import annotations

import ntpath
import posixpath
import shutil
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, AgentConfig, CacheConfig, ClassificationConfig,
    DEFAULT_PERSONAS, ProviderConfig, ResilientProviderConfig, ServerConfig,
)
from agentb.vec import EMBED_DIM, count_orphans, orphan_candidates, record_file

VEC = [0.0] * EMBED_DIM
VEC[0] = 1.0
STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
          "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


# ── the name compare, per platform ──

def test_windows_compare_folds_case_and_reads_both_separators():
    rows = [("a", "D:/mnemo-data/memory/AbC.json"),
            ("b", "D:\\mnemo-data\\memory\\dEf.json"),
            ("c", "D:/mnemo-data/memory/gone.json"),
            ("d", None), ("e", "")]
    missing, unknown = orphan_candidates(rows, ["abc.json", "DEF.json"], pathmod=ntpath)
    assert missing == [("c", "D:/mnemo-data/memory/gone.json")]
    assert unknown == ["d", "e"]


def test_posix_compare_is_exact_and_splits_on_slash_only():
    """recall's Path(source_file).name on Linux/macOS does not split on a
    backslash, so the compare must not either: such a row goes to the disk
    check instead of counting as present."""
    rows = [("a", "/srv/mnemo-data/memory/AbC.json"),
            ("b", "/srv/mnemo-data/memory/abc.json"),
            ("c", "D:\\mnemo-data\\memory\\abc.json")]
    missing, _ = orphan_candidates(rows, ["abc.json"], pathmod=posixpath)
    assert [mid for mid, _ in missing] == ["a", "c"]


def test_count_orphans_checks_each_miss_against_the_disk(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "here.json").write_text("{}")
    (memory / "dir.json").mkdir()                          # a name in the folder, no record
    (memory / "broken.json").symlink_to(tmp_path / "nowhere.json")
    elsewhere = tmp_path / "other" / "kept.json"
    elsewhere.parent.mkdir()
    elsewhere.write_text("{}")
    rows = [("here", "/old/home/memory/here.json"),     # data dir moved: rebased, not orphan
            ("kept", elsewhere.as_posix()),            # record outside the folder: not orphan
            ("gone", (memory / "gone.json").as_posix()),
            ("dir", (memory / "dir.json").as_posix()),
            ("broken", (memory / "broken.json").as_posix()),
            ("blank", None)]
    assert count_orphans(rows, memory) == (["gone", "dir", "broken"], ["blank"])
    # The count IS recall's rule: record_file, row by row, on this platform.
    rows.append(("bslash", "C:\\old\\memory\\here.json"))
    recall_drops = [mid for mid, sf in rows if sf and record_file(sf, memory) is None]
    assert count_orphans(rows, memory)[0] == recall_drops
    # A missing folder: every row whose stored path holds no record either.
    assert count_orphans(rows, tmp_path / "unmounted") == (
        ["here", "gone", "dir", "broken", "bslash"], ["blank"])


# ── /vec/status against a real app ──

class FakeEmbedding:
    active_label = "fake/embed"
    status = STATUS

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        return list(VEC)

    async def health_check(self):
        return True


class FakeReasoning:
    active_label = "fake/reason"
    status = STATUS

    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        return "[]"

    async def health_check(self):
        return True


def _client(data: Path) -> TestClient:
    from agentb.server import create_app
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        cache=CacheConfig(), classification=ClassificationConfig(enabled=False),
        server=ServerConfig(host="127.0.0.1", port=50099),
        data_dir=str(data.parent / "default"), personas=dict(DEFAULT_PERSONAS),
        agents={"cody": AgentConfig(data_dir=str(data))},
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        return TestClient(create_app(cfg))


def _save(client, text: str) -> str:
    r = client.post("/writeback", json={"session_id": "s", "summary": text, "agent_id": "cody",
                                        "category": "doctrine", "source": "inferred", "batch": True})
    assert r.status_code == 200 and r.json()["status"] == "archived", r.text
    return r.json()["memory_id"]


def _status(client) -> dict:
    r = client.get("/vec/status", params={"agent_id": "cody"})
    assert r.status_code == 200, r.text
    return r.json()


def test_status_counts_orphans_and_rows_with_no_source(tmp_path):
    with _client(tmp_path / "cody") as client:
        memory = tmp_path / "cody" / "memory"
        _save(client, "the lathe is in bay two")
        gone = _save(client, "the mill is in bay three")
        before = _status(client)                             # control: nothing missing yet
        assert before["indexed"] == 2 and before["orphans"] == 0 and before["source_unknown"] == 0
        (memory / f"{gone}.json").unlink()
        client.app.state.tenants.get("cody")["vec"].upsert(
            "legacy0000000000", "a row from before source_file", list(VEC))
        s = _status(client)
        assert s["indexed"] == 3 and s["memory_entries_on_disk"] == 1
        assert s["orphans"] == 1 and s["source_unknown"] == 1


def test_status_after_the_data_dir_moved_counts_no_orphans(tmp_path):
    old, new = tmp_path / "old" / "cody", tmp_path / "new" / "cody"
    with _client(old) as client:
        _save(client, "the lathe is in bay two")
        _save(client, "the mill is in bay three")
    shutil.move(str(old), str(new))
    with _client(new) as client:
        s = _status(client)
        assert s["indexed"] == 2 and s["orphans"] == 0 and s["source_unknown"] == 0
        # Presence: the same index read against an empty folder counts both.
        for f in (new / "memory").glob("*.json"):
            f.unlink()
        assert _status(client)["orphans"] == 2
