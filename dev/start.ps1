# Startet die Rechnungsapp (Windows): venv anlegen, Abhängigkeiten installieren, Server starten.
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv")) { py -m venv .venv }
& ".\.venv\Scripts\python.exe" -m pip install -q -r requirements.txt
Start-Process "http://127.0.0.1:5000"
& ".\.venv\Scripts\python.exe" app.py
