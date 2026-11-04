#!/usr/bin/env bash
# Install or update the Hermes `stock-trader` profile from this repository.
#
# Usage: scripts/install-profile.sh [--model CLAUDE_MODEL_ID] [--enable-jobs]
#                                  [--refresh-skills | --tooling-only] [--dry-run]
#
#   --model ID     Claude model id written into the profile's config.yaml (model.default).
#                  Defaults to $ANTHROPIC_MODEL. Required unless --dry-run.
#   --enable-jobs  Create missing scheduled jobs active. By default they are created paused.
#                  Existing jobs keep their paused or active state either way.
#   --refresh-skills Replace differing repository skill files, with unique backups.
#                  Default: seed missing skill files; preserve existing files byte-for-byte.
#   --tooling-only Update dependencies, config, MCP, gate script and jobs for an existing
#                  profile; never write skills or SOUL.md, even if missing. Conflicts with
#                  --refresh-skills. Config is replaced, not merged; still requires --model.
#   --dry-run      Print what would change and the commands that would run; change nothing
#                  under ~/.hermes. (uv may still create this checkout's .venv to read
#                  profile/cron/jobs.yaml.)
#
# Safe to re-run: installed skills are the learning source of truth, including supporting
# files. Existing skill files are preserved unless --refresh-skills is explicit. Other
# managed files are backed up with a unique timestamp suffix before replacement. The
# profile's .env is written from .env.example only while it has no NAME=value line (absent,
# or the comment-only placeholder `hermes profile create` seeds); a .env with settings is
# never overwritten. A scheduled job is created only if no job with its name exists; an
# existing job whose schedule, prompt, skills, script or delivery targets differ from
# jobs.yaml is updated with `hermes cron edit`, which keeps it paused or active.
#
# The default Hermes profile (~/.hermes/config.yaml) is never written. The routes that let
# the shared Discord and Telegram bots reach this profile are printed at the end for the
# operator to merge by hand.
set -euo pipefail

PROFILE_NAME="stock-trader"
PROFILE_DESCRIPTION="Swing-trades US stocks through the trader-broker MCP server, which sizes every order and enforces the risk limits."

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="$INSTALL_DIR/profile"
PROFILE_DIR="$HOME/.hermes/profiles/$PROFILE_NAME"

usage() {
    sed -n '/^# Usage:/,/^# Safe to re-run:/{ /^# Safe to re-run:/d; s/^# \{0,1\}//; p; }' "${BASH_SOURCE[0]}"
}

