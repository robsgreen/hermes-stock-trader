"""Checks on the Hermes `stock-trader` profile files under profile/.

Hermes itself is not available in tests, so these pin the settings the profile depends
on: the trader-broker MCP server entry, the tool allow and deny lists, that every
setting trader-broker reads reaches it from the profile's .env, the trading skills'
frontmatter and tool names, and the scheduled jobs.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from trader_broker.config import ReviewerSettings, Settings
from trader_broker.server import build_server
from trader_broker.service import TraderService

PROFILE_DIR = Path(__file__).resolve().parents[2] / "profile"
PROFILE_NAME = "stock-trader"


def _load_yaml(name: str) -> Any:
    return yaml.safe_load((PROFILE_DIR / name).read_text())


def _env_example() -> dict[str, str]:
    """Variable name to value for every assignment line in .env.example."""
    assignments = {}
    for line in (PROFILE_DIR / ".env.example").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            assignments[name.strip()] = value.strip()
    return assignments


def _settings_env_names() -> set[str]:
    """Every environment variable `Settings.from_env` reads, with default reviewer key names."""
    names = set()
    defaults = Settings()
    for field_name, field in Settings.model_fields.items():
        if field.annotation is ReviewerSettings:
            prefix = field_name.upper()
            names |= {
                f"{prefix}_{key.upper()}" for key in ("provider", "base_url", "model", "transport")
            }
            names.add(f"{prefix}_API_KEY_ENV")
            names.add(getattr(defaults, field_name).api_key_env)
        elif field.alias:
            names.add(field.alias)
    return names


@pytest.fixture
def config() -> dict[str, Any]:
    loaded: dict[str, Any] = _load_yaml("config.yaml")
    return loaded


@pytest.fixture
def trader_broker(config: dict[str, Any]) -> dict[str, Any]:
    server: dict[str, Any] = config["mcp_servers"]["trader_broker"]
    return server


def test_settings_env_names_cover_every_kind_of_setting() -> None:
    names = _settings_env_names()

    assert {"TRADER_BROKER_MODE", "TRADER_MAX_STOP_PCT", "ALPACA_SECRET_KEY"} <= names
    assert {"REVIEWER_1_MODEL", "REVIEWER_2_API_KEY_ENV", "REVIEWER_TIMEOUT_S"} <= names
    assert {"OPENAI_API_KEY", "XAI_API_KEY"} <= names
    assert {
        "REVIEWER_1_TRANSPORT",
        "REVIEWER_2_TRANSPORT",
        "TRADER_HERMES_PYTHON",
        "TRADER_HERMES_HOME",
    } <= names


def test_both_yaml_files_parse() -> None:
    assert isinstance(_load_yaml("config.yaml"), dict)
    assert isinstance(_load_yaml("profile_routes.example.yaml"), dict)


def test_config_uses_eastern_time_and_anthropic_without_fallback(config: dict[str, Any]) -> None:
    assert config["timezone"] == "America/New_York"
    assert config["model"]["provider"] == "anthropic"
    assert config["model"]["default"] == "<ANTHROPIC_MODEL>"
    assert "fallback_providers" not in config
    assert "fallback_model" not in config


def test_trader_broker_runs_serve_through_uv(trader_broker: dict[str, Any]) -> None:
    assert trader_broker["command"] == "uv"
    assert trader_broker["args"] == [
        "run",
        "--directory",
        "<INSTALL_DIR>",
        "trader-broker",
        "serve",
    ]


def test_trader_broker_is_trusted_with_a_300_second_timeout(
    trader_broker: dict[str, Any],
) -> None:
    assert trader_broker["trust"] == "full"
    assert trader_broker["timeout"] == 300
    # A write tool waits for the account lock, then may run a buy whose two reviewers
    # each get a retry; the agent must get the answer before Hermes gives up on the call.
    defaults = Settings()
    longest_call_s = defaults.account_lock_wait_s + 2 * defaults.reviewer_timeout_s
    assert longest_call_s < trader_broker["timeout"]


def test_trader_broker_env_passes_through_every_setting(trader_broker: dict[str, Any]) -> None:
    env = trader_broker["env"]

    assert set(env) == _settings_env_names()
    assert all(value == f"${{{name}}}" for name, value in env.items())


def test_toolsets_allow_only_trader_broker_web_x_search_memory_and_skills(
    config: dict[str, Any],
) -> None:
    platforms = config["platform_toolsets"]

    assert {"cli", "cron", "discord", "telegram"} <= set(platforms)
    for toolsets in platforms.values():
        assert set(toolsets) == {"trader_broker", "web", "x_search", "memory", "skills"}


def test_terminal_code_file_and_browser_toolsets_are_disabled(config: dict[str, Any]) -> None:
    disabled = set(config["agent"]["disabled_toolsets"])

    assert {"terminal", "code_execution", "file", "browser"} <= disabled
    assert not disabled & {"trader_broker", "web", "x_search", "memory", "skills"}


def test_skill_learning_is_autonomous_and_memory_is_on(config: dict[str, Any]) -> None:
    assert config["skills"]["write_approval"] is False
    assert config["memory"]["memory_enabled"] is True


def test_no_plugin_loads(config: dict[str, Any]) -> None:
    assert config["plugins"]["enabled"] == []


def test_soul_states_tool_enforced_sizing_and_earnings() -> None:
    soul = (PROFILE_DIR / "SOUL.md").read_text().lower()

    assert "earnings" in soul
    assert "never choose a share count" in soul
    assert "sizes every order and enforces every cap" in soul


def test_env_example_names_every_setting_and_defaults_to_paper() -> None:
    assignments = _env_example()

    assert _settings_env_names() <= set(assignments)
    assert {"ANTHROPIC_API_KEY", "TRADER_DISCORD_TARGET", "TRADER_TELEGRAM_TARGET"} <= set(
        assignments
    )
    assert assignments["TRADER_BROKER_MODE"] == "paper"
    assert assignments["TRADER_BROKER_LIVE_ACK"] == ""


def test_env_example_defaults_match_settings_defaults() -> None:
    """Values the template fills in load to the server's own defaults."""
    filled = {name: value for name, value in _env_example().items() if value}

    assert Settings.from_env(filled) == Settings.from_env({})


