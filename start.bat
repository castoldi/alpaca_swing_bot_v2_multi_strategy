@echo off
pwsh -NonInteractive -File "%~dp0scripts\manage.ps1" start-dashboard
pwsh -NonInteractive -File "%~dp0scripts\manage.ps1" start-bot
