@echo off
cd /d "%~dp0"
py -3 server.py --launch
if %errorlevel%==0 goto :eof
python server.py --launch
if %errorlevel%==0 goto :eof
echo.
echo   Couldn't start Python - see any error above this message.
echo.
echo   If Python isn't installed yet, that's a free, one-time
echo   install - no admin rights needed. Opening the Microsoft
echo   Store: click "Get", wait for it to finish, then
echo   double-click this file again.
echo.
echo   If Python IS already installed and you still see this,
echo   email samuelbf@uark.edu with a screenshot of this window.
echo.
start "" "ms-windows-store://search/?query=Python 3"
pause
