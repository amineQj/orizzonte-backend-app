@echo off
cd /d "%~dp0"
C:\orizzonte_easyocr_env\Scripts\python.exe -m uvicorn easyocr_server:app --host 127.0.0.1 --port 8001
if errorlevel 1 pause