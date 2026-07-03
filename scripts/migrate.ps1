#Requires -Version 5.1
<#
.SYNOPSIS
  M-Agent-ECRM single migration runner (PowerShell edition).

.DESCRIPTION
  Establishes ONE fixed, idempotent, repeatable order for bringing a PostgreSQL
  database (empty OR existing) to the platform schema:

    1. Prisma migrate deploy   -> application tables / indexes / FK constraints
                                   (source of truth: gateway/prisma/schema.prisma)
    2. Raw SQL migrations 01-11 -> event store, outbox, read models, twins,
                                   governance columns outside Prisma
                                   (CREATE TABLE IF NOT EXISTS -> idempotent)
    3. 02-rls-policies.sql      -> ENABLE + FORCE RLS, USING + WITH CHECK,
                                   crm_app role + grants

  Designed to run against an EMPTY database. All raw SQL is idempotent; Prisma
  migrate deploy is itself idempotent. Runs against the high-privilege owner
  account (POSTGRES_USER / crm_user). This is REQUIRED for DDL and FORCE RLS --
  the low-privilege crm_app role must NOT run migrations (it cannot FORCE RLS,
  so RLS would silently fail to apply, masking the security control).

  A session-level PostgreSQL advisory lock is held for the entire runner lifetime.
  The lock is released via a finally block even if the script exits early.

.PARAMETER DriftOnly
  Only run schema drift detection.

.PARAMETER SkipPrisma
  Skip the Prisma migrate deploy step (apply SQL + RLS only).

.PARAMETER AuditWarn
  Drift/RLS issues only warn and exit 0 (development use).

.EXAMPLE
  ./scripts/migrate.ps1
  ./scripts/migrate.ps1 -DriftOnly
  ./scripts/migrate.ps1 -SkipPrisma
  ./scripts/migrate.ps1 -AuditWarn
#>
[CmdletBinding()]
param(
  [switch]$DriftOnly,
  [switch]$SkipPrisma,
  [switch]$AuditWarn
)

$ErrorActionPreference = 'Stop'

# Lock constants
$LockKey = '405011'
$LockTimeoutSeconds = 30

# Resolve repo root (script lives in <repo>/scripts/)
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot    = Resolve-Path (Join-Path $ScriptDir '..')
$SqlDir      = Join-Path $RepoRoot 'database/migrations'

# Load .env if present (do not override already-exported vars)
$EnvFile = Join-Path $RepoRoot '.env'
if (Test-Path $EnvFile) {
  Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
      $idx  = $line.IndexOf('=')
      $key  = $line.Substring(0, $idx).Trim()
      $val  = $line.Substring($idx + 1).Trim()
      if (-not [Environment]::GetEnvironmentVariable($key, 'Process')) {
        [Environment]::SetEnvironmentVariable($key, $val, 'Process')
      }
    }
  }
}

if (-not $env:POSTGRES_USER)     { $env:POSTGRES_USER     = 'crm_user' }
if (-not $env:POSTGRES_PASSWORD) { $env:POSTGRES_PASSWORD = 'crm_password' }
if (-not $env:POSTGRES_HOST)     { $env:POSTGRES_HOST     = 'localhost' }
if (-not $env:POSTGRES_PORT)     { $env:POSTGRES_PORT     = '5432' }
if (-not $env:POSTGRES_DB)       { $env:POSTGRES_DB       = 'enterprise_crm' }
if (-not $env:GATEWAY_DIR)       { $env:GATEWAY_DIR       = (Join-Path $RepoRoot 'gateway') }

if (-not $env:DATABASE_URL) {
  $env:DATABASE_URL = "postgresql://$($env:POSTGRES_USER):$($env:POSTGRES_PASSWORD)@$($env:POSTGRES_HOST):$($env:POSTGRES_PORT)/$($env:POSTGRES_DB)"
}

