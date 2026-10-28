@echo off
setlocal
cd /d "%~dp0"

rem If server.py isn't beside us, this was launched from INSIDE the .zip.
if not exist "server.py" goto notextracted

rem Find a working Python 3.9+ without tripping the Store alias. Each
rem candidate is run with an argument (the alias only opens the Store when
rem run with none) and must report 3.9 or newer - an old Python 2 on the
rem PATH must not be mistaken for a working install.
set "PY="
for %%C in ("py -3" "python" "python3") do (
  if not defined PY (
    %%~C -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=%%~C"
  )
)
if defined PY goto havepython
py -3 --version >nul 2>&1 && goto tooold
python --version >nul 2>&1 && goto tooold
python3 --version >nul 2>&1 && goto tooold
goto nopython

:havepython
%PY% server.py --launch
if %errorlevel%==0 goto :eof
goto startfailed

:notextracted
echo.
echo   It looks like you opened this from INSIDE the .zip file.
echo   Windows cannot run the app from there.
echo.
echo   Do this instead:
echo     1. Close this window.
echo     2. Right-click GrantsManager.zip and choose "Extract All...".
echo     3. Open the extracted folder.
echo     4. Double-click this file again.
echo.
pause
goto :eof

:tooold
echo.
echo   The Python on this computer is too old - this app needs Python 3.9
echo   or newer. Installing a current one is free and needs no admin rights.
echo   Opening the Microsoft Store: click "Get" (or "Update"), wait for it
echo   to finish, then double-click this file again.
echo.
start "" "ms-windows-store://search/?query=Python 3"
pause
goto :eof

:nopython
echo.
echo   Python 3 isn't installed yet. It's a free, one-time install -
echo   no admin rights needed. Opening the Microsoft Store: click
echo   "Get", wait for it to finish, then double-click this file again.
echo.
start "" "ms-windows-store://search/?query=Python 3"
pause
goto :eof

:startfailed
echo.
echo   Python is installed, but the app did not start - see the error
echo   above. If it mentions a missing file, make sure you EXTRACTED the
echo   whole folder rather than running from inside the .zip.
echo   Still stuck? Email samuelbf@uark.edu with a screenshot.
echo.
pause
