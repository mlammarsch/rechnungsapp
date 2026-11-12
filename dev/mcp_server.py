"""MCP-Server (Model Context Protocol), direkt in die Flask-App integriert.

Transport: Streamable HTTP, ein Endpunkt  POST /mcp  (JSON-RPC 2.0, Antworten als application/json).
Authentifizierung: Bearer-Token (siehe auth.py, Verwaltung unter /account).
Rechte: Token mit Scope "read" dürfen nur lesende Tools, Scope "full" alle.

Die Tools bilden alles ab, was auch die Weboberfläche kann (Übersicht, Kunden, Rechnungen, Suche,
Einstellungen, PDF). Bewusst NICHT enthalten: Benutzer-/Token-Verwaltung – ein Token soll sich
nicht selbst neue Zugänge ausstellen können.

PDF: get_invoice_pdf erzeugt das PDF und liefert Dateiname, Größe, SHA-256 und einen kurzlebigen,
signierten Download-Link (/download/<signatur>), der ohne weitere Anmeldung funktioniert.
"""
import base64
import hashlib
import io
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request, send_file
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import HTTPException

log = logging.getLogger("rechnungsapp.mcp")

bp = Blueprint("mcp", __name__)

SERVER_NAME = "rechnungsapp"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
DOWNLOAD_TTL_SECONDS = 600

INSTRUCTIONS = (
    "Rechnungsverwaltung für einen Kleinunternehmer (§ 19 UStG, keine Umsatzsteuer). "
    "Beträge in Euro. Datumsangaben als TT.MM.JJJJ oder JJJJ-MM-TT; Ausgaben immer als JJJJ-MM-TT. "
    "Kunden- und Rechnungsnummern werden automatisch nach dem Nummernkreis aus den Einstellungen vergeben. "
    "Bezahlte Rechnungen sind gesperrt: erst mit set_invoice_status wieder auf 'offen' setzen. "
    "Löschen erfordert confirm=true und ist endgültig. "
    "Für ein PDF get_invoice_pdf aufrufen und den Download-Link an den Benutzer weitergeben."
)

# Frisch erzeugte PDFs, damit der Download exakt die Bytes liefert, deren Größe/SHA-256 angekündigt wurden
# (ReportLab schreibt Zeitstempel/ID ins PDF, ein erneutes Erzeugen hätte einen anderen Hash).
_pdf_cache = {}  # signed -> (ablauf_epoch, bytes, dateiname)
_PDF_CACHE_MAX = 20

core = None  # das App-Modul (wird in init_mcp übergeben, vermeidet doppelten Import von __main__)


class ToolError(Exception):
    """Fachlicher Fehler, wird dem Modell als Tool-Ergebnis mit isError=true gemeldet."""


# --------------------------------------------------------------------------------------
# Tool-Registry
# --------------------------------------------------------------------------------------
TOOLS = {}


def tool(name, title, description, props=None, required=(), write=False, destructive=False,
         idempotent=False):
    def deco(fn):
        TOOLS[name] = {
            "fn": fn, "write": write,
            "def": {
                "name": name, "title": title, "description": description,
                "inputSchema": {"type": "object", "properties": props or {},
                                "required": list(required), "additionalProperties": False},
                "annotations": {"readOnlyHint": not write, "destructiveHint": destructive,
                                "idempotentHint": idempotent or not write, "openWorldHint": False},
            },
        }
        return fn
    return deco


S = lambda desc, **kw: {"type": "string", "description": desc, **kw}          # noqa: E731
I = lambda desc, **kw: {"type": "integer", "description": desc, **kw}         # noqa: E731
N = lambda desc, **kw: {"type": "number", "description": desc, **kw}          # noqa: E731
B = lambda desc, **kw: {"type": "boolean", "description": desc, **kw}         # noqa: E731