# Fixed SQL execution order. 02-rls-policies defensively skips non-existent
# tables via to_regclass, so numeric order is safe; per-table RLS in 03-11
# re-applies ENABLE+FORCE so the end state is stable.
$SqlFiles = @(
  '00-advisory-lock.sql',
  '01-core-tables.sql',
  '02-rls-policies.sql',
  '03-event-log.sql',
  '04-aggregate-snapshots.sql',
  '05-replay-jobs.sql',
  '06-event-store.sql',
  '07-outbox.sql',
  '08-read-models.sql',
  '09-agent-decisions.sql',
  '10-data-governance.sql',
  '11-intelligence-twins.sql',
  '12-type-convergence.sql'
)

# Tenant tables that MUST have ENABLE + FORCE RLS and an ALL policy.
$TenantTables = @(
  'users','roles','user_roles','policies','leads','deals','tickets','customers',
  'agent_tasks','agent_events','agent_decisions','ai_memory','approvals',
  'audit_logs','domain_events','event_streams','events','outbox_events',
  'processed_events','lead_read_model','deal_pipeline_view','customer_timeline_view',
  'security_events','data_retention_policies','automation_policies',
  'automation_simulations','automation_executions','customer_profiles',
  'customer_timelines','knowledge_articles','knowledge_drafts',
  'productivity_proposals','predictions'
)

# Tables intentionally not tenant-scoped.
$NonTenantTables = @(
  '_prisma_migrations','event_log','aggregate_snapshots','replay_jobs',
  'customer_twins','twin_simulation_log','devx_insights'
)

function Write-Log([string]$Msg)     { Write-Host "[migrate] $Msg" }
function Write-Warn2([string]$Msg)   { Write-Host "[migrate][warn] $Msg" -ForegroundColor Yellow }
function Write-Err2([string]$Msg)    { Write-Host "[migrate][error] $Msg" -ForegroundColor Red }
function Require-Tool([string]$Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    Write-Err2 "required tool not found: $Name"; exit 1
  }
}

# ---------------------------------------------------------------------------
# Session-level advisory lock
# ---------------------------------------------------------------------------
$LockProcess = $null
$LockOutputFile = $null

function Clear-Lock {
  if ($script:LockProcess -and -not $script:LockProcess.HasExited) {
    $script:LockProcess | Stop-Process -Force -ErrorAction SilentlyContinue
    $null = $script:LockProcess.WaitForExit(5000)
  }
  if ($script:LockOutputFile -and (Test-Path $script:LockOutputFile)) {
    Remove-Item $script:LockOutputFile -Force -ErrorAction SilentlyContinue
  }
  if ($script:LockErrorFile -and (Test-Path $script:LockErrorFile)) {
    Remove-Item $script:LockErrorFile -Force -ErrorAction SilentlyContinue
  }
  if ($script:LockSqlFile -and (Test-Path $script:LockSqlFile)) {
    Remove-Item $script:LockSqlFile -Force -ErrorAction SilentlyContinue
  }
}

