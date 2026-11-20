@echo off

rem Activate the virtual environment
call venv\Scripts\activate.bat

rem Run BD2pineViewer.py from the virtual environment and close the cmd window immediately
start "" pythonw "BD2pineViewer.py"

rem Deactivate the virtual environment (this won't affect the already-running GUI app)
call venv\Scripts\deactivate.bat
exit