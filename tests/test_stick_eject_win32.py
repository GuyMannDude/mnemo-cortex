"""Cortex Stick eject on Windows: the Win32 route must fail LOUDLY.

The Shell.Application Eject verb returned 0 on IGOR-2 while the volume
stayed mounted (something held a handle; GoogleDriveFS suspected), and the
only message anyone got was "still mounted". The Win32 route opens
\\\\.\\E:, then FSCTL_LOCK_VOLUME (retried) → FSCTL_DISMOUNT_VOLUME →
IOCTL_STORAGE_MEDIA_REMOVAL(FALSE) → IOCTL_STORAGE_EJECT_MEDIA, and every
failure names the step and the Win32 error by name and number. The Shell
verb stays as the fallback when the open or the lock fails.

kernel32 is ALWAYS faked here — no test touches a real volume, on any host.
"""
from __future__ import annotations

import ctypes
import os
import subprocess

import pytest

from agentb import cli

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
HANDLE = 0x1234

STEPS = [cli.FSCTL_LOCK_VOLUME, cli.FSCTL_DISMOUNT_VOLUME,
         cli.IOCTL_STORAGE_MEDIA_REMOVAL, cli.IOCTL_STORAGE_EJECT_MEDIA]


class FakeKernel32:
    """Records every call. `open_err` fails CreateFileW; `fail` maps an
    IOCTL code to the error it returns (the lock fails `lock_fails` times
    first, then succeeds, unless it is also in `fail`)."""

    def __init__(self, open_err=None, fail=None, lock_fails=0):
        self.open_err, self.fail, self.lock_fails = open_err, fail or {}, lock_fails
        self.err = 0
        self.opens, self.ioctls, self.closed = [], [], []
        self.removal_input = None

    def CreateFileW(self, path, access, share, sa, disposition, flags, template):
        self.opens.append((path, access, share, sa, disposition, flags, template))
        if self.open_err is not None:
            self.err = self.open_err
            return INVALID_HANDLE_VALUE
        return HANDLE

    def DeviceIoControl(self, h, code, inbuf, insize, outbuf, outsize, returned, ov):
        assert h == HANDLE and outbuf is None and ov is None
        assert returned is not None          # lpBytesReturned is mandatory without overlapped
        self.ioctls.append(code)
        if code == cli.IOCTL_STORAGE_MEDIA_REMOVAL:
            self.removal_input = (inbuf._obj.value, insize)
        if code == cli.FSCTL_LOCK_VOLUME and self.lock_fails:
            self.lock_fails -= 1
            self.err = 5
            return 0
        if code in self.fail:
            self.err = self.fail[code]
            return 0
        return 1

    def CloseHandle(self, h):
        self.closed.append(h)
        return 1

    def api(self):
        return self, lambda: self.err


@pytest.fixture(autouse=True)
def _never_real_kernel32(monkeypatch):
    """Safety net: a test that forgets to fake kernel32 fails, it does not
    eject whatever is at E: on the machine running the suite."""
    def boom():
        raise AssertionError("real kernel32 reached from a test")
    monkeypatch.setattr(cli, "_win32_kernel32", boom)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)


@pytest.fixture
def nt(monkeypatch):
    """os.name = 'nt' for the duration, scoped so a failure restores it before
    pytest builds a Path to report it (the 4.18.5 trap on Python 3.11)."""
    with monkeypatch.context() as m:
        m.setattr(os, "name", "nt")
        m.setenv("SystemDrive", "C:")
        yield m


def _shell(m, rc=0, stderr=""):
    calls = []
    m.setattr(subprocess, "run",
              lambda argv, **kw: (calls.append(argv),
                                  subprocess.CompletedProcess(argv, rc, stdout="", stderr=stderr))[1])
    return calls


def _fake(m, **kw):
    k = FakeKernel32(**kw)
    m.setattr(cli, "_win32_kernel32", k.api)
    return k


# ── control codes: derived with CTL_CODE, pinned to winioctl.h's values ────

def test_control_codes_match_winioctl():
    assert cli._ctl_code(0x09, 6, 0, 0) == cli.FSCTL_LOCK_VOLUME == 0x00090018
    assert cli._ctl_code(0x09, 8, 0, 0) == cli.FSCTL_DISMOUNT_VOLUME == 0x00090020
    assert cli._ctl_code(0x2D, 0x201, 0, 1) == cli.IOCTL_STORAGE_MEDIA_REMOVAL == 0x002D4804
    assert cli._ctl_code(0x2D, 0x202, 0, 1) == cli.IOCTL_STORAGE_EJECT_MEDIA == 0x002D4808


# ── success ────────────────────────────────────────────────────────────────

