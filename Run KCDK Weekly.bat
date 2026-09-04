@echo off
setlocal
cd /d "%~dp0"

set "KCDK_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%KCDK_PYTHON%" (
    echo KCDK's local Python environment was not found.
    echo.
    echo From PowerShell in this folder, run:
    echo   py -3 -m venv .venv
    echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    echo   .\.venv\Scripts\python.exe -m pip install -e .
    echo.
    echo Then double-click this launcher again.
    pause
    exit /b 1
)

"%KCDK_PYTHON%" -c "import kcdk" >nul 2>nul
if errorlevel 1 (
    echo KCDK is not installed in this project's Python environment.
    echo.
    echo From PowerShell in this folder, run:
    echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    echo   .\.venv\Scripts\python.exe -m pip install -e .
    echo.
    echo Then double-click this launcher again.
    pause
    exit /b 1
)

"%KCDK_PYTHON%" -m kcdk weekly-runner %*
set "KCDK_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%KCDK_EXIT_CODE%"=="0" echo KCDK weekly runner ended with an error.
pause
exit /b %KCDK_EXIT_CODE%
