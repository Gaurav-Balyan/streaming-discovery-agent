# Starts Agent B, the Streaming Discovery Agent, in THIS window. Nothing here knows about Agent A:
# Agent A only needs this agent's URL (DISCOVERY_AGENT_URL) and a Keycloak token for audience discovery-agent.
# Run it from this repo's folder, with this repo's environment active:
#   conda activate discovery-agent
#   .\start-discovery-agent.ps1
# 1. checks that the token issuer (Keycloak) answers: without its keys every caller is refused
# 2. checks that the port is free
# 3. runs  python -m discovery_agent  (Ctrl+C stops it)
# In the cloud (Phase 8) this script is not used: the container runs  python -m discovery_agent  itself.

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
Set-Location $root

function Get-Setting($name, $default) {
    # A setting from this window's environment, else from .env (commented lines with # are skipped), else the default.
    $value = [Environment]::GetEnvironmentVariable($name)
    if (-not $value -and (Test-Path "$root\.env")) {
        $line = Select-String -Path "$root\.env" -Pattern "^\s*$name\s*=\s*(\S+)" | Select-Object -First 1
        if ($line) { $value = $line.Matches[0].Groups[1].Value.Trim('"', "'") }
    }
    if (-not $value) { $value = $default }
    return $value
}

$issuer = Get-Setting "OIDC_ISSUER" "http://localhost:8080/realms/household"
$port = [int](Get-Setting "DISCOVERY_PORT" "8010")

# ---- 1. The token issuer
try {
    Invoke-WebRequest "$issuer/.well-known/openid-configuration" -UseBasicParsing -TimeoutSec 5 | Out-Null
    Write-Host "Token issuer answers: $issuer"
} catch {
    Write-Host "The token issuer does not answer at $issuer. Agent B will start, but refuse every caller (503)" -ForegroundColor Yellow
    Write-Host "until it does. Start Keycloak first (wherever it runs)." -ForegroundColor Yellow
}

# ---- 2. The port
$busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "Port $port is already in use: is Agent B already running? (Invoke-WebRequest http://127.0.0.1:$port/healthz)" -ForegroundColor Red
    exit 1
}

# ---- 3. Run it
$host.UI.RawUI.WindowTitle = "discovery-agent (Agent B) :$port"
python -m discovery_agent
