# Updates an existing install from this kit: backup, stop, copy, packages, start (the database
# is brought up to date when it starts). Run UPDATE.bat from the NEW kit folder.
param([string]$Root = 'C:\ZephyrPlatform')
. (Join-Path $PSScriptRoot 'common.ps1')
$Kit = Split-Path -Parent $PSScriptRoot
Assert-Admin
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
if (-not (Test-Path -LiteralPath (Join-Path $Root '.env'))) { throw "No install in $Root. Run INSTALL.bat first." }
$envValues = Read-EnvFile (Join-Path $Root '.env')
$Port = [int]$envValues['ZP_PORT']

Write-Step 'Backup before updating'
if ((Invoke-Py $Root @('-m', 'app.deploy.backup', '--root', $Root)) -ne 0) { throw 'The backup failed: nothing was changed.' }
Write-Step 'Stopping (the workers finish their current job)'
Stop-Platform $Root
Write-Step 'Copying the new version'
Copy-Tree (Join-Path $Kit 'app') (Join-Path $Root 'app')
Install-Scripts $Kit $Root
Write-Ok (Get-Content -LiteralPath (Join-Path $Root 'app\VERSION') -ErrorAction SilentlyContinue)
Write-Step 'Packages'
Sync-Python $Root | Out-Null
Register-PlatformTasks $Root
Write-Step 'Starting'
Start-Platform $Root
if (-not (Wait-Ready $Port 180)) { throw "The panel did not answer on port $Port. See $Root\logs\supervisor.log, migrations.log and api.log." }
Write-Host ''
Write-Host "Updated. http://localhost:$Port" -ForegroundColor Green
