@echo off
pwsh -NonInteractive -File "%~dp0scripts\manage.ps1" restart-bot
pwsh -NonInteractive -File "%~dp0scripts\manage.ps1" restart-dashboard
