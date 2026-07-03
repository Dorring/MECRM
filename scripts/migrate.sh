#!/usr/bin/env bash
#
# M-Agent-ECRM single migration runner
#
# Establishes ONE fixed, idempotent, repeatable order for bringing a PostgreSQL
# database (empty OR existing) to the platform schema:
#
#   1. Prisma migrate deploy        -> application tables / indexes / FK constraints
#                                       (source of truth: gateway/prisma/schema.prisma)
#   2. Raw SQL migrations 01-11     -> event store, outbox, read models, twins,
#                                       governance columns that live outside Prisma
#                                       (CREATED with IF NOT EXISTS -> idempotent)
#   3. 02-rls-policies.sql           -> ENABLE + FORCE RLS, USING + WITH CHECK,
#                                       crm_app role + grants
#
# Design notes:
#   - Designed to run against an EMPTY database (CREATE DATABASE already done by
#     the postgres image / POSTGRES_DB). Prisma migrate deploy will create the
#     _prisma_migrations shadow state on a fresh DB.
#   - All raw SQL uses CREATE TABLE IF NOT EXISTS / DROP POLICY IF EXISTS, so it
#     can be re-run safely. Prisma migrate deploy is itself idempotent.
#   - Runs against the high-privilege owner account (POSTGRES_USER / crm_user).
#     This is REQUIRED for DDL and for ALTER TABLE ... FORCE ROW LEVEL SECURITY
#     (the table owner / superuser is the only role that can FORCE RLS). The
#     low-privilege crm_app role is granted at the END of 02-rls-policies.sql and
#     must NOT be used to run migrations -- doing so would silently fail to apply
#     RLS because non-owners cannot FORCE RLS, masking the security control.
#   - A session-level PostgreSQL advisory lock is held for the entire runner
#     lifetime to prevent concurrent migration deployments. The lock is released
#     via a trap/finally even if the script exits early.
#   - Schema drift detection runs after migration and FAILS (non-zero) when any
#     tenant table in the explicit allowlist is missing ENABLE+FORCE RLS or a
#     USING/WITH CHECK policy.
#
# Usage:
#   ./scripts/migrate.sh                 # apply all migrations
#   ./scripts/migrate.sh --drift-only    # only run drift detection
#   ./scripts/migrate.sh --skip-prisma   # skip Prisma step (SQL + RLS only)
#   ./scripts/migrate.sh --audit-warn    # drift/RLS issues only warn (dev)
#
# Env (read from .env if present, else from environment):
#   DATABASE_URL            e.g. postgresql://crm_user:crm_password@localhost:5432/enterprise_crm
#   POSTGRES_USER           owner account (default crm_user)
#   POSTGRES_PASSWORD       owner password (default crm_password)
#   POSTGRES_HOST           default localhost
#   POSTGRES_PORT           default 5432
#   POSTGRES_DB             default enterprise_crm
#   GATEWAY_DIR             default ./gateway  (where prisma schema + migrations live)
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Global constants
# ---------------------------------------------------------------------------
LOCK_KEY="405011"
LOCK_TIMEOUT_SECONDS="30"

# ---------------------------------------------------------------------------
# Resolve repo root (script lives in <repo>/scripts/)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SQL_DIR="${REPO_ROOT}/database/migrations"

# ---------------------------------------------------------------------------
# Load .env if present (do not override already-exported vars)
# ---------------------------------------------------------------------------
if [[ -f "${REPO_ROOT}/.env" ]]; then
  # shellcheck disable=SC1090,SC1091
  set -a; . "${REPO_ROOT}/.env"; set +a
fi

: "${POSTGRES_USER:=crm_user}"
: "${POSTGRES_PASSWORD:=crm_password}"
: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:=enterprise_crm}"
: "${GATEWAY_DIR:=${REPO_ROOT}/gateway}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
  export DATABASE_URL
fi

# Fixed SQL execution order. 02-rls-policies is deliberately kept in this
# numeric order (it defensively skips tables that do not yet exist via
# to_regclass, so it is safe even before some tables are created; it is also
# the grant step that must run last among the RLS-bearing files, but the
# per-table RLS in 03-11 re-applies ENABLE+FORCE so the end state is stable).
SQL_FILES=(
  "00-advisory-lock.sql"
  "01-core-tables.sql"
  "02-rls-policies.sql"
  "03-event-log.sql"
  "04-aggregate-snapshots.sql"
  "05-replay-jobs.sql"
  "06-event-store.sql"
  "07-outbox.sql"
  "08-read-models.sql"
  "09-agent-decisions.sql"
  "10-data-governance.sql"
  "11-intelligence-twins.sql"
  "12-type-convergence.sql"
)