CUSTOMER_PROPS = {
    "company": S("Firma"), "name": S("Name der Kontaktperson (Pflicht beim Anlegen)"),
    "street": S("Straße und Hausnummer"), "zip": S("PLZ"), "city": S("Ort"),
    "email": S("E-Mail"), "phone": S("Telefon"), "notes": S("Interne Notizen"),
}
ITEM_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["description", "unit_price"],
    "properties": {
        "description": S("Beschreibungstext der Position"),
        "quantity": N("Menge (Standard 1)"),
        "unit_price": N("Einzelpreis in Euro"),
        "discount": N("Positionsrabatt in Prozent (0–100, Standard 0)"),
    },
}
INVOICE_PROPS = {
    "invoice_date": S("Rechnungsdatum (Standard: heute)"),
    "service_date": S("Leistungsdatum (optional)"),
    "due_date": S("Fälligkeitsdatum (Standard: Rechnungsdatum + Zahlungsfrist aus den Einstellungen)"),
    "notes": S("Anmerkung, erscheint im PDF unter 'Hinweise und Anmerkungen'"),
    "discount_percent": N("Gesamtrabatt in Prozent (0–100)"),
    "discount_amount": N("Gesamtrabatt als fester Betrag in Euro"),
    "items": {"type": "array", "items": ITEM_SCHEMA, "description": "Rechnungspositionen"},
}


# --------------------------------------------------------------------------------------
# Eingabeprüfung (kleiner JSON-Schema-Ausschnitt reicht für die eigenen Schemas)
# --------------------------------------------------------------------------------------
def _validate(schema, value, path="arguments"):
    t = schema.get("type")
    checks = {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }
    if t and not checks[t](value):
        return f"{path}: erwartet {t}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: erlaubt sind {', '.join(map(str, schema['enum']))}"
    if t == "array":
        for i, v in enumerate(value):
            err = _validate(schema.get("items", {}), v, f"{path}[{i}]")
            if err:
                return err
    if t == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}: Pflichtfeld '{key}' fehlt"
        for key, v in value.items():
            if key not in props:
                if schema.get("additionalProperties") is True:
                    continue
                return f"{path}: unbekanntes Feld '{key}'"
            err = _validate(props[key], v, f"{path}.{key}" if path != "arguments" else key)
            if err:
                return err
    return None


# --------------------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------------------
def _fmt_de(d):
    return core.fmt_date_de(d.isoformat())


def _customer_out(r):
    return {k: r[k] for k in ("id", "number", "company", "name", "street", "zip", "city",
                              "email", "phone", "notes", "created_at")}


def _invoice_out(conn, iid):
    inv = conn.execute(
        "SELECT v.*, c.number AS c_number, c.company AS c_company, c.name AS c_name "
        "FROM invoices v JOIN customers c ON c.id = v.customer_id WHERE v.id = ?", (iid,)).fetchone()
    if inv is None:
        raise ToolError(f"Rechnung mit ID {iid} nicht gefunden.")
    items = conn.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position",
                         (iid,)).fetchall()
    subtotal, discount_total, total = core.calc_totals(
        sum(i["total"] for i in items), inv["discount_percent"], inv["discount_amount"])
    return {
        "id": inv["id"], "number": inv["number"], "status": inv["status"],
        "invoice_date": inv["invoice_date"], "service_date": inv["service_date"] or None,
        "due_date": inv["due_date"], "notes": inv["notes"],
        "discount_percent": inv["discount_percent"], "discount_amount": inv["discount_amount"],
        "customer": {"id": inv["customer_id"], "number": inv["c_number"],
                     "company": inv["c_company"], "name": inv["c_name"]},
        "items": [{"position": i["position"], "description": i["description"],
                   "quantity": i["quantity"], "unit_price": i["unit_price"],
                   "discount": i["discount"], "total": i["total"]} for i in items],
        "subtotal": subtotal, "discount_total": discount_total, "total": total,
        "locked": inv["status"] == "bezahlt",
    }


def _items_form(form, items):
    for it in items:
        form.add("description", str(it["description"]))
        form.add("quantity", str(it.get("quantity", 1)))
        form.add("unit_price", str(it["unit_price"]))
        form.add("discount", str(it.get("discount", 0)))


def _customer_exists(conn, cid):
    if conn.execute("SELECT 1 FROM customers WHERE id = ?", (cid,)).fetchone() is None:
        raise ToolError(f"Kunde mit ID {cid} nicht gefunden.")


def _need_confirm(args, what):
    if args.get("confirm") is not True:
        raise ToolError(f"{what} wird endgültig gelöscht. Zum Bestätigen confirm=true übergeben.")


def _public_base():
    import os
    return os.environ.get("RECHNUNGSAPP_PUBLIC_URL", request.url_root).rstrip("/")


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pdf-download")


