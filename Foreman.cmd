@echo off
rem The Foreman: double-click to open the control panel in your browser.
rem It runs in the background, without a console window. Close it with the Quit link
rem at the bottom of the page; it also closes itself after 10 idle minutes.
setlocal
cd /d "%~dp0"
where pyw >nul 2>&1 && (start "" pyw -3 "%~dp0foreman_ui.py" & exit /b 0)
where pythonw >nul 2>&1 && (start "" pythonw "%~dp0foreman_ui.py" & exit /b 0)
where python >nul 2>&1 && (start "The Foreman" /min python "%~dp0foreman_ui.py" & exit /b 0)
echo The Foreman needs Python 3.11 or newer.
echo Opening the official download page: https://www.python.org/downloads/
start "" https://www.python.org/downloads/
pause
