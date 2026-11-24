"""Dreamer git-sync check: every watched path resolves to its repo root, a path
that cannot be checked says why, a failed fetch says so, and a pinned checkout's
"behind" is a note that sends no letter. Real git, scratch repos, scratch HOME.

Each test asserts the printed block AND whether run_git_sync sent the letter
(the bus + Discord notification). The letter rule: any line carrying ⚠️."""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

_DREAM_PATH = Path(__file__).resolve().parent.parent / "mnemo-dream.py"
_spec = importlib.util.spec_from_file_location("mnemo_dream_git_sync", _DREAM_PATH)
dream = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dream)

DATE = "2026-01-01"


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed: {p.stderr}"
    return p.stdout.strip()


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("")
    for var, val in {
        "HOME": str(home), "USERPROFILE": str(home),
        "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        # git must never find a repository above the scratch dir.
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }.items():
        monkeypatch.setenv(var, val)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "BRAIN_DIR",
                "MNEMO_DREAM_GIT_SYNC_REPOS", "MNEMO_DREAM_GIT_SYNC_PINNED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(dream, "MNEMO_DREAM_GIT_SYNC_CHECK", True)


@pytest.fixture
def letters(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(dream, "notify_git_sync", lambda block, date: sent.append(block))
    return sent


@pytest.fixture
def make_repo(tmp_path):
    """make_repo(name) -> (clone, seed): clone tracks origin/master of a bare
    remote; seed is a second clone that can push to put `clone` behind."""
    def make(name: str) -> tuple[Path, Path]:
        bare = tmp_path / f"{name}.git"
        seed = tmp_path / f"{name}-seed"
        _git(tmp_path, "init", "--bare", "-b", "master", str(bare))
        _git(tmp_path, "init", "-b", "master", str(seed))
        (seed / "brain").mkdir()
        (seed / "brain" / "a.md").write_text("a\n")
        _git(seed, "add", "brain/a.md")
        _git(seed, "commit", "-m", "first")
        _git(seed, "remote", "add", "origin", str(bare))
        _git(seed, "push", "-u", "origin", "master")
        clone = tmp_path / name
        _git(tmp_path, "clone", str(bare), str(clone))
        return clone, seed
    return make


def _push_ahead(seed: Path, n: int) -> None:
    for i in range(n):
        (seed / f"n{i}.txt").write_text(f"{i}\n")
        _git(seed, "add", f"n{i}.txt")
        _git(seed, "commit", "-m", f"remote {i}")
    _git(seed, "push")


def _watch(monkeypatch, *paths) -> None:
    monkeypatch.setenv("MNEMO_DREAM_GIT_SYNC_REPOS", ",".join(str(p) for p in paths))


def _pin(monkeypatch, *paths) -> None:
    monkeypatch.setenv("MNEMO_DREAM_GIT_SYNC_PINNED", ",".join(str(p) for p in paths))


# ── control: a clean repo prints exactly what it printed before this change ──

def test_clean_repo_same_output_as_before(make_repo, monkeypatch, letters):
    clone, _ = make_repo("proj")
    _watch(monkeypatch, clone)
    block = dream.run_git_sync(DATE)
    assert block == (
        "### git sync status\n"
        f"**proj** (`{clone}`)\n"
        "- ✅ working tree clean\n"
        "- ✅ no unpushed commits\n"
        "- ✅ up to date with origin/master"
    )
    assert letters == []


def test_disabled_returns_none(monkeypatch, letters):
    monkeypatch.setattr(dream, "MNEMO_DREAM_GIT_SYNC_CHECK", False)
    assert dream.run_git_sync(DATE) is None
    assert letters == []


# ── 1. a subdir is watched, and the block names the repo root ──

def test_brain_dir_subdir_is_watched(make_repo, monkeypatch, letters):
    own, _ = make_repo("dreamer")
    clone, _ = make_repo("brainrepo")
    # Default watch list (no REPOS override): the dreamer's own checkout + BRAIN_DIR.
    monkeypatch.setattr(dream, "__file__", str(own / "mnemo-dream.py"))
    monkeypatch.setenv("BRAIN_DIR", str(clone / "brain"))
    block = dream.run_git_sync(DATE)
    assert f"**brainrepo** (`" in block
    assert f"watched via `{clone / 'brain'}`" in block
    assert "not checked" not in block
    assert block.count("✅ working tree clean") == 2
    assert letters == []


def test_brain_dir_subdir_dirty_sends_letter(make_repo, monkeypatch, letters):
    clone, _ = make_repo("brainrepo")
    (clone / "brain" / "a.md").write_text("changed\n")
    _watch(monkeypatch, clone / "brain")
    block = dream.run_git_sync(DATE)
    assert "⚠️ 1 uncommitted change(s): brain/a.md" in block
    assert letters == [block]


def test_two_paths_in_one_repo_give_one_block(make_repo, monkeypatch, letters):
    clone, _ = make_repo("proj")
    _watch(monkeypatch, clone, clone / "brain")
    block = dream.run_git_sync(DATE)
    assert block.count("**proj**") == 1
    assert block.count("working tree clean") == 1
    assert f"**proj** (`{clone}`) — watched via `{clone / 'brain'}`" in block
    assert letters == []


# ── 2. a path that cannot be checked prints why, and sends the letter ──

def test_path_outside_any_checkout(tmp_path, monkeypatch, letters):
    plain = tmp_path / "plain"
    plain.mkdir()
    _watch(monkeypatch, plain)
    block = dream.run_git_sync(DATE)
    assert f"**plain** (`{plain}`)\n- ⚠️ not checked: not inside a git checkout" in block
    assert letters == [block]


def test_missing_path(tmp_path, monkeypatch, letters):
    gone = tmp_path / "gone"
    _watch(monkeypatch, gone)
    block = dream.run_git_sync(DATE)
    assert f"**gone** (`{gone}`)\n- ⚠️ not checked: path does not exist" in block
    assert letters == [block]


def test_git_not_runnable(tmp_path, monkeypatch, letters):
    plain = tmp_path / "plain"
    plain.mkdir()
    _watch(monkeypatch, plain)

    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", no_git)
    block = dream.run_git_sync(DATE)
    assert "⚠️ not checked: git could not run: git" in block
    assert letters == [block]


# ── 3. pinned ──

def test_pinned_behind_only_is_a_note_no_letter(make_repo, monkeypatch, letters):
    clone, seed = make_repo("host")
    _push_ahead(seed, 2)
    _watch(monkeypatch, clone)
    _pin(monkeypatch, clone)
    block = dream.run_git_sync(DATE)
    assert "- 📌 pinned: 2 behind origin/master, expected for a pinned checkout" in block
    assert "⚠️" not in block
    assert letters == []


def test_pinned_dirty_sends_letter(make_repo, monkeypatch, letters):
    clone, seed = make_repo("host")
    _push_ahead(seed, 1)
    (clone / "stray.txt").write_text("x\n")
    _watch(monkeypatch, clone)
    _pin(monkeypatch, clone)
    block = dream.run_git_sync(DATE)
    assert "⚠️ 1 uncommitted change(s): stray.txt" in block
    assert "📌 pinned: 1 behind origin/master" in block
    assert letters == [block]


def test_pinned_unpushed_sends_letter(make_repo, monkeypatch, letters):
    clone, _ = make_repo("host")
    (clone / "local.txt").write_text("x\n")
    _git(clone, "add", "local.txt")
    _git(clone, "commit", "-m", "local only")
    _watch(monkeypatch, clone)
    _pin(monkeypatch, clone)
    block = dream.run_git_sync(DATE)
    assert "⚠️ 1 unpushed commit(s)" in block
    assert letters == [block]


def test_unpinned_behind_sends_letter_as_before(make_repo, monkeypatch, letters):
    clone, seed = make_repo("proj")
    _push_ahead(seed, 2)
    _watch(monkeypatch, clone)
    block = dream.run_git_sync(DATE)
    assert "- ⚠️ 2 behind origin/master — another machine pushed; pull to refresh" in block
    assert "📌" not in block
    assert letters == [block]


def test_pin_given_as_subdir_matches(make_repo, monkeypatch, letters):
    clone, seed = make_repo("host")
    _push_ahead(seed, 1)
    _watch(monkeypatch, clone)
    _pin(monkeypatch, clone / "brain")
    block = dream.run_git_sync(DATE)
    assert "📌 pinned: 1 behind" in block
    assert letters == []


@pytest.mark.skipif(os.name != "nt", reason="Windows path spelling")
def test_pin_matches_across_windows_case_and_slashes(make_repo, monkeypatch, letters):
    clone, seed = make_repo("host")
    _push_ahead(seed, 1)
    _watch(monkeypatch, clone)
    _pin(monkeypatch, str(clone).upper().replace("\\", "/") + "/")
    block = dream.run_git_sync(DATE)
    assert "📌 pinned: 1 behind" in block
    assert letters == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privilege on Windows")
def test_pin_matches_through_a_symlink(make_repo, tmp_path, monkeypatch, letters):
    clone, seed = make_repo("host")
    _push_ahead(seed, 1)
    link = tmp_path / "host-link"
    link.symlink_to(clone, target_is_directory=True)
    _watch(monkeypatch, clone)
    _pin(monkeypatch, link)
    block = dream.run_git_sync(DATE)
    assert "📌 pinned: 1 behind" in block
    assert letters == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privilege on Windows")
def test_watched_symlink_to_root_is_the_root_not_a_subdir(make_repo, tmp_path, monkeypatch, letters):
    clone, _ = make_repo("proj")
    link = tmp_path / "proj-link"
    link.symlink_to(clone, target_is_directory=True)
    _watch(monkeypatch, link)
    block = dream.run_git_sync(DATE)
    assert f"**proj-link** (`{link}`)\n- ✅ working tree clean" in block
    assert "watched via" not in block
    assert letters == []


def test_pin_matching_nothing_is_a_note_no_letter(make_repo, tmp_path, monkeypatch, letters):
    clone, _ = make_repo("proj")
    other, _ = make_repo("other")
    _watch(monkeypatch, clone)
    _pin(monkeypatch, tmp_path / "nowhere", other)
    block = dream.run_git_sync(DATE)
    assert f"- 📌 pinned entry `{tmp_path / 'nowhere'}` matches no watched repo (path does not exist)" in block
    assert f"- 📌 pinned entry `{other}` matches no watched repo\n" in block + "\n"
    assert "⚠️" not in block
    assert letters == []


@pytest.mark.skipif(os.name != "nt", reason="Windows path spelling")
def test_watched_root_in_other_case_and_slashes_is_the_root(make_repo, monkeypatch, letters):
    clone, _ = make_repo("proj")
    _watch(monkeypatch, str(clone).upper().replace("\\", "/"))
    block = dream.run_git_sync(DATE)
    assert block.count("✅ working tree clean") == 1
    assert "watched via" not in block
    assert letters == []


def _short_name(path: Path) -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))
    assert n, "GetShortPathNameW failed"
    return buf.value


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 short names")
def test_short_name_watch_and_pin_match_the_long_root(make_repo, monkeypatch, letters):
    clone, seed = make_repo("long-repo-name-for-short")
    _push_ahead(seed, 1)
    short = _short_name(clone)
    if short.lower() == str(clone).lower():
        pytest.skip("8.3 names are off on this volume")
    _watch(monkeypatch, short)
    _pin(monkeypatch, short)
    block = dream.run_git_sync(DATE)
    assert "watched via" not in block
    assert "📌 pinned: 1 behind" in block
    assert "⚠️" not in block
    assert letters == []


