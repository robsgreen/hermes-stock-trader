"""Single tool-free inference bridge; run as a file with the Hermes interpreter.

Intentionally stdlib-only until the native imports: the broker's package and Python
version need not be installed in Hermes. The parent validates the verdict schema.
"""

import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Do not silently normalize ambiguous model output before the parent sees it."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def main() -> int:
    """Read one bound request and return one bound verdict, never diagnostic output."""
    output = sys.stdout
    try:
        request = json.load(sys.stdin)
        if request["provider"] not in {"openai-codex", "xai"} or not request["model"]:
            return 1
        if hashlib.sha256(request["packet"].encode()).hexdigest() != request["packet_sha256"]:
            return 1
        # Discard all incidental native-library output, including auth diagnostics.
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            from dotenv import load_dotenv

            home = os.environ["HERMES_HOME"]
            load_dotenv(Path(home) / ".env", override=True)
            # Authentication identity is selected by the operator, never by dotenv.
            os.environ["HERMES_HOME"] = home
            from agent.auxiliary_client import resolve_provider_client  # type: ignore[import-not-found]

            client, model = resolve_provider_client(request["provider"], model=request["model"])
            if client is None or model != request["model"]:
                return 1
            effective = getattr(client, "_hermes_aux_effective_provider", request["provider"])
            if effective != request["provider"]:
                return 1
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": request["system_prompt"]},
                    {"role": "user", "content": request["packet"]},
                ],
                response_format={"type": "json_object"},
                timeout=request["timeout_s"],
            )
            verdict = json.loads(
                completion.choices[0].message.content, object_pairs_hook=_unique_object
            )
        reply: dict[str, Any] = {
            "packet_sha256": request["packet_sha256"],
            "provider": request["provider"],
            "model": model,
            "verdict": verdict,
        }
        output.write(json.dumps(reply, allow_nan=False))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
