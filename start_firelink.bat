@echo off
chcp 65001 >nul
REM Double-click = project starts. The map opens immediately, and the data then loads in the same window.
REM A .env file must be next to the program containing FIRMS_MAP_KEY=your_key (the shipped copy includes a ready .env).
cd /d "%~dp0"

REM Find Python: python or the py launcher on Windows
where python >nul 2>nul
if errorlevel 1 (
  set "PY=py -3"
) else (
  set "PY=python"
)

echo Checking the one required package (requests)...
%PY% -m pip install --quiet --disable-pip-version-check requests >nul 2>nul

echo Starting FireLink - the map opens in your browser, data follows...
%PY% -u run_all.py --bbox world --monitor %*

echo.
echo FireLink stopped. Press any key to close.
pause >nul
