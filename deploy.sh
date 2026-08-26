#!/usr/bin/env bash
#
# Deploy helper for OverWallVpn bot.
#
# Interactive modes:
#   [m] MAIN     -> deploy origin/main into THIS directory (classic behaviour,
#                   service: overwallbot)
#   [b] BRANCH   -> deploy a selected git branch into an ISOLATED directory
#                   ../overwallbot-<branch> with its own venv / .env / database /
#                   web-panel port / systemd service (service: overwallbot-<branch>)
#                   so it can never interfere with the production install.
#
# Every finished install ends with a HEALTH CHECK: the script waits until the
# service is active AND the bot logged its successful startup; otherwise it
# dumps the recent journal and exits 1.
#
set -euo pipefail

MAIN_SVC="${SERVICE_NAME:-overwallbot}"
SUCCESS_MARKER="ربات با موفقیت استارت"   # logged by main.py once everything is up

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

log()  { echo "==> $*"; }
warn() { echo "⚠️  $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

ask() {  # ask PROMPT -> echoes answer; dies on EOF (non-interactive shells)
  local ans=""
  read -r -p "$1" ans || die "no interactive terminal available; cancel."
  printf '%s' "$ans"
}

# --------------------------------------------------------------- helpers ---
sanitize_ident() {
  # Make a branch name safe for directory / db / systemd-unit names.
  local id
  id=$(printf '%s' "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -E 's#[^a-z0-9._-]+#-#g; s/^[-.]+//; s/[-.]+$//' | cut -c1-38)
  printf '%s' "${id:-dev}"
}

get_env_value() {  # FILE KEY DEFAULT
  local val=""
  [ -f "$1" ] && val=$(grep -m1 -E "^${2}=" "$1" | cut -d= -f2- || true)
  printf '%s' "${val:-$3}"
}

set_env_key() {  # FILE KEY VALUE  (sed-free to survive arbitrary characters)
  local file="$1" key="$2" value="$3"
  grep -v -E "^${key}=" "$file" > "${file}.tmp" || true
  printf '%s=%s\n' "$key" "$value" >> "${file}.tmp"
  mv "${file}.tmp" "$file"
}

find_free_port() {  # PYTHON BASE_PORT -> free port on stdout (fallback: base)
  local py="$1" base="$2"
  "$py" - "$base" <<'PY'
import socket, sys
base = int(sys.argv[1])
for p in range(base + 1, base + 31):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", p)); s.close(); print(p); break
    except OSError:
        pass
else:
    print(base)
PY
}

prepare_python_env() {  # DIR -> sets VENV_PY global
  local dir="$1"
  log "Checking Python virtual environment in ${dir}..."
  if [ ! -x "${dir}/.venv/bin/python" ]; then
    log "Creating virtual environment..."
    rm -rf "${dir:?}/.venv"
    python3 -m venv "${dir}/.venv"
  fi
  VENV_PY="${dir}/.venv/bin/python"
  log "Installing dependencies..."
  "$VENV_PY" -m pip install --upgrade pip >/dev/null
  "$VENV_PY" -m pip install -r requirements.txt
}

compile_sources() {
  local dir="$1"
  log "Checking Python syntax..."
  ( cd "$dir" && .venv/bin/python -m py_compile \
      main.py db.py pricing.py rewards.py webpanel.py panel.py links.py config.py keyboards.py )
}

list_remote_branches() {
  # Prints raw remote head names. Non-zero exit code => origin unreachable.
  git ls-remote --heads origin 2>/dev/null \
    | awk '{print $2}' | sed 's#^refs/heads/##'
}

# ------------------------------------------------------ shared health check ---
# Waits until the service is active and has logged its successful startup.
health_check() {
  local svc="$1" dir="$2"
  log "Running health check for ${svc} (up to ~60s)..."

  local i state ready=0
  for i in $(seq 1 30); do
    state=$($SUDO systemctl is-active "$svc" 2>/dev/null || true)
    if [ "$state" != "active" ]; then
      echo
      warn "Service ${svc} is NOT active (state: ${state:-unknown})."
      break
    fi
    if $SUDO journalctl -u "$svc" --since "-120 seconds" --no-pager 2>/dev/null \
         | grep -qF "$SUCCESS_MARKER"; then
      ready=1
      break
    fi
    sleep 2
  done

  if [ "$ready" -eq 1 ]; then
    echo "======================================"
    echo " ✅ HEALTH CHECK PASSED — ${svc} is up"
    echo "======================================"
    echo "Version: $(git -C "$dir" log -1 --oneline --decorate)"
    echo "Logs:     journalctl -u ${svc} -f"
    echo "Restart:  systemctl restart ${svc}"
    return 0
  fi

  echo
  echo "--- Last 60 log lines ---------------------------------------"
  $SUDO journalctl -u "$svc" -n 60 --no-pager 2>/dev/null || true
  echo "--------------------------------------------------------------"
  cat >&2 <<'TIPS'
Possible causes checklist:
  • Database unreachable/missing   -> check DB_* values in .env, PostgreSQL running
  • Invalid BOT_TOKEN              -> double-check the token in .env
  • Panel URL wrong                -> PANEL_URL/PANEL_USER/PANEL_PASS in .env
  • Port already in use            -> WEB_PORT in .env
Fix, then re-run:  bash deploy.sh
TIPS
  die "health check failed for ${svc}"
}

# ------------------------------------------------------------ main branch ---
deploy_main() {
  local TARGET
  TARGET="$(cd "$(dirname "$0")" && pwd)"
  cd "$TARGET"

  echo "==> Working directory: $(pwd)"
  [ -d .git ] || die "this folder is not a git repository."

  # Protect local environment/config files from the hard reset below.
  if [ -f .env ]; then
    cp .env /tmp/overwallbot.env.backup
    echo "==> .env backed up."
  fi

  log "Fetching origin..."
  git fetch origin

  log "Switching to main..."
  # -B also works on fresh clones that do not have a local main branch yet.
  git checkout -B main origin/main
  echo "==> Current version:"
  git log -1 --oneline --decorate

  # Restore .env
  if [ -f /tmp/overwallbot.env.backup ]; then
    cp /tmp/overwallbot.env.backup .env
    rm -f /tmp/overwallbot.env.backup
    echo "==> .env restored."
  elif [ ! -f .env ]; then
    cp .env.example .env
    warn ".env created. Configure it before running the bot."
  fi

  prepare_python_env "$TARGET"
  compile_sources "$TARGET"

  if ! systemctl list-unit-files 2>/dev/null | grep -q "^${MAIN_SVC}.service"; then
    warn "Service ${MAIN_SVC} is not installed yet."
    echo "Run:  bash install_service.sh      (or start manually: bash run.sh)"
    return 0
  fi

  log "Restarting ${MAIN_SVC}..."
  $SUDO systemctl restart "$MAIN_SVC"
  health_check "$MAIN_SVC" "$TARGET"
}

# ----------------------------------------------------------- branch deploy ---
try_create_branch_db() {
  # Best effort: production data must never be touched by a branch install.
  local dbname="$1" dbuser="$2" dbpass="$3"
  command -v psql >/dev/null 2>&1 || {
    warn "psql not found; create DB '${dbname}' manually (README step 3)."
    return 0
  }
  log "Ensuring database '${dbname}' exists (best effort)..."
  if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${dbuser}'" 2>/dev/null | grep -q 1; then
    sudo -u postgres psql -c "CREATE USER ${dbuser} WITH PASSWORD '${dbpass}';" >/dev/null 2>&1 \
      || sudo -u postgres psql -c "ALTER USER ${dbuser} WITH PASSWORD '${dbpass}';" >/dev/null 2>&1 \
      || warn "Could not create DB role '${dbuser}' (needs root/postgres access)."
  fi
  sudo -u postgres psql -c "CREATE DATABASE ${dbname} OWNER ${dbuser};" >/dev/null 2>&1 \
    || warn "Could not create DB '${dbname}' (maybe it already exists / no rights). Create it manually."
}

seed_branch_env() {
  # Copies the production .env into the branch copy and isolates its identity.
  local src_env="$1" dst_dir="$2" san="$3"
  local dst_env="${dst_dir}/.env"

  if [ -f "$dst_env" ]; then
    echo "==> Keeping existing ${dst_env} (branch already configured)."
    return 0
  fi

  if [ -f "$src_env" ]; then
    cp "$src_env" "$dst_env"
  else
    warn "Production .env not found; creating a minimal one."
    [ -f "${dst_dir}/.env.example" ] || die "cannot seed .env: copy your production .env manually to ${dst_env} and re-run."
    cp "${dst_dir}/.env.example" "$dst_env"
  fi

  # 1) dedicated database name  -> branch can never write into production data
  set_env_key "$dst_env" DB_NAME "overwall_${san}"

  # 2) separate bot token (strongly recommended!)
  echo
  warn "Telegram rule: TWO installs polling with the SAME token produce"
  echo "    'Conflict: terminated by other getUpdates' and random failures."
  local tok_new=""
  tok_new=$(ask "Enter a SEPARATE bot token for this branch (blank = risky, keep production token): ")
  if [ -n "$tok_new" ]; then
    set_env_key "$dst_env" BOT_TOKEN "$tok_new"
  else
    warn "Keeping the PRODUCTION bot token in $(basename "$dst_dir")/.env — expect conflicts if both bots run."
  fi
  echo "==> .env seeded for the branch."
}

deploy_branch() {
  local branch="$1"
  local san target src_dir src_env svc
  san=$(sanitize_ident "$branch")
  svc="overwallbot-${san}"
  src_dir="$(cd "$(dirname "$0")" && pwd)"
  target="$(dirname "$src_dir")/overwallbot-${san}"
  src_env="${SRC_ENV_FILE:-${src_dir}/.env}"

  echo "==> Branch       : ${branch}"
  echo "==> Install dir  : ${target}"
  echo "==> Service name : ${svc}"

  if [ ! -d "$target/.git" ]; then
    log "Cloning origin/${branch} into ${target} ..."
    git clone --branch "$branch" origin "$target"
  else
    log "Existing branch install found; updating it..."
    git -C "$target" fetch origin
    git -C "$target" checkout "$branch" >/dev/null 2>&1 || git -C "$target" checkout "origin/${branch}"
    git -C "$target" reset --hard "origin/${branch}"
  fi

  seed_branch_env "$src_env" "$target" "$san"
  prepare_python_env "$target"
  compile_sources "$target"

  # Dedicated web panel port (once; afterwards respect manual edits).
  local conf="${target}/.env" webport newport
  if ! grep -q "^WEB_PORT_BRANCHED_OK=" "$conf" 2>/dev/null; then
    webport=$(get_env_value "$conf" WEB_PORT 8080)
    newport=$(find_free_port "$VENV_PY" "$webport")
    if [ "$newport" != "$webport" ]; then
      set_env_key "$conf" WEB_PORT "$newport"
      echo "==> Web panel port moved to ${newport} (keeps distance from main)."
    fi
    echo "WEB_PORT_BRANCHED_OK=1" >> "$conf"
  fi

  # Dedicated database (best effort).
  try_create_branch_db \
    "overwall_${san}" \
    "$(get_env_value "$conf" DB_USER overwall_user)" \
    "$(get_env_value "$conf" DB_PASS '')"

  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemd not available here; start the branch manually:"
    echo "  cd ${target} && bash run.sh"
    return 0
  fi

  # Install the service FROM THIS BRANCH COPY so WorkingDirectory / ExecStart
  # point at ${target} and never at the production directory.
  [ -f "${target}/install_service.sh" ] \
    || cp "${src_dir}/install_service.sh" "${target}/install_service.sh"

  log "Installing service ${svc} ..."
  if ! INSTALL_DIR="$target" SERVICE_NAME="$svc" bash "${target}/install_service.sh"; then
    warn "Service installation failed; you can still run it manually:"
    echo "  cd ${target} && bash run.sh"
    return 0
  fi

  health_check "$svc" "$target"
}

# ------------------------------------------------------------------- menu ---
# NOTE: prints go to real stdout; the chosen mode is returned via $MODE.
# (Do NOT call this inside $(...) — command substitution would swallow the
#  entire menu and the user would only see a silent script.)
MODE=""
choose_mode() {
  echo
  echo "======================================================"
  echo "  OverWall Bot — Deploy"
  echo "======================================================"
  echo "  What do you want to install/update?"
  echo "    [m] Main project  (production · origin/main · service: ${MAIN_SVC})"
  echo "    [b] A git branch  (isolated copy: own folder/db/port/service)"
  echo
  while true; do
    local ans
    ans=$(ask "Choice [m/b/q]: ")
    case "$ans" in
      m|M|"")  MODE="main";   return 0 ;;
      b|B)     MODE="branch"; return 0 ;;
      q|Q)     die "cancelled by user." ;;
      *)       echo "Please answer m, b or q." ;;
    esac
  done
}

main() {
  local idx n branch
  choose_mode

  if [ "$MODE" = "main" ]; then
    deploy_main
    return 0
  fi

  git fetch origin --prune >/dev/null 2>&1 || true
  local b_raw=""
  if ! b_raw="$(list_remote_branches)"; then
    die "cannot list branches from origin (network/credentials problem)."
  fi
  mapfile -t B_LIST < <(printf '%s\n' "$b_raw" | sort -u | grep -Ev '^(main|master)$' || true)
  [ "${#B_LIST[@]}" -gt 0 ] || die "no selectable feature branches found on origin."

  echo
  echo "Available branches:"
  n=0
  local b
  for b in "${B_LIST[@]}"; do
    n=$((n + 1)); echo "  [${n}] ${b}"
  done

  idx=$(ask "Branch number to install (blank/q=cancel): ")
  if ! [[ "$idx" =~ ^[1-9][0-9]*$ ]] || (( idx > n )); then
    die "invalid choice."
  fi
  branch="${B_LIST[$((idx - 1))]}"

  deploy_branch "$branch"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