# --------------------------------------------------------------------------------------
# Tools: Übersicht & Suche
# --------------------------------------------------------------------------------------
@tool("get_overview", "Übersicht", "Kennzahlen (Anzahl Kunden/Rechnungen, offene und bezahlte Summen) "
      "und die 5 neuesten Rechnungen – entspricht der Startseite.")
def t_overview(a):
    stats, recent = core.dashboard_data()
    return {"stats": dict(stats),
            "recent_invoices": [{"id": r["id"], "number": r["number"], "invoice_date": r["invoice_date"],
                                 "status": r["status"], "customer_name": r["customer_name"]} for r in recent]}


@tool("search", "Suche", "Volltextsuche über Kunden und Rechnungen (alle Begriffe müssen vorkommen, "
      "ohne Groß-/Kleinschreibung; sucht u. a. in Positionstexten, Datum und Betrag).",
      {"query": S("Suchbegriffe, durch Leerzeichen getrennt")}, ["query"])
def t_search(a):
    customers, invoices = core.run_search(a["query"])
    return {"customers": [_customer_out(c) for c in customers],
            "invoices": [{"id": v["id"], "number": v["number"], "invoice_date": v["invoice_date"],
                          "status": v["status"], "total": v["total"], "customer_id": v["customer_id"],
                          "customer_name": v["customer_name"], "customer_company": v["customer_company"]}
                         for v in invoices]}


# --------------------------------------------------------------------------------------
# Tools: Kunden
# --------------------------------------------------------------------------------------
@tool("list_customers", "Kunden auflisten", "Alle Kunden, sortiert nach Kundennummer.")
def t_list_customers(a):
    conn = core.get_db()
    try:
        rows = conn.execute("SELECT * FROM customers ORDER BY number").fetchall()
    finally:
        conn.close()
    return {"customers": [_customer_out(r) for r in rows]}


@tool("get_customer", "Kunde abrufen", "Einen Kunden samt seiner Rechnungen abrufen.",
      {"id": I("Kunden-ID")}, ["id"])
def t_get_customer(a):
    conn = core.get_db()
    try:
        c = conn.execute("SELECT * FROM customers WHERE id = ?", (a["id"],)).fetchone()
        if c is None:
            raise ToolError(f"Kunde mit ID {a['id']} nicht gefunden.")
        invs = conn.execute(
            f"SELECT v.id, v.number, v.invoice_date, v.status, {core.SQL_FINAL} AS total "
            "FROM invoices v WHERE v.customer_id = ? ORDER BY v.id DESC", (a["id"],)).fetchall()
    finally:
        conn.close()
    return {"customer": _customer_out(c), "invoices": [dict(r) for r in invs]}


@tool("create_customer", "Kunde anlegen", "Legt einen Kunden an; die Kundennummer wird automatisch vergeben.",
      CUSTOMER_PROPS, ["name"], write=True)
def t_create_customer(a):
    name = a["name"].strip()
    if not name:
        raise ToolError("Name ist erforderlich.")
    number = core.next_number("customer", core.get_settings())
    vals = {k: a.get(k, "").strip() for k in core.CUSTOMER_FIELDS}
    vals["name"] = name
    conn = core.get_db()
    try:
        cur = conn.execute(
            "INSERT INTO customers(number, company, name, street, zip, city, email, phone, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (number, *(vals[k] for k in core.CUSTOMER_FIELDS)))
        conn.commit()
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return {"customer": _customer_out(row)}


@tool("update_customer", "Kunde ändern", "Ändert die übergebenen Felder eines Kunden; nicht genannte bleiben unverändert.",
      {"id": I("Kunden-ID"), **CUSTOMER_PROPS}, ["id"], write=True, idempotent=True)
def t_update_customer(a):
    conn = core.get_db()
    try:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (a["id"],)).fetchone()
        if row is None:
            raise ToolError(f"Kunde mit ID {a['id']} nicht gefunden.")
        vals = {k: (a[k].strip() if k in a else row[k]) for k in core.CUSTOMER_FIELDS}
        if not vals["name"]:
            raise ToolError("Name ist erforderlich.")
        conn.execute("UPDATE customers SET company=?, name=?, street=?, zip=?, city=?, email=?, phone=?, "
                     "notes=? WHERE id=?", (*(vals[k] for k in core.CUSTOMER_FIELDS), a["id"]))
        conn.commit()
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (a["id"],)).fetchone()
    finally:
        conn.close()
    return {"customer": _customer_out(row)}