def test_every_route_maps_to_the_trader_profile() -> None:
    routes = _load_yaml("profile_routes.example.yaml")["gateway"]["profile_routes"]

    assert {route["platform"] for route in routes} == {"discord", "telegram"}
    assert all(route["profile"] == PROFILE_NAME for route in routes)
    assert all(route["chat_id"] for route in routes)
    discord = next(route for route in routes if route["platform"] == "discord")
    assert discord["guild_id"]


# Skills and cron jobs

SKILL_NAMES = {
    "market-regime",
    "candidate-screen",
    "trade-analysis",
    "position-review",
    "journal-review",
    "portfolio-bootstrap",
}
TRADER_BROKER_TOOLSET = "mcp-trader_broker"
"""Hermes registers an MCP server's tools under the toolset `mcp-<server name>`, and skill
conditions such as `requires_toolsets` are checked against that name."""
TOOL_REFERENCE = re.compile(r"mcp__trader_broker__(\w+)")
"""A trader-broker tool as the model sees it: `mcp__<server>__<tool>` (Hermes
v2026.9.24 `tools/mcp_tool_schema.py`, `mcp_prefixed_tool_name`)."""
ANY_TOOL_NAME_FORM = re.compile(r"\bmcp_+trader[-_]broker_+([a-z]\w*)")
"""A trader-broker tool name in any spelling, such as `mcp_trader_broker_get_quote`."""

GATE_SCRIPT = "market-open-gate.sh"
JOB_TIMES = {
    # name: (minute, hour, day of week)
    "premarket-plan": ("0", "9", "1-5"),
    "entries": ("45", "9", "1-5"),
    "midday": ("30", "12", "1-5"),
    "close-review": ("45", "15", "1-5"),
    "weekly-report": ("30", "16", "5"),
}
GATED_JOBS = {"premarket-plan", "entries", "midday", "close-review"}


def _skill(name: str) -> tuple[dict[str, Any], str]:
    """The frontmatter and body of profile skill `name`."""
    text = (PROFILE_DIR / "skills" / "trading" / name / "SKILL.md").read_text()
    _, frontmatter, body = text.split("---\n", 2)
    return yaml.safe_load(frontmatter), body


@pytest.fixture
def server_tool_names(trader_service: TraderService) -> set[str]:
    server = build_server(trader_service)
    return {tool.name for tool in asyncio.run(server.list_tools())}


@pytest.fixture
def jobs() -> dict[str, dict[str, Any]]:
    return {job["name"]: job for job in _load_yaml("cron/jobs.yaml")["jobs"]}


def test_skill_directories_are_exactly_the_six_skills() -> None:
    skill_dirs = {path.parent.name for path in PROFILE_DIR.glob("skills/trading/*/SKILL.md")}

    assert skill_dirs == SKILL_NAMES


@pytest.mark.parametrize("name", sorted(SKILL_NAMES))
def test_skill_frontmatter_has_every_required_key(name: str) -> None:
    frontmatter, _ = _skill(name)

    assert frontmatter["name"] == name
    assert 0 < len(frontmatter["description"]) <= 1024
    assert frontmatter["version"]
    hermes = frontmatter["metadata"]["hermes"]
    assert hermes["category"] == "trading"
    assert hermes["requires_toolsets"] == [TRADER_BROKER_TOOLSET]


@pytest.mark.parametrize("name", sorted(SKILL_NAMES))
def test_skill_names_only_server_tools(name: str, server_tool_names: set[str]) -> None:
    _, body = _skill(name)
    named_tools = set(TOOL_REFERENCE.findall(body))

    # Bootstrap is a pacing policy that delegates to the other trading skills.
    if name != "portfolio-bootstrap":
        assert named_tools
    assert named_tools <= server_tool_names


def test_trade_analysis_states_earnings_and_proposes_without_a_share_count() -> None:
    _, body = _skill("trade-analysis")

    assert "web_search" in body
    assert "Next earnings:" in body
    assert "mcp__trader_broker__propose_buy" in body
    assert "qty" not in body


