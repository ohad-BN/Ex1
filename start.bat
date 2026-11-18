@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"

echo ==================================================
echo   ClassPulse - Starting Docker + Cloudflare Tunnel
echo ==================================================
echo.

:: 1. Ensure Docker Desktop / Daemon is running
docker info >nul 2>&1
if errorlevel 1 (
    echo [1/4] Docker is not running yet. Launching Docker Desktop...
    if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
        start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    )
    powershell -NoProfile -Command "for ($i=1; $i -le 45; $i++) { docker info *> $null; if ($LASTEXITCODE -eq 0) { exit 0 }; Write-Host -NoNewline '.'; Start-Sleep -Seconds 2 }; exit 1"
    if errorlevel 1 (
        echo.
        echo [ERROR] Docker Desktop did not become ready in time. Open Docker Desktop and try again.
        pause
        popd
        exit /b 1
    )
    echo.
) else (
    echo [1/4] Docker daemon is running.
)

:: 2. Start web container and force a fresh Cloudflare Quick Tunnel URL
echo [2/4] Starting web app and fresh Cloudflare tunnel...
docker compose up -d --build
if errorlevel 1 (
    echo [ERROR] docker compose up failed.
    pause
    popd
    exit /b 1
)
docker compose up -d --force-recreate tunnel
if errorlevel 1 (
    echo [ERROR] Failed to start tunnel container.
    pause
    popd
    exit /b 1
)

:: 3. Wait for local health check (5000 primary, 5055 fallback if Windows WSL relay holds 5000) & tunnel URL
echo [3/4] Waiting for ClassPulse health check and public tunnel URL...
set "LOCAL_BASE=http://127.0.0.1:5000"
for /f "usebackq delims=" %%U in (`powershell -NoProfile -Command "$base = ''; for ($i=1; $i -le 25; $i++) { foreach ($p in @('5000','5055')) { try { $r = Invoke-RestMethod ('http://127.0.0.1:' + $p + '/health') -TimeoutSec 1; if ($r.status -eq 'ok') { $base = 'http://127.0.0.1:' + $p; break } } catch {} }; if ($base) { break }; Start-Sleep -Seconds 1 }; if (-not $base) { $base = 'http://127.0.0.1:5055' }; Write-Output $base"`) do set "LOCAL_BASE=%%U"

powershell -NoProfile -Command "$base='%LOCAL_BASE%'; $url = ''; for ($j=1; $j -le 20; $j++) { $logs = docker compose logs tunnel --tail 40 2>&1 | Out-String; $m = [regex]::Matches($logs, 'https://[a-zA-Z0-9-]+\.trycloudflare\.com'); if ($m.Count -gt 0) { $url = $m[$m.Count - 1].Value; break }; Start-Sleep -Seconds 1 }; Write-Host ''; Write-Host '=================================================='; Write-Host '  ClassPulse is UP and READY!' -ForegroundColor Green; Write-Host '=================================================='; Write-Host ('  Local Teacher Portal : ' + $base + '/teacher'); Write-Host ('  Local Portal Hub     : ' + $base + '/portal'); if ($url) { Write-Host ('  Public Tunnel URL    : ' + $url + '/teacher') -ForegroundColor Cyan; Write-Host ('  Public Student Link  : ' + $url + '/student') -ForegroundColor Cyan } else { Write-Host '  Public Tunnel URL    : Still starting (run: docker compose logs tunnel --tail 20)' -ForegroundColor Yellow }; Write-Host '=================================================='; Write-Host ''"

:: 4. Open working local Teacher Portal in default browser
echo [4/4] Opening Teacher Portal in your browser (%LOCAL_BASE%/teacher)...
start "" "%LOCAL_BASE%/teacher"

echo Done! Containers keep running in the background after you close this window.
echo To stop everything later, run: docker compose down
echo.
pause
popd