@tool("delete_customer", "Kunde löschen", "Löscht einen Kunden endgültig. Nur möglich, wenn keine Rechnungen existieren.",
      {"id": I("Kunden-ID"), "confirm": B("Muss true sein, um das Löschen zu bestätigen")},
      ["id", "confirm"], write=True, destructive=True, idempotent=True)
def t_delete_customer(a):
    _need_confirm(a, "Der Kunde")
    conn = core.get_db()
    try:
        row = conn.execute("SELECT number FROM customers WHERE id = ?", (a["id"],)).fetchone()
        if row is None:
            raise ToolError(f"Kunde mit ID {a['id']} nicht gefunden.")
        try:
            conn.execute("DELETE FROM customers WHERE id = ?", (a["id"],))
            conn.commit()
        except sqlite3.IntegrityError:
            raise ToolError("Der Kunde hat noch Rechnungen und kann nicht gelöscht werden. "
                            "Erst die Rechnungen löschen.")
    finally:
        conn.close()
    return {"deleted": True, "customer_number": row["number"]}


# --------------------------------------------------------------------------------------
# Tools: Rechnungen
# --------------------------------------------------------------------------------------
@tool("list_invoices", "Rechnungen auflisten",
      "Rechnungen mit Summen, neueste zuerst. Standard: aktuelles Jahr (wie die Weboberfläche).",
      {"year": S("Jahr (z. B. '2026') oder 'all' für alle Jahre"),
       "customer_id": I("Nur Rechnungen dieses Kunden"),
       "status": S("Nur Rechnungen mit diesem Status", enum=["offen", "bezahlt"])})
def t_list_invoices(a):
    conn = core.get_db()
    try:
        rows = conn.execute(
            f"""SELECT v.id, v.number, v.invoice_date, v.due_date, v.status, v.customer_id,
                       c.name AS customer_name, c.company AS customer_company, {core.SQL_FINAL} AS total
                FROM invoices v JOIN customers c ON c.id = v.customer_id ORDER BY v.id DESC""").fetchall()
    finally:
        conn.close()
    year = a.get("year", str(date.today().year))
    if year != "all":
        rows = [r for r in rows if r["invoice_date"][:4] == year]
    if "customer_id" in a:
        rows = [r for r in rows if r["customer_id"] == a["customer_id"]]
    if "status" in a:
        rows = [r for r in rows if r["status"] == a["status"]]
    return {"invoices": [dict(r) for r in rows], "stats": core._sum_stats(rows)}


@tool("get_invoice", "Rechnung abrufen", "Eine Rechnung mit Kunde, Positionen und Summen.",
      {"id": I("Rechnungs-ID")}, ["id"])
def t_get_invoice(a):
    conn = core.get_db()
    try:
        return {"invoice": _invoice_out(conn, a["id"])}
    finally:
        conn.close()


@tool("create_invoice", "Rechnung erstellen",
      "Erstellt eine Rechnung (Status 'offen'); die Rechnungsnummer wird automatisch vergeben.",
      {"customer_id": I("ID des Kunden"), **INVOICE_PROPS}, ["customer_id", "items"], write=True)
def t_create_invoice(a):
    if not a["items"] or not any(str(i["description"]).strip() for i in a["items"]):
        raise ToolError("Mindestens eine Position mit Beschreibung ist erforderlich.")
    settings = core.get_settings()
    form = MultiDict({
        "customer_id": str(a["customer_id"]),
        "invoice_date": a.get("invoice_date") or _fmt_de(date.today()),
        "service_date": a.get("service_date", ""), "due_date": a.get("due_date", ""),
        "notes": a.get("notes", ""),
        "discount_percent": str(a.get("discount_percent", 0)),
        "discount_amount": str(a.get("discount_amount", 0)),
    })
    _items_form(form, a["items"])
    conn = core.get_db()
    try:
        _customer_exists(conn, a["customer_id"])
        f = core.invoice_form_values(form, settings)
        number = core.next_number("invoice", settings, core.parse_date(f["invoice_date"]))
        cur = conn.execute(
            """INSERT INTO invoices(number, invoice_date, service_date, due_date, customer_id, status, notes,
                                    discount_percent, discount_amount)
               VALUES (?,?,?,?,?, 'offen', ?,?,?)""",
            (number, f["invoice_date"], f["service_date"], f["due_date"], f["customer_id"],
             f["notes"], f["discount_percent"], f["discount_amount"]))
        core.save_invoice_items(conn, cur.lastrowid, form)
        conn.commit()
        return {"invoice": _invoice_out(conn, cur.lastrowid)}
    finally:
        conn.close()