def test_journal_review_records_lessons_in_journal_and_memory() -> None:
    _, body = _skill("journal-review")

    assert 'mcp__trader_broker__get_journal` with `status="pending_lessons"' in body
    assert "mcp__trader_broker__journal_note" in body
    assert "`memory` tool" in body


def test_journal_review_learns_from_evidence_without_weakening_broker_protections() -> None:
    _, body = _skill("journal-review")

    assert "`skill_manage`" in body
    assert "repeated" in body
    assert "tentative" in body
    assert "journal entry IDs" in body
    assert "Do not loosen broker protections" in body
    assert "circumvent a rejection" in body
    assert "Skill changes" in body
    assert "No skill changes" in body
    assert "not required on every run" in body


def test_jobs_are_exactly_the_five_in_the_profile_timezone(
    jobs: dict[str, dict[str, Any]], config: dict[str, Any]
) -> None:
    assert set(jobs) == set(JOB_TIMES)
    assert _load_yaml("cron/jobs.yaml")["timezone"] == config["timezone"] == "America/New_York"


@pytest.mark.parametrize("name", sorted(JOB_TIMES))
def test_job_schedule(name: str, jobs: dict[str, dict[str, Any]]) -> None:
    minute, hour, days = JOB_TIMES[name]

    assert jobs[name]["schedule"].split() == [minute, hour, "*", "*", days]


def test_gate_script_runs_before_exactly_the_market_day_jobs(
    jobs: dict[str, dict[str, Any]],
) -> None:
    gated = {name for name, job in jobs.items() if job.get("script") == GATE_SCRIPT}

    assert gated == GATED_JOBS
    assert all(job.get("script") in (None, GATE_SCRIPT) for job in jobs.values())


@pytest.mark.parametrize("name", sorted(JOB_TIMES))
def test_job_loads_known_skills_its_prompt_names(
    name: str, jobs: dict[str, dict[str, Any]]
) -> None:
    job = jobs[name]

    assert job["skills"]
    assert set(job["skills"]) <= SKILL_NAMES
    assert all(skill in job["prompt"] for skill in job["skills"])


@pytest.mark.parametrize("name", sorted(JOB_TIMES))
def test_job_delivers_to_both_env_targets(name: str, jobs: dict[str, dict[str, Any]]) -> None:
    assert jobs[name]["deliver"] == ["${TRADER_DISCORD_TARGET}", "${TRADER_TELEGRAM_TARGET}"]


@pytest.mark.parametrize("name", sorted(JOB_TIMES))
def test_job_prompt_names_only_server_tools(
    name: str, jobs: dict[str, dict[str, Any]], server_tool_names: set[str]
) -> None:
    assert set(TOOL_REFERENCE.findall(jobs[name]["prompt"])) <= server_tool_names


def test_premarket_plan_never_proposes_a_buy(jobs: dict[str, dict[str, Any]]) -> None:
    job = jobs["premarket-plan"]

    assert "trade-analysis" not in job["skills"]
    assert "Do not call mcp__trader_broker__propose_buy" in job["prompt"]


def test_close_review_checks_the_clock_runs_housekeeping_and_settles_the_journal(
    jobs: dict[str, dict[str, Any]],
) -> None:
    prompt = jobs["close-review"]["prompt"]

    assert "Call mcp__trader_broker__get_clock first" in prompt
    assert "Call mcp__trader_broker__get_risk_status" in prompt
    assert '"pending_lessons"' in prompt
    assert "mcp__trader_broker__journal_note" in prompt
    assert "journal-review" in jobs["close-review"]["skills"]


# Installer and market gate scripts

SCRIPTS_DIR = PROFILE_DIR.parent / "scripts"
INSTALLER = SCRIPTS_DIR / "install-profile.sh"
GATE = SCRIPTS_DIR / "market-open-gate.sh"
MODEL = "claude-test-model"

