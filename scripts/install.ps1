# Smart Build installer for Windows:  irm https://github.com/michael-berardi/smart-build/releases/latest/download/install.ps1 | iex
# Smart Build needs POSIX file locks (fcntl.flock) and process-group signals, which Python on Windows does not
# provide, so there is no native Windows build. On Windows it runs inside WSL (Windows Subsystem for Linux).
# This script explains that, prints the one-step WSL command, and exits non-zero. It installs nothing.
$ErrorActionPreference = 'Stop'
Write-Host 'Smart Build runs on macOS and Linux. It needs POSIX file locks and process-group signals,'
Write-Host 'which Python on Windows does not provide, so on Windows it runs inside WSL (Windows Subsystem for Linux).'
Write-Host ''
Write-Host 'One step:'
Write-Host '  1. If WSL is missing:  wsl --install -d Ubuntu   (restart, then open Ubuntu once to create your user)'
Write-Host '  2. Install Smart Build inside WSL:'
Write-Host '     wsl -e sh -c "curl -fsSL https://github.com/michael-berardi/smart-build/releases/latest/download/install.sh | sh"'
Write-Host ''
Write-Host 'Then use it from your WSL shell:  smart-build -- <your build command>'
exit 1