@tool("update_invoice", "Rechnung ändern",
      "Ändert die übergebenen Felder einer offenen Rechnung. Werden 'items' übergeben, ersetzen sie alle "
      "bisherigen Positionen. Bezahlte Rechnungen sind gesperrt.",
      {"id": I("Rechnungs-ID"), "customer_id": I("Neuer Kunde (optional)"), **INVOICE_PROPS},
      ["id"], write=True, idempotent=True)
def t_update_invoice(a):
    conn = core.get_db()
    try:
        inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (a["id"],)).fetchone()
        if inv is None:
            raise ToolError(f"Rechnung mit ID {a['id']} nicht gefunden.")
        if inv["status"] == "bezahlt":
            raise ToolError("Bezahlte Rechnungen sind gesperrt. Erst mit set_invoice_status auf 'offen' setzen.")
        if "items" in a and not any(str(i["description"]).strip() for i in a["items"]):
            raise ToolError("Mindestens eine Position mit Beschreibung ist erforderlich.")
        cid = a.get("customer_id", inv["customer_id"])
        _customer_exists(conn, cid)
        form = MultiDict({
            "customer_id": str(cid),
            "invoice_date": a.get("invoice_date", inv["invoice_date"]),
            "service_date": a.get("service_date", inv["service_date"]),
            "due_date": a.get("due_date", inv["due_date"]),
            "notes": a.get("notes", inv["notes"]),
            "discount_percent": str(a.get("discount_percent", inv["discount_percent"])),
            "discount_amount": str(a.get("discount_amount", inv["discount_amount"])),
        })
        items = a.get("items")
        if items is None:
            items = [dict(r) for r in conn.execute(
                "SELECT description, quantity, unit_price, discount FROM invoice_items "
                "WHERE invoice_id = ? ORDER BY position", (a["id"],))]
        _items_form(form, items)
        f = core.invoice_form_values(form, core.get_settings())
        conn.execute(
            """UPDATE invoices SET invoice_date=?, service_date=?, due_date=?, customer_id=?, notes=?,
                      discount_percent=?, discount_amount=? WHERE id=?""",
            (f["invoice_date"], f["service_date"], f["due_date"], f["customer_id"], f["notes"],
             f["discount_percent"], f["discount_amount"], a["id"]))
        core.save_invoice_items(conn, a["id"], form)
        conn.commit()
        return {"invoice": _invoice_out(conn, a["id"])}
    finally:
        conn.close()


@tool("set_invoice_status", "Rechnungsstatus setzen",
      "Setzt eine Rechnung auf 'bezahlt' (sperrt sie) oder zurück auf 'offen'.",
      {"id": I("Rechnungs-ID"), "status": S("Neuer Status", enum=["offen", "bezahlt"])},
      ["id", "status"], write=True, idempotent=True)
def t_set_status(a):
    conn = core.get_db()
    try:
        cur = conn.execute("UPDATE invoices SET status = ? WHERE id = ?", (a["status"], a["id"]))
        if cur.rowcount == 0:
            raise ToolError(f"Rechnung mit ID {a['id']} nicht gefunden.")
        conn.commit()
        return {"invoice": _invoice_out(conn, a["id"])}
    finally:
        conn.close()


@tool("delete_invoice", "Rechnung löschen", "Löscht eine Rechnung samt Positionen endgültig.",
      {"id": I("Rechnungs-ID"), "confirm": B("Muss true sein, um das Löschen zu bestätigen")},
      ["id", "confirm"], write=True, destructive=True, idempotent=True)
def t_delete_invoice(a):
    _need_confirm(a, "Die Rechnung")
    conn = core.get_db()
    try:
        row = conn.execute("SELECT number FROM invoices WHERE id = ?", (a["id"],)).fetchone()
        if row is None:
            raise ToolError(f"Rechnung mit ID {a['id']} nicht gefunden.")
        conn.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (a["id"],))
        conn.execute("DELETE FROM invoices WHERE id = ?", (a["id"],))
        conn.commit()
    finally:
        conn.close()
    return {"deleted": True, "invoice_number": row["number"]}