HERMES_STUB = """#!{python}
# Records each call and does what Hermes v2026.9.24 does on disk: `profile create` builds the
# profile directory as hermes_cli/profiles.py `_finish_profile_layout` does (placeholder .env
# and default SOUL.md, plus the opt-out marker for --no-skills), and `cron create` and
# `cron edit` keep the profile's cron/jobs.json the way cron/jobs.py stores jobs.
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
with open(Path(os.environ["STUB_LOG"], "hermes.calls"), "a") as log:
    log.write("".join(arg + "\\0" for arg in args) + "\\n")
profiles = Path(os.environ["HOME"], ".hermes", "profiles")

def options(rest):
    found = {{}}
    for flag, value in zip(rest, rest[1:]):
        if flag.startswith("--"):
            found.setdefault(flag[2:], []).append(value)
    return found

if args[:2] == ["profile", "create"]:
    profile = profiles / args[2]
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: {{default: hermes-default}}\\n")
    (profile / ".env").write_text({placeholder_env!r})
    (profile / ".env").chmod(0o600)
    (profile / "SOUL.md").write_text("You are Hermes, a helpful assistant.\\n")
    if "--no-skills" in args:
        (profile / ".no-bundled-skills").write_text("opted out\\n")
elif args[:1] == ["-p"] and args[2:4] in (["cron", "create"], ["cron", "edit"]):
    store = profiles / args[1] / "cron" / "jobs.json"
    jobs = json.loads(store.read_text())["jobs"] if store.exists() else []
    if args[3] == "create":
        schedule, prompt, *rest = args[4:]
        found = options(rest)
        jobs.append({{
            "id": f"job{{len(jobs) + 1}}",
            "name": found["name"][0],
            "prompt": prompt.strip(),
            "skills": found.get("skill", []),
            "script": found.get("script", [None])[0],
            "deliver": found.get("deliver", [None])[0],
            "schedule": {{"kind": "cron", "expr": schedule, "display": schedule}},
            "schedule_display": schedule,
            "state": "paused" if "--paused" in rest else "scheduled",
        }})
    else:
        job = next(job for job in jobs if job["id"] == args[4])
        found = options(args[5:])
        if "schedule" in found:
            job["schedule"] = {{"kind": "cron", "expr": found["schedule"][0]}}
        if "prompt" in found:
            job["prompt"] = found["prompt"][0].strip()
        if "skill" in found:
            job["skills"] = found["skill"]
        if "script" in found:
            job["script"] = found["script"][0] or None
        if "deliver" in found:
            job["deliver"] = found["deliver"][0]
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({{"jobs": jobs}}))
"""
PLACEHOLDER_ENV = (
    "# Per-profile secrets for this Hermes profile.\n"
    "# API keys and tokens set here override the shell environment.\n"
    "# Behavioral settings belong in config.yaml, not here.\n"
)
"""The comment-only .env `hermes profile create` seeds (hermes_cli/profiles.py
`_PLACEHOLDER_ENV`)."""

UV_STUB = """#!/usr/bin/env bash
# `uv run ... python` runs the test's Python; `uv run ... trader-broker market-open-gate`
# prints what it saw and a canned answer; anything else is only recorded.
set -euo pipefail
echo "$*" >>"$STUB_LOG/uv.calls"
if [[ $1 == run ]]; then
    shift
    directory=""
    if [[ $1 == --directory ]]; then directory=$2; shift 2; fi
    if [[ $1 == python ]]; then shift; exec "{python}" "$@"; fi
    if [[ $1 == trader-broker ]]; then
        echo "dir=$directory mode=${{TRADER_BROKER_MODE:-unset}}"
        echo '{{"wakeAgent": false}}'
    fi
fi
"""


@pytest.fixture
def stub_env(tmp_path: Path) -> dict[str, str]:
    """An environment with stub `hermes` and `uv` first on PATH and an empty temp HOME."""
    bin_dir, home, log = tmp_path / "bin", tmp_path / "home", tmp_path / "log"
    for directory in (bin_dir, home, log):
        directory.mkdir()
    stubs = {
        "hermes": HERMES_STUB.format(python=sys.executable, placeholder_env=PLACEHOLDER_ENV),
        "uv": UV_STUB.format(python=sys.executable),
    }
    for name, text in stubs.items():
        stub = bin_dir / name
        stub.write_text(text)
        stub.chmod(0o755)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "STUB_LOG": str(log),
    }


def _run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True, text=True, check=False
    )


