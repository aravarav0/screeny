@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\screeny.exe" (
    echo Screeny is not set up yet.
    echo Run: python -m venv .venv
    echo Then: .venv\Scripts\pip install -e ".[voice]"
    pause
    exit /b 1
)

echo Checking Ollama...
set /a tries=0
:wait_ollama
".venv\Scripts\python.exe" -c "import httpx; httpx.get('http://127.0.0.1:11434/api/tags', timeout=2).raise_for_status()" >nul 2>&1
if %errorlevel%==0 goto ollama_ready
set /a tries+=1
if %tries% GEQ 15 (
    echo Ollama is not responding. Open the Ollama app, wait a few seconds, then press any key...
    pause >nul
    goto wait_ollama
)
timeout /t 2 /nobreak >nul
goto wait_ollama

:ollama_ready
echo Ollama is ready.
".venv\Scripts\screeny.exe"

if errorlevel 1 pause
