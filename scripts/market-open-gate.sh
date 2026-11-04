#!/usr/bin/env bash
# Pre-run gate for the stock-trader profile's market-day cron jobs.
#
# Hermes runs this before each gated job and reads its last stdout line: {"wakeAgent": false}
# skips the run (a weekend or market holiday), anything else runs it. The answer comes from
# `trader-broker market-open-gate`, whose output is passed through unchanged.
#
# Hermes resolves `script: market-open-gate.sh` only inside the profile's own scripts/
# directory, so scripts/install-profile.sh copies this file to
# ~/.hermes/profiles/stock-trader/scripts/ and fills in INSTALL_DIR below. Run from this
# repository instead, it uses the checkout it sits in.
#
# Hermes strips the profile's .env from a cron script's environment, so this script loads
# the profile's .env (the directory above its own) itself; trader-broker needs the broker
# keys from it.
#
# A broken gate must never silently skip a trading run: if trader-broker cannot run, this
# prints {"wakeAgent": true} and the server's own rules still apply.
set -euo pipefail

INSTALL_DIR="<INSTALL_DIR>"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Still the placeholder: this copy runs from the repository checkout.
if [[ $INSTALL_DIR == "<"*">" ]]; then
    INSTALL_DIR="$(dirname "$script_dir")"
fi
env_file="$(dirname "$script_dir")/.env"

# Export every NAME=value line of a dotenv file without executing it as shell code.
# Surrounding single or double quotes are removed from the value.
load_env_file() {
    local line name value
    while IFS= read -r line || [[ -n $line ]]; do
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line#export }"
        [[ $line =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
        name="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        if [[ $value =~ ^\"(.*)\"$ || $value =~ ^\'(.*)\'$ ]]; then
            value="${BASH_REMATCH[1]}"
        fi
        export "$name=$value"
    done <"$1"
}

if [[ -f $env_file ]] && ! load_env_file "$env_file"; then
    echo "market-open-gate.sh: could not read $env_file" >&2
fi

if ! uv run --directory "$INSTALL_DIR" trader-broker market-open-gate; then
    echo "market-open-gate.sh: trader-broker market-open-gate did not run; waking the agent anyway" >&2
    echo '{"wakeAgent": true}'
fi