def _install(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    result = _run(INSTALLER, env, "--model", MODEL, *args)
    assert result.returncode == 0, result.stderr
    return result


def _hermes_calls(env: dict[str, str]) -> list[list[str]]:
    calls = Path(env["STUB_LOG"], "hermes.calls")
    if not calls.exists():
        return []
    return [record.split("\0") for record in calls.read_text().split("\0\n") if record]


def _cron_calls(env: dict[str, str], command: str) -> list[list[str]]:
    """The arguments after `hermes -p <profile> cron <command>` of every such call."""
    return [call[4:] for call in _hermes_calls(env) if call[2:4] == ["cron", command]]


def _cron_creates(env: dict[str, str]) -> list[list[str]]:
    return _cron_calls(env, "create")


def _stored_jobs(env: dict[str, str]) -> dict[str, dict[str, Any]]:
    """The profile's jobs as the hermes stub stored them, by name."""
    store = Path(env["HOME"], ".hermes", "profiles", PROFILE_NAME, "cron", "jobs.json")
    return {job["name"]: job for job in json.loads(store.read_text())["jobs"]}


def _files_under(directory: Path) -> set[Path]:
    return {path for path in directory.rglob("*") if path.is_file()}


@pytest.mark.parametrize("script", [INSTALLER, GATE], ids=lambda path: path.name)
def test_script_is_executable_strict_bash(script: Path) -> None:
    lines = script.read_text().splitlines()

    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0
    assert lines[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in lines
    assert next(line for line in lines if line and not line.startswith("#")) == (
        "set -euo pipefail"
    )
    assert os.access(script, os.X_OK)


def test_dry_run_prints_the_plan_and_writes_nothing(stub_env: dict[str, str]) -> None:
    home = Path(stub_env["HOME"])

    result = _run(INSTALLER, stub_env, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "uv sync" in result.stdout
    assert "hermes profile create stock-trader" in result.stdout
    assert result.stdout.count("hermes -p stock-trader cron create") == len(JOB_TIMES)
    assert _files_under(home) == set()
    assert _hermes_calls(stub_env) == []


def test_install_without_a_model_is_refused(stub_env: dict[str, str]) -> None:
    result = _run(INSTALLER, stub_env)

    assert result.returncode != 0
    assert "--model" in result.stderr
    assert _files_under(Path(stub_env["HOME"])) == set()


def test_install_creates_the_profile_files(stub_env: dict[str, str]) -> None:
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)

    _install(stub_env)

    config_text = (profile / "config.yaml").read_text()
    assert "<INSTALL_DIR>" not in config_text and "<ANTHROPIC_MODEL>" not in config_text
    config = yaml.safe_load(config_text)
    assert config["model"]["default"] == MODEL
    assert config["mcp_servers"]["trader_broker"]["args"][2] == str(PROFILE_DIR.parent)
    assert list(profile.glob("config.yaml.*.bak"))
    assert list(profile.glob("SOUL.md.*.bak"))
    assert (profile / "SOUL.md").read_text() == (PROFILE_DIR / "SOUL.md").read_text()
    for name in SKILL_NAMES:
        skill = Path("skills", "trading", name, "SKILL.md")
        assert (profile / skill).read_text() == (PROFILE_DIR / skill).read_text()
    gate = profile / "scripts" / GATE_SCRIPT
    assert os.access(gate, os.X_OK)
    assert f'INSTALL_DIR="{PROFILE_DIR.parent}"' in gate.read_text()


def test_jobs_are_created_paused_with_their_skills_script_and_name(
    stub_env: dict[str, str], jobs: dict[str, dict[str, Any]]
) -> None:
    _install(stub_env)

    creates = {args[args.index("--name") + 1]: args for args in _cron_creates(stub_env)}
    assert set(creates) == set(JOB_TIMES)
    for name, args in creates.items():
        assert args[0] == jobs[name]["schedule"]
        assert args[1] == jobs[name]["prompt"].strip()
        assert "--paused" in args
        skills = [args[i + 1] for i, arg in enumerate(args) if arg == "--skill"]
        assert skills == jobs[name]["skills"]
        assert ("--script" in args) == (name in GATED_JOBS)
        assert "--deliver" not in args  # both targets are empty in the fresh .env


def test_install_replaces_the_placeholder_env_hermes_seeds(stub_env: dict[str, str]) -> None:
    """Regression: `hermes profile create` seeds a comment-only .env, which used to keep the
    template from ever being installed."""
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)

    _install(stub_env)

    env_file = profile / ".env"
    assert env_file.read_text() == (PROFILE_DIR / ".env.example").read_text()
    assert env_file.stat().st_mode & 0o777 == 0o600
    (backup,) = profile.glob(".env.*.bak")
    assert backup.read_text() == PLACEHOLDER_ENV


def test_profile_is_created_without_bundled_skills(stub_env: dict[str, str]) -> None:
    _install(stub_env)

    (create,) = [call for call in _hermes_calls(stub_env) if call[:2] == ["profile", "create"]]
    assert create[2] == PROFILE_NAME
    assert "--no-skills" in create


def test_enable_jobs_creates_active_jobs_delivering_to_the_env_targets(
    stub_env: dict[str, str],
) -> None:
    env_file = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME, ".env")
    env_file.parent.mkdir(parents=True)
    env_file.write_text("TRADER_DISCORD_TARGET=discord:123\nTRADER_TELEGRAM_TARGET=\n")

    _install(stub_env, "--enable-jobs")

    creates = _cron_creates(stub_env)
    assert len(creates) == len(JOB_TIMES)
    for args in creates:
        assert "--paused" not in args
        assert args[args.index("--deliver") + 1] == "discord:123"


def test_second_install_keeps_env_and_creates_no_duplicate_jobs(
    stub_env: dict[str, str],
) -> None:
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)
    _install(stub_env)
    (profile / ".env").write_text("TRADER_BROKER_MODE=paper\n")
    backups = set(profile.glob("**/*.bak"))

    result = _install(stub_env)

    assert (profile / ".env").read_text() == "TRADER_BROKER_MODE=paper\n"
    assert len(_cron_creates(stub_env)) == len(JOB_TIMES)
    assert _cron_calls(stub_env, "edit") == []
    assert result.stdout.count("matches jobs.yaml") == len(JOB_TIMES)
    assert set(profile.glob("**/*.bak")) == backups
    assert [call[:2] for call in _hermes_calls(stub_env)].count(["profile", "create"]) == 1
    assert "profile_routes" in result.stdout


def test_reinstall_preserves_evolved_skills_and_seeds_missing_files(
    stub_env: dict[str, str],
) -> None:
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)
    _install(stub_env)
    skills = profile / "skills"
    evolved = skills / "trading/journal-review/SKILL.md"
    evolved.write_bytes(b"Local learning\r\n\x00")
    evolved.chmod(0o600)
    supporting = evolved.parent / "references/evidence.bin"
    supporting.parent.mkdir()
    supporting.write_bytes(b"\x00local evidence\xff")
    missing = skills / "trading/portfolio-bootstrap/SKILL.md"
    missing.unlink()
    before = _snapshot(skills)

    _install(stub_env)

    after = _snapshot(skills)
    assert {path: after[path] for path in before} == before
    assert (
        missing.read_bytes()
        == (PROFILE_DIR / "skills/trading/portfolio-bootstrap/SKILL.md").read_bytes()
    )
    assert not list(skills.rglob("*.bak"))