function Lock-Held {
  Require-Tool 'psql'
  Write-Log "acquiring session-level advisory lock (key=$LockKey, timeout=${LockTimeoutSeconds}s)"

  $script:LockOutputFile = Join-Path $env:TEMP "migrate-lock-out-$(Get-Random).txt"
  $script:LockErrorFile  = Join-Path $env:TEMP "migrate-lock-err-$(Get-Random).txt"
  $script:LockSqlFile    = Join-Path $env:TEMP "migrate-lock-sql-$(Get-Random).txt"
  $holdSeconds = if ($env:MIGRATE_LOCK_HOLD_SECONDS) { $env:MIGRATE_LOCK_HOLD_SECONDS } else { 3600 }

  # Write the lock SQL to a temp file and execute it with -f.  This is required
  # because psql -c batches all statements into one request and only returns
  # output after pg_sleep() finishes, hiding the LOCK_ACQUIRED marker.  A file
  # (-f) sends statements sequentially, so \echo emits the marker immediately
  # after the lock is acquired and statement_timeout is cleared.
  @"
SET statement_timeout = '${LockTimeoutSeconds}s';
SELECT pg_advisory_lock($LockKey);
SET statement_timeout = '0';
\echo LOCK_ACQUIRED
SELECT pg_sleep($holdSeconds);
"@ | Set-Content -Path $script:LockSqlFile -Encoding UTF8 -NoNewline

  $argList = @(
    '-v', 'ON_ERROR_STOP=1',
    '-h', $env:POSTGRES_HOST,
    '-p', $env:POSTGRES_PORT,
    '-U', $env:POSTGRES_USER,
    '-d', $env:POSTGRES_DB,
    '-f', $script:LockSqlFile
  )
  $env:PGPASSWORD = $env:POSTGRES_PASSWORD
  $script:LockProcess = Start-Process -FilePath 'psql' -ArgumentList $argList `
    -RedirectStandardOutput $script:LockOutputFile `
    -RedirectStandardError  $script:LockErrorFile `
    -NoNewWindow -PassThru

  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  $acquired = $false
  while ($sw.Elapsed.TotalSeconds -lt ($LockTimeoutSeconds + 5)) {
    # If the lock-holder exited before we saw LOCK_ACQUIRED, fail fast.
    if ($script:LockProcess.HasExited) {
      Write-Err2 "lock-holder psql exited before lock acquisition"
      if (Test-Path $script:LockOutputFile) {
        Get-Content $script:LockOutputFile | ForEach-Object { Write-Err2 "  $_" }
      }
      if (Test-Path $script:LockErrorFile) {
        Get-Content $script:LockErrorFile | ForEach-Object { Write-Err2 "  $_" }
      }
      Clear-Lock
      exit 1
    }

    if (Test-Path $script:LockOutputFile) {
      $content = Get-Content $script:LockOutputFile -Raw -ErrorAction SilentlyContinue
      if ($content -and $content.Contains('LOCK_ACQUIRED')) {
        $acquired = $true
        break
      }
    }
    Start-Sleep -Milliseconds 250
  }
  $sw.Stop()

  if (-not $acquired) {
    Write-Err2 "failed to acquire advisory lock within ${LockTimeoutSeconds}s (another migration may be running)"
    Clear-Lock
    exit 1
  }

  Write-Log "advisory lock acquired and held (pid=$($LockProcess.Id))"
}

function Invoke-PrismaMigrate {
  Require-Tool 'npx'
  $prismaDir = Join-Path $env:GATEWAY_DIR 'prisma'
  if (-not (Test-Path $prismaDir)) { Write-Err2 "gateway prisma dir not found: $prismaDir"; exit 1 }
  Write-Log "Prisma migrate deploy (schema source: $(Join-Path $prismaDir 'schema.prisma'))"
  Push-Location $env:GATEWAY_DIR
  try { npx prisma migrate deploy }
  finally { Pop-Location }
}

function Invoke-SqlMigrations {
  Require-Tool 'psql'
  foreach ($f in $SqlFiles) {
    $path = Join-Path $SqlDir $f
    if (-not (Test-Path $path)) { Write-Err2 "missing SQL migration: $path"; exit 1 }
    Write-Log "Applying $f"
    $env:PGPASSWORD = $env:POSTGRES_PASSWORD
    & psql -v ON_ERROR_STOP=1 `
           -h $env:POSTGRES_HOST -p $env:POSTGRES_PORT `
           -U $env:POSTGRES_USER -d $env:POSTGRES_DB `
           -f $path
    if ($LASTEXITCODE -ne 0) { Write-Err2 "psql failed applying $f (exit $LASTEXITCODE)"; exit 1 }
  }
}