# Tenant tables that MUST have ENABLE + FORCE RLS and a USING/WITH CHECK policy.
# Keep in sync with gateway/prisma/schema.prisma @@map and raw-SQL track tables.
TENANT_TABLES=(
  users roles user_roles policies leads deals tickets customers
  agent_tasks agent_events agent_decisions ai_memory approvals
  audit_logs domain_events event_streams events outbox_events
  processed_events lead_read_model deal_pipeline_view customer_timeline_view
  security_events data_retention_policies automation_policies
  automation_simulations automation_executions customer_profiles
  customer_timelines knowledge_articles knowledge_drafts
  productivity_proposals predictions
)

# Tables that are intentionally not tenant-scoped (raw-SQL track or Prisma internal).
NON_TENANT_TABLES=(
  _prisma_migrations
  event_log aggregate_snapshots replay_jobs customer_twins
  twin_simulation_log devx_insights
)

log() { printf '\033[1;34m[migrate]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[migrate][warn]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[1;31m[migrate][error]\033[0m %s\n' "$*" >&2; }

require() {
  command -v "$1" >/dev/null 2>&1 || { err "required tool not found: $1"; exit 1; }
}

# ---------------------------------------------------------------------------
# Session-level advisory lock
# ---------------------------------------------------------------------------
LOCK_PID=""
LOCK_OUT=""
LOCK_BACKEND_PID=""
LOCK_HOLD_SECONDS="${MIGRATE_LOCK_HOLD_SECONDS:-3600}"

_psql() {
  PGPASSWORD="${POSTGRES_PASSWORD}" PGAPPNAME="${1:-mecrm-migration}" psql \
    -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    "${@:2}"
}

cleanup_lock() {
  # 1. Terminate the local psql client process with escalating signals.
  if [[ -n "${LOCK_PID:-}" ]] && kill -0 "${LOCK_PID}" 2>/dev/null; then
    kill -TERM "${LOCK_PID}" 2>/dev/null || true
    local waited=0
    while [[ ${waited} -lt 20 ]] && kill -0 "${LOCK_PID}" 2>/dev/null; do
      sleep 0.25
      waited=$((waited + 1))
    done
    if kill -0 "${LOCK_PID}" 2>/dev/null; then
      kill -KILL "${LOCK_PID}" 2>/dev/null || true
      wait "${LOCK_PID}" 2>/dev/null || true
    fi
  fi

  # 2. If we know the database backend PID, verify it is gone and terminate only
  #    that specific backend if needed. Never mass-kill by LOCK_KEY.
  if [[ -n "${LOCK_BACKEND_PID:-}" ]]; then
    local db_backend_still
    db_backend_still=$(_psql mecrm-migration-cleanup -At \
      -c "SELECT 1 FROM pg_stat_activity WHERE pid = ${LOCK_BACKEND_PID} AND application_name = 'mecrm-migration-lock'" 2>/dev/null || true)

    if [[ "${db_backend_still}" == "1" ]]; then
      warn "terminating lingering migration lock backend pid=${LOCK_BACKEND_PID}"
      _psql mecrm-migration-cleanup -At \
        -c "SELECT pg_terminate_backend(${LOCK_BACKEND_PID})" >/dev/null 2>&1 || true

      local waited=0
      while [[ ${waited} -lt 40 ]]; do
        db_backend_still=$(_psql mecrm-migration-cleanup -At \
          -c "SELECT 1 FROM pg_stat_activity WHERE pid = ${LOCK_BACKEND_PID} AND application_name = 'mecrm-migration-lock'" 2>/dev/null || true)
        [[ "${db_backend_still}" != "1" ]] && break
        sleep 0.25
        waited=$((waited + 1))
      done

      if [[ "${db_backend_still}" == "1" ]]; then
        err "backend pid=${LOCK_BACKEND_PID} still exists after pg_terminate_backend"
      fi
    fi
  fi

  # 3. Defensive verification: no advisory-lock holder with our app name remains.
  local lock_still
  lock_still=$(_psql mecrm-migration-cleanup -At \
    -c "SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid WHERE l.locktype = 'advisory' AND l.objid = ${LOCK_KEY} AND a.application_name = 'mecrm-migration-lock'" 2>/dev/null || true)
  if [[ "${lock_still}" == "1" ]]; then
    warn "advisory lock ${LOCK_KEY} still held by a mecrm-migration-lock backend after cleanup"
  fi

  if [[ -n "${LOCK_OUT:-}" && -e "${LOCK_OUT}" ]]; then
    rm -f "${LOCK_OUT}"
  fi
}

