"""One retry the same night when the dream run dies before writing its brief
(snag-dreamer-no-second-attempt-when-night-run-fails-2026-11-13). One, not a loop."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

_DREAM_PATH = Path(__file__).resolve().parent.parent / "mnemo-dream.py"
_spec = importlib.util.spec_from_file_location("mnemo_dream_retry", _DREAM_PATH)
dream = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dream)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(dream, "_brief_written", False)
    monkeypatch.setattr(dream, "_strategies_posted", False)
    monkeypatch.setattr(dream, "_truncated", False)


def _runner(*outcomes):
    """Each call pops the next outcome: an exception is raised, anything else returned."""
    calls = []

    def run():
        calls.append(1)
        out = outcomes[len(calls) - 1]
        if isinstance(out, BaseException):
            raise out
        return out
    return run, calls


def test_success_first_try_never_sleeps():
    run, calls = _runner("ok")
    sleep = mock.Mock()
    assert dream.run_with_one_retry(run, retry=True, minutes=120, sleep=sleep) == "ok"
    assert len(calls) == 1 and not sleep.called


def test_failure_then_success_waits_once():
    run, calls = _runner(SystemExit(1), "ok")
    sleep = mock.Mock()
    assert dream.run_with_one_retry(run, retry=True, minutes=120, sleep=sleep) == "ok"
    assert len(calls) == 2
    sleep.assert_called_once_with(120 * 60)


def test_two_failures_exit_nonzero_and_stop():
    run, calls = _runner(SystemExit(1), SystemExit(1), "never")
    with pytest.raises(SystemExit) as e:
        dream.run_with_one_retry(run, retry=True, minutes=1, sleep=mock.Mock())
    assert e.value.code == 1
    assert len(calls) == 2  # one retry, not a loop


def test_crash_is_retried_too():
    run, calls = _runner(RuntimeError("boom"), "ok")
    assert dream.run_with_one_retry(run, retry=True, minutes=1, sleep=mock.Mock()) == "ok"
    assert len(calls) == 2


def test_no_retry_after_the_brief_landed():
    def run():
        dream._brief_written = True
        raise SystemExit(1)
    sleep = mock.Mock()
    with pytest.raises(SystemExit):
        dream.run_with_one_retry(run, retry=True, minutes=1, sleep=sleep)
    assert not sleep.called


@pytest.mark.parametrize("kw", [{"retry": False, "minutes": 120}, {"retry": True, "minutes": 0}])
def test_dry_run_or_zero_minutes_never_retries(kw):
    run, calls = _runner(SystemExit(1), "ok")
    sleep = mock.Mock()
    with pytest.raises(SystemExit):
        dream.run_with_one_retry(run, sleep=sleep, **kw)
    assert len(calls) == 1 and not sleep.called


def test_clean_exit_is_not_a_failure():
    run, calls = _runner(SystemExit(0), "ok")
    with pytest.raises(SystemExit):
        dream.run_with_one_retry(run, retry=True, minutes=1, sleep=mock.Mock())
    assert len(calls) == 1


def test_main_wires_the_retry(monkeypatch):
    """main() -> _dream_once fails once -> sleeps DREAM_RETRY_MINUTES -> runs again."""
    calls = []

    def once(args):
        calls.append(args.dry_run)
        if len(calls) == 1:
            sys.exit(1)
    monkeypatch.setattr(dream, "_dream_once", once)
    monkeypatch.setattr(dream, "_require_claude", lambda: None)
    monkeypatch.setattr(dream, "DREAM_RETRY_MINUTES", 7.0)
    monkeypatch.setattr(sys, "argv", ["mnemo-dream.py"])
    sleep = mock.Mock()
    monkeypatch.setattr(dream.time, "sleep", sleep)
    dream.main()
    assert calls == [False, False]
    sleep.assert_called_once_with(7.0 * 60)


class _Resp:
    status_code = 503
    text = ""

    def json(self):
        return {}

    def raise_for_status(self):
        pass


def test_write_dream_marks_the_brief_written_at_the_json(monkeypatch, tmp_path):
    """The window anchor (dreamer JSON) is what makes a retry unsafe, so the flag
    flips there, even if the .md write that follows fails."""
    monkeypatch.setattr(dream, "DREAM_DIR", tmp_path / "dreams")
    monkeypatch.setattr(dream, "DREAMER_MEMORY_DIR", tmp_path / "dreamer")
    monkeypatch.setenv("MNEMO_RULEKEEPER_ADVISORY", str(tmp_path / "no-advisory.md"))
    monkeypatch.setattr(dream.httpx, "post", lambda url, **kw: _Resp())
    real_write = Path.write_text

    def md_fails(self, *a, **k):
        if self.suffix == ".md":
            raise OSError("disk full")
        return real_write(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", md_fails)
    (tmp_path / "dreams").mkdir()
    (tmp_path / "dreamer").mkdir()
    with pytest.raises(OSError):
        dream.write_dream("brief", [{"agent_id": "cc"}], dream.datetime(2026, 11, 14, tzinfo=dream.timezone.utc))
    assert list((tmp_path / "dreams" / "records").glob("*.json"))
    assert dream._brief_written is True


def _args(**kw):
    import argparse
    return argparse.Namespace(**{"dry_run": False, "hours": 0, "verbose": False, **kw})


def _stub_pipeline(monkeypatch, *, synth):
    monkeypatch.setattr(dream, "get_last_dream_time", lambda: None)
    monkeypatch.setattr(dream, "harvest_agentb", lambda since: [
        {"agent_id": "cc", "summary": "s", "key_facts": [], "timestamp": "2026-11-14T10:00:00", "session_id": "x"}])
    monkeypatch.setattr(dream, "harvest_mnemo_sqlite", lambda since: [])
    monkeypatch.setattr(dream, "DREAM_SKIP_FACTS", True)
    monkeypatch.setattr(dream, "synthesize", synth)
    monkeypatch.setattr(dream, "proposals_block", lambda: "")
    monkeypatch.setattr(dream, "write_dream", lambda *a: "id")
    monkeypatch.setattr(dream, "triage_contradictions", lambda c: (c, []))
    monkeypatch.setattr(dream, "notify_contradictions", lambda *a, **k: None)
    monkeypatch.setattr(dream, "check_git_sync", lambda: "")


def test_retry_starts_untruncated(monkeypatch):
    seen = []

    def synth(memories, dry_run=False):
        seen.append(dream._truncated)
        if len(seen) == 1:
            dream._truncated = True  # the 10-10 shape: max_tokens, zero text, exit 1
            sys.exit(1)
        return "brief"
    _stub_pipeline(monkeypatch, synth=synth)
    dream.run_with_one_retry(lambda: dream._dream_once(_args()), retry=True, minutes=1, sleep=mock.Mock())
    assert seen == [False, False]


def test_strategies_post_once_across_the_retry(monkeypatch):
    posts = []
    calls = []

    def synth(memories, dry_run=False):
        calls.append(1)
        if len(calls) == 1:
            sys.exit(1)
        return "brief"
    _stub_pipeline(monkeypatch, synth=synth)
    monkeypatch.setattr(dream, "MNEMO_DREAM_STRATEGIES", True)
    monkeypatch.setattr(dream, "_session_streams", lambda m: {"cc": ["stream"]})
    monkeypatch.setattr(dream, "_narrative_context", lambda m, a: "")
    monkeypatch.setattr(dream, "distill_strategies_for_agent", lambda a, s, n: [{"item": 1}])
    monkeypatch.setattr(dream, "post_strategies", lambda items: posts.append(items) or (1, 0))
    monkeypatch.setattr(dream, "flag_stale_trajectories", lambda: [])
    dream.run_with_one_retry(lambda: dream._dream_once(_args()), retry=True, minutes=1, sleep=mock.Mock())
    assert len(calls) == 2
    assert len(posts) == 1
