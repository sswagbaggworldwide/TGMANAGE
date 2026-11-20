param(
    [Parameter(Mandatory=$true)][int]$WaitPid,
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][string]$InstallDir
)
$ErrorActionPreference = 'Stop'
try {
    $appProcess = Get-Process -Id $WaitPid -ErrorAction SilentlyContinue
    if ($appProcess) { $appProcess | Wait-Process -Timeout 120 -ErrorAction Stop }
    if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) { throw 'Installer not found' }
    $updateLog = Join-Path (Split-Path -Parent $Installer) 'install.log'
    $installerArgs = '/SILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /RESTARTAPP=1 /DIR="' + $InstallDir + '" /LOG="' + $updateLog + '"'
    $setup = Start-Process -FilePath $Installer -ArgumentList $installerArgs -WindowStyle Hidden -Wait -PassThru
    if ($setup.ExitCode -ne 0) { throw ('Installer exit code: ' + $setup.ExitCode) }
} catch {
    $_ | Out-String | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'update-error.txt') -Encoding UTF8
}
