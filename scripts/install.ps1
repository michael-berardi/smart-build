# Smart Build installer for Windows:  irm https://github.com/michael-berardi/smart-build/releases/latest/download/install.ps1 | iex
# Smart Build needs POSIX file locks (fcntl.flock) and process-group signals, which Python on Windows does not
# provide, so there is no native Windows build. On Windows it runs inside WSL (Windows Subsystem for Linux).
# This script explains that, prints the one-step WSL command, and fails with a non-zero status. It installs nothing.
# The body is a function so nothing leaks into the session under `irm | iex`, and it throws instead of calling
# `exit`, which would close the user's own PowerShell window before the instructions can be read.
function Install-SmartBuild {
    Write-Host 'Smart Build runs on macOS and Linux. It needs POSIX file locks and process-group signals,'
    Write-Host 'which Python on Windows does not provide, so on Windows it runs inside WSL (Windows Subsystem for Linux).'
    Write-Host ''
    Write-Host 'One step:'
    Write-Host '  1. If WSL is missing:  wsl --install -d Ubuntu   (restart, then open Ubuntu once to create your user)'
    Write-Host '  2. Install Smart Build inside WSL:'
    Write-Host '     wsl -e sh -c "curl -fsSL https://github.com/michael-berardi/smart-build/releases/latest/download/install.sh | sh"'
    Write-Host ''
    Write-Host 'Then use it from your WSL shell:  smart-build -- <your build command>'
    throw 'Smart Build has no native Windows build; install it inside WSL as shown above.'
}
Install-SmartBuild
