param([Parameter(Mandatory=$true)][string]$NotesFile)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$config = Get-Content -LiteralPath 'version.json' -Raw | ConvertFrom-Json
$tag = 'v' + $config.version
if (-not (Test-Path -LiteralPath $NotesFile)) { throw 'Release notes file not found' }
# gh prompts for login if required; no access token belongs in this project.
& gh release create $tag 'release/setup.exe' 'release/setup.exe.sha256' --repo $config.repository --title ('Telegram Manager ' + $config.version) --notes-file $NotesFile --verify-tag
if ($LASTEXITCODE -ne 0) { throw 'Release publication failed' }
