# One-shot installer for chat-timeline on Windows (PowerShell 5.1+ / 7+).
# Usage:  .\install.ps1
#         iwr -useb <raw-url>/install.ps1 | iex
$ErrorActionPreference = 'Stop'

function Test-Cmd($name) {
  $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}

$py = $null
foreach ($candidate in 'python', 'py') {
  if (Test-Cmd $candidate) { $py = $candidate; break }
}
if (-not $py) {
  Write-Error "Python 3.9+ is required. Install from https://www.python.org/ or `winget install Python.Python.3.12`."
  exit 1
}

$ver = & $py -c 'import sys; print("%d.%d" % sys.version_info[:2])'
if (-not ($ver -match '^3\.(9|1\d)$')) {
  Write-Warning "Detected Python $ver; chat-timeline requires 3.9+"
}

if (Test-Cmd pipx) {
  pipx install --force chat-timeline
} else {
  Write-Host "pipx not found — installing into the user site with pip"
  & $py -m pip install --user --upgrade chat-timeline
}

if (-not (Test-Cmd timeline)) {
  Write-Host ""
  Write-Host "note: 'timeline' is not on PATH. User-base scripts dir:" -ForegroundColor Yellow
  & $py -m site --user-base
  exit 0
}

$inGit = $false
try {
  $null = git rev-parse --show-toplevel 2>$null
  if ($LASTEXITCODE -eq 0) { $inGit = $true }
} catch {}

if ($inGit) {
  timeline init
} else {
  Write-Host ""
  Write-Host "Skipped 'timeline init' — current directory is not a git repository."
  Write-Host "Run 'timeline init' from inside your project to finish setup."
}
