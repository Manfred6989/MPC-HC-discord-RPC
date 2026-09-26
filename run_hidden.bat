@echo off
rem Starts MPC-HC Discord RPC with a tray icon and no console window.
rem Put a shortcut to this file in shell:startup to launch it with Windows.
cd /d "%~dp0"
where pythonw >nul 2>&1 && (start "" pythonw mpc_discord_rpc.py) || (start "" pyw -3 mpc_discord_rpc.py)
