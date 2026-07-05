# tgvault installer for Windows (PowerShell). No Git or Python knowledge needed.
$ErrorActionPreference = "Stop"

$Repo = if ($env:TGVAULT_REPO) { $env:TGVAULT_REPO } else { "git+https://github.com/vlad-ds/tgvault" }

Write-Host "Installing tgvault..."

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (Python package manager)..."
    irm https://astral.sh/uv/install.ps1 | iex
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

uv tool install --force --python 3.12 $Repo

Write-Host ""
Write-Host "Done! Next steps:"
Write-Host "  1. Open a NEW PowerShell window (so the tgvault command is found)"
Write-Host "  2. Run: tgvault login      (scan the QR code with the Telegram app)"
Write-Host "  3. Run: tgvault chats      (see your chats)"
Write-Host "  4. Run: tgvault watch `"<chat name>`"   then   tgvault sync"
