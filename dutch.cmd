@echo off
rem Windows launcher for the Dutch SRS app.
rem Resolves its own folder, then runs the Python CLI inside it.
setlocal
set "HERE=%~dp0"
python "%HERE%app\cli.py" %*
