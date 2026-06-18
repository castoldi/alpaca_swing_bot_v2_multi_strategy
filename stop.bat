@echo off
pwsh -NonInteractive -File "%~dp0scripts\manage.ps1" stop-bot
pwsh -NonInteractive -File "%~dp0scripts\manage.ps1" stop-dashboard
