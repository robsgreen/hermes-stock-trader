"""Hermes interpreter resolution: a configured path that went stale falls back."""

import os
from pathlib import Path

from trader_broker.config import resolve_hermes_python
from trader_broker.review import HermesReviewer, ReviewPacket


def _managed_env(home: Path, install: str, env: str, mtime: float, deps: bool = True) -> Path:
    venv = home / "installs" / install / "environments" / env / "venv"
    python = venv / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o700)
    if deps:
        (venv / "lib" / "python3.14" / "site-packages" / "dotenv").mkdir(parents=True)
    os.utime(venv, (mtime, mtime))
    return python


def test_configured_path_wins_while_it_exists(tmp_path: Path) -> None:
    configured = _managed_env(tmp_path, "a", "old", 1)
    _managed_env(tmp_path, "a", "new", 2)
    assert resolve_hermes_python(configured, tmp_path) == configured


def test_stale_path_falls_back_to_newest_complete_environment(tmp_path: Path) -> None:
    _managed_env(tmp_path, "a", "older", 1)
    newest = _managed_env(tmp_path, "b", "newest", 3)
    _managed_env(tmp_path, "b", "half-built", 5, deps=False)
    stale = tmp_path / "installs" / "a" / "environments" / "gone" / "venv" / "bin" / "python3"
    assert resolve_hermes_python(stale, tmp_path) == newest
    assert resolve_hermes_python(None, tmp_path) == newest


def test_nothing_found_is_none(tmp_path: Path) -> None:
    assert resolve_hermes_python(tmp_path / "missing", tmp_path) is None


def test_reviewer_reports_a_missing_interpreter(
    tmp_path: Path, review_packet: ReviewPacket
) -> None:
    reviewer = HermesReviewer("xai", "grok-test", tmp_path / "missing", tmp_path, 1)
    outcome = reviewer.review(review_packet)
    assert outcome.verdict_name == "error"
    assert outcome.error == "Hermes interpreter not found"
