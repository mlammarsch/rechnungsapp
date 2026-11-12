"""Einmaliger Import: GnuCash (Gewerbe.gnucash) -> neue Rechnungsapp-Datenbank.

Aufruf (aus dem dev-Ordner):  .venv\\Scripts\\python.exe -I import_gnucash.py
Schreibt NUR in rechnungsapp_import.db; die produktive rechnungsapp.db wird nur lesend
angefasst (Firmendaten/Benutzer werden daraus kopiert).
"""
import os
import sqlite3
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SRC_DB = os.path.join(HERE, "rechnungsapp.db")
NEW_DB = os.path.join(HERE, "rechnungsapp_import.db")

# (Nummer, Firma, Ansprechpartner, Straße, PLZ, Ort)  -- Quelle: GnuCash-Kundenstamm
CUSTOMERS = [
    ("10001", "Six 4 Blues", "Ulruch Hänig", "Lindenbergstraße 17", "89134", "Blaustein"),
    ("10002", "Team Lee Management", "Lee Mayall", "Hermann Voith Straße 30 / 32", "89522", "Heidenheim"),
    ("10003", "", "Harmony & Sound", "Sudetenstrasse 6", "89143", "Blaubeuren"),
    ("10004", "MPNG", "Samuel Reichert", "Königin-Elisabeth-Str. 56", "14059", "Berlin"),
    ("10005", "Brunnenbutzer", "Andreas David", "Kirchengasse 7", "76767", "Hagenbach"),
    ("10006", "Flap Jacks", "Bernd Prinzing", "Schiessmauer 7", "89174", "Altheim/Alb"),
    ("10007", "Six 4 Blues", "Henning Dampel", "Eselsbergsteige 162/3", "89075", "Ulm"),
    ("10008", "Harmony & Sound", "Tim Beck", "Hinter der Burg 9", "88471", "Laupheim"),
    ("10009", "", "Gemeindebücherei Heroldstatt", "Am Berg 9/2", "72535", "Heroldstatt"),
    ("10010", "Musikverein Jockgrim e.V.", "Uwe Schröter", "Tulpenstraße 11", "76751", "Jockgrim"),
    ("10011", "Siedlerheim Karlsruhe", "Marco Rossi", "Hohlohstrasse 100", "76189", "Karlsruhe"),
    ("10012", "JS-Event Licht & Ton", "Heiko Jäger", "Zeppelinstraße 23", "76870", "Kandel"),
    ("10025", "", "Hans Hruschka", "Wasgaustraße 3", "76870", "Kandel"),
    ("10026", "Freizeit Sportverein Berg", "Jürgen Theuring", "Körnerstraße 4", "76768", "Berg"),
    ("10027", "CJN Veranstaltungstechnik", "Christian Nattermüller", "Föhrenweg 12e", "67454", "Haßloch"),
]

# (Nummer, Kundennr., Datum, Leistungsdatum, Status, [(Text, Menge, Preis), ...])
# RA-00034 wurde auf Wunsch in RG-00034 korrigiert. RA-00027, RA-00028 (Altlasten) und EB1 entfallen.
INVOICES = [
    ("RG-00031", "10012", "2026-06-07", "", "bezahlt",
     [("Gage für Dienstleistungen in Scheibenhardt, Brückenfest am 06.06.2026", 1, 350)]),
    ("RG-00032", "10025", "2026-06-07", "", "bezahlt",
     [("Gage Tiefenklang am 30.05.2026 im Cheers", 1, 100)]),
    ("RG-00033", "10026", "2026-07-06", "", "bezahlt",
     [("Gage für Veranstaltung am 4.7.2026 (50 Jahre Jubiläum)", 1, 950)]),
    ("RG-00034", "10027", "2026-08-02", "", "bezahlt",
     [("Gage für Dienstleistung Rock am Speyerbach am 01.08.2026", 1, 350)]),
    ("RG-00035", "10012", "2026-09-23", "", "bezahlt",
     [("Gage für Dienstleistung in Kandel, Bienwaldhalle am 20.09.2026", 1, 250)]),
    ("RG-00036", "10012", "2026-10-27", "", "offen",
     [("Spesenaufschlag", 3, 15), ("Gagen für Oktobermarkt", 1, 950)]),
    ("RG-00037", "10012", "2026-11-06", "2026-10-31", "offen",
     [("Gage für Dienstleistung beim Weiterabtrieb in Neupotz am 31.10.2026", 1, 250)]),
    ("RG-00038", "10009", "2026-11-06", "2026-11-05", "offen",
     [("Dienstleistung Tontechnik für die Veranstaltung Rock'n'Roll Stammtisch am 5.11.2026", 1, 300)]),
]

