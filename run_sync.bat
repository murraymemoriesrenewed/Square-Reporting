@echo off
REM Runs the Square sync once. Point Windows Task Scheduler at this file.
cd /d "%~dp0"
docker compose run --rm sync >> sync.log 2>&1
