@echo off
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0scripts\connect_shared.py" --data-dir "%USERPROFILE%\Desktop\agent-orchestrator\.orchestra-data" --workspace-root "%USERPROFILE%\Desktop\agent-orchestrator" --workspace-root "%~dp0." --allow-any-workspace --apply
if errorlevel 1 pause