# Nächste Nummer = Startwert + Anzahl vorhandener Datensätze (so rechnet app.next_number).
INVOICE_START = 39 - len(INVOICES)      # -> 31, nächste Rechnung RG-00039
CUSTOMER_START = 10028 - len(CUSTOMERS)  # -> 10013, nächster Kunde 10028


def main():
    if os.path.exists(NEW_DB):
        sys.exit(f"{NEW_DB} existiert bereits - bitte erst umbenennen/löschen.")

    import app  # Schema + Migrationen der App wiederverwenden
    app.DB_PATH = NEW_DB
    app.init_db()

    src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    db = sqlite3.connect(NEW_DB)
    db.execute("PRAGMA foreign_keys = ON")

    # Firmendaten, Texte, Bank usw. aus der bestehenden DB übernehmen (Nummernkreise überschreibe ich unten)
    for r in src.execute("SELECT key, value FROM settings"):
        db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (r["key"], r["value"]))
    # Login übernehmen, sonst müsste in der neuen DB ein Benutzer neu angelegt werden
    for r in src.execute("SELECT id, username, password_hash FROM users"):
        db.execute("INSERT OR REPLACE INTO users(id, username, password_hash) VALUES (?,?,?)",
                   (r["id"], r["username"], r["password_hash"]))
    due_days = int(db.execute("SELECT value FROM settings WHERE key='payment_due_days'").fetchone()[0] or 14)

    for key, value in {
        "invoice_number_pattern": "RG-{NNNNN}",
        "invoice_number_start": str(INVOICE_START),
        "invoice_number_reset_yearly": "0",
        "customer_number_pattern": "{NNNNN}",
        "customer_number_start": str(CUSTOMER_START),
        "customer_number_reset_yearly": "0",
    }.items():
        db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))

    cust_id = {}
    for number, company, name, street, zip_, city in CUSTOMERS:
        cur = db.execute(
            "INSERT INTO customers(number, company, name, street, zip, city, notes) VALUES (?,?,?,?,?,?,?)",
            (number, company, name, street, zip_, city, "Aus GnuCash übernommen"))
        cust_id[number] = cur.lastrowid

    for number, cnr, inv_date, service, status, items in INVOICES:
        due = (date.fromisoformat(inv_date) + timedelta(days=due_days)).isoformat()
        cur = db.execute(
            "INSERT INTO invoices(number, invoice_date, due_date, customer_id, status, notes, service_date, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (number, inv_date, due, cust_id[cnr], status, "", service, inv_date + " 00:00:00"))
        for pos, (text, qty, price) in enumerate(items, 1):
            db.execute(
                "INSERT INTO invoice_items(invoice_id, position, description, quantity, unit_price, total)"
                " VALUES (?,?,?,?,?,?)", (cur.lastrowid, pos, text, qty, price, round(qty * price, 2)))

    db.commit()
    src.close()
    db.close()

    # Kontrolle: welche Nummern würde die App als nächstes vergeben?
    s = app.get_settings()
    print("Nächste Rechnungsnummer:", app.next_number("invoice", s))
    print("Nächste Kundennummer:  ", app.next_number("customer", s))


if __name__ == "__main__":
    main()
