# FORK: one-command installer for the rdnot/nanobot-medical-research fork
# (Windows PowerShell). Resolves the newest wheel published by the fork-release
# workflow (release tag latest-<branch>, WebUI already bundled) and hands it to
# the regular nanobot installer via NANOBOT_INSTALL_TARGET. Re-run to update.
#
#   irm https://raw.githubusercontent.com/rdnot/nanobot-medical-research/main/scripts/install-fork.ps1 | iex
#   $env:NANOBOT_FORK_BRANCH = "scrapling"; irm ... | iex      # browser-tier branch

$ErrorActionPreference = "Stop"

$Repo = "rdnot/nanobot-medical-research"
$Branch = if ($env:NANOBOT_FORK_BRANCH) { $env:NANOBOT_FORK_BRANCH } else { "main" }
if ($Branch -notin @("main", "scrapling")) {
    throw "NANOBOT_FORK_BRANCH must be `"main`" or `"scrapling`" (got `"$Branch`")."
}

$Api = "https://api.github.com/repos/$Repo/releases/tags/latest-$Branch"
$WheelUrl = $null
try {
    $Release = Invoke-RestMethod -Uri $Api -Headers @{ Accept = "application/vnd.github+json" }
    if ($Release -and $Release.assets) {
        $Asset = $Release.assets | Where-Object { $_.name -like "*.whl" } | Select-Object -First 1
        if ($Asset) { $WheelUrl = $Asset.browser_download_url }
    }
} catch {
    $WheelUrl = $null
}

if (-not $WheelUrl) {
    [Console]::Error.WriteLine("Error: no published build found for branch '$Branch' (release tag latest-$Branch).")
    [Console]::Error.WriteLine("The fork publishes one from GitHub Actions on every push. Until it exists, install from source:")
    [Console]::Error.WriteLine("  git clone https://github.com/$Repo.git; cd nanobot-medical-research; uv sync; uv run nanobot onboard")
    throw "no published build for branch '$Branch'"
}

Write-Host "Installing the nanobot medical-research fork ($Branch branch)"
Write-Host "Wheel: $WheelUrl"

$env:NANOBOT_INSTALL_TARGET = "nanobot-ai @ $WheelUrl"
$env:NANOBOT_INSTALL_SOURCE = "the $Repo fork ($Branch)"

$Installer = Invoke-RestMethod -Uri "https://raw.githubusercontent.com/$Repo/$Branch/scripts/install.ps1"
Invoke-Expression $Installer