dry_run=0
enable_jobs=0
refresh_skills=0
tooling_only=0
model="${ANTHROPIC_MODEL:-}"
while (($# > 0)); do
    case $1 in
        --dry-run) dry_run=1 ;;
        --enable-jobs) enable_jobs=1 ;;
        --refresh-skills) refresh_skills=1 ;;
        --tooling-only) tooling_only=1 ;;
        --model)
            [[ $# -ge 2 ]] || { echo "--model needs a value" >&2; exit 2; }
            model=$2
            shift
            ;;
        --model=*) model=${1#--model=} ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

# Run a command that changes something, or only print it under --dry-run.
run() {
    if ((dry_run)); then
        printf '[dry-run] would run:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

# Copy SRC to DEST with permission MODE, unless DEST already has the same content.
# An existing, different DEST is first kept as DEST.<timestamp>.<unique>.bak.
install_file() {
    local src=$1 dest=$2 mode=$3 backup
    if [[ -f $dest ]] && cmp -s "$src" "$dest"; then
        return 0
    fi
    if ((dry_run)); then
        if [[ -e $dest ]]; then
            say "[dry-run] would back up and replace $dest"
        else
            say "[dry-run] would create $dest"
        fi
        return 0
    fi
    mkdir -p "$(dirname "$dest")"
    if [[ -e $dest ]]; then
        # Unique even when several upgrades run in the same second.
        backup="$(mktemp "$dest.$timestamp.XXXXXX.bak")"
        cp -p "$dest" "$backup"
        say "backed up $dest to $backup"
    fi
    cp "$src" "$dest"
    chmod "$mode" "$dest"
    say "installed $dest"
}

# Write SRC to DEST (as install_file does) with every <INSTALL_DIR> and <ANTHROPIC_MODEL>
# placeholder filled in. Both values are checked against a safe character set below.
install_rendered() {
    local src=$1 dest=$2 mode=$3 rendered content
    rendered="$staging/$(basename "$dest")"
    content="$(<"$src")"
    content="${content//"<INSTALL_DIR>"/"$INSTALL_DIR"}"
    content="${content//"<ANTHROPIC_MODEL>"/"$model"}"
    printf '%s\n' "$content" >"$rendered"
    install_file "$rendered" "$dest" "$mode"
}

# Succeed when dotenv file $1 has at least one NAME=value line.
has_assignments() {
    grep -Eq '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' "$1"
}

# Print each job in profile/cron/jobs.yaml as eight NUL-terminated fields:
#   name; the id of the profile's existing job with that name ("" when there is none); the
#   comma-joined settings that existing job has differently (schedule, prompt, skills,
#   script, deliver); then jobs.yaml's schedule, prompt, pre-run script ("" for none),
#   comma-joined deliver targets and comma-joined skills.
# Deliver entries of the form ${VAR} are filled in from the profile's .env, and empty ones
# are dropped. When every target is empty, deliver is not compared, so an existing job's
# targets are never cleared.
# Existing jobs are read from the profile's cron store, cron/jobs.json, because
# `hermes cron list` does not print prompts (Hermes v2026.9.24 cron/jobs.py, create_job).
read_jobs() {
    uv run --directory "$INSTALL_DIR" python -c '
import json, re, sys
from pathlib import Path

import yaml

jobs_file, env_file, store_file = (Path(arg) for arg in sys.argv[1:4])
env = {}
if env_file.is_file():
    for line in env_file.read_text().splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_]\w*)=(.*)$", line)
        if match:
            env[match[1]] = match[2].strip().strip("\"'\''")

stored_jobs = {}
if store_file.is_file():
    for stored in json.loads(store_file.read_text()).get("jobs", []):
        stored_jobs.setdefault(stored.get("name"), stored)

def settings(stored):
    deliver = stored.get("deliver") or ""
    return {
        "schedule": (stored.get("schedule") or {}).get("expr") or stored.get("schedule_display"),
        "prompt": (stored.get("prompt") or "").strip(),
        "skills": ",".join(stored.get("skills") or []),
        "script": stored.get("script") or "",
        "deliver": deliver if isinstance(deliver, str) else ",".join(deliver),
    }

for job in yaml.safe_load(jobs_file.read_text())["jobs"]:
    targets = [re.sub(r"\$\{(\w+)\}", lambda m: env.get(m[1], ""), entry).strip()
               for entry in job.get("deliver", [])]
    wanted = {
        "schedule": job["schedule"],
        "prompt": job["prompt"].strip(),
        "skills": ",".join(job["skills"]),
        "script": job.get("script") or "",
        "deliver": ",".join(target for target in targets if target),
    }
    stored = stored_jobs.get(job["name"])
    differs = []
    if stored is not None:
        have = settings(stored)
        differs = [key for key, value in wanted.items()
                   if value != have[key] and (value or key != "deliver")]
    fields = [job["name"], stored["id"] if stored else "", ",".join(differs),
              wanted["schedule"], wanted["prompt"], wanted["script"], wanted["deliver"],
              wanted["skills"]]
    sys.stdout.write("".join(field + "\0" for field in fields))
' "$SOURCE_DIR/cron/jobs.yaml" "$PROFILE_DIR/.env" "$PROFILE_DIR/cron/jobs.json"
}

# --- Preflight ---------------------------------------------------------------------------

if ((tooling_only && refresh_skills)); then
    echo "--tooling-only and --refresh-skills conflict" >&2
    exit 2
fi
if ((tooling_only)) && [[ ! -d $PROFILE_DIR ]]; then
    echo "--tooling-only requires an existing profile: $PROFILE_DIR" >&2
    exit 2
fi

missing=()
for tool in hermes uv; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if ((${#missing[@]} > 0)); then
    echo "missing required commands: ${missing[*]}" >&2
    exit 1
fi

if [[ -z $model ]]; then
    if ((dry_run)); then
        warn "no Claude model given (--model or ANTHROPIC_MODEL); config.yaml would keep <ANTHROPIC_MODEL>"
        model="<ANTHROPIC_MODEL>"
    else
        echo "no Claude model given: pass --model <id> or set ANTHROPIC_MODEL" >&2
        exit 2
    fi
elif [[ ! $model =~ ^[A-Za-z][A-Za-z0-9._:/@+-]*[A-Za-z0-9]$ ]]; then
    # It is written unquoted into config.yaml; these characters keep it a plain YAML string.
    echo "invalid Claude model id: $model (use letters, digits and ._:/@+- only)" >&2
    exit 2
fi

# The checkout path is written into config.yaml (inside a YAML flow list) and into the gate
# script (inside a bash double-quoted string); these characters are literal in both.
if [[ ! $INSTALL_DIR =~ ^/[A-Za-z0-9._/+-]*$ ]]; then
    echo "unsupported checkout path: $INSTALL_DIR (use letters, digits and ._/+- only); clone the repository to such a path" >&2
    exit 2
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

say "Installing the $PROFILE_NAME profile from $INSTALL_DIR into $PROFILE_DIR"
if ((dry_run)); then
    say "(dry run: nothing under ~/.hermes will be changed)"
fi

# --- trader-broker ------------------------------------------------------------------------

run uv sync --directory "$INSTALL_DIR"

# --- Profile files ------------------------------------------------------------------------

if [[ -d $PROFILE_DIR ]]; then
    say "profile $PROFILE_NAME already exists"
else
    # --no-skills: the profile gets only its own trading skills, not Hermes's bundled ones.
    run hermes profile create "$PROFILE_NAME" --description "$PROFILE_DESCRIPTION" --no-skills
fi

install_rendered "$SOURCE_DIR/config.yaml" "$PROFILE_DIR/config.yaml" 644
if (( ! tooling_only )); then
    install_file "$SOURCE_DIR/SOUL.md" "$PROFILE_DIR/SOUL.md" 644
    while IFS= read -r -d '' skill_file; do
        destination="$PROFILE_DIR/skills/${skill_file#"$SOURCE_DIR/skills/"}"
        if (( ! refresh_skills )) && [[ -e $destination || -L $destination ]]; then
            say "preserved installed skill file $destination"
            continue
        fi
        install_file "$skill_file" "$destination" 644
    done < <(find "$SOURCE_DIR/skills" -type f -print0 | sort -z)
else
    say "tooling-only: skills and SOUL.md left unchanged (no seeding)"
fi
install_rendered "$INSTALL_DIR/scripts/market-open-gate.sh" \
    "$PROFILE_DIR/scripts/market-open-gate.sh" 755

# `hermes profile create` seeds a comment-only .env. Replace that one (keeping a backup),
# but never a .env that holds settings.
if [[ -f $PROFILE_DIR/.env ]] && has_assignments "$PROFILE_DIR/.env"; then
    say "$PROFILE_DIR/.env already has settings; left unchanged"
else
    install_file "$SOURCE_DIR/.env.example" "$PROFILE_DIR/.env" 600
fi

# --- Scheduled jobs -----------------------------------------------------------------------

# Read the jobs before the loop so a failure to read them stops the install.
read_jobs >"$staging/jobs"

create_args=()
undelivered_jobs=()
((enable_jobs)) || create_args+=(--paused --paused-reason "created by install-profile.sh; resume once the paper smoke test passes")

while IFS= read -r -d '' name && IFS= read -r -d '' job_id && IFS= read -r -d '' differs &&
    IFS= read -r -d '' schedule && IFS= read -r -d '' prompt && IFS= read -r -d '' script &&
    IFS= read -r -d '' deliver && IFS= read -r -d '' skills; do
    [[ -n $deliver ]] || undelivered_jobs+=("$name")
    skill_args=()
    IFS=',' read -r -a skill_names <<<"$skills"
    for skill in "${skill_names[@]}"; do
        skill_args+=(--skill "$skill")
    done

    if [[ -z $job_id ]]; then
        args=("$schedule" "$prompt" --name "$name" "${skill_args[@]}")
        [[ -z $script ]] || args+=(--script "$script")
        [[ -z $deliver ]] || args+=(--deliver "$deliver")
        run hermes -p "$PROFILE_NAME" cron create "${args[@]}" "${create_args[@]}"
        continue
    fi
    if [[ -z $differs ]]; then
        say "scheduled job $name already exists and matches jobs.yaml"
        continue
    fi

    say "scheduled job $name differs from jobs.yaml in: ${differs//,/, }"
    args=()
    IFS=',' read -r -a fields <<<"$differs"
    for field in "${fields[@]}"; do
        case $field in
            schedule) args+=(--schedule "$schedule") ;;
            prompt) args+=(--prompt "$prompt") ;;
            skills) args+=("${skill_args[@]}") ;;
            script) args+=(--script "$script") ;;
            deliver) args+=(--deliver "$deliver") ;;
        esac
    done
    run hermes -p "$PROFILE_NAME" cron edit "$job_id" "${args[@]}"
