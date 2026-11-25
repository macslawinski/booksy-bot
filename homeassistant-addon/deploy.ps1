# Copy the add-on and current bot code to Home Assistant OS over the Samba share add-on.
#   powershell -File homeassistant-addon\deploy.ps1 [-WithData]
# -WithData also copies .env and booksy_bot.sqlite3 (first install only: later it would overwrite the bot's live DB).
param([switch]$WithData, [string]$HaHost = "homeassistant.local")
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot
$addon = "\\$HaHost\addons\booksy-bot"

New-Item -ItemType Directory -Force $addon | Out-Null
Copy-Item "$PSScriptRoot\config.yaml", "$PSScriptRoot\Dockerfile", "$repo\pyproject.toml" $addon -Force
# run.sh must keep Unix line endings or the container fails with "not found"
[IO.File]::WriteAllText("$addon\run.sh", ([IO.File]::ReadAllText("$PSScriptRoot\run.sh") -replace "`r`n", "`n"))
robocopy "$repo\booksy_bot" "$addon\booksy_bot" /MIR /XD __pycache__ /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }

if ($WithData) {
    $data = "\\$HaHost\config\booksy-bot"
    New-Item -ItemType Directory -Force $data | Out-Null
    # only the bot's own settings: other secrets kept in the local .env (e.g. Samba login) stay on this PC
    $keep = Get-Content "$repo\.env" -Encoding UTF8 | Where-Object { $_ -match '^\s*(BOOKSY_|TELEGRAM_|CHECK_MINUTES|BOT_DB)' }
    [IO.File]::WriteAllText("$data\.env", (($keep -join "`n") + "`n"))
    if (Test-Path "$repo\booksy_bot.sqlite3") { Copy-Item "$repo\booksy_bot.sqlite3" $data -Force }
}
Write-Host "OK: $addon. In HA: Apps -> Store -> ... -> Check for updates -> Booksy bot (Local) -> Install / Rebuild."
