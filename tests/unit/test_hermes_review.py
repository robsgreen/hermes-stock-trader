"""Hermes transport tests: never import or call a real Hermes installation."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from trader_broker import review
from trader_broker.config import ConfigError, Settings


def hermes_env(tmp_path: Path) -> dict[str, str]:
    return {
        "REVIEWER_1_TRANSPORT": "hermes",
        "REVIEWER_1_PROVIDER": "openai-codex",
        "REVIEWER_1_MODEL": "gpt-test",
        "REVIEWER_2_TRANSPORT": "hermes",
        "REVIEWER_2_MODEL": "grok-test",
        "TRADER_HERMES_PYTHON": sys.executable,
        "TRADER_HERMES_HOME": str(tmp_path),
    }


def test_hermes_reviewers_need_no_broker_api_keys(tmp_path: Path) -> None:
    settings = Settings.from_env(hermes_env(tmp_path))
    settings.require_reviewers()
    assert settings.reviewer_1.transport == "hermes"
    assert settings.reviewer_2.api_key is None


@pytest.mark.parametrize("value", ["", "python", "/nonexistent/python"])
def test_hermes_requires_absolute_executable(tmp_path: Path, value: str) -> None:
    env = hermes_env(tmp_path)
    env["TRADER_HERMES_PYTHON"] = value
    with pytest.raises(ConfigError, match="TRADER_HERMES_PYTHON"):
        Settings.from_env(env).require_reviewers()


@pytest.mark.parametrize("value", ["", "relative", "/nonexistent/auth-home"])
def test_hermes_requires_absolute_existing_auth_home(tmp_path: Path, value: str) -> None:
    env = hermes_env(tmp_path)
    env["TRADER_HERMES_HOME"] = value
    with pytest.raises(ConfigError, match="TRADER_HERMES_HOME"):
        Settings.from_env(env).require_reviewers()


def test_hermes_rejects_unsupported_provider(tmp_path: Path) -> None:
    env = hermes_env(tmp_path)
    env["REVIEWER_1_PROVIDER"] = "auto"
    with pytest.raises(ConfigError, match="PROVIDER"):
        Settings.from_env(env).require_reviewers()


def test_hermes_review_binds_packet_and_isolates_environment(
    tmp_path: Path, review_packet: review.ReviewPacket, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings.from_env(hermes_env(tmp_path))
    monkeypatch.setenv("XAI_API_KEY", "broker-secret-must-not-pass")
    calls = []

    def run(args: list[str], **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        request = json.loads(kwargs["input"])
        assert request["packet"] == review_packet.canonical_json()
        assert request["packet_sha256"] == review_packet.sha256
        assert request["system_prompt"] == review.SYSTEM_PROMPT
        assert request["provider"] == "openai-codex"
        assert request["model"] == "gpt-test"
        assert kwargs["env"]["HERMES_HOME"] == str(tmp_path)
        assert "XAI_API_KEY" not in kwargs["env"]
        assert "PYTHONPATH" not in kwargs["env"]
        assert kwargs["cwd"] == str(tmp_path)
        assert args[0] == sys.executable
        assert Path(args[1]).name == "hermes_inference.py"
        assert kwargs["timeout"] <= settings.reviewer_timeout_s
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "packet_sha256": request["packet_sha256"],
                        "provider": request["provider"],
                        "model": request["model"],
                        "verdict": {
                            "verdict": "approve",
                            "confidence": "medium",
                            "reasons": ["Sound"],
                        },
                    }
                ),
            },
        )()

    monkeypatch.setattr("subprocess.run", run)
    reviewer = review.build_reviewer(settings.reviewer_1, settings)
    outcome = reviewer.review(review_packet)
    assert outcome.verdict_name == "approve"
    assert len(calls) == 1


@pytest.mark.parametrize("field", ["packet_sha256", "provider", "model"])
def test_hermes_refuses_mismatched_binding(
    tmp_path: Path, review_packet: review.ReviewPacket, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    settings = Settings.from_env(hermes_env(tmp_path))
    calls = []

    def run(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        request = json.loads(kwargs["input"])
        reply = {key: request[key] for key in ("packet_sha256", "provider", "model")}
        reply[field] = "wrong"
        reply["verdict"] = {"verdict": "approve", "confidence": "high", "reasons": ["Sound"]}
        return type("Result", (), {"returncode": 0, "stdout": json.dumps(reply)})()

    monkeypatch.setattr("subprocess.run", run)
    outcome = review.build_reviewer(settings.reviewer_1, settings).review(review_packet)
    assert outcome.verdict_name == "error"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "failure", ["timeout", "process", "invalid", "noise", "duplicate", "extra", "oversized"]
)
def test_hermes_failure_retries_and_blocks_without_diagnostics(
    tmp_path: Path,
    review_packet: review.ReviewPacket,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import subprocess

    settings = Settings.from_env(hermes_env(tmp_path))
    calls = []

    def run(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if failure == "timeout":
            raise subprocess.TimeoutExpired("fake", 1, output="secret", stderr="secret")
        request = json.loads(kwargs["input"])
        reply = {key: request[key] for key in ("packet_sha256", "provider", "model")}
        reply["verdict"] = {"verdict": "approve", "confidence": "high", "reasons": ["Sound"]}
        if failure == "invalid":
            reply["verdict"] = {"verdict": "execute_shell", "reasons": []}
        if failure == "extra":
            reply["command"] = "do not execute"
        stdout = json.dumps(reply)
        if failure == "noise":
            stdout = "secret diagnostics\n" + stdout
        if failure == "duplicate":
            stdout = stdout[:-1] + ', "provider": "openai-codex"}'
        if failure == "oversized":
            reply["verdict"]["reasons"] = ["x" * 100_000]
            stdout = json.dumps(reply)
        return subprocess.CompletedProcess(args, 1 if failure == "process" else 0, stdout, "secret")

    monkeypatch.setattr("subprocess.run", run)
    outcome = review.build_reviewer(settings.reviewer_1, settings).review(review_packet)
    assert outcome.verdict_name == "error"
    assert "secret" not in (outcome.error or "")
    assert len(calls) == 2