done <"$staging/jobs"

if ((${#undelivered_jobs[@]} > 0)); then
    warn "TRADER_DISCORD_TARGET and TRADER_TELEGRAM_TARGET are empty in $PROFILE_DIR/.env, so this run set no delivery target on: ${undelivered_jobs[*]}. Fill them in and re-run this installer; it sets them on the existing jobs."
fi

say ""
say "Note: Hermes cron has no per-job time zone. The schedules run in the profile's timezone"
say "(America/New_York, from config.yaml)."

# --- Next steps ---------------------------------------------------------------------------

cat <<EOF

Routes for the DEFAULT profile. This installer never edits ~/.hermes/config.yaml; merge this
into its gateway: section by hand, with your own channel and group ids:

$(cat "$SOURCE_DIR/profile_routes.example.yaml")

Next steps:
  1. Fill in $PROFILE_DIR/.env (broker keys, reviewer keys, ANTHROPIC_API_KEY, delivery
     targets), then re-run this installer so the jobs get the delivery targets.
  2. Create the state database and check the paper account end to end. trader-broker reads
     its settings from the environment, so load the profile's .env first:
       set -a; . $PROFILE_DIR/.env; set +a
       uv run --directory $INSTALL_DIR trader-broker init-state
       uv run --directory $INSTALL_DIR trader-broker smoke-test
  3. Merge the routes above into ~/.hermes/config.yaml and restart the gateway.
  4. If the jobs were created paused, resume each one: hermes -p $PROFILE_NAME cron list,
     then hermes -p $PROFILE_NAME cron resume <job id>
EOF
