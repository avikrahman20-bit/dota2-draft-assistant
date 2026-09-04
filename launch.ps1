# Launch Dota 2 Draft Assistant with Cloudflare Tunnel
# Run: powershell -ExecutionPolicy Bypass -File launch.ps1

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = "C:\Users\Khan Gadget\AppData\Local\Programs\Python\Python312\python.exe"
$CloudflaredPath = "$env:LOCALAPPDATA\cloudflared\cloudflared.exe"

Write-Host "======================================"
Write-Host "Dota 2 Draft Assistant - Launch"
Write-Host "======================================"
Write-Host ""

# Check if cloudflared exists
if (-not (Test-Path $CloudflaredPath)) {
    Write-Host "ERROR: cloudflared not found at $CloudflaredPath"
    Write-Host "Please run installation steps first."
    exit 1
}

# Check if .env exists
if (-not (Test-Path "$AppDir\.env")) {
    Write-Host "ERROR: .env file not found"
    Write-Host "Copy .env.example to .env and add your API keys:"
    Write-Host "  - STRATZ_API_KEY from https://stratz.com/api-token"
    Write-Host "  - ANTHROPIC_API_KEY from https://console.anthropic.com/"
    exit 1
}

Write-Host "Starting Dota 2 Draft Assistant..."
Write-Host ""

# Kill any existing python processes on port 8000
$existing = Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Handle -gt 0 }
if ($existing) {
    Write-Host "Stopping existing processes..."
    Stop-Process -Name python -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# Start the app server in background
Write-Host "1. Starting FastAPI server on http://127.0.0.1:8000"
$serverProcess = Start-Process -FilePath $PythonPath -ArgumentList "$AppDir\app.py" -WorkingDirectory $AppDir -PassThru -NoNewWindow

Start-Sleep -Seconds 3

# Start cloudflared tunnel in background
Write-Host "2. Starting Cloudflare Tunnel..."
$tunnelProcess = Start-Process -FilePath $CloudflaredPath -ArgumentList "tunnel", "--url", "http://localhost:8000" -PassThru -NoNewWindow

Write-Host ""
Write-Host "======================================"
Write-Host "Both processes started!"
Write-Host "======================================"
Write-Host ""
Write-Host "Your public URL will appear in the cloudflared window above."
Write-Host "It will be something like: https://xxxx-xxxx-xxxx.trycloudflare.com"
Write-Host ""
Write-Host "Press Ctrl+C to stop both services."
Write-Host ""

# Wait for both processes
$serverProcess | Wait-Process
$tunnelProcess | Wait-Process