def test_win32_success_runs_the_four_steps_in_order_and_closes(nt):
    k = _fake(nt)
    shell = _shell(nt)
    nt.setattr(cli, "_volume_present_nt", lambda d: False)
    ok, detail = cli._eject_stick(r"E:\cortex")
    assert ok, detail
    assert detail == "ejected"
    path, access, share, sa, disposition, flags, template = k.opens[0]
    assert path == "\\\\.\\E:"
    assert access == 0x80000000 | 0x40000000          # GENERIC_READ | GENERIC_WRITE
    assert share == 0x1 | 0x2                         # FILE_SHARE_READ | FILE_SHARE_WRITE
    assert disposition == 3                           # OPEN_EXISTING
    assert k.ioctls == STEPS
    assert k.removal_input == (0, 1)                  # PREVENT_MEDIA_REMOVAL{FALSE}, one BOOLEAN
    assert k.closed == [HANDLE]
    assert shell == []                                # no fallback when Win32 worked


def test_win32_lock_retries_until_the_holder_lets_go(nt):
    k = _fake(nt, lock_fails=3)
    sleeps = []
    nt.setattr(cli.time, "sleep", sleeps.append)
    ok, detail, early = cli._eject_volume_win32("E:")
    assert ok, detail
    assert k.ioctls == [cli.FSCTL_LOCK_VOLUME] * 4 + STEPS[1:]
    assert sleeps == [cli._LOCK_WAIT] * 3


def test_win32_success_but_letter_stays_is_still_a_failure(nt):
    _fake(nt)
    shell = _shell(nt)
    nt.setattr(cli, "_volume_present_nt", lambda d: True)
    ok, detail = cli._eject_stick(r"E:\cortex")
    assert ok is False
    assert "Win32 eject reported success" in detail and "still mounted" in detail
    assert shell == []


# ── each step failing names the step and the error ─────────────────────────

@pytest.mark.parametrize("code,name", [
    (2, "ERROR_FILE_NOT_FOUND (2)"),
    (5, "ERROR_ACCESS_DENIED (5)"),
    (32, "ERROR_SHARING_VIOLATION (32)"),
    (21, "ERROR_NOT_READY (21)"),
    (1, "ERROR_INVALID_FUNCTION (1)"),
])
def test_open_failure_is_named_and_early(nt, code, name):
    k = _fake(nt, open_err=code)
    ok, detail, early = cli._eject_volume_win32("E:")
    assert ok is False and early is True
    assert detail.startswith("CreateFileW(\\\\.\\E:) failed: " + name), detail
    assert k.ioctls == [] and k.closed == []          # nothing to close


def test_lock_failure_names_the_holder_after_every_retry(nt):
    k = _fake(nt, fail={cli.FSCTL_LOCK_VOLUME: 5})
    ok, detail, early = cli._eject_volume_win32("H:")
    assert ok is False and early is True
    assert detail.startswith(
        f"FSCTL_LOCK_VOLUME failed after {cli._LOCK_TRIES} tries: ERROR_ACCESS_DENIED (5), "
        "a process still has files open on H:"), detail
    assert k.ioctls == [cli.FSCTL_LOCK_VOLUME] * cli._LOCK_TRIES
    assert k.closed == [HANDLE]


@pytest.mark.parametrize("step,label,code,name", [
    (cli.FSCTL_DISMOUNT_VOLUME, "FSCTL_DISMOUNT_VOLUME", 5, "ERROR_ACCESS_DENIED (5)"),
    (cli.IOCTL_STORAGE_MEDIA_REMOVAL, "IOCTL_STORAGE_MEDIA_REMOVAL", 1, "ERROR_INVALID_FUNCTION (1)"),
    (cli.IOCTL_STORAGE_EJECT_MEDIA, "IOCTL_STORAGE_EJECT_MEDIA", 21, "ERROR_NOT_READY (21)"),
])
def test_late_step_failure_is_final_no_shell_fallback(nt, step, label, code, name):
    """From the dismount on, the volume's state has changed: report the step,
    do not hand a half-ejected volume to the Shell verb."""
    k = _fake(nt, fail={step: code})
    shell = _shell(nt)
    ok, detail = cli._eject_stick(r"E:\cortex")
    assert ok is False
    assert detail.startswith(f"Win32 eject: {label} failed: {name}"), detail
    assert k.ioctls == STEPS[:STEPS.index(step) + 1]   # stopped at the failing step
    assert k.closed == [HANDLE]
    assert shell == []
    if step != cli.FSCTL_DISMOUNT_VOLUME:
        assert "dismounted cleanly, not ejected" in detail


def test_unnamed_error_falls_back_to_formatmessage(nt):
    _fake(nt, fail={cli.FSCTL_DISMOUNT_VOLUME: 1117})
    nt.setattr(cli, "_win32_format_message",
               lambda c: "The request could not be performed because of an I/O device error.")
    ok, detail, early = cli._eject_volume_win32("E:")
    assert detail == ("FSCTL_DISMOUNT_VOLUME failed: Win32 error 1117 (The request could "
                      "not be performed because of an I/O device error.)")
    assert early is False