acquire_and_hold_lock() {
  require psql
  log "acquiring session-level advisory lock (key=${LOCK_KEY}, timeout=${LOCK_TIMEOUT_SECONDS}s)"

  LOCK_OUT="$(mktemp /tmp/migrate-lock.XXXXXX)"

  # Start a background psql session that holds the advisory lock.
  # Statements are fed via stdin so each returns independently; \echo emits the
  # confirmation marker *after* the lock is acquired and statement_timeout is
  # cleared, but *before* the long pg_sleep hold. A single -c would batch all
  # statements and only return after pg_sleep completes, hiding the marker.
  PGPASSWORD="${POSTGRES_PASSWORD}" PGAPPNAME=mecrm-migration-lock stdbuf -oL -eL psql \
    -v ON_ERROR_STOP=1 \
    -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    >"${LOCK_OUT}" 2>&1 <<SQL &
SET statement_timeout = '${LOCK_TIMEOUT_SECONDS}s';
SELECT pg_advisory_lock(${LOCK_KEY});
SET statement_timeout = '0';
SELECT 'LOCK_ACQUIRED:' || pg_backend_pid();
SELECT pg_sleep(${LOCK_HOLD_SECONDS});
SQL

  LOCK_PID=$!

  local acquired=0
  local deadline=$((SECONDS + LOCK_TIMEOUT_SECONDS + 5))

  while [[ ${SECONDS} -lt ${deadline} ]]; do
    # If the lock-holder exited before we saw LOCK_ACQUIRED, something failed
    # (wrong host, auth error, another lock holder blocking past statement_timeout).
    if ! kill -0 "${LOCK_PID}" 2>/dev/null; then
      err "lock-holder psql exited before lock acquisition"
      if [[ -s "${LOCK_OUT}" ]]; then
        err "lock-holder output:"
        sed 's/^/  /' "${LOCK_OUT}" >&2
      fi
      cleanup_lock
      exit 1
    fi

    if [[ -f "${LOCK_OUT}" ]]; then
      local marker
      marker=$(grep -oE 'LOCK_ACQUIRED:[0-9]+' "${LOCK_OUT}" 2>/dev/null | tail -n1 || true)
      if [[ -n "${marker}" ]]; then
        LOCK_BACKEND_PID="${marker#LOCK_ACQUIRED:}"
        acquired=1
        break
      fi
    fi

    sleep 0.25
  done

  if [[ ${acquired} -ne 1 ]]; then
    err "failed to acquire advisory lock within ${LOCK_TIMEOUT_SECONDS}s (another migration may be running)"
    cleanup_lock
    exit 1
  fi

  log "advisory lock acquired and held (local pid=${LOCK_PID}, backend pid=${LOCK_BACKEND_PID})"
}

run_prisma_migrate() {
  require npx
  [[ -d "${GATEWAY_DIR}/prisma" ]] || { err "gateway prisma dir not found: ${GATEWAY_DIR}/prisma"; exit 1; }
  log "Prisma migrate deploy (schema source: ${GATEWAY_DIR}/prisma/schema.prisma)"
  ( cd "${GATEWAY_DIR}" && npx prisma migrate deploy )
}

run_sql_migrations() {
  require psql
  local f
  for f in "${SQL_FILES[@]}"; do
    local path="${SQL_DIR}/${f}"
    [[ -f "${path}" ]] || { err "missing SQL migration: ${path}"; exit 1; }
    log "Applying ${f}"
    PGPASSWORD="${POSTGRES_PASSWORD}" psql \
      -v ON_ERROR_STOP=1 \
      -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" \
      -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
      -f "${path}"
  done
}

