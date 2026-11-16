# Shared functions for the Zephyr Platform Windows scripts. ASCII only (Windows PowerShell 5.1).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:PYTHONUNBUFFERED = '1'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$TaskName = 'Zephyr Platform'
$BackupTaskName = 'Zephyr Platform backup'
$PgService = 'ZephyrPostgres'

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "    $Text" }

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this as Administrator (right click > Run as administrator).'
    }
}

function Read-EnvFile([string]$Path) {
    $values = @{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in [IO.File]::ReadAllLines($Path)) {
            $t = $line.Trim()
            if ($t -eq '' -or $t.StartsWith('#') -or -not $t.Contains('=')) { continue }
            $i = $t.IndexOf('=')
            $values[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim().Trim('"').Trim("'")
        }
    }
    return $values
}

function Test-PortFree([int]$Port) {
    $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return -not $busy
}

function Invoke-Download([string]$Url, [string]$Target) {
    Write-Note "Downloading $Url"
    $tmp = "$Target.part"
    Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing
    Move-Item -LiteralPath $tmp -Destination $Target -Force
}

function Get-Uv([string]$Root) {
    $uv = Join-Path $Root 'tools\uv.exe'
    if (-not (Test-Path -LiteralPath $uv)) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Root 'tools') | Out-Null
        $zip = Join-Path $Root 'tools\uv.zip'
        Invoke-Download 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' $zip
        Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $Root 'tools') -Force
        Remove-Item -LiteralPath $zip
    }
    return $uv
}

function Sync-Python([string]$Root) {
    # Native programs write progress to stderr; with output redirected (scheduled or remote runs)
    # Windows PowerShell turns that into errors. Only the exit code decides here.
    $ErrorActionPreference = 'Continue'
    $uv = Get-Uv $Root
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $Root 'python'
    $env:UV_CACHE_DIR = Join-Path $Root 'cache'
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $Root 'venv'
    $env:UV_LINK_MODE = 'copy'
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    # Out-Host: the output is shown, not returned (a function returns everything it outputs).
    & $uv sync --frozen --no-dev --no-install-project --python 3.12 --project (Join-Path $Root 'app\backend') | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Installing the Python packages failed (uv exit $LASTEXITCODE)." }
    return (Join-Path $Root 'venv\Scripts\python.exe')
}

function Invoke-Py([string]$Root, [string[]]$Arguments) {
    $ErrorActionPreference = 'Continue'  # see Sync-Python: the exit code decides
    $py = Join-Path $Root 'venv\Scripts\python.exe'
    Push-Location (Join-Path $Root 'app\backend')
    try {
        & $py @Arguments | Out-Host
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    return $code
}

function Wait-Ready([int]$Port, [int]$Seconds = 120) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/v1/ready" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Get-PlatformProcesses([string]$Root) {
    # Only processes started from this install (the old bots use another Python).
    $prefix = $Root.TrimEnd('\') + '\'
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) }
}

function Stop-Platform([string]$Root, [int]$Seconds = 90) {
    $run = Join-Path $Root 'run'
    New-Item -ItemType Directory -Force -Path $run | Out-Null
    Set-Content -LiteralPath (Join-Path $run 'stop') -Value 'stop' -Encoding ASCII
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline -and (Get-PlatformProcesses $Root)) { Start-Sleep -Seconds 1 }
    $left = @(Get-PlatformProcesses $Root)
    if ($left.Count -gt 0) {
        Write-Note "Ending $($left.Count) process(es) that did not stop in time."
        $left | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName }
}

function Start-Platform([string]$Root) {
    Remove-Item -LiteralPath (Join-Path $Root 'run\stop') -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName
}

function Register-PlatformTasks([string]$Root) {
    $py = Join-Path $Root 'venv\Scripts\python.exe'
    $backend = Join-Path $Root 'app\backend'
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

    $action = New-ScheduledTaskAction -Execute $py -Argument "-m app.deploy.supervisor --root `"$Root`"" -WorkingDirectory $backend
    $atStartup = New-ScheduledTaskTrigger -AtStartup
    $every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atStartup, $every5) -Principal $principal `
        -Settings $settings -Description 'Zephyr Platform: panel, workers and scheduler (app.deploy.supervisor).' -Force | Out-Null

    $bAction = New-ScheduledTaskAction -Execute $py -Argument "-m app.deploy.backup --root `"$Root`"" -WorkingDirectory $backend
    $nightly = New-ScheduledTaskTrigger -Daily -At '04:17'
    $bSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable
    Register-ScheduledTask -TaskName $BackupTaskName -Action $bAction -Trigger $nightly -Principal $principal `
        -Settings $bSettings -Description 'Zephyr Platform: nightly database dump, 14 days kept.' -Force | Out-Null
}

function Copy-Tree([string]$From, [string]$To) {
    # Mirror: files removed from the kit are removed from the install too.
    & robocopy $From $To /MIR /NFL /NDL /NJH /NJS /NP /R:2 /W:2 /XD __pycache__ | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $From failed (robocopy $LASTEXITCODE)." }
    $global:LASTEXITCODE = 0
}

function Install-Scripts([string]$Kit, [string]$Root) {
    Copy-Tree (Join-Path $Kit 'scripts') (Join-Path $Root 'scripts')
    foreach ($name in @('START.bat', 'STOP.bat', 'CHECK.bat', 'REMOTE_ACCESS.bat', 'SNAPSHOT_OLD.bat', 'zephyr.bat')) {
        Copy-Item -LiteralPath (Join-Path $Kit "scripts\$name") -Destination (Join-Path $Root $name) -Force
    }
}
