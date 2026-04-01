@echo off
echo Installing Image Deduper dependencies...
pip install -r "%~dp0..\requirements.txt"
echo.
echo Done! You can now run the app with run.bat
pause
