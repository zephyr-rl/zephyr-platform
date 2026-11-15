param([string]$Root = 'C:\ZephyrPlatform')
. (Join-Path $PSScriptRoot 'common.ps1')
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
Write-Host 'Zephyr Platform - check' -ForegroundColor White
$pg = Get-Service -Name $PgService -ErrorAction SilentlyContinue
if ($pg -and $pg.Status -eq 'Running') { Write-Host '  [OK] database service running' } else { Write-Host '  [!!] database service not running' }
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) { Write-Host "  [OK] task '$TaskName': $($task.State)" } else { Write-Host "  [!!] task '$TaskName' missing" }
Write-Host "  processes: $(@(Get-PlatformProcesses $Root).Count) (api, scheduler, workers and the supervisor)"
$code = Invoke-Py $Root @('-m', 'app.deploy.setup', 'check', '--root', $Root)
exit $code
