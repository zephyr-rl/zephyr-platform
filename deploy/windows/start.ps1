param([string]$Root = 'C:\ZephyrPlatform')
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Admin
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
$Port = [int](Read-EnvFile (Join-Path $Root '.env'))['ZP_PORT']
Start-Platform $Root
if (Wait-Ready $Port 180) { Write-Host "Running: http://localhost:$Port" -ForegroundColor Green } else { throw "Not answering. See $Root\logs\supervisor.log" }