def test_refresh_skills_explicitly_replaces_with_a_backup(stub_env: dict[str, str]) -> None:
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)
    _install(stub_env)
    skill = profile / "skills/trading/journal-review/SKILL.md"
    skill.write_bytes(b"Learned locally\r\n")
    skill.chmod(0o600)

    _install(stub_env, "--refresh-skills")

    assert (
        skill.read_bytes() == (PROFILE_DIR / "skills/trading/journal-review/SKILL.md").read_bytes()
    )
    (backup,) = skill.parent.glob("SKILL.md.*.bak")
    assert backup.read_bytes() == b"Learned locally\r\n"
    assert backup.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("dry_run", [False, True], ids=["upgrade", "dry-run"])
@pytest.mark.parametrize("missing_soul", [False, True], ids=["evolved-soul", "missing-soul"])
def test_tooling_only_updates_tooling_without_writing_skills_or_soul(
    stub_env: dict[str, str],
    dry_run: bool,
    missing_soul: bool,
    jobs: dict[str, dict[str, Any]],
) -> None:
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)
    _install(stub_env)
    (profile / "SOUL.md").write_bytes(b"Evolved persona\r\n")
    skill = profile / "skills/trading/journal-review/SKILL.md"
    skill.write_bytes(b"Evolved skill\r\n")
    (profile / "skills/trading/market-regime/SKILL.md").unlink()
    for relative in (
        ".env",
        "memories/lessons.md",
        "sessions/history.jsonl",
        "pending/skills/test-proposal.json",
        "broker/paper/state.db",
        "unrelated.bin",
    ):
        path = profile / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"LOCAL_VALUE=preserved\n")
    # Tooling-only must also not seed a missing SOUL.
    if missing_soul:
        (profile / "SOUL.md").unlink()
    (profile / "config.yaml").write_text("old: config\n")
    gate = profile / "scripts" / GATE_SCRIPT
    gate.write_text("old gate\n")
    store = profile / "cron/jobs.json"
    stored = json.loads(store.read_text())
    for index, job in enumerate(stored["jobs"]):
        job.update(prompt="old prompt", state="paused" if index % 2 else "scheduled")
    store.write_text(json.dumps(stored))
    before = _snapshot(profile)
    calls_before = len(_hermes_calls(stub_env))
    uv_log = Path(stub_env["STUB_LOG"], "uv.calls")
    uv_before = uv_log.read_text()

    result = _install(stub_env, "--tooling-only", *(["--dry-run"] if dry_run else []))

    new_uv_calls = uv_log.read_text()[len(uv_before) :]
    if dry_run:
        assert "uv sync" in result.stdout
        assert "sync --directory" not in new_uv_calls
        assert _snapshot(profile) == before
        assert len(_hermes_calls(stub_env)) == calls_before
        return
    assert f"sync --directory {PROFILE_DIR.parent}\n" in new_uv_calls
    for path, contents in before.items():
        if path not in {profile / "config.yaml", gate, store}:
            assert (path.read_bytes(), path.stat().st_mode) == contents
    assert _snapshot(profile / "skills") == {
        path: contents
        for path, contents in before.items()
        if path.is_relative_to(profile / "skills")
    }
    assert (profile / "SOUL.md").exists() is not missing_soul
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["model"]["default"] == MODEL
    assert config["skills"]["write_approval"] is False
    assert config["mcp_servers"]["trader_broker"]["args"][2] == str(PROFILE_DIR.parent)
    assert f'INSTALL_DIR="{PROFILE_DIR.parent}"' in gate.read_text()
    assert os.access(gate, os.X_OK)
    for name, job in _stored_jobs(stub_env).items():
        original = next(item for item in stored["jobs"] if item["name"] == name)
        assert job["state"] == original["state"]
        assert job["prompt"] == jobs[name]["prompt"].strip()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_tooling_only_refuses_missing_profile_or_conflicting_refresh_before_changes(
    stub_env: dict[str, str], dry_run: bool, existing: bool
) -> None:
    home = Path(stub_env["HOME"])
    if existing:
        _install(stub_env)
    before = _snapshot(home)
    calls_before = len(_hermes_calls(stub_env))
    args = ["--tooling-only"]
    if existing:
        args.append("--refresh-skills")
    if dry_run:
        args.append("--dry-run")

    result = _run(INSTALLER, stub_env, "--model", MODEL, *args)

    assert result.returncode == 2
    assert ("conflict" if existing else "existing profile") in result.stderr
    assert _snapshot(home) == before
    assert len(_hermes_calls(stub_env)) == calls_before


def test_repeated_refreshes_do_not_overwrite_existing_backups(stub_env: dict[str, str]) -> None:
    date_stub = Path(stub_env["PATH"].split(os.pathsep)[0], "date")
    date_stub.write_text("#!/usr/bin/env bash\nprintf 'fixed-timestamp\\n'\n")
    date_stub.chmod(0o755)
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)
    _install(stub_env)
    skill = profile / "skills/trading/journal-review/SKILL.md"
    for version in (b"first lesson", b"second lesson"):
        skill.write_bytes(version)
        _install(stub_env, "--refresh-skills")
    assert {path.read_bytes() for path in skill.parent.glob("SKILL.md.*.bak")} == {
        b"first lesson",
        b"second lesson",
    }


