# Release Notes

Neueste Version zuerst. Die aktuelle Version steht in der Datei `VERSION` und wird im Fuß der App angezeigt.

## 1.2.0 – 2026-11-16

- Sicherheit: API-Token liegt nicht mehr in den Claude-Settings, sondern in einer lokalen `.env` (Variable `RECHNUNGSAPP_TOKEN`); `.env`, `.claude/` und `.mcp.json` sind in der `.gitignore` gesperrt, `.env.example` dient als Vorlage. Der README beschreibt das Einrichten. Der bisher verwendete Token muss widerrufen und neu erzeugt werden.
- Hell/Dunkel-Schalter mit Outline-Icons (Sonne/Mond) nebeneinander; der aktive Modus ist hervorgehoben.
- Fehlerkorrektur: Die Stylesheet-Datei wird mit einer Versionsnummer eingebunden, damit der Browser nach einem Update nicht mehr das alte Aussehen aus dem Cache zeigt (z. B. unformatierte Versionsnummer im Seitenfuß).
- Experimente mit einem Wisch-Seitenmenü und Karten-Listen für die Handy-Ansicht wurden verworfen; es bleibt bei der Ansicht aus 1.1.0.

## 1.1.0 – 2026-11-13

- Mobile Ansicht: Navigationsleiste deutlich kompakter (Marke und Dark-Mode-Schalter oben, Links in einer Zeile, Suche darunter, kleinere Abstände).
- Mobile Ansicht: Eingabefelder mit 16 px Schrift (kein Auto-Zoom auf iOS) und größerer Tippfläche, Zahlenfelder öffnen die Zifferntastatur, Rechnungspositionen erscheinen als Karten statt als 7-spaltige Tabelle, Listen sind seitlich wischbar, Filterleiste, Buttons und Dialoge passen sich der Bildschirmbreite an.
- Suche: Bei Kunden gibt es jetzt „Bearbeiten“, bei Rechnungen „Ansehen“, „PDF-Vorschau“ und „Bearbeiten“ (bezahlte Rechnungen sind weiterhin gesperrt und mit 🔒 markiert).
- Paging: Kunden- und Rechnungsliste zeigen 20 Einträge pro Seite mit Blättern-Leiste; Filter und Gruppierung bleiben erhalten, die Umsatzkennzahlen gelten für alle Treffer.
- Versionierung: Versionsnummer im Seitenfuß, Release-Notes-Seite (/release-notes) und diese Datei.

## 1.0.0

- Bisheriger Stand vor Einführung der Versionierung (Kunden, Rechnungen, PDF-Export, Volltextsuche, Einstellungen, Login, API/MCP).
