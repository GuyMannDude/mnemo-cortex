"""The systemd unit robot-install.sh writes must back off when the server keeps
failing to start, and must never give up for good: a server whose address is
not up yet at boot has to come back on its own once it is."""

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "robot-install.sh").read_text(encoding="utf-8")


def _unit() -> dict[str, dict[str, str]]:
    m = re.search(r"<<EOF\n(\[Unit\]\n.*?)\nEOF\n", SRC, flags=re.S)
    assert m, "robot-install.sh's unit heredoc moved; update this test"
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line, {})
        else:
            k, v = line.split("=", 1)
            current[k] = v
    return sections


def test_backoff_keys_are_in_the_service_section():
    """systemd.service(5): RestartSteps=/RestartMaxDelaySec= belong in
    [Service]; anywhere else systemd ignores them."""
    unit = _unit()
    for key in ("RestartSteps", "RestartMaxDelaySec"):
        assert key in unit["[Service]"], f"{key} missing from [Service]"
        assert key not in unit["[Unit]"], f"{key} in [Unit] is ignored"


def test_backoff_takes_effect():
    """Both keys work only together and with a non-zero RestartSec, and the
    cap must not be below the first delay."""
    service = _unit()["[Service]"]
    assert service["Restart"] == "on-failure"
    restart_sec = int(service["RestartSec"])
    assert restart_sec > 0
    assert int(service["RestartSteps"]) > 0
    assert int(service["RestartMaxDelaySec"]) >= restart_sec


def test_backoff_cap_stays_short():
    """A good run does not reset the backoff (seen on systemd 259), so after
    a slow boot every later crash waits up to the cap before the server is
    back. Keep that wait to a minute."""
    assert int(_unit()["[Service]"]["RestartMaxDelaySec"]) <= 60


def test_no_start_limit():
    """A start limit stops restarts for good, so a slow boot leaves the server
    down until someone runs reset-failed. Backoff replaces it."""
    for section in _unit().values():
        assert not [k for k in section if k.startswith("StartLimit")]
