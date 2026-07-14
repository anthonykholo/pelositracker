@echo off
setlocal
cd /d "%~dp0"
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat" -arch=x64
set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
".venv\Scripts\python.exe" -m maturin develop --release

