
# AutoSub Windows One-Click Installer
# This script installs Python, FFmpeg, and all dependencies for the AutoSub Workflow.

$ErrorActionPreference = "Stop"

function Write-Header($msg) {
    Write-Host "`n=== $msg ===" -ForegroundColor Cyan
}

function Write-Success($msg) {
    Write-Host "✅ $msg" -ForegroundColor Green
}

function Write-Info($msg) {
    Write-Host "ℹ️ $msg" -ForegroundColor Blue
}

function Write-Warn($msg) {
    Write-Host "⚠️ $msg" -ForegroundColor Yellow
}

# 1. Define Paths
$InstallDir = $PSScriptRoot
$ToolsDir = Join-Path $InstallDir "Library\Tools"
$WorkflowDir = Join-Path $InstallDir ".agent\workflows"

Write-Header "Starting AutoSub Installation"

# 2. Check Python
Write-Info "Checking Python installation..."
try {
    $pyVersion = python --version 2>$null
    if ($pyVersion -match "Python 3\.(1[2-9]|[2-9])") {
        Write-Success "Found Python $pyVersion"
    } else {
        throw "Python version too old or not found"
    }
} catch {
    Write-Warn "Python 3.12+ not found. Attempting to install via WinGet..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements
        Write-Info "Python installed. Please restart this script in a new terminal window if it fails next."
        # Try to refresh path
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    } else {
        Write-Error "WinGet not found. Please install Python 3.12 manually from https://www.python.org/downloads/"
        exit 1
    }
}

# 3. Check FFmpeg
Write-Info "Checking FFmpeg..."
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Success "Found FFmpeg"
} else {
    Write-Warn "FFmpeg not found. Attempting to install via WinGet..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
        Write-Success "FFmpeg installed."
    } else {
        Write-Warn "Could not install FFmpeg automatically. AutoSub will try to find it in common directories."
    }
}

# 4. Install Dependencies
Write-Header "Installing Python Dependencies"
$requirements = @(
    "yt-dlp",
    "faster-whisper",
    "google-generativeai",
    "python-dotenv",
    "requests"
)

foreach ($req in $requirements) {
    Write-Info "Installing $req..."
    python -m pip install $req --quiet
}
Write-Success "All dependencies installed."

# 5. Path Adaptation (Magic Step)
Write-Header "Adapting Workflow Paths"
$WorkflowFile = Join-Path $WorkflowDir "autosub.md"
if (Test-Path $WorkflowFile) {
    $content = Get-Content $WorkflowFile -Raw
    # Replace the hardcoded d:\cc with the actual current installation directory
    # Note: We escape backslashes for the markdown file
    $ActualPath = $InstallDir.Replace("\", "\\")
    $NewContent = $content -replace "d:\\\\cc", $ActualPath
    
    # Also handle single backslash cases if any
    $NewContent = $NewContent -replace "d:\\cc", $InstallDir
    
    Set-Content $WorkflowFile $NewContent
    Write-Success "Workflow paths updated to: $InstallDir"
} else {
    Write-Warn "Workflow file not found at $WorkflowFile"
}

# 6. Setup .env template
$EnvFile = Join-Path $InstallDir ".env"
if (-not (Test-Path $EnvFile)) {
    Write-Header "Creating .env Template"
    "GEMINI_API_KEY=YOUR_KEY_HERE`nGEMINI_MODEL=gemini-1.5-flash" | Set-Content $EnvFile
    Write-Info "Created .env file. Please add your GEMINI_API_KEY!"
}

Write-Header "Installation Complete!"
Write-Info "To start the GUI, run:"
Write-Info "python `"$InstallDir\Library\Tools\autosub\autosub_gui.py`""
Write-Info "Or use the /autosub command in your IDE."

Pause
