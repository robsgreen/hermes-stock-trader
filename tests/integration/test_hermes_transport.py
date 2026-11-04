"""Real subprocess boundary, using an isolated fake Hermes interpreter launcher."""

import sys
import time
from pathlib import Path

import pytest

from trader_broker.review import HermesReviewer, ReviewPacket, run_reviewers


@pytest.mark.parametrize("failure", [None, "timeout", "process"])
def test_real_process_boundary(
    tmp_path: Path, review_packet: ReviewPacket, failure: str | None
) -> None:
    launcher = tmp_path / "fake-hermes-python"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, runpy, types, json, time\n"
        "assert 'XAI_API_KEY' not in os.environ\n"
        f"failure = {failure!r}\n"
        "if failure == 'timeout': time.sleep(10)\n"
        "if failure == 'process':\n"
        "    sys.stderr.write('sensitive error details')\n"
        "    sys.exit(2)\n"
        "def resolve(provider, *, model):\n"
        "    def create(**kwargs):\n"
        "        assert set(kwargs) == {'model', 'messages', 'response_format', 'timeout'}\n"
        "        N = types.SimpleNamespace\n"
        "        verdict = {'verdict': 'approve', 'confidence': 'medium', 'reasons': ['Sound']}\n"
        "        return N(choices=[N(message=N(content=json.dumps(verdict)))])\n"
        "    N = types.SimpleNamespace\n"
        "    return N(chat=N(completions=N(create=create))), model\n"
        "module = types.ModuleType('agent.auxiliary_client')\n"
        "module.resolve_provider_client = resolve\n"
        "sys.modules['agent'] = types.ModuleType('agent')\n"
        "sys.modules['agent.auxiliary_client'] = module\n"
        "dotenv = types.ModuleType('dotenv')\n"
        "dotenv.load_dotenv = lambda *a, **kw: None\n"
        "sys.modules['dotenv'] = dotenv\n"
        "runpy.run_path(sys.argv[1], run_name='__main__')\n"
    )
    launcher.chmod(0o700)
    timeout = 0.1 if failure == "timeout" else 2
    reviewers = [
        HermesReviewer(provider, model, launcher, tmp_path, timeout)
        for provider, model in (("openai-codex", "gpt-test"), ("xai", "grok-test"))
    ]
    started = time.monotonic()
    outcomes = run_reviewers(reviewers, review_packet)
    elapsed = time.monotonic() - started
    assert [outcome.reviewer for outcome in outcomes] == ["openai-codex", "xai"]
    assert all(
        outcome.verdict_name == ("approve" if failure is None else "error") for outcome in outcomes
    )
    assert all("sensitive" not in (outcome.error or "") for outcome in outcomes)
    assert elapsed < 3
