@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found.
  echo Run: python -m venv .venv
  echo Then: .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)
if not exist ".env" (
  if exist "env.example" (
    copy /y "env.example" ".env" >nul
    echo Created .env from env.example.
  ) else (
    echo No .env file found; using the app's built-in defaults.
  )
)
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8765
