# A temporary HTTPS link to the panel (for the phone), like the old START_REMOTE_TUNNEL.bat.
# The link changes every time; it works while this window is open.
param([string]$Root = 'C:\ZephyrPlatform')
. (Join-Path $PSScriptRoot 'common.ps1')
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
$Port = [int](Read-EnvFile (Join-Path $Root '.env'))['ZP_PORT']
$exe = Join-Path $Root 'tools\cloudflared.exe'
if (-not (Test-Path -LiteralPath $exe)) {
    $old = Get-ChildItem -Path 'C:\EldoradoBots' -Filter 'cloudflared.exe' -Recurse -Depth 3 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($old) { Copy-Item -LiteralPath $old.FullName -Destination $exe } else {
        Invoke-Download 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' $exe
    }
}
Write-Host "Link to the panel (port $Port). Look for https://....trycloudflare.com below; close this window to end it." -ForegroundColor Cyan
& $exe tunnel --no-autoupdate --url "http://127.0.0.1:$Port"