# ---------------------------------------------------------------------------
# Schema drift and RLS enforcement audit
# ---------------------------------------------------------------------------
detect_drift() {
  require psql
  log "Schema drift detection (table-presence level)"
  local schema_file="${GATEWAY_DIR}/prisma/schema.prisma"
  [[ -f "${schema_file}" ]] || { warn "schema.prisma not found, skipping drift check"; return 0; }

  # Tables Prisma owns (from @@map("..."))
  local prisma_tables
  prisma_tables=$(grep -oE '@@map\("[a-z_]+"' "${schema_file}" | sed -E 's/@@map\("([^"]+)"/\1/' | sort -u)

  # Tables actually in the DB (public schema)
  local db_tables
  db_tables=$(PGPASSWORD="${POSTGRES_PASSWORD}" psql -At \
      -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" \
      -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
      -c "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1;" \
    | sort -u)

  local missing=0 extra_reported=0
  log "Prisma-declared tables missing from DB:"
  while IFS= read -r t; do
    [[ -z "${t}" ]] && continue
    if ! printf '%s\n' "${db_tables}" | grep -qx "${t}"; then
      printf '  - MISSING: %s\n' "${t}"; missing=$((missing+1))
    fi
  done <<< "${prisma_tables}"
  [[ ${missing} -eq 0 ]] && log "  (none)"

  # Build regex of known non-tenant tables.
  local non_tenant_regex
  non_tenant_regex=$(printf '%s|' "${NON_TENANT_TABLES[@]}")
  non_tenant_regex="^(${non_tenant_regex%,})$"

  log "DB tables not declared in Prisma schema (raw-SQL track or unknown):"
  while IFS= read -r t; do
    [[ -z "${t}" ]] && continue
    if [[ "${t}" =~ ${non_tenant_regex} ]]; then
      continue   # expected, owned by the raw SQL track or Prisma internal
    fi
    if ! printf '%s\n' "${prisma_tables}" | grep -qx "${t}"; then
      printf '  - EXTRA: %s\n' "${t}"; extra_reported=$((extra_reported+1))
    fi
  done <<< "${db_tables}"
  [[ ${extra_reported} -eq 0 ]] && log "  (none beyond known raw-SQL tables)"

  # RLS enforcement audit: tenant tables missing ENABLE/FORCE or policy.
  log "RLS enforcement audit (tenant tables missing ENABLE, FORCE, or policy):"
  local rls_issues=""
  local tenant_list
  tenant_list=$(printf "'%s'," "${TENANT_TABLES[@]}")
  tenant_list="${tenant_list%,}"

  rls_issues=$(PGPASSWORD="${POSTGRES_PASSWORD}" psql -At \
      -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" \
      -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
      -c "
        WITH tenant_tables AS (
          SELECT c.oid, c.relname
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = 'public' AND c.relkind = 'r'
            AND c.relname IN (${tenant_list})
        ),
        policy_check AS (
          SELECT pc.relname,
                 pc.relrowsecurity AS enabled,
                 pc.relforcerowsecurity AS forced,
                 bool_or(p.polcmd = '*') AS has_all_policy,
                 bool_or(p.polpermissive) AS has_permissive_policy
          FROM tenant_tables pc
          LEFT JOIN pg_policy p ON p.polrelid = pc.oid
          GROUP BY pc.relname, pc.relrowsecurity, pc.relforcerowsecurity
        )
        SELECT relname || ' enabled=' || CASE WHEN enabled THEN 'on' ELSE 'OFF' END ||
               ' forced=' || CASE WHEN forced THEN 'on' ELSE 'OFF' END ||
               ' all_policy=' || CASE WHEN has_all_policy THEN 'yes' ELSE 'no' END
        FROM policy_check
        WHERE NOT (enabled AND forced AND has_all_policy)
        ORDER BY relname;" 2>/dev/null || true)

  local rls_failed=0
  if [[ -n "${rls_issues}" ]]; then
    printf '  %s\n' "${rls_issues}"
    rls_failed=1
  else
    log "  (all tenant tables have ENABLE+FORCE+ALL policy)"
  fi

  if [[ ${missing} -gt 0 || ${extra_reported} -gt 0 || ${rls_failed} -gt 0 ]]; then
    if [[ "${AUDIT_WARN:-0}" == "1" ]]; then
      warn "drift/RLS issues detected (--audit-warn enabled; exiting 0)"
      return 0
    fi
    err "drift/RLS audit failed"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
DRIFT_ONLY=0
SKIP_PRISMA=0
AUDIT_WARN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --drift-only) DRIFT_ONLY=1; shift ;;
    --skip-prisma) SKIP_PRISMA=1; shift ;;
    --audit-warn) AUDIT_WARN=1; shift ;;
    -h|--help)
      sed -n '2,50p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) err "unknown argument: $1"; exit 2 ;;
  esac
done

trap cleanup_lock EXIT

log "repo root: ${REPO_ROOT}"
log "sql dir:   ${SQL_DIR}"
log "target DB: ${POSTGRES_DB} @ ${POSTGRES_HOST}:${POSTGRES_PORT} (user ${POSTGRES_USER})"

if [[ ${DRIFT_ONLY} -eq 1 ]]; then
  detect_drift
  exit 0
fi

acquire_and_hold_lock

[[ ${SKIP_PRISMA} -eq 0 ]] && run_prisma_migrate || warn "skipping Prisma step (--skip-prisma)"
run_sql_migrations
detect_drift

log "Migration complete."