@tool("get_invoice_pdf", "Rechnungs-PDF erzeugen",
      "Erzeugt das PDF einer Rechnung und liefert Download-Informationen: Dateiname, Größe, SHA-256 und "
      "einen signierten Download-Link (10 Minuten gültig, ohne weitere Anmeldung nutzbar). "
      "Optional wird das PDF zusätzlich Base64-codiert als eingebettete Ressource mitgeliefert.",
      {"id": I("Rechnungs-ID"),
       "embed": B("PDF zusätzlich als eingebettete Ressource (Base64) mitsenden (Standard false)")},
      ["id"])
def t_pdf(a):
    result = core.build_invoice_pdf(a["id"])
    if result is None:
        raise ToolError(f"Rechnung mit ID {a['id']} nicht gefunden.")
    data, filename = result
    token = _serializer().dumps({"i": a["id"], "t": request.environ["mcp.token_id"]})
    now = time.time()
    for k in [k for k, v in _pdf_cache.items() if v[0] < now]:
        del _pdf_cache[k]
    while len(_pdf_cache) >= _PDF_CACHE_MAX:
        _pdf_cache.pop(next(iter(_pdf_cache)))
    _pdf_cache[token] = (now + DOWNLOAD_TTL_SECONDS, data, filename)
    expires = datetime.now(timezone.utc) + timedelta(seconds=DOWNLOAD_TTL_SECONDS)
    info = {
        "invoice_id": a["id"], "filename": filename, "mime_type": "application/pdf",
        "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "download_url": f"{_public_base()}/download/{token}",
        "expires_at": expires.replace(microsecond=0).isoformat(),
        "expires_in_seconds": DOWNLOAD_TTL_SECONDS,
        "hint": "Link im Browser öffnen oder per GET abrufen; er läuft nach 10 Minuten ab. "
                "Mit ?inline=1 öffnet das PDF stattdessen als Vorschau im Browser.",
    }
    if a.get("embed"):
        info["_embed"] = {"type": "resource", "resource": {
            "uri": f"rechnungsapp://invoices/{a['id']}/pdf", "mimeType": "application/pdf",
            "blob": base64.b64encode(data).decode("ascii")}}
    return info


# --------------------------------------------------------------------------------------
# Tools: Einstellungen
# --------------------------------------------------------------------------------------
@tool("get_settings", "Einstellungen abrufen", "Alle Einstellungen: Firmendaten, Bankverbindung, "
      "Nummernkreise (Muster mit {YYYY},{YY},{MM},{DD} und Zähler {NNNN}), Zahlungsfrist, PDF-Texte.")
def t_get_settings(a):
    return {"settings": core.get_settings()}


@tool("update_settings", "Einstellungen ändern",
      "Ändert die übergebenen Einstellungen; nicht genannte bleiben unverändert. "
      "Schlüssel siehe get_settings. Werte immer als Text.",
      {"values": {"type": "object", "description": "Schlüssel → neuer Wert, z. B. {\"payment_due_days\": \"14\"}",
                  "additionalProperties": True}},
      ["values"], write=True, idempotent=True)
def t_update_settings(a):
    values = a["values"]
    unknown = [k for k in values if k not in core.DEFAULT_SETTINGS]
    if unknown:
        raise ToolError("Unbekannte Einstellungen: " + ", ".join(unknown))
    if any(not isinstance(v, (str, int, float)) or isinstance(v, bool) for v in values.values()):
        raise ToolError("Alle Werte müssen Text oder Zahl sein.")
    conn = core.get_db()
    try:
        for k, v in values.items():
            conn.execute("UPDATE settings SET value = ? WHERE key = ?", (str(v), k))
        conn.commit()
    finally:
        conn.close()
    return {"settings": core.get_settings()}


