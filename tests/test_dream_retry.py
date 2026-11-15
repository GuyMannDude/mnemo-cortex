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
