@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" "view\serve_review_dashboard.py" --host 127.0.0.1 --port 8765
pause
