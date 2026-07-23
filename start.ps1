# Starts the Jumia Feed Sync dashboard: syncs deps, ensures .env exists,
# opens the Run screen in your browser, then runs the server in the
# foreground (Ctrl+C to stop). See Readme.md #4, #10.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is not installed. Install it from https://astral.sh/uv, then re-run this script."
    exit 1
}

uv sync

if (-not (Test-Path ".env")) {
    Copy-Item "env.example" ".env"
    Write-Host "Created .env from env.example -- edit it if you need non-default paths."
}

$port = if ($env:DASHBOARD_PORT) { $env:DASHBOARD_PORT } else { "8000" }

Start-Job -ScriptBlock {
    param($url)
    Start-Sleep -Seconds 2
    Start-Process $url
} -ArgumentList "http://127.0.0.1:$port/run" | Out-Null

Write-Host "Starting dashboard at http://127.0.0.1:$port/run (Ctrl+C to stop)"
uv run jumia-feed-sync serve