def test_repository_supporting_skill_files_are_seeded_preserved_and_explicitly_refreshed(
    stub_env: dict[str, str], tmp_path: Path
) -> None:
    # Isolate source additions; never edit the real source tree or copy dotenv/swap files.
    checkout = tmp_path / "checkout"
    shutil.copytree(SCRIPTS_DIR, checkout / "scripts")
    for relative in ("skills", "cron"):
        shutil.copytree(PROFILE_DIR / relative, checkout / "profile" / relative)
    for name in ("config.yaml", "SOUL.md", ".env.example", "profile_routes.example.yaml"):
        shutil.copy2(PROFILE_DIR / name, checkout / "profile" / name)
    relative = Path("skills/trading/journal-review/references/evidence.bin")
    source = checkout / "profile" / relative
    source.parent.mkdir()
    source.write_bytes(b"repository evidence\x00")
    installer = checkout / "scripts/install-profile.sh"
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)
    installed = profile / relative
    assert _run(installer, stub_env, "--model", MODEL).returncode == 0
    assert installed.read_bytes() == source.read_bytes()
    installed.write_bytes(b"local evidence\xff")
    installed.chmod(0o600)
    before = _snapshot(profile / "skills")

    assert _run(installer, stub_env, "--model", MODEL).returncode == 0
    assert _snapshot(profile / "skills") == before

    assert _run(installer, stub_env, "--model", MODEL, "--refresh-skills").returncode == 0
    assert installed.read_bytes() == source.read_bytes()
    (backup,) = installed.parent.glob("evidence.bin.*.bak")
    assert backup.read_bytes() == b"local evidence\xff"


def test_reinstall_edits_a_job_that_differs_from_jobs_yaml_and_keeps_it_paused(
    stub_env: dict[str, str], jobs: dict[str, dict[str, Any]]
) -> None:
    _install(stub_env)
    store = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME, "cron", "jobs.json")
    stored = json.loads(store.read_text())
    midday = next(job for job in stored["jobs"] if job["name"] == "midday")
    midday.update(prompt="An old prompt.", skills=["position-review"], script=None)
    store.write_text(json.dumps(stored))

    result = _install(stub_env, "--enable-jobs")

    (edit,) = _cron_calls(stub_env, "edit")
    assert edit[0] == midday["id"]
    assert edit[edit.index("--prompt") + 1] == jobs["midday"]["prompt"].strip()
    assert [edit[i + 1] for i, arg in enumerate(edit) if arg == "--skill"] == jobs["midday"][
        "skills"
    ]
    assert edit[edit.index("--script") + 1] == GATE_SCRIPT
    assert "--schedule" not in edit and "--deliver" not in edit
    assert "midday differs from jobs.yaml in: prompt, skills, script" in result.stdout
    assert _stored_jobs(stub_env)["midday"]["state"] == "paused"


def test_reinstall_sets_targets_filled_in_after_the_first_install(
    stub_env: dict[str, str],
) -> None:
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)
    first = _install(stub_env)
    assert "TRADER_DISCORD_TARGET and TRADER_TELEGRAM_TARGET are empty" in first.stderr
    (profile / ".env").write_text(
        "TRADER_DISCORD_TARGET=discord:123\nTRADER_TELEGRAM_TARGET=telegram:-100\n"
    )

    second = _install(stub_env)

    edits = _cron_calls(stub_env, "edit")
    assert len(edits) == len(JOB_TIMES)
    assert all(edit[1:] == ["--deliver", "discord:123,telegram:-100"] for edit in edits)
    assert "are empty" not in second.stderr
    assert {job["deliver"] for job in _stored_jobs(stub_env).values()} == {
        "discord:123,telegram:-100"
    }


def test_reinstall_never_clears_targets_when_the_env_has_none(
    stub_env: dict[str, str],
) -> None:
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)
    profile.mkdir(parents=True)
    (profile / ".env").write_text("TRADER_DISCORD_TARGET=discord:123\n")
    _install(stub_env)
    (profile / ".env").write_text("TRADER_DISCORD_TARGET=\n")

    _install(stub_env)

    assert _cron_calls(stub_env, "edit") == []


@pytest.mark.parametrize(
    "model", ["bad model", "x: y", "claude-[1]", "claude:", "-claude", "{claude}"]
)
def test_install_refuses_a_model_id_that_is_not_a_plain_yaml_string(
    stub_env: dict[str, str], model: str
) -> None:
    result = _run(INSTALLER, stub_env, "--model", model)

    assert result.returncode == 2
    assert "invalid Claude model id" in result.stderr
    assert _files_under(Path(stub_env["HOME"])) == set()


def test_install_accepts_a_bedrock_style_model_id(stub_env: dict[str, str]) -> None:
    model = "us.anthropic.claude-test-v1:0"
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)

    result = _run(INSTALLER, stub_env, "--model", model)

    assert result.returncode == 0, result.stderr
    assert yaml.safe_load((profile / "config.yaml").read_text())["model"]["default"] == model


