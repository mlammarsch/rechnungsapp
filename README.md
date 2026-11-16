# Rechnungsapp

Webanwendung zur Rechnungsstellung (Flask, SQLite, PDF-Export) mit Einzelbenutzer-Login und einem MCP-Endpunkt (`/mcp`) fuer KI-Assistenten.

## Funktionalitaet

Die Rechnungsapp bietet eine vollstaendige, webbasierte Loesung zur Rechnungsverwaltung:

### Rechnungsverwaltung
- **Rechnungen erstellen & verwalten:** Erfasse neue Rechnungen mit Positionen (Beschreibung, Menge, Einzelpreis)
- **Kundenverwaltung:** Speichere Kundendetails (Name, Adresse, E-Mail, Steuernummer)
- **PDF-Export:** Exportiere Rechnungen als professionelle PDF-Dokumente mit eigenem Logo und Texten
- **Rechnungsarchiv:** Uebersicht aller erstellten Rechnungen mit Suchfunktion und Status-Verwaltung

### Sicherheit & Zugang
- **Einzelbenutzer-Login:** Passwortgeschuetzter Zugang (mindestens 12 Zeichen)
- **Session-Management:** Sichere Cookie-basierte Sitzungen mit optionalem HTTPS
- **Login-Rate-Limiting:** Schutz vor Brute-Force-Attacken (IPs nach Versuchen blockiert)

### Integration
- **MCP (Model Context Protocol):** REST-API fuer KI-Assistenten und externe Tools
- **API-Token-Verwaltung:** Granulare Zugriffskontrolle (read/full, Ablaufdaten)
- **Automatisiertes Reporting:** Daten abrufbar fuer externe Systeme

### Verwaltung
- **Einstellungen & Konfiguration:** Firmendetails, Zahlungsbedingungen, Steuersaetze lokal anpassen
- **Datensicherung:** Ein-Kommando-Backup der SQLite-Datenbank
- **Multi-Environment-Support:** Lokal entwickeln, produktiv in Docker deployen

## Schnellstart mit Docker

Voraussetzung: Docker mit Compose.

```bash
mkdir rechnungsapp && cd rechnungsapp
# docker-compose.yml aus diesem Repository (Ordner dev/) hier ablegen
docker compose up -d
```

Danach ist die App unter `http://<Server-IP>:8000` erreichbar. Beim ersten Aufruf wird der einzige Benutzer registriert (mindestens 12 Zeichen Passwort). Danach ist die Registrierung dauerhaft gesperrt. **Den Benutzer sofort anlegen, solange die App im Netz erreichbar ist.**

## Konfiguration

Die Einstellungen erfolgen ueber Umgebungsvariablen, zum Beispiel in einer `.env` neben der `docker-compose.yml` (Vorlage: `dev/.env.example`).

| Variable | Standard | Bedeutung |
|---|---|---|
| `RECHNUNGSAPP_PORT` | `8000` | Port auf dem Host |
| `RECHNUNGSAPP_PUBLIC_URL` | `http://localhost:8000` | Oeffentliche URL, wird fuer MCP- und PDF-Links verwendet |
| `RECHNUNGSAPP_HTTPS` | `0` | `1` setzt das Secure-Flag fuer das Sitzungs-Cookie. Nur aktivieren, wenn die App per HTTPS erreichbar ist, sonst funktioniert der Login nicht |
| `RECHNUNGSAPP_PROXY` | `0` | `1`, wenn ein Reverse Proxy davorsteht. Dann wird die echte Client-IP aus `X-Forwarded-*` uebernommen (wichtig fuer die Login-Sperre) |

## Daten und Datensicherung

Alle Daten liegen im Ordner `data/` neben der `docker-compose.yml` (im Container: `/data`):

| Datei | Inhalt |
|---|---|
| `rechnungsapp.db` | komplette Datenbank (Rechnungen, Kunden, Einstellungen, API-Tokens) |
| `secret.key` | Signaturschluessel fuer Sitzungen und PDF-Download-Links |

**Sicherung:** Beide Dateien kopieren. Konsistenter Snapshot bei laufendem Betrieb:

```bash
docker compose exec rechnungsapp python -c "import sqlite3;s=sqlite3.connect('/data/rechnungsapp.db');d=sqlite3.connect('/data/backup.db');s.backup(d)"
```

**Bestehende Datenbank uebernehmen:**

```bash
docker compose down
cp rechnungsapp.db data/rechnungsapp.db
cp secret.key data/secret.key   # optional, haelt bestehende Sitzungen und Links gueltig
docker compose up -d
```

Die Dateirechte im Ordner `data/` setzt der Container beim Start selbst.

## Betrieb hinter einem Reverse Proxy (HTTPS)

Der Container spricht nur HTTP auf Port 8000. Fuer den Zugriff aus dem Internet einen Reverse Proxy (nginx, Caddy, Traefik, ...) mit TLS davorschalten und alles an `http://<Server-IP>:8000` weiterleiten, einschliesslich `/mcp`.

In der `.env`:

```
RECHNUNGSAPP_PUBLIC_URL=https://rechnungsapp.example.com
RECHNUNGSAPP_HTTPS=1
RECHNUNGSAPP_PROXY=1
```

Der Proxy muss die Header `X-Forwarded-For`, `X-Forwarded-Proto` und `X-Forwarded-Host` setzen. Beispiel fuer nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
}
```

Port 8000 sollte dann nicht aus dem Internet erreichbar sein (Firewall) oder in der Compose-Datei auf `127.0.0.1:8000:8000` beschraenkt werden.

## MCP-Anbindung

Unter `/account` in der Weboberflaeche lassen sich API-Tokens erzeugen (Geltungsbereich `read` oder `full`, optionales Ablaufdatum). Der MCP-Server ist unter `<PUBLIC_URL>/mcp` erreichbar:

```json
{
  "url": "https://rechnungsapp.example.com/mcp",
  "headers": { "Authorization": "Bearer ra_..." }
}
```

### Token fuer Claude Code / lokale Skripte (`.env`)

Wer die MCP-Schnittstelle aus Claude Code oder eigenen Skripten nutzt, legt den Token **nicht** in Settings-Dateien ab, sondern in einer lokalen `.env` im Projektordner:

```bash
cp .env.example .env     # Windows: copy .env.example .env
```

Danach in `.env` den unter `/account` erzeugten Token eintragen:

```
RECHNUNGSAPP_TOKEN=ra_...
```

Die Datei ist ueber `.gitignore` gesperrt und darf nie eingecheckt werden (ebenso `.claude/` und `.mcp.json`). In Befehlen und Berechtigungen wird nur die Variable `$RECHNUNGSAPP_TOKEN` verwendet, nie der Klartext-Token. Die Variable muss in der Shell gesetzt sein, die `.env` wird nicht automatisch geladen (z. B. `set -a; source .env; set +a` in Bash).

## Passwort vergessen

```bash
docker compose exec rechnungsapp python app.py reset-user
```

Der Benutzer und alle API-Tokens werden geloescht, danach ist die Registrierung wieder offen. Rechnungen und Kunden bleiben erhalten.

## Update

```bash
docker compose pull
docker compose up -d
```

## Image selbst bauen

Im Ordner `dev/`:

```bash
docker build -t <dockerhub-user>/rechnungsapp:latest .
docker push <dockerhub-user>/rechnungsapp:latest
```

## Lokale Entwicklung ohne Docker (Windows)

```powershell
cd dev
.\start.ps1
```

Die App laeuft dann auf `http://127.0.0.1:5000`.
