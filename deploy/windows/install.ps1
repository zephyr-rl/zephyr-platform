# Installs Zephyr Platform on this Windows Server, next to the old bots, without touching them.
# Run INSTALL.bat (it asks for administrator rights). Safe to run again: nothing is replaced
# that already works (the keys in .env are never changed).
param(
    [string]$Root = 'C:\ZephyrPlatform',
    [int]$Port = 8600,
    [int]$PgPort = 5433,
    [string]$OwnerEmail = '',
    [string]$OwnerPasswordEnv = '',
    [switch]$NonInteractive
)
. (Join-Path $PSScriptRoot 'common.ps1')

$Kit = Split-Path -Parent $PSScriptRoot
Assert-Admin
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')

Write-Host ''
Write-Host 'Zephyr Platform - install' -ForegroundColor White
Write-Host "  Folder:   $Root"
Write-Host "  Panel:    http://localhost:$Port (only on this server; REMOTE_ACCESS.bat for the phone)"
Write-Host "  Database: PostgreSQL service '$PgService' on port $PgPort (only on this server)"
Write-Host '  The old bots and their folders are not touched. Every store starts in shadow mode.'

Write-Step 'Checking the ports'
$firstInstall = -not (Test-Path -LiteralPath (Join-Path $Root '.env'))
if ($firstInstall -and -not (Test-PortFree $Port)) { throw "Port $Port is already in use. Run again with -Port <another>." }
$hasPg = [bool](Get-Service -Name $PgService -ErrorAction SilentlyContinue)
if (-not $hasPg -and -not (Test-PortFree $PgPort)) { throw "Port $PgPort is already in use. Run again with -PgPort <another>." }
Write-Ok 'free'

Write-Step 'Copying the program'
foreach ($d in @('app', 'logs', 'run', 'backups', 'legacy', 'tools')) { New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null }
$running = @(Get-PlatformProcesses $Root)
if ($running.Count -gt 0) { Stop-Platform $Root }
Copy-Tree (Join-Path $Kit 'app') (Join-Path $Root 'app')
Install-Scripts $Kit $Root
Write-Ok (Get-Content -LiteralPath (Join-Path $Root 'app\VERSION') -ErrorAction SilentlyContinue)

Write-Step 'Installing Python and the packages (first time: a few minutes)'
$py = Sync-Python $Root
Write-Ok $py

Write-Step 'Settings (.env)'
if ((Invoke-Py $Root @('-m', 'app.deploy.setup', 'init-env', '--root', $Root, '--port', "$Port", '--pg-port', "$PgPort")) -ne 0) { throw 'Creating .env failed.' }
$envValues = Read-EnvFile (Join-Path $Root '.env')
$Port = [int]$envValues['ZP_PORT']
$PgPort = [int]$envValues['ZP_PG_PORT']
# Only Administrators and SYSTEM may read the folder with the keys.
& icacls (Join-Path $Root '.env') /inheritance:r /grant:r '*S-1-5-32-544:F' '*S-1-5-18:F' | Out-Null
Write-Ok 'keys created (kept if they existed)'

Write-Step 'PostgreSQL'
if ($hasPg) {
    Write-Ok "service '$PgService' already installed"
} else {
    $installer = Get-ChildItem -Path (Join-Path $Kit 'downloads') -Filter 'postgresql-16*-windows-x64.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($installer) {
        $exe = $installer.FullName
    } else {
        $exe = Join-Path $Root 'tools\postgresql-16-windows-x64.exe'
        if (-not (Test-Path -LiteralPath $exe)) {
            $found = $null
            foreach ($minor in 20..8) {
                $url = "https://get.enterprisedb.com/postgresql/postgresql-16.$minor-1-windows-x64.exe"
                try {
                    $head = Invoke-WebRequest -Uri $url -Method Head -UseBasicParsing -TimeoutSec 20
                    if ($head.StatusCode -eq 200) { $found = $url; break }
                } catch { }
            }
            if (-not $found) { throw 'Could not find the PostgreSQL 16 installer. Download it from https://www.enterprisedb.com/downloads/postgres-postgresql-downloads into the kit''s downloads folder and run again.' }
            Invoke-Download $found $exe
        }
    }
    $pgArgs = @(
        '--mode unattended', '--unattendedmodeui none',
        "--superpassword `"$($envValues['ZP_PG_SUPERPASSWORD'])`"",
        "--serverport $PgPort",
        "--prefix `"$Root\pgsql`"", "--datadir `"$Root\pgdata`"",
        "--servicename $PgService",
        '--disable-components pgAdmin,stackbuilder',
        '--install_runtimes 1'
    ) -join ' '
    Write-Note 'Installing (about 2 minutes)...'
    $p = Start-Process -FilePath $exe -ArgumentList $pgArgs -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "The PostgreSQL installer failed (exit $($p.ExitCode)). Its log is in $env:TEMP (install-postgresql.log)." }
    # Listen on this machine only.
    $conf = Join-Path $Root 'pgdata\postgresql.conf'
    $text = [IO.File]::ReadAllText($conf)
    $text = [Regex]::Replace($text, "(?m)^\s*#?\s*listen_addresses\s*=.*$", "listen_addresses = 'localhost'")
    [IO.File]::WriteAllText($conf, $text, (New-Object Text.UTF8Encoding($false)))
    Restart-Service -Name $PgService
    Write-Ok "installed in $Root\pgsql, data in $Root\pgdata"
}
Set-Service -Name $PgService -StartupType Automatic

Write-Step 'Database'
if ((Invoke-Py $Root @('-m', 'app.deploy.setup', 'init-db', '--root', $Root)) -ne 0) { throw 'Preparing the database failed.' }
Write-Ok 'ready'

Write-Step 'Owner account (to sign in to the panel)'
if ((Invoke-Py $Root @('-m', 'app.deploy.setup', 'has-owner', '--root', $Root)) -eq 0) {
    Write-Ok 'already exists'
} else {
    if (-not $OwnerEmail) {
        if ($NonInteractive) { throw 'No owner account: pass -OwnerEmail.' }
        $OwnerEmail = Read-Host 'Your e-mail for signing in'
    }
    $cli = @('-m', 'app.deploy.run', '--root', $Root, 'create-owner', '--email', $OwnerEmail)
    if ($OwnerPasswordEnv) { $cli += @('--password-env', $OwnerPasswordEnv) }
    if ((Invoke-Py $Root $cli) -ne 0) { throw 'Creating the owner account failed.' }
}

Write-Step 'Starting automatically with Windows'
Register-PlatformTasks $Root
Start-Platform $Root
if (-not (Wait-Ready $Port 180)) { throw "The panel did not answer on port $Port. See $Root\logs\supervisor.log and api.log." }
Write-Ok "tasks '$TaskName' and '$BackupTaskName'"

$shortcut = Join-Path $env:PUBLIC 'Desktop\Zephyr Platform.url'
Set-Content -LiteralPath $shortcut -Value "[InternetShortcut]`r`nURL=http://localhost:$Port/" -Encoding ASCII

Write-Host ''
Write-Host "Done. Open http://localhost:$Port on this server (shortcut on the desktop)." -ForegroundColor Green
Write-Host "Keep a copy of $Root\.env outside the server: without it the stored credentials cannot be read."
Write-Host 'Next: docs\DEPLOY-WINDOWS.md, step "Import the stores".'