# --------------------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------------------
def _rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _call_tool(params, token):
    name = params.get("name")
    t = TOOLS.get(name)
    if t is None:
        return None, _rpc_error(None, -32602, f"Unbekanntes Tool: {name}")
    args = params.get("arguments") or {}
    err = _validate(t["def"]["inputSchema"], args)
    if err:
        return {"content": [{"type": "text", "text": f"Ungültige Eingabe – {err}"}], "isError": True}, None
    if t["write"] and token["scope"] != "full":
        return {"content": [{"type": "text", "text": "Dieses Token ist schreibgeschützt (Scope 'read')."}],
                "isError": True}, None
    request.environ["mcp.token_id"] = token["id"]
    try:
        out = t["fn"](args)
        embed = out.pop("_embed", None)
        content = [{"type": "text", "text": _json_text(out)}]
        if embed:
            content.append(embed)
        log.info("tool=%s token=%s ok", name, token["name"])
        return {"content": content, "structuredContent": out, "isError": False}, None
    except ToolError as e:
        msg = str(e)
    except HTTPException as e:  # z. B. ungültiges Datum aus parse_date_input
        msg = e.description or e.name
    except (ValueError, KeyError) as e:
        msg = f"Ungültige Werte: {e}"
    except Exception:
        log.exception("tool=%s fehlgeschlagen", name)
        msg = "Interner Fehler."
    log.info("tool=%s token=%s error=%s", name, token["name"], msg)
    return {"content": [{"type": "text", "text": msg}], "isError": True}, None


def _json_text(obj):
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _handle(msg, token):
    """Verarbeitet eine JSON-RPC-Nachricht. Liefert die Antwort oder None (Notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "Ungültige Anfrage")
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None:
        return None  # Antwort des Clients auf eine Server-Anfrage – wir stellen keine
    if "id" not in msg:
        return None  # Notification (z. B. notifications/initialized)
    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": "Rechnungsverwaltung", "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [t["def"] for t in TOOLS.values()]}}
    if method == "tools/call":
        result, err = _call_tool(params, token)
        if err:
            err["id"] = rid
            return err
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    return _rpc_error(rid, -32601, f"Methode nicht unterstützt: {method}")


def _origin_ok():
    """Schutz vor DNS-Rebinding/Browser-Zugriffen (MCP-Spezifikation): Origin muss zum Host passen."""
    origin = request.headers.get("Origin")
    return origin is None or urlsplit(origin).netloc == request.host


@bp.route("/mcp", methods=["POST", "GET", "DELETE"])
def endpoint():
    if not _origin_ok():
        return jsonify(error="Origin nicht erlaubt"), 403
    token = current_app.extensions["auth"].authenticate_bearer()
    if token is None:
        resp = jsonify(error="Nicht autorisiert: gültiges Bearer-Token erforderlich.")
        resp.status_code = 401
        resp.headers["WWW-Authenticate"] = 'Bearer realm="rechnungsapp"'
        return resp
    if request.method != "POST":
        resp = jsonify(error="Nur POST wird unterstützt.")
        resp.status_code = 405
        resp.headers["Allow"] = "POST"
        return resp
    body = request.get_json(silent=True)
    if body is None:
        return jsonify(_rpc_error(None, -32700, "Ungültiges JSON")), 400
    if isinstance(body, list):
        responses = [r for r in (_handle(m, token) for m in body) if r is not None]
        return (jsonify(responses), 200) if responses else ("", 202)
    response = _handle(body, token)
    return (jsonify(response), 200) if response is not None else ("", 202)


@bp.route("/download/<signed>")
def download(signed):
    """Kurzlebiger, signierter PDF-Download (Link aus get_invoice_pdf)."""
    try:
        payload = _serializer().loads(signed, max_age=DOWNLOAD_TTL_SECONDS)
    except SignatureExpired:
        return jsonify(error="Download-Link abgelaufen. Bitte get_invoice_pdf erneut aufrufen."), 410
    except BadSignature:
        return jsonify(error="Ungültiger Download-Link."), 404
    if not current_app.extensions["auth"].token_is_active(payload.get("t")):
        return jsonify(error="Das Token, mit dem der Link erzeugt wurde, ist nicht mehr gültig."), 410
    cached = _pdf_cache.get(signed)
    if cached and cached[0] >= time.time():
        _, data, filename = cached
    else:  # z. B. nach Neustart des Servers
        result = core.build_invoice_pdf(payload["i"])
        if result is None:
            return jsonify(error="Rechnung nicht gefunden."), 404
        data, filename = result
    return send_file(io.BytesIO(data), mimetype="application/pdf",
                     as_attachment=request.args.get("inline") != "1", download_name=filename)


def init_mcp(app, core_module):
    global core
    core = core_module
    app.register_blueprint(bp)
