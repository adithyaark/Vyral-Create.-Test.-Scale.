@echo off
title Vyral
cd /d "%~dp0"
uv run python -m vyral %*
pause
