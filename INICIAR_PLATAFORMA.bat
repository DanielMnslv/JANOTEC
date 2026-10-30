@echo off
rem Abre la plataforma JANOTEC: el servidor y el tunel publico, cada uno en su ventana.
rem Ambos se reinician solos si fallan. Para apagar la plataforma, cierre las dos ventanas.
rem Si ya estan abiertos no los duplica (asi la direccion publica no cambia).
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { exit 1 }"
if errorlevel 1 (
  echo El servidor ya esta abierto.
) else (
  start "JANOTEC - Servidor" cmd /c "%~dp0arranque\servidor.bat"
  timeout /t 5 /nobreak >nul
)
tasklist /fi "imagename eq cloudflared.exe" | find /i "cloudflared.exe" >nul
if errorlevel 1 (
  start "JANOTEC - Tunel publico" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0arranque\tunel.ps1"
) else (
  echo El tunel ya esta abierto. Enlace actual:
  type "%~dp0ENLACE_ACTUAL.txt"
  timeout /t 8 >nul
)
