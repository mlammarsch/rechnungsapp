import sqlite3
import re
from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, abort, send_file, jsonify
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable, KeepTogether)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import io
import os
import sys
from xml.sax.saxutils import escape as xml_escape

import auth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Persistente Daten (DB + secret.key) liegen im Docker-Betrieb in einem Volume (RECHNUNGSAPP_DATA_DIR)
DATA_DIR = os.environ.get("RECHNUNGSAPP_DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "rechnungsapp.db")

app = Flask(__name__)

# Hinter genau einem Reverse Proxy (Caddy): echte Client-IP/Schema aus X-Forwarded-* übernehmen
if os.environ.get("RECHNUNGSAPP_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

DEFAULT_SETTINGS = {
    "company_name": "",
    "company_street": "",
    "company_zip": "",
    "company_city": "",
    "company_email": "",
    "company_phone": "",
    "tax_id": "",
    "ust_id": "",
    "account_holder": "",
    "bank_name": "",
    "iban": "",
    "bic": "",
    "doc_title": "Rechnung",
    "footer_note": "Vielen Dank für Ihren Auftrag. Bitte senden Sie Zahlungen vor dem Fälligkeitsdatum.",
    "small_business_note": "Gemäß § 19 UStG wird aufgrund der Kleinunternehmerregelung keine Umsatzsteuer erhoben.",
    "invoice_number_pattern": "RG-{NNNNN}",
    "invoice_number_start": "1",
    "invoice_number_reset_yearly": "0",
    "customer_number_pattern": "KD-{NNNN}",
    "customer_number_start": "1",
    "customer_number_reset_yearly": "0",
    "payment_due_days": "14",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL UNIQUE,
    company TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    street TEXT NOT NULL DEFAULT '',
    zip TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL UNIQUE,
    invoice_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL DEFAULT 'offen',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    description TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    auth.init_auth_db(conn)
    # Migrationen für bestehende Datenbanken
    migrations = [
        ("invoices", "service_date", "TEXT NOT NULL DEFAULT ''"),
        ("invoice_items", "discount", "REAL NOT NULL DEFAULT 0"),
        ("invoices", "discount_percent", "REAL NOT NULL DEFAULT 0"),
        ("invoices", "discount_amount", "REAL NOT NULL DEFAULT 0"),
    ]
    for table, column, ddl in migrations:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


DATE_TOKENS = {
    "YYYY": lambda d: f"{d.year:04d}",
    "YY": lambda d: f"{d.year % 100:02d}",
    "MM": lambda d: f"{d.month:02d}",
    "M": lambda d: str(d.month),
    "DD": lambda d: f"{d.day:02d}",
    "D": lambda d: str(d.day),
}

COUNTER_RE = re.compile(r"\{(N+)\}")


def render_pattern(pattern, counter_value, ref=None):
    ref = ref or date.today()
    out = pattern
    for token, fn in DATE_TOKENS.items():
        out = out.replace("{" + token + "}", fn(ref))
    m = COUNTER_RE.search(out)
    if m:
        width = len(m.group(1))
        out = out[:m.start()] + str(counter_value).zfill(width) + out[m.end():]
    return out


def to_int(value, default=1):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def next_number(kind, settings, ref=None):
    """Nächste Nummer = Startwert + Anzahl vorhandener Datensätze.

    kind: "invoice" oder "customer". Mit aktivierter Jahres-Rücksetzung zählen
    nur Datensätze des Referenzjahres. Bei Kollisionen (z. B. nach Löschungen)
    wird weitergezählt, bis die Nummer frei ist.
    """
    ref = ref or date.today()
    table, datecol = ("invoices", "invoice_date") if kind == "invoice" else ("customers", "created_at")
    pattern = settings[f"{kind}_number_pattern"]
    start = to_int(settings.get(f"{kind}_number_start"), 1)
    reset = settings.get(f"{kind}_number_reset_yearly") == "1"
    conn = get_db()
    try:
        if reset:
            count = conn.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE strftime('%Y', {datecol}) = ?",
                (f"{ref.year:04d}",),
            ).fetchone()["c"]
        else:
            count = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        n = start + count
        while True:
            number = render_pattern(pattern, n, ref)
            if not conn.execute(f"SELECT 1 FROM {table} WHERE number = ?", (number,)).fetchone():
                return number
            n += 1
    finally:
        conn.close()


def calc_totals(items_sum, discount_percent, discount_amount):
    """Positionssumme -> (Zwischensumme, Rabatt gesamt, Endbetrag)."""
    after = items_sum * (1 - (discount_percent or 0) / 100)
    final = max(round(after - (discount_amount or 0), 2), 0.0)
    return round(items_sum, 2), round(items_sum - final, 2), final


# SQL-Variante von calc_totals für Listen/Statistik
SQL_FINAL = """MAX(ROUND((SELECT COALESCE(SUM(i.total),0) FROM invoice_items i WHERE i.invoice_id=v.id)
                  * (1 - v.discount_percent/100.0) - v.discount_amount, 2), 0)"""


def parse_date(s):
    return date.fromisoformat(s)


def fmt_eur(value):
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_date_de(s):
    try:
        d = parse_date(s)
        return d.strftime("%d.%m.%Y")
    except Exception:
        return s


auth.init_auth(app, get_db, DATA_DIR)


@app.context_processor
def inject_helpers():
    return {"fmt_eur": fmt_eur, "fmt_date_de": fmt_date_de}


def dashboard_data():
    conn = get_db()
    stats = {
        "customers": conn.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"],
        "invoices": conn.execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"],
        "open": conn.execute("SELECT COUNT(*) c FROM invoices WHERE status='offen'").fetchone()["c"],
        "sum_open": conn.execute(
            f"SELECT COALESCE(SUM({SQL_FINAL}),0) s FROM invoices v WHERE v.status='offen'"
        ).fetchone()["s"],
        "sum_paid": conn.execute(
            f"SELECT COALESCE(SUM({SQL_FINAL}),0) s FROM invoices v WHERE v.status='bezahlt'"
        ).fetchone()["s"],
        "sum_total": conn.execute(
            f"SELECT COALESCE(SUM({SQL_FINAL}),0) s FROM invoices v"
        ).fetchone()["s"],
    }
    recent = conn.execute(
        "SELECT v.*, c.name AS customer_name FROM invoices v JOIN customers c ON c.id=v.customer_id ORDER BY v.id DESC LIMIT 5"
    ).fetchall()
    conn.close()
    return stats, recent


@app.route("/")
def index():
    stats, recent = dashboard_data()
    return render_template("index.html", stats=stats, recent=recent)


@app.route("/customers")
def customers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM customers ORDER BY number").fetchall()
    conn.close()
    return render_template("customers.html", customers=rows)


@app.route("/customers/new", methods=["GET", "POST"])
def customer_new():
    settings = get_settings()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        company = request.form.get("company", "").strip()
        if not name and request.headers.get("X-Requested-With") == "fetch":
            return jsonify(error="Name ist erforderlich."), 400
        number = next_number("customer", settings)
        conn = get_db()
        cur = conn.execute(
            """INSERT INTO customers(number, company, name, street, zip, city, email, phone, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                number,
                company,
                name,
                request.form.get("street", "").strip(),
                request.form.get("zip", "").strip(),
                request.form.get("city", "").strip(),
                request.form.get("email", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("notes", "").strip(),
            ),
        )
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        if request.headers.get("X-Requested-With") == "fetch":
            # Anlage aus dem Kunden-Dropdown der Rechnung (Modal, ohne Seitenwechsel)
            label = f"{number} – {name}" + (f" ({company})" if company else "")
            return jsonify(id=new_id, label=label)
        return redirect(url_for("customers"))
    return render_template("customer_form.html", customer=None, pattern=settings["customer_number_pattern"])


CUSTOMER_FIELDS = ("company", "name", "street", "zip", "city", "email", "phone", "notes")


@app.route("/customers/<int:cid>/edit", methods=["GET", "POST"])
def customer_edit(cid):
    conn = get_db()
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if row is None:
        conn.close()
        abort(404)
    if request.method == "POST":
        vals = {k: request.form.get(k, "").strip() for k in CUSTOMER_FIELDS}
        if not vals["name"]:
            conn.close()
            return render_template("customer_form.html", customer={**dict(row), **vals},
                                   error="Name ist erforderlich."), 400
        conn.execute(
            """UPDATE customers SET company=?, name=?, street=?, zip=?, city=?, email=?, phone=?, notes=?
               WHERE id=?""",
            (*(vals[k] for k in CUSTOMER_FIELDS), cid),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("customers"))
    conn.close()
    return render_template("customer_form.html", customer=row)


@app.route("/customers/<int:cid>/delete", methods=["POST"])
def customer_delete(cid):
    conn = get_db()
    conn.execute("DELETE FROM customers WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    return redirect(url_for("customers"))


def _sum_stats(rows):
    """Umsatz-Kennzahlen (offen/bezahlt/gesamt) für eine Liste von Rechnungszeilen."""
    s = {"count": len(rows), "open": 0.0, "paid": 0.0}
    for r in rows:
        s["paid" if r["status"] == "bezahlt" else "open"] += r["total"]
    s["total"] = s["open"] + s["paid"]
    return s


@app.route("/invoices")
def invoices():
    conn = get_db()
    all_customers = conn.execute("SELECT id, number, name, company FROM customers ORDER BY name").fetchall()
    rows = conn.execute(
        f"""SELECT v.*, c.name AS customer_name, c.company AS customer_company,
                  {SQL_FINAL} AS total
           FROM invoices v JOIN customers c ON c.id=v.customer_id ORDER BY v.id DESC"""
    ).fetchall()
    conn.close()

    this_year = str(date.today().year)
    years = sorted({r["invoice_date"][:4] for r in rows} | {this_year}, reverse=True)
    year = request.args.get("year", this_year)          # "all" = alle Jahre
    customer_id = request.args.get("customer", "")
    group = request.args.get("group") == "1"

    if year != "all":
        rows = [r for r in rows if r["invoice_date"][:4] == year]
    if customer_id.isdigit():
        rows = [r for r in rows if r["customer_id"] == int(customer_id)]

    groups = []
    if group:
        by_cust = {}
        for r in rows:
            by_cust.setdefault(r["customer_id"], []).append(r)
        for cid, rs in by_cust.items():
            groups.append({"name": rs[0]["customer_name"], "company": rs[0]["customer_company"],
                           "invoices": rs, "stats": _sum_stats(rs)})
        groups.sort(key=lambda g: (g["company"] or g["name"]).casefold())

    return render_template("invoices.html", invoices=rows, stats=_sum_stats(rows), groups=groups,
                           customers=all_customers, years=years, year=year,
                           customer_id=customer_id, group=group)


def run_search(q):
    """Volltextsuche über Kunden und Rechnungen (alle Begriffe müssen vorkommen, ohne Groß-/Kleinschreibung)."""
    terms = q.strip().casefold().split()
    found_customers, found_invoices = [], []
    if terms:
        conn = get_db()
        cust_rows = conn.execute("SELECT * FROM customers ORDER BY number").fetchall()
        inv_rows = conn.execute(
            f"""SELECT v.*, c.name AS customer_name, c.company AS customer_company, c.number AS customer_number,
                      {SQL_FINAL} AS total,
                      (SELECT GROUP_CONCAT(i.description, ' ') FROM invoice_items i WHERE i.invoice_id=v.id) AS item_text
               FROM invoices v JOIN customers c ON c.id=v.customer_id ORDER BY v.id DESC"""
        ).fetchall()
        conn.close()

        def hit(*values):
            hay = " ".join(str(v) for v in values if v not in (None, "")).casefold()
            return all(term in hay for term in terms)

        found_customers = [c for c in cust_rows if hit(
            c["number"], c["company"], c["name"], c["street"], c["zip"], c["city"],
            c["email"], c["phone"], c["notes"])]
        found_invoices = [v for v in inv_rows if hit(
            v["number"], v["status"], v["notes"], v["invoice_date"], fmt_date_de(v["invoice_date"]),
            v["due_date"], fmt_date_de(v["due_date"]), v["customer_number"], v["customer_name"],
            v["customer_company"], v["item_text"], fmt_eur(v["total"]))]
    return found_customers, found_invoices


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    found_customers, found_invoices = run_search(q)
    return render_template("search.html", q=q, customers=found_customers, invoices=found_invoices)


def parse_date_input(value):
    """Akzeptiert TT.MM.JJJJ (auch T.M.JJ) sowie JJJJ-MM-TT, liefert ISO-String ('' bei leer)."""
    value = (value or "").strip()
    if not value:
        return ""
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    abort(400, f"Ungültiges Datum: {value} (erwartet TT.MM.JJJJ)")


def to_float(value, default=0.0):
    try:
        return float(str(value).replace(",", ".").strip() or default)
    except ValueError:
        return default


def save_invoice_items(conn, invoice_id, form):
    conn.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    pos = 1
    discounts = form.getlist("discount")
    for idx, (desc, qty, price) in enumerate(zip(
            form.getlist("description"), form.getlist("quantity"), form.getlist("unit_price"))):
        desc = desc.strip()
        if not desc:
            continue
        q = to_float(qty, 1)
        p = to_float(price)
        disc = min(max(to_float(discounts[idx] if idx < len(discounts) else 0), 0.0), 100.0)
        conn.execute(
            """INSERT INTO invoice_items(invoice_id, position, description, quantity, unit_price, discount, total)
               VALUES (?,?,?,?,?,?,?)""",
            (invoice_id, pos, desc, q, p, disc, round(q * p * (1 - disc / 100), 2)),
        )
        pos += 1


def invoice_form_values(form, settings):
    invoice_date = parse_date_input(form["invoice_date"])
    due_date = parse_date_input(form.get("due_date")) or (
        parse_date(invoice_date) + timedelta(days=to_int(settings["payment_due_days"], 14))
    ).isoformat()
    return {
        "customer_id": int(form["customer_id"]),
        "invoice_date": invoice_date,
        "service_date": parse_date_input(form.get("service_date")),
        "due_date": due_date,
        "notes": form.get("notes", "").strip(),
        "discount_percent": min(max(to_float(form.get("discount_percent")), 0.0), 100.0),
        "discount_amount": max(to_float(form.get("discount_amount")), 0.0),
    }


def render_invoice_form(settings, inv=None, items=None):
    conn = get_db()
    customers = conn.execute("SELECT * FROM customers ORDER BY name").fetchall()
    conn.close()
    if inv is None:
        today = date.today()
        due = today + timedelta(days=to_int(settings["payment_due_days"], 14))
        v = {"customer_id": None, "invoice_date": fmt_date_de(today.isoformat()),
             "service_date": fmt_date_de(today.isoformat()), "due_date": fmt_date_de(due.isoformat()),
             "notes": "", "discount_percent": 0, "discount_amount": 0}
        items = []
    else:
        v = {"customer_id": inv["customer_id"], "invoice_date": fmt_date_de(inv["invoice_date"]),
             "service_date": fmt_date_de(inv["service_date"]) if inv["service_date"] else "",
             "due_date": fmt_date_de(inv["due_date"]), "notes": inv["notes"],
             "discount_percent": inv["discount_percent"], "discount_amount": inv["discount_amount"]}
        items = [{"description": i["description"], "quantity": i["quantity"],
                  "unit_price": i["unit_price"], "discount": i["discount"]} for i in items]
    return render_template("invoice_form.html", customers=customers, v=v, items=items, inv=inv,
                           pattern=settings["invoice_number_pattern"],
                           due_days=settings["payment_due_days"],
                           customer_pattern=settings["customer_number_pattern"])


@app.route("/invoices/new", methods=["GET", "POST"])
def invoice_new():
    settings = get_settings()
    if request.method == "POST":
        f = invoice_form_values(request.form, settings)
        number = next_number("invoice", settings, parse_date(f["invoice_date"]))
        conn = get_db()
        try:
            cur = conn.execute(
                """INSERT INTO invoices(number, invoice_date, service_date, due_date, customer_id, status, notes,
                                        discount_percent, discount_amount)
                   VALUES (?,?,?,?,?, 'offen', ?,?,?)""",
                (number, f["invoice_date"], f["service_date"], f["due_date"], f["customer_id"],
                 f["notes"], f["discount_percent"], f["discount_amount"]),
            )
            invoice_id = cur.lastrowid
            save_invoice_items(conn, invoice_id, request.form)
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for("invoice_detail", iid=invoice_id))
    return render_invoice_form(settings)


@app.route("/invoices/<int:iid>/edit", methods=["GET", "POST"])
def invoice_edit(iid):
    settings = get_settings()
    conn = get_db()
    try:
        inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (iid,)).fetchone()
        if inv is None:
            abort(404)
        if inv["status"] == "bezahlt":
            abort(403, "Bezahlte Rechnungen sind gesperrt. Erst wieder als offen markieren, um sie zu bearbeiten.")
        if request.method == "POST":
            f = invoice_form_values(request.form, settings)
            conn.execute(
                """UPDATE invoices SET invoice_date=?, service_date=?, due_date=?, customer_id=?, notes=?,
                          discount_percent=?, discount_amount=? WHERE id=?""",
                (f["invoice_date"], f["service_date"], f["due_date"], f["customer_id"], f["notes"],
                 f["discount_percent"], f["discount_amount"], iid),
            )
            save_invoice_items(conn, iid, request.form)
            conn.commit()
            return redirect(url_for("invoice_detail", iid=iid))
        items = conn.execute(
            "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position", (iid,)).fetchall()
    finally:
        conn.close()
    return render_invoice_form(settings, inv, items)


@app.route("/invoices/<int:iid>")
def invoice_detail(iid):
    conn = get_db()
    inv = conn.execute(
        "SELECT v.*, c.number AS customer_number, c.company, c.name, c.street, c.zip, c.city, c.email, c.phone "
        "FROM invoices v JOIN customers c ON c.id=v.customer_id WHERE v.id = ?",
        (iid,),
    ).fetchone()
    if inv is None:
        conn.close()
        abort(404)
    items = conn.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position", (iid,)
    ).fetchall()
    subtotal, discount_total, total = calc_totals(
        sum(i["total"] for i in items), inv["discount_percent"], inv["discount_amount"])
    conn.close()
    return render_template("invoice_detail.html", inv=inv, items=items, total=total,
                           subtotal=subtotal, discount_total=discount_total)


@app.route("/invoices/<int:iid>/status", methods=["POST"])
def invoice_status(iid):
    status = request.form.get("status", "offen")
    if status not in ("offen", "bezahlt"):
        status = "offen"
    conn = get_db()
    conn.execute("UPDATE invoices SET status = ? WHERE id = ?", (status, iid))
    conn.commit()
    conn.close()
    return redirect(url_for("invoice_detail", iid=iid))


@app.route("/invoices/<int:iid>/delete", methods=["POST"])
def invoice_delete(iid):
    conn = get_db()
    conn.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (iid,))
    conn.execute("DELETE FROM invoices WHERE id = ?", (iid,))
    conn.commit()
    conn.close()
    return redirect(url_for("invoices"))


FONT_DIRS = [
    os.path.join(BASE_DIR, "fonts"),
    r"C:\Windows\Fonts",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/open-sans",
]
# (Regular, Bold) – Open Sans entspricht der Vorlage, danach ähnliche Fallbacks
FONT_CANDIDATES = [
    ("OpenSans-Regular.ttf", "OpenSans-Bold.ttf"),
    ("segoeui.ttf", "segoeuib.ttf"),
    ("arial.ttf", "arialbd.ttf"),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
]


def register_fonts():
    for regular, bold in FONT_CANDIDATES:
        for d in FONT_DIRS:
            r, b = os.path.join(d, regular), os.path.join(d, bold)
            if os.path.exists(r) and os.path.exists(b):
                name = os.path.splitext(regular)[0]
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, r))
                    pdfmetrics.registerFont(TTFont(name + "-Bold", b))
                return name, name + "-Bold"
    return "Helvetica", "Helvetica-Bold"


def esc(text):
    return xml_escape(str(text or ""))


def fmt_qty(value):
    return f"{value:g}".replace(".", ",")


COLOR_TEXT = colors.HexColor("#222222")
COLOR_MUTED = colors.HexColor("#444444")
COLOR_LINE = colors.HexColor("#dddddd")
COLOR_LINE_STRONG = colors.HexColor("#888888")
COLOR_BG = colors.HexColor("#f5f5f5")


def pdf_filename(inv):
    """Rechnungsnummer - Kunde - Leistungsdatum.pdf"""
    customer = inv["company"] or inv["name"]
    service = inv["service_date"] or inv["invoice_date"]
    name = f"{inv['number']} - {customer} - {service}"
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip()
    return name + ".pdf"


def build_invoice_pdf(iid):
    """Erzeugt das Rechnungs-PDF. Liefert (bytes, Dateiname) oder None, wenn die Rechnung fehlt."""
    settings = get_settings()
    conn = get_db()
    inv = conn.execute(
        "SELECT v.*, c.number AS customer_number, c.company, c.name, c.street, c.zip, c.city "
        "FROM invoices v JOIN customers c ON c.id=v.customer_id WHERE v.id = ?",
        (iid,),
    ).fetchone()
    if inv is None:
        conn.close()
        return None
    items = conn.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position", (iid,)
    ).fetchall()
    conn.close()

    font, font_bold = register_fonts()
    base = ParagraphStyle("base", fontName=font, fontSize=9, leading=13, textColor=COLOR_TEXT)
    right = ParagraphStyle("right", parent=base, alignment=2)
    bold = ParagraphStyle("bold", parent=base, fontName=font_bold)
    bold_right = ParagraphStyle("boldr", parent=bold, alignment=2)
    muted = ParagraphStyle("muted", parent=base, textColor=COLOR_MUTED)
    muted_right = ParagraphStyle("mutedr", parent=muted, alignment=2)
    name_style = ParagraphStyle("name", parent=bold, fontSize=15, leading=19)
    title_style = ParagraphStyle("title", parent=bold, fontSize=15, leading=19)
    note_head = ParagraphStyle("notehead", parent=bold, fontSize=9, spaceAfter=4)
    note = ParagraphStyle("note", parent=base, spaceAfter=7)

    page_w = A4[0] - 2 * 18.5 * mm - 12  # Nutzbreite abzgl. Frame-Padding (2 x 6 pt)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18.5 * mm, rightMargin=18.5 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"{settings['doc_title'] or 'Rechnung'} {inv['number']}",
                            author=settings["company_name"])
    story = []

    # --- Kopf: Absender links, Zahlungsdetails rechts -------------------
    left_lines = [Paragraph(esc(settings["company_name"] or "Firmenname"), name_style)]
    for line in (
        settings["company_street"],
        f"{settings['company_zip']} {settings['company_city']}".strip(),
        f"Fon: {settings['company_phone']}" if settings["company_phone"] else "",
        f"Email: {settings['company_email']}" if settings["company_email"] else "",
        f"Steuernummer: {settings['tax_id']}" if settings["tax_id"] else "",
        f"USt-IdNr.: {settings['ust_id']}" if settings["ust_id"] else "",
    ):
        if line:
            left_lines.append(Paragraph(esc(line), base))

    right_lines = [Paragraph("Zahlungsdetails:", right)]
    for line in (
        f"Kontoinhaber: {settings['account_holder']}" if settings["account_holder"] else "",
        f"Bank: {settings['bank_name']}" if settings["bank_name"] else "",
        f"IBAN: {settings['iban']}" if settings["iban"] else "",
        f"BIC: {settings['bic']}" if settings["bic"] else "",
    ):
        if line:
            right_lines.append(Paragraph(esc(line), right))

    header = Table([[left_lines, right_lines]], colWidths=[page_w * 0.55, page_w * 0.45])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("TOPPADDING", (0, 0), (-1, -1), 0),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    story.append(header)
    story.append(Spacer(1, 7 * mm))
    story.append(HRFlowable(width="100%", thickness=0.8, color=COLOR_LINE, spaceAfter=0))
    story.append(Spacer(1, 8 * mm))

    # --- Empfänger links, Rechnungsdaten rechts ------------------------
    # Absenderzeile für das Fenster im DIN-lang-Umschlag (klein, eine Zeile)
    sender_parts = [settings["company_name"], settings["company_street"],
                    f"{settings['company_zip']} {settings['company_city']}".strip()]
    sender_line = " - ".join(p for p in sender_parts if p)
    sender_style = ParagraphStyle("sender", parent=base, fontSize=6.5, leading=8, textColor=COLOR_MUTED,
                                  spaceAfter=3)
    rec = []
    if sender_line:
        rec.append(Paragraph(f"<u>{esc(sender_line)}</u>", sender_style))
    rec.append(Paragraph("Empfänger:", bold))
    if inv["company"]:
        rec.append(Paragraph(esc(inv["company"]), base))
    if inv["name"] and inv["name"] != inv["company"]:
        rec.append(Paragraph(esc(inv["name"]), base))
    if inv["street"]:
        rec.append(Paragraph(esc(inv["street"]), base))
    rec.append(Paragraph(esc(f"{inv['zip']} {inv['city']}".strip()), base))

    meta = [("Rechnungs-Nr.", inv["number"]),
            ("Rechnungsdatum:", fmt_date_de(inv["invoice_date"]))]
    if inv["service_date"]:
        meta.append(("Leistungsdatum:", fmt_date_de(inv["service_date"])))
    meta.append(("Fälligkeitsdatum:", fmt_date_de(inv["due_date"])))
    meta.append(("Kundennummer:", inv["customer_number"]))
    meta_table = Table([[Paragraph(esc(a), base), Paragraph(esc(b), right)] for a, b in meta],
                       colWidths=[30 * mm, 26 * mm])
    meta_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    parties = Table([[rec, meta_table]], colWidths=[page_w - 56 * mm, 56 * mm])
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("TOPPADDING", (0, 0), (-1, -1), 0),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    story.append(parties)
    story.append(Spacer(1, 16 * mm))

    # --- Titel ----------------------------------------------------------
    story.append(Paragraph(esc(settings["doc_title"] or "Rechnung"), title_style))
    story.append(Spacer(1, 5 * mm))

    # --- Positionen -----------------------------------------------------
    head = [Paragraph("Position", bold), "",
            Paragraph("Anzahl", bold_right), Paragraph("Preis", bold_right),
            Paragraph("Rabatt", bold_right), Paragraph("Gesamt", bold_right)]
    data = [head]
    for i in items:
        disc = i["discount"] or 0
        data.append([
            Paragraph(str(i["position"]), base),
            Paragraph(esc(i["description"]).replace("\n", "<br/>"), base),
            Paragraph(fmt_qty(i["quantity"]), right),
            Paragraph(esc(fmt_eur(i["unit_price"])), right),
            Paragraph(f"{fmt_qty(disc)}%", right),
            Paragraph(esc(fmt_eur(i["total"])), right),
        ])
    subtotal, discount_total, total = calc_totals(
        sum(i["total"] for i in items), inv["discount_percent"], inv["discount_amount"])
    col_w = [8 * mm, page_w - (8 + 16 + 24 + 17 + 24) * mm, 16 * mm, 24 * mm, 17 * mm, 24 * mm]
    t = Table(data, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ("SPAN", (0, 0), (1, 0)),
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 1, COLOR_LINE_STRONG),
        ("LINEBELOW", (0, 1), (-1, -1), 0.6, COLOR_LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (0, -1), 6),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 3),
    ]))
    story.append(t)
    story.append(Spacer(1, 9 * mm))

    # --- Summen ---------------------------------------------------------
    rows = [[Paragraph("Nettobetrag:", base), Paragraph(esc(fmt_eur(subtotal)), right)]]
    if discount_total > 0:
        pct, amt = inv["discount_percent"] or 0, inv["discount_amount"] or 0
        label = "Gesamtrabatt"
        if pct and not amt:
            label += f" ({fmt_qty(pct)}%)"
        rows.append([Paragraph(label + ":", base), Paragraph("-" + esc(fmt_eur(discount_total)), right)])
    rows.append([Paragraph("Rechnungsbetrag:", bold), Paragraph(esc(fmt_eur(total)), bold_right)])
    last = len(rows) - 1
    sums = Table(rows, colWidths=[40 * mm, 30 * mm], hAlign="RIGHT")
    sums.setStyle(TableStyle([
        ("LINEABOVE", (0, last), (-1, last), 1.5, COLOR_LINE_STRONG),
        ("LINEBELOW", (0, last), (-1, last), 0.6, COLOR_LINE),
        ("BACKGROUND", (0, last), (-1, last), COLOR_BG),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (0, -1), 6),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 3),
    ]))
    story.append(sums)
    story.append(Spacer(1, 16 * mm))

    # --- Hinweise und Anmerkungen --------------------------------------
    notes = [inv["notes"], settings["footer_note"], settings["small_business_note"]]
    notes = [n for n in notes if n and n.strip()]
    if notes:
        block = [Paragraph("Hinweise und Anmerkungen", note_head)]
        for n in notes:
            block.append(Paragraph(esc(n).replace("\n", "<br/>"), note))
        story.append(KeepTogether(block))

    doc.build(story)
    return buf.getvalue(), pdf_filename(inv)


@app.route("/invoices/<int:iid>/pdf")
def invoice_pdf(iid):
    result = build_invoice_pdf(iid)
    if result is None:
        abort(404)
    data, filename = result
    # Standard: Inline-Vorschau im Browser; mit ?download=1 als Datei-Download.
    return send_file(io.BytesIO(data), mimetype="application/pdf",
                     as_attachment=request.args.get("download") == "1",
                     download_name=filename)


@app.route("/settings", methods=["GET", "POST"])
def settings_view():
    if request.method == "POST":
        conn = get_db()
        for key in DEFAULT_SETTINGS:
            conn.execute("UPDATE settings SET value = ? WHERE key = ?",
                         (request.form.get(key, ""), key))
        conn.commit()
        conn.close()
        return redirect(url_for("settings_view"))
    return render_template("settings.html", s=get_settings())


import mcp_server  # noqa: E402

mcp_server.init_mcp(app, sys.modules[__name__])

init_db()  # auch beim Start über gunicorn (Schema/Migrationen sind idempotent)


if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "reset-user":
        # Notausgang bei vergessenem Passwort: Benutzer löschen, Registrierung ist danach wieder offen
        auth.reset_user(get_db)
        print("Benutzer gelöscht. Beim nächsten Aufruf kann ein neuer Benutzer registriert werden.")
        sys.exit(0)
    # Der Werkzeug-Debugger erlaubt Codeausführung -> nur auf ausdrücklichen Wunsch (RECHNUNGSAPP_DEBUG=1)
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("RECHNUNGSAPP_DEBUG") == "1")
