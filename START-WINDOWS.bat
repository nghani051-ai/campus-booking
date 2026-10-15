@echo off
cd /d "%~dp0"
where python >nul 2>nul && (python app.py) || (where py >nul 2>nul && (py app.py) || (echo Python is not installed. Get it from python.org, tick "Add to PATH", then double-click this again. & pause))