def test_install_refuses_a_checkout_path_with_unsafe_characters(
    stub_env: dict[str, str], tmp_path: Path
) -> None:
    checkout = tmp_path / "my checkout"
    (checkout / "scripts").mkdir(parents=True)
    installer = checkout / "scripts" / INSTALLER.name
    installer.write_text(INSTALLER.read_text())

    result = _run(installer, stub_env, "--model", MODEL)

    assert result.returncode == 2
    assert "unsupported checkout path" in result.stderr
    assert _hermes_calls(stub_env) == []


def test_no_run_writes_the_default_profile_config(stub_env: dict[str, str]) -> None:
    default_config = Path(stub_env["HOME"], ".hermes", "config.yaml")

    _run(INSTALLER, stub_env, "--dry-run")
    _install(stub_env)
    _install(stub_env, "--enable-jobs")

    assert not default_config.exists()


def test_gate_prints_the_trader_broker_answer_last(stub_env: dict[str, str]) -> None:
    result = _run(GATE, stub_env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == '{"wakeAgent": false}'


def test_installed_gate_loads_the_profile_env_and_uses_the_install_dir(
    stub_env: dict[str, str],
) -> None:
    profile = Path(stub_env["HOME"], ".hermes", "profiles", PROFILE_NAME)
    _install(stub_env)
    (profile / ".env").write_text('# trading mode\nTRADER_BROKER_MODE="paper"\n')

    result = _run(profile / "scripts" / GATE_SCRIPT, stub_env)

    assert result.returncode == 0, result.stderr
    assert f"dir={PROFILE_DIR.parent} mode=paper" in result.stdout
    assert result.stdout.splitlines()[-1] == '{"wakeAgent": false}'


def test_gate_wakes_the_agent_when_trader_broker_cannot_run(
    stub_env: dict[str, str],
) -> None:
    uv = Path(stub_env["PATH"].split(os.pathsep)[0], "uv")
    uv.write_text("#!/usr/bin/env bash\necho 'uv: broken' >&2\nexit 2\n")

    result = _run(GATE, stub_env)

    assert result.returncode == 0
    assert result.stdout.splitlines()[-1] == '{"wakeAgent": true}'


def _snapshot(directory: Path) -> dict[Path, tuple[bytes, int]]:
    return {path: (path.read_bytes(), path.stat().st_mode) for path in _files_under(directory)}


@pytest.mark.parametrize("flags", [(), ("--refresh-skills",)], ids=["preserve", "refresh"])
def test_dry_run_over_an_installed_profile_reports_changes_and_writes_nothing(
    stub_env: dict[str, str],
    flags: tuple[str, ...],
) -> None:
    home = Path(stub_env["HOME"])
    profile = home / ".hermes" / "profiles" / PROFILE_NAME
    _install(stub_env)
    (profile / "SOUL.md").write_text("Edited on the server by hand.\n")
    skill = profile / "skills/trading/journal-review/SKILL.md"
    skill.write_bytes(b"Local lessons\r\n")
    (profile / ".env").write_text("ALPACA_API_KEY=kept\n")
    before = _snapshot(home)
    calls_before = len(_hermes_calls(stub_env))

    result = _run(INSTALLER, stub_env, "--model", MODEL, "--dry-run", *flags)

    assert result.returncode == 0, result.stderr
    assert f"would back up and replace {profile / 'SOUL.md'}" in result.stdout
    if flags:
        assert f"would back up and replace {skill}" in result.stdout
    else:
        assert f"preserved installed skill file {skill}" in result.stdout
    assert "would create" not in result.stdout
    assert "profile stock-trader already exists" in result.stdout
    assert result.stdout.count("already exists") >= len(JOB_TIMES)
    assert "cron create" not in result.stdout
    assert _snapshot(home) == before
    assert _hermes_calls(stub_env)[calls_before:] == []


@pytest.mark.parametrize("flags", [(), ("--dry-run",)], ids=["install", "dry-run"])
def test_every_run_prints_the_routes_snippet_verbatim(
    stub_env: dict[str, str], flags: tuple[str, ...]
) -> None:
    result = _run(INSTALLER, stub_env, "--model", MODEL, *flags)

    assert result.returncode == 0, result.stderr
    assert (PROFILE_DIR / "profile_routes.example.yaml").read_text().rstrip("\n") in result.stdout


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "AGENTS.md",
        "docs/system/architecture.md",
        *sorted(
            str(path.relative_to(PROFILE_DIR.parent))
            for path in PROFILE_DIR.rglob("*")
            if path.suffix in {".md", ".yaml"}
        ),
    ],
)
def test_docs_and_profile_spell_tool_names_as_hermes_does(
    path: str, server_tool_names: set[str]
) -> None:
    text = (PROFILE_DIR.parent / path).read_text()

    for match in ANY_TOOL_NAME_FORM.finditer(text):
        assert match.group(0) == f"mcp__trader_broker__{match.group(1)}", match.group(0)
        assert match.group(1) in server_tool_names
