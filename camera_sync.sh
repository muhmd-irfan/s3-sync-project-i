#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/var/ftp/local"
BUCKET="<S3-BUCKET-NAME>"
PREFIX="cam"
LOG_DIR="/var/log/camera"
SYNC_LOG="${LOG_DIR}/sync.log"
ERR_LOG="${LOG_DIR}/error.log"
ALIVE_LOG="${LOG_DIR}/cron_alive.log"
LOCKFILE="/var/run/camera_sync.lock"

# Do not upload files still being written
MIN_AGE_SEC=120

# Kill aws s3 sync if it runs longer than 9 min (just under the 10 min cron window)
SYNC_TIMEOUT_SEC=540

log()        { echo "$(date -u '+%F %T') | $*"         >> "$SYNC_LOG"; }
err()        { echo "$(date -u '+%F %T') | ERROR | $*" >> "$ERR_LOG"; }
mark_alive() { echo "$(date -u '+%F %T') OK"           >> "$ALIVE_LOG"; }

sync_user() {
  local user="$1"
  local user_root="${BASE_DIR}/${user}"
  local spool_dir="${user_root}/.spool"

  # ── Recover files stranded in .spool/ from a previous crashed/timed-out run ─
  if [[ -d "$spool_dir" ]]; then
    while IFS= read -r -d '' f; do
      local rel="${f#${spool_dir}/}"
      local recover_target="${user_root}/${rel}"
      mkdir -p "$(dirname "$recover_target")"
      mv "$f" "$recover_target"
      log "${user} | Recovered from spool | ${rel}"
    done < <(find "$spool_dir" -type f -print0 2>/dev/null)
  fi

  # ── Stage eligible files into .spool/ ──────────────────────────────────────
  local -a staged=()
  while IFS= read -r -d '' f; do
    local rel="${f#${user_root}/}"

    # Skip internal dirs
    [[ "$rel" == .spool/*  ]] && continue
    [[ "$rel" == failed/*  ]] && continue

    local spool_target="${spool_dir}/${rel}"
    mkdir -p "$(dirname "$spool_target")"
    mv "$f" "$spool_target"
    staged+=("$rel")
  done < <(find "$user_root" -type f \
              \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.txt" \) \
              -mmin "+$((MIN_AGE_SEC / 60))" -print0 2>/dev/null)

  if [[ ${#staged[@]} -eq 0 ]]; then
    log "${user} | No eligible files"
    return 0
  fi

  log "${user} | Staged ${#staged[@]} file(s) → syncing to s3://${BUCKET}/${PREFIX}/${user}/"

  # ── Single sync call for the entire spool tree ─────────────────────────────
  # Temporarily disable set -e to capture the exact exit code.
  # exit 124 = timeout fired; any other non-zero = aws error.
  # In both cases we do NOT blindly mark files as failed.
  set +e
  timeout "$SYNC_TIMEOUT_SEC" \
    aws s3 sync "${spool_dir}/" "s3://${BUCKET}/${PREFIX}/${user}/" \
        --only-show-errors \
        --no-progress \
        2>>"$ERR_LOG"
  local sync_exit=$?
  set -e

  # If timeout triggered, leave files in .spool/ — spool recovery will retry next run
  if [[ $sync_exit -eq 124 ]]; then
    err "${user} | Sync timed out after ${SYNC_TIMEOUT_SEC}s — files left in spool for next run"
    return 0
  fi

  # ── Verify each file individually on S3 ────────────────────────────────────
  # Even if aws exited non-zero, some files may have made it — check each one.
  local ok=0 fail=0
  for rel in "${staged[@]}"; do
    local s3_key="${PREFIX}/${user}/${rel}"

    if aws s3api head-object \
          --bucket "$BUCKET" \
          --key    "$s3_key" \
          --output json &>/dev/null; then

      # Confirmed on S3 — delete local spool copy
      rm -f "${spool_dir}/${rel}"
      log "${user} | OK | ${rel}"
      (( ok++ ))

    else
      # Not found on S3 — move to failed/ for manual retry
      err "${user} | Missing on S3 | ${rel}"
      local failed_target="${user_root}/failed/${rel}"
      mkdir -p "$(dirname "$failed_target")"
      mv "${spool_dir}/${rel}" "$failed_target"
      (( fail++ ))

    fi
  done

  # ── Clean up empty spool subdirs ───────────────────────────────────────────
  find "$spool_dir" -mindepth 1 -type d -empty -delete 2>/dev/null || true

  log "${user} | Done | ok=${ok} failed=${fail}"
}

# ── Main ───────────────────────────────────────────────────────────────────────

# Acquire exclusive lock — exit silently if previous run is still in progress
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  log "Sync skipped | previous run still in progress"
  exit 0
fi

log "Sync start | base=${BASE_DIR} | bucket=${BUCKET} | prefix=${PREFIX}"

for user_path in "${BASE_DIR}"/*; do
  [[ -d "$user_path" ]] || continue
  user="$(basename "$user_path")"
  sync_user "$user"
done

mark_alive
log "Sync end"
