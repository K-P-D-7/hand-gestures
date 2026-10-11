@echo off
rem Starts Hand Gestures without a console window. Extra arguments (e.g. --camera 1) are passed through.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0hand_gestures.py" %*
