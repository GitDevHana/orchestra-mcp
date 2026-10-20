$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$data = Join-Path (Split-Path -Parent $project) 'Desktop\agent-orchestrator\.orchestra-data'
$meta = Get-Content -LiteralPath (Join-Path $data 'server.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$uri = [Uri]$meta.dashboard
$runsUrl = $uri.GetLeftPart([System.UriPartial]::Authority) + '/api/runs' + $uri.Query
$runs = Invoke-RestMethod -Uri $runsUrl
if ($runs | Where-Object { $_.status -in @('running','cleaning_up','awaiting_quota_confirmation') }) { throw 'Active tasks exist; restart refused' }
$owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($meta.pid)"
if (-not $owner -or $owner.CommandLine -notlike '*-m orchestrator*--shared-http*' -or $owner.CommandLine -notlike "*$data*") { throw 'Owner identity mismatch; restart refused' }
Stop-Process -Id $owner.ProcessId
Wait-Process -Id $owner.ProcessId -ErrorAction SilentlyContinue
& (Join-Path $project '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'connect_shared.py') --data-dir $data --workspace-root (Join-Path (Split-Path -Parent $project) 'Desktop\agent-orchestrator') --workspace-root $project --allow-any-workspace --apply
if ($LASTEXITCODE -ne 0) { throw 'Restart failed; inspect shared-owner.log' }
$fresh = Get-Content -LiteralPath (Join-Path $data 'server.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$uri = [Uri]$fresh.dashboard
$catalog = Invoke-RestMethod -Uri ($uri.GetLeftPart([System.UriPartial]::Authority) + '/api/models' + $uri.Query)
if (-not $catalog.models.codex) { throw 'New model catalog unavailable' }
Start-Process $fresh.dashboard
Write-Output 'Restarted updated Orchestra; catalog verified; dashboard opened.'