# ── fallback to the Shell verb, and the both-failed message ───────────────

def test_lock_failure_falls_back_to_shell_verb_which_works(nt):
    _fake(nt, fail={cli.FSCTL_LOCK_VOLUME: 32})
    shell = _shell(nt)
    nt.setattr(cli, "_volume_present_nt", lambda d: False)
    ok, detail = cli._eject_stick(r"E:\cortex")
    assert ok, detail
    assert len(shell) == 1 and shell[0][0] == "powershell"
    assert "ParseName('E:')" in shell[0][-1]


def test_both_routes_fail_shell_returns_zero_but_volume_stays(nt):
    """The IGOR-2 case: lock refused (a holder), Shell verb returns 0, letter
    stays. The message carries both reasons."""
    _fake(nt, fail={cli.FSCTL_LOCK_VOLUME: 5})
    _shell(nt, rc=0)
    nt.setattr(cli, "_volume_present_nt", lambda d: True)
    ok, detail = cli._eject_stick(r"H:\cortex")
    assert ok is False
    assert detail.startswith("both eject routes failed — Win32: FSCTL_LOCK_VOLUME failed")
    assert "ERROR_ACCESS_DENIED (5)" in detail and "files open on H:" in detail
    assert "Shell verb: returned 0 but the volume is still mounted" in detail


def test_both_routes_fail_shell_exits_nonzero(nt):
    _fake(nt, open_err=5)
    _shell(nt, rc=3, stderr="no Eject verb offered")
    ok, detail = cli._eject_stick(r"E:\cortex")
    assert ok is False
    assert detail == ("both eject routes failed — Win32: CreateFileW(\\\\.\\E:) failed: "
                      "ERROR_ACCESS_DENIED (5), this process may not open E: for raw access "
                      "(try an elevated shell, or check the removable-storage policy); "
                      "Shell verb: no Eject verb offered")


def test_no_kernel32_falls_back_to_shell(nt):
    def gone():
        raise AttributeError("module 'ctypes' has no attribute 'WinDLL'")
    nt.setattr(cli, "_win32_kernel32", gone)
    shell = _shell(nt, rc=0)
    nt.setattr(cli, "_volume_present_nt", lambda d: False)
    ok, detail = cli._eject_stick(r"E:\cortex")
    assert ok, detail
    assert len(shell) == 1


def test_named_reason_reaches_the_eject_failed_toast(monkeypatch, tmp_path):
    """The EJECT FAILED toast body keeps the first 120 chars of the detail;
    the step and the error name must land inside them."""
    import agentb.config
    import agentb.stick
    from agentb.stick import SyncReport
    with monkeypatch.context() as m:              # Windows half: build the real detail
        m.setattr(os, "name", "nt")
        m.setenv("SystemDrive", "C:")
        _fake(m, fail={cli.FSCTL_LOCK_VOLUME: 5})
        _shell(m, rc=0)
        m.setattr(cli, "_volume_present_nt", lambda d: True)
        result = cli._eject_stick(r"H:\cortex")
    assert result[0] is False

    class Cfg:
        data_dir = str(tmp_path)

    monkeypatch.setattr(agentb.config, "load_config", lambda: Cfg())
    monkeypatch.setattr(agentb.stick, "load_host_config", lambda d: {})
    monkeypatch.setattr(agentb.stick, "sync", lambda *a, **kw: SyncReport())
    monkeypatch.setattr(cli, "_stick_locate", lambda mnt, h: tmp_path)
    monkeypatch.setattr(cli, "_eject_stick", lambda d, extra_roots=None: result)
    toasts = []
    monkeypatch.setattr(cli, "_notify_desktop", lambda t, b: toasts.append((t, b)))
    assert cli._stick_run_sync(None, (), None, False, False, notify=True, eject=True) is False
    title, body = toasts[0]
    assert "EJECT FAILED" in title
    assert "FSCTL_LOCK_VOLUME" in body and "ERROR_ACCESS_DENIED (5)" in body


# ── non-Windows: the Win32 route is never reached ──────────────────────────

def test_posix_never_touches_win32(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_eject_volume_win32", lambda d: called.append(d))
    with monkeypatch.context() as m:
        m.setattr(os, "name", "posix")
        shell = _shell(m)
        # whatever the POSIX branch decides on this host (on Linux: refuses,
        # "/" is not under a stick root), it never goes near Win32/PowerShell
        ok, detail = cli._eject_stick("/media/someone/CORTEX/cortex")
    assert called == []
    assert all(argv[0] != "powershell" for argv in shell)
