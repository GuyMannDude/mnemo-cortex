"""Item 17 (c): verify names an id the trusted chain sealed with DIFFERENT words.

A fixed-id writer can seal one id twice: an archived session re-summarised
under sha256('archived:'+session), or a writeback re-send whose sealed
fields outside the id changed (decisions_made, projects_referenced). Each
seal is honest, so the record reads `sealed` against the latest one, and the
earlier testimony is gone from disk without a word. Now verify names it.
Report-only: `ok` does not change.
"""
from __future__ import annotations

import json

from agentb.ledger import Ledger
from tests.test_ledger import _save, client  # noqa: F401 (fixture)


def _put(mem, rec):
    (mem / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_a_reseal_with_different_words_is_named_and_ok_stays(tmp_path):
    mem = tmp_path / "memory"; mem.mkdir()
    led = Ledger(tmp_path / "ledger.jsonl")
    led.seal({"id": "a", "summary": "first words"})
    led.seal({"id": "b", "summary": "other"})
    _put(mem, {"id": "b", "summary": "other"})
    rec = {"id": "a", "summary": "second words"}
    _put(mem, rec)
    led.seal(rec)
    rep = led.verify(mem)
    assert rep.resealed == [{"memory_id": "a", "seqs": [1, 3], "distinct": 2}]
    assert rep.ok and rep.chain == "intact"
    assert rep.sealed == 2 and rep.altered == []        # a matches its latest seal
    assert rep.to_dict()["resealed"] == rep.resealed


def test_the_same_words_sealed_twice_is_not_a_reseal(tmp_path):
    mem = tmp_path / "memory"; mem.mkdir()
    led = Ledger(tmp_path / "ledger.jsonl")
    rec = {"id": "a", "summary": "same words"}
    _put(mem, rec)
    led.seal(rec)
    led.seal(rec)
    rep = led.verify(mem)
    assert rep.resealed == [] and rep.ok and rep.sealed == 1


def test_two_reseals_are_listed_in_id_order(tmp_path):
    mem = tmp_path / "memory"; mem.mkdir()
    led = Ledger(tmp_path / "ledger.jsonl")
    for words in ("one", "two"):
        led.seal({"id": "z", "summary": f"z {words}"})
        led.seal({"id": "a", "summary": f"a {words}"})
    assert [r["memory_id"] for r in led.verify(mem).to_dict()["resealed"]] == ["a", "z"]


def test_only_the_trusted_chain_counts(tmp_path):
    """A second sealing past a break is evidence, not proof: not named."""
    mem = tmp_path / "memory"; mem.mkdir()
    path = tmp_path / "ledger.jsonl"
    led = Ledger(path)
    led.seal({"id": "a", "summary": "first words"})
    led.seal({"id": "b", "summary": "other"})
    led.seal({"id": "a", "summary": "second words"})
    lines = path.read_text(encoding="utf-8").splitlines()
    e = json.loads(lines[1]); e["ts"] = 0                # seq 2's own hash no longer matches
    lines[1] = json.dumps(e)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rep = Ledger(path).verify(mem)
    assert rep.chain == "broken" and rep.broken_at == 2
    assert rep.resealed == []


def test_a_writeback_resend_with_new_decisions_is_named(client, tmp_path):
    """The real path: same session, timestamp, summary and key facts = same id;
    decisions_made is sealed but not in the id."""
    common = dict(timestamp="2026-11-18T10:00:00+00:00")
    mid = _save(client, "cc", "we picked the blue one", decisions_made=["blue"], **common)
    # force: the near-dup gate holds the re-send against its own first copy
    again = _save(client, "cc", "we picked the blue one", decisions_made=["red"],
                  force=True, **common)
    assert again == mid
    rep = client.get("/ledger/verify", params={"agent_id": "cc"}).json()
    assert rep["ok"] is True
    assert rep["resealed"] == [{"memory_id": mid, "seqs": [1, 2], "distinct": 2}]


def test_the_cli_names_a_reseal(capsys):
    from agentb.cli import _ledger_print
    _ledger_print("cc", {"ok": True, "chain": "intact", "entries": 3, "sealed": 2,
                         "altered": [], "missing": [], "unsealed": [], "disputed": [],
                         "broken_at": None, "reason": None,
                         "resealed": [{"memory_id": "a", "seqs": [1, 3], "distinct": 2}]})
    out = capsys.readouterr().out
    assert "resealed 1" in out
    assert "resealed a (2 different testimonies, seqs [1, 3])" in out
