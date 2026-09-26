@echo off
rem Starts the presence in the background (no console window).
rem Put a shortcut to this file in shell:startup to launch it with Windows.
cd /d "%~dp0"
start "" pythonw mpc_discord_rpc.py --log-file mpc_discord_rpc.log
