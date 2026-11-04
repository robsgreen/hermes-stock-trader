"""Exercise the shipped helper using fake native modules, never real auth."""

import hashlib
import io
import json
import os
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from trader_broker import review

HELPER = Path(review.__file__).with_name("hermes_inference.py")


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "model_drift",
        "provider_drift",
        "missing_client",
        "bad_provider",
        "bad_hash",
        "exception",
        "duplicate_verdict",
        "dotenv_home_override",
    ],
)
def test_helper_uses_native_resolver_for_one_tool_free_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str | None
) -> None:
    calls = []
    verdict = {"verdict": "approve", "confidence": "medium", "reasons": ["Sound"]}

    def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        print("native diagnostic secret")
        print("native diagnostic secret", file=sys.stderr)
        content = json.dumps(verdict)
        if failure == "duplicate_verdict":
            content = content[:-1] + ', "verdict": "approve"}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    def resolve(provider: str, *, model: str) -> Any:
        assert os.environ["HERMES_HOME"] == str(tmp_path)
        if failure == "exception":
            raise RuntimeError("secret auth details")
        assert provider == "openai-codex" and model == "gpt-test"
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
            _hermes_aux_effective_provider="other" if failure == "provider_drift" else provider,
        )
        return (
            None if failure == "missing_client" else client,
            "drift" if failure == "model_drift" else model,
        )

    auxiliary = ModuleType("agent.auxiliary_client")
    auxiliary.resolve_provider_client = resolve  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)
    dotenv = ModuleType("dotenv")

    def load_dotenv(path: Path, **kwargs: Any) -> None:
        assert path == tmp_path / ".env"
        if failure == "dotenv_home_override":
            os.environ["HERMES_HOME"] = "/wrong/home"

    dotenv.load_dotenv = load_dotenv  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    request = {
        "packet": "{}",
        "packet_sha256": hashlib.sha256(b"{}").hexdigest(),
        "provider": "openai-codex",
        "model": "gpt-test",
        "system_prompt": review.SYSTEM_PROMPT,
        "timeout_s": 5,
    }
    output = io.StringIO()
    if failure == "bad_provider":
        request["provider"] = "auto"
    if failure == "bad_hash":
        request["packet_sha256"] = "wrong"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, "stdout", output)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(HELPER), run_name="__main__")
    if failure not in (None, "dotenv_home_override"):
        assert exit_info.value.code == 1
        assert output.getvalue() == ""
        assert len(calls) == (1 if failure == "duplicate_verdict" else 0)
        return
    assert exit_info.value.code == 0
    reply = json.loads(output.getvalue())
    assert reply == {key: request[key] for key in ("packet_sha256", "provider", "model")} | {
        "verdict": verdict
    }
    assert len(calls) == 1
    assert calls[0] == {
        "model": "gpt-test",
        "messages": [
            {"role": "system", "content": review.SYSTEM_PROMPT},
            {"role": "user", "content": "{}"},
        ],
        "response_format": {"type": "json_object"},
        "timeout": 5,
    }
