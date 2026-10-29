@echo off
setlocal
title UltraK - upscale video 100%% local
cd /d "%~dp0"

rem --- trouve Python (py en priorite, evite le leurre du Microsoft Store) ---
where py >/dev/null 2>/dev/null && (set "ULTRAK=py -3")
if not defined ULTRAK where python >/dev/null 2>/dev/null && (set "ULTRAK=python")
if not defined ULTRAK goto :no_python

%ULTRAK% -c "import sys;sys.exit(0 if sys.version_info>=(3,8) else 1)" >/dev/null 2>/dev/null
if errorlevel 1 goto :old_python

if "%~1"=="" (
  %ULTRAK% ultrak.py gui
) else (
  %ULTRAK% ultrak.py gui %*
)
if errorlevel 1 goto :crash
endlocal & exit /b 0

:no_python
echo [UltraK] Python 3.8+ est necessaire.
echo   1) telecharge-le:  https://www.python.org/downloads/windows/
echo   2) pendant l'installation, coche "Add python.exe to PATH"
echo   3) relance ce Launch.bat
start "" "https://www.python.org/downloads/windows/"
pause
endlocal & exit /b 1

:old_python
echo [UltraK] Python trouve mais trop vieux (il faut 3.8 ou plus).
echo   Telecharge une version recente: https://www.python.org/downloads/windows/
pause
endlocal & exit /b 1

:crash
echo.
echo [UltraK] le rendu s'est arrete sur une erreur.
echo   Relance avec plus de detail dans une fenetre de commande:
echo     set ULTRAK_DEBUG=1 ^&^& ultrak.py doctor
pause
endlocal & exit /b 1