def test_non_ascii_repo_name_same_output(make_repo, monkeypatch, letters):
    clone, _ = make_repo("projé")
    _watch(monkeypatch, clone)
    block = dream.run_git_sync(DATE)
    assert block == (
        "### git sync status\n"
        f"**projé** (`{clone}`)\n"
        "- ✅ working tree clean\n"
        "- ✅ no unpushed commits\n"
        "- ✅ up to date with origin/master"
    )
    assert letters == []


def test_path_that_is_a_file_says_what_git_said(tmp_path, monkeypatch, letters):
    f = tmp_path / "afile.txt"
    f.write_text("x\n")
    _watch(monkeypatch, f)
    block = dream.run_git_sync(DATE)
    assert "- ⚠️ not checked: git said:" in block
    assert letters == [block]


@pytest.mark.parametrize("pinned", [False, True])
def test_upstream_deleted_on_remote_is_a_warning(make_repo, monkeypatch, letters, pinned):
    clone, seed = make_repo("proj")
    _git(clone, "checkout", "-b", "feature")
    _git(clone, "push", "-u", "origin", "feature")
    _git(seed, "push", "origin", "--delete", "feature")
    _git(clone, "config", "fetch.prune", "true")
    _watch(monkeypatch, clone)
    if pinned:
        _pin(monkeypatch, clone)
    block = dream.run_git_sync(DATE)
    # Both compares (unpushed and behind) fail, and each says so.
    assert block.count("- ⚠️ could not compare with origin/feature:") == 2
    assert letters == [block]


# ── fetch failure: its own line, no "up to date", pinned or not ──

@pytest.mark.parametrize("pinned", [False, True])
def test_fetch_failure_is_a_warning(make_repo, tmp_path, monkeypatch, letters, pinned):
    clone, _ = make_repo("proj")
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "no-such-remote.git"))
    _watch(monkeypatch, clone)
    if pinned:
        _pin(monkeypatch, clone)
    block = dream.run_git_sync(DATE)
    assert "- ⚠️ fetch failed, so the counts below may be stale:" in block
    assert "up to date" not in block
    assert letters == [block]
