@echo off
REM MyWealthLens — Scheduled Jobs Runner (Batch 4, Sep 2026)
REM ============================================================
REM Runs every one of the app's existing `flask ... ` CLI jobs in one
REM go, so Windows Task Scheduler only ever needs to point at ONE
REM thing (this file) instead of five separate scheduled tasks.
REM
REM Every command below is already designed to be safe to run any
REM number of times a day (see each command's own module docstring):
REM   - prices refresh          — re-fetches stock prices / MF NAVs
REM   - wealth snapshot         — records today's net worth snapshot
REM   - notifications monthly-summary — only actually emails on the 1st
REM   - notifications reminders — only emails when something's actually due
REM   - backup run              — rebuilds the local encrypted backup zip
REM So running this once a day, every day, is exactly the intended use
REM — nothing here needs a fancier schedule than "daily."
REM
REM %~dp0 always resolves to THIS file's own folder, regardless of what
REM directory Task Scheduler happens to launch it from — so this still
REM works correctly no matter how the scheduled task is configured.
setlocal
cd /d "%~dp0"

echo ==== %date% %time% ==== >> scheduled_jobs.log

echo Running: prices refresh >> scheduled_jobs.log
py -m flask --app app prices refresh >> scheduled_jobs.log 2>&1

echo Running: wealth snapshot >> scheduled_jobs.log
py -m flask --app app wealth snapshot >> scheduled_jobs.log 2>&1

echo Running: notifications monthly-summary >> scheduled_jobs.log
py -m flask --app app notifications monthly-summary >> scheduled_jobs.log 2>&1

echo Running: notifications reminders >> scheduled_jobs.log
py -m flask --app app notifications reminders >> scheduled_jobs.log 2>&1

echo Running: backup run >> scheduled_jobs.log
py -m flask --app app backup run >> scheduled_jobs.log 2>&1

echo ==== done ==== >> scheduled_jobs.log
echo. >> scheduled_jobs.log

endlocal
