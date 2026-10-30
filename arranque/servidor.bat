@echo off
rem Servidor de la plataforma JANOTEC: si se detiene, vuelve a arrancar solo.
title JANOTEC - Servidor
cd /d "%~dp0.."
:inicio
echo.
echo [%date% %time%] Iniciando el servidor en http://127.0.0.1:8000 ...
".venv\Scripts\python.exe" manage.py runserver 8000
echo.
echo [%date% %time%] El servidor se detuvo. Se reinicia en 5 segundos (cierre esta ventana para apagarlo).
timeout /t 5 /nobreak >nul
goto inicio