function Invoke-DriftDetection {
  Require-Tool 'psql'
  Write-Log "Schema drift detection (table-presence level)"
  $schemaFile = Join-Path $env:GATEWAY_DIR 'prisma/schema.prisma'
  if (-not (Test-Path $schemaFile)) { Write-Warn2 "schema.prisma not found, skipping drift check"; return }

  $prismaTables = Select-String -Path $schemaFile -Pattern '@@map\("([a-z_]+)"\)' -AllMatches |
    ForEach-Object { $_.Matches } |
    ForEach-Object { $_.Groups[1].Value } |
    Sort-Object -Unique

  $env:PGPASSWORD = $env:POSTGRES_PASSWORD
  $dbTables = & psql -At `
    -h $env:POSTGRES_HOST -p $env:POSTGRES_PORT `
    -U $env:POSTGRES_USER -d $env:POSTGRES_DB `
    -c "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1;"
  if ($LASTEXITCODE -ne 0) { Write-Warn2 "could not query information_schema; skipping drift"; return }
  $dbTables = $dbTables | Where-Object { $_ } | Sort-Object -Unique

  Write-Log "Prisma-declared tables missing from DB:"
  $missing = 0
  foreach ($t in $prismaTables) {
    if ($dbTables -notcontains $t) { Write-Host "  - MISSING: $t"; $missing++ }
  }
  if ($missing -eq 0) { Write-Log "  (none)" }

  $nonTenantRegex = '^(' + ($NonTenantTables -join '|') + ')$'
  Write-Log "DB tables not declared in Prisma schema (raw-SQL track or unknown):"
  $extra = 0
  foreach ($t in $dbTables) {
    if ($t -match $nonTenantRegex) { continue }
    if ($prismaTables -notcontains $t) { Write-Host "  - EXTRA: $t"; $extra++ }
  }
  if ($extra -eq 0) { Write-Log "  (none beyond known raw-SQL tables)" }

  Write-Log "RLS enforcement audit (tenant tables missing ENABLE, FORCE, or policy):"
  $tenantList = ($TenantTables | ForEach-Object { "'$_'" }) -join ','
  $rlsQuery = @"
    WITH tenant_tables AS (
      SELECT c.oid, c.relname
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind = 'r'
        AND c.relname IN ($tenantList)
    ),
    policy_check AS (
      SELECT pc.relname,
             pc.relrowsecurity AS enabled,
             pc.relforcerowsecurity AS forced,
             bool_or(p.polcmd = '*') AS has_all_policy
      FROM tenant_tables pc
      LEFT JOIN pg_policy p ON p.polrelid = pc.oid
      GROUP BY pc.relname, pc.relrowsecurity, pc.relforcerowsecurity
    )
    SELECT relname || ' enabled=' || CASE WHEN enabled THEN 'on' ELSE 'OFF' END ||
           ' forced=' || CASE WHEN forced THEN 'on' ELSE 'OFF' END ||
           ' all_policy=' || CASE WHEN has_all_policy THEN 'yes' ELSE 'no' END
    FROM policy_check
    WHERE NOT (enabled AND forced AND has_all_policy)
    ORDER BY relname;
"@
  $rlsIssues = & psql -At `
    -h $env:POSTGRES_HOST -p $env:POSTGRES_PORT `
    -U $env:POSTGRES_USER -d $env:POSTGRES_DB `
    -c $rlsQuery 2>$null

  $rlsFailed = 0
  if ($rlsIssues) { $rlsIssues | ForEach-Object { Write-Host "  $_" }; $rlsFailed = 1 }
  else { Write-Log "  (all tenant tables have ENABLE+FORCE+ALL policy)" }

  if ($missing -gt 0 -or $extra -gt 0 -or $rlsFailed -gt 0) {
    if ($AuditWarn) {
      Write-Warn2 "drift/RLS issues detected (-AuditWarn enabled; exiting 0)"
      return
    }
    Write-Err2 "drift/RLS audit failed"
    exit 1
  }
}

Write-Log "repo root: $RepoRoot"
Write-Log "sql dir:   $SqlDir"
Write-Log "target DB: $($env:POSTGRES_DB) @ $($env:POSTGRES_HOST):$($env:POSTGRES_PORT) (user $($env:POSTGRES_USER))"

if ($DriftOnly) {
  Invoke-DriftDetection
  exit 0
}

try {
  Lock-Held

  if (-not $SkipPrisma) { Invoke-PrismaMigrate }
  else { Write-Warn2 "skipping Prisma step (-SkipPrisma)" }
  Invoke-SqlMigrations
  Invoke-DriftDetection
}
finally {
  Clear-Lock
}

Write-Log "Migration complete."
