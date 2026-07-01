# Real smoke test (PowerShell / Windows host): register -> login -> create lead -> list leads.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\smoke-test.ps1 -Gateway http://localhost:4000
param(
    [string]$Gateway = "http://localhost:4000"
)

$ErrorActionPreference = "Stop"
$TenantSlug = "smoke-" + [int64](Get-Date -UFormat %s)
$Email = "$TenantSlug@example.com"
$Password = "SmokePass123!"

Write-Host "SMOKE: target=$Gateway tenant=$TenantSlug"

# 1. Register
Write-Host "SMOKE: register"
$body = @{ tenantName="Smoke Co"; tenantSlug=$TenantSlug; name="Smoke User"; email=$Email; password=$Password } | ConvertTo-Json
$resp = Invoke-WebRequest -Uri "$Gateway/api/v1/auth/register" -Method Post -ContentType "application/json" -Body $body -UseBasicParsing
Write-Host "SMOKE: register -> HTTP $($resp.StatusCode)"
if ($resp.StatusCode -notin 200,201) { Write-Host "FAIL: register"; Write-Host $resp.Content; exit 1 }

# 2. Login
Write-Host "SMOKE: login"
$body = @{ tenantSlug=$TenantSlug; email=$Email; password=$Password } | ConvertTo-Json
$resp = Invoke-WebRequest -Uri "$Gateway/api/v1/auth/login" -Method Post -ContentType "application/json" -Body $body -UseBasicParsing
Write-Host "SMOKE: login -> HTTP $($resp.StatusCode)"
if ($resp.StatusCode -notin 200,201) { Write-Host "FAIL: login"; Write-Host $resp.Content; exit 1 }
$login = $resp.Content | ConvertFrom-Json
$Token = $login.accessToken
if (-not $Token) { Write-Host "FAIL: no accessToken"; Write-Host $resp.Content; exit 1 }
Write-Host "SMOKE: token acquired"

# 3. Create lead
Write-Host "SMOKE: create lead"
$body = @{ name="Smoke Lead"; email="lead@example.com"; company="SmokeCo"; status="new" } | ConvertTo-Json
$resp = Invoke-WebRequest -Uri "$Gateway/api/v1/leads" -Method Post -ContentType "application/json" -Headers @{ Authorization = "Bearer $Token" } -Body $body -UseBasicParsing
Write-Host "SMOKE: create lead -> HTTP $($resp.StatusCode)"
if ($resp.StatusCode -notin 200,201,202) { Write-Host "FAIL: create lead"; Write-Host $resp.Content; exit 1 }

# 4. List leads
Write-Host "SMOKE: list leads"
$resp = Invoke-WebRequest -Uri "$Gateway/api/v1/leads?search=Smoke" -Method Get -Headers @{ Authorization = "Bearer $Token" } -UseBasicParsing
Write-Host "SMOKE: list leads -> HTTP $($resp.StatusCode)"
if ($resp.StatusCode -ne 200) { Write-Host "FAIL: list leads"; Write-Host $resp.Content; exit 1 }
if ($resp.Content -notmatch "Smoke Lead") { Write-Host "FAIL: created lead not in list"; Write-Host $resp.Content; exit 1 }

# 5. Negative: no token -> 401
Write-Host "SMOKE: unauthorized request"
try {
    $resp = Invoke-WebRequest -Uri "$Gateway/api/v1/leads" -Method Get -UseBasicParsing
    Write-Host "FAIL: unauthenticated request was not rejected (got $($resp.StatusCode))"; exit 1
} catch [System.Net.WebException] {
    $code = [int]$_.Exception.Response.StatusCode
    Write-Host "SMOKE: no-token -> HTTP $code (expect 401)"
    if ($code -ne 401) { Write-Host "FAIL: expected 401, got $code"; exit 1 }
} catch {
    # PowerShell 7+ throws HttpResponseException with .StatusCode
    $code = $_.Exception.Response.StatusCode.value__
    Write-Host "SMOKE: no-token -> HTTP $code (expect 401)"
    if ($code -ne 401) { Write-Host "FAIL: expected 401, got $code"; exit 1 }
}

Write-Host "SMOKE: ALL CHECKS PASSED"
