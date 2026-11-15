# Stops the panel, the workers and the scheduler until START.bat (also after a restart of Windows,
# because the stop file stays). The database keeps running.
param([string]$Root = 'C:\ZephyrPlatform')
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Admin
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
Stop-Platform $Root
Write-Host 'Stopped. START.bat starts it again.' -ForegroundColor Green
