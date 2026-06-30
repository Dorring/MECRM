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

.PARAMETER DriftOnly
  Only run schema drift detection.

.PARAMETER SkipPrisma
  Skip the Prisma migrate deploy step (apply SQL + RLS only).

.EXAMPLE
  ./scripts/migrate.ps1
  ./scripts/migrate.ps1 -DriftOnly
  ./scripts/migrate.ps1 -SkipPrisma
#>
[CmdletBinding()]
param(
  [switch]$DriftOnly,
  [switch]$SkipPrisma
)

$ErrorActionPreference = 'Stop'

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
  '11-intelligence-twins.sql'
)

function Write-Log([string]$Msg)     { Write-Host "[migrate] $Msg" }
function Write-Warn2([string]$Msg)   { Write-Host "[migrate][warn] $Msg" -ForegroundColor Yellow }
function Write-Err2([string]$Msg)    { Write-Host "[migrate][error] $Msg" -ForegroundColor Red }
function Require-Tool([string]$Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    Write-Err2 "required tool not found: $Name"; exit 1
  }
}

function Invoke-PrismaMigrate {
  Require-Tool 'npx'
  $prismaDir = Join-Path $env:GATEWAY_DIR 'prisma'
  if (-not (Test-Path $prismaDir)) { Write-Err2 "gateway prisma dir not found: $prismaDir"; exit 1 }
  Write-Log "Prisma migrate deploy (schema source: $(Join-Path $prismaDir 'schema.prisma'))"
  Write-Log "DATABASE_URL=$($env:DATABASE_URL)"
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

<#
  Basic schema drift detection. Compares the set of tables Prisma expects
  (parsed from schema.prisma @@map) against tables actually present in
  information_schema.tables. Prints any Prisma-declared table missing from the
  DB, plus any unexpected extra table. Coarse (columns/types NOT compared); for
  full fidelity use `npx prisma migrate diff` against a shadow DB. Does not fail
  the run -- extra non-Prisma tables (event_log, customer_twins, devx_insights,
  ...) are intentionally managed by the raw SQL track.
#>
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

  # Tables managed ONLY by the raw SQL track (legitimately absent from Prisma).
  $sqlOnlyAllowlist = '^(_prisma_migrations|event_log|aggregate_snapshots|replay_jobs|customer_twins|twin_simulation_log|devx_insights)$'
  Write-Log "DB tables not declared in Prisma schema (raw-SQL track or unknown):"
  $extra = 0
  foreach ($t in $dbTables) {
    if ($t -match $sqlOnlyAllowlist) { continue }
    if ($prismaTables -notcontains $t) { Write-Host "  - EXTRA: $t"; $extra++ }
  }
  if ($extra -eq 0) { Write-Log "  (none beyond known raw-SQL tables)" }

  # RLS enforcement audit: tenant tables that have ENABLE but not FORCE (or neither).
  Write-Log "RLS enforcement audit (tables missing ENABLE or FORCE):"
  $rlsIssues = & psql -At `
    -h $env:POSTGRES_HOST -p $env:POSTGRES_PORT `
    -U $env:POSTGRES_USER -d $env:POSTGRES_DB `
    -c "SELECT relname || ' enabled=' || CASE WHEN relrowsecurity THEN 'on' ELSE 'OFF' END || ' forced=' || CASE WHEN relforcerowsecurity THEN 'on' ELSE 'OFF' END FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND (relrowsecurity = false OR relforcerowsecurity = false) ORDER BY relname;" 2>$null
  if ($rlsIssues) { $rlsIssues | ForEach-Object { Write-Host "  $_" } }
  else { Write-Log "  (all tables either FORCE RLS or correctly unscoped)" }
}

Write-Log "repo root: $RepoRoot"
Write-Log "sql dir:   $SqlDir"
Write-Log "target DB: $($env:POSTGRES_DB) @ $($env:POSTGRES_HOST):$($env:POSTGRES_PORT) (user $($env:POSTGRES_USER))"

if ($DriftOnly) { Invoke-DriftDetection; exit 0 }

if (-not $SkipPrisma) { Invoke-PrismaMigrate }
else { Write-Warn2 "skipping Prisma step (-SkipPrisma)" }
Invoke-SqlMigrations
Invoke-DriftDetection

Write-Log "Migration complete."
