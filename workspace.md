# Workspace: hermes-stock-trader

Default branch: main   Remote: robsgreen/hermes-stock-trader   GitHub: yes

Updated 2026-11-03 after spec `hermes-trader-profile` Task 1.1 (branch `sdlc/hermes-trader-profile`): Python 3.12 + uv project `trader-broker` (src layout, module `trader_broker`). Commands below come from `pyproject.toml` and `.github/workflows/ci.yml` on that branch.

## Capabilities

| Capability | Status |
|------------|--------|
| Install | ✅ `uv sync` |
| Build | ✅ none separate (pure Python; `uv sync` builds the package) |
| Type-check | ✅ `uv run mypy src` |
| Lint/Format | ✅ `uv run ruff check .` / `uv run ruff format --check .` |
| Unit Tests | ✅ `uv run pytest -m "not paper"` |
| Integration Tests | ⚠️ `uv run pytest -m paper` (needs user's Alpaca paper keys) |
| Local Dev | ⚠️ `uv run trader-broker serve` (after Task 4.3) |
| Logging | ⚠️ planned: JSON logs to stderr + rotating file |
| Metrics | ❌ Not established |

## Modules
- `.` — trader-broker MCP server + Hermes `stock-trader` profile files  (build: `uv sync`, test: `uv run pytest -m "not paper"`)

## Module Dependencies

No intra-repo dependencies — all modules are independent.

## Docs Layout
Only `README.md` at the root. No `AGENTS.md`, `CLAUDE.md`, `docs/system/`, or `docs/specs/` yet; follow the `doc-standards` layout (`AGENTS.md` + `CLAUDE.md` importing it, `docs/system/architecture.md`, `docs/system/modules/`, `docs/specs/YYYYMM/`) when they are created.

## Integration Tests
NOT SUPPORTED — no integration or end-to-end suite, docker-compose file, emulator, or env template exists.

## Local Development
NOT SUPPORTED — no dev scripts or run configuration exist.

## Observability
### Logging
NOT ESTABLISHED — no code exists.

### Metrics
NOT ESTABLISHED — no code exists.
