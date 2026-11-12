"""Integrationstest für Token-Verwaltung und MCP-Server (temporäre DB, echte DB bleibt unberührt).

Aufruf:  .venv\\Scripts\\python.exe test_mcp.py
"""
import hashlib
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A  # noqa: E402
import mcp_server as M  # noqa: E402

A.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
A.init_db()
failed = []


def chk(name, cond, extra=""):
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        failed.append(name)


def tok(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


PW = "Korrektes Pferd Batterie 7"
c = A.app.test_client()
t = tok(c.get("/login").get_data(as_text=True))
c.post("/register", data={"csrf_token": t, "username": "max", "password": PW, "confirm": PW})
h = c.get("/account").get_data(as_text=True)
chk("Konto-Seite zeigt Token-Bereich", "API-Zugänge" in h)
t = tok(h)
chk("leerer Name -> 400", c.post("/account/tokens", data={"csrf_token": t, "name": "", "scope": "full", "expiry": "90"}).status_code == 400)
chk("Token ohne CSRF -> 400", c.post("/account/tokens", data={"name": "x", "scope": "full", "expiry": "90"}).status_code == 400)
h = c.post("/account/tokens", data={"csrf_token": t, "name": "Claude", "scope": "full", "expiry": "90"}).get_data(as_text=True)
FULL = re.search(r'value="(ra_[^"]+)"', h).group(1)
READ = re.search(r'value="(ra_[^"]+)"', c.post("/account/tokens", data={"csrf_token": t, "name": "Reader", "scope": "read", "expiry": "never"}).get_data(as_text=True)).group(1)
h = c.get("/account").get_data(as_text=True)
chk("Klartext-Token nur einmal sichtbar", FULL not in h and FULL[:8] in h)
con = A.get_db()
rows = [tuple(r) for r in con.execute("select * from api_tokens")]
con.close()
chk("nur Hash gespeichert", all(FULL not in str(r) for r in rows))

m = A.app.test_client()  # ohne Cookie


def rpc(method, params=None, tk=FULL, rid=1, headers=None):
    body = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    hd = {"Authorization": f"Bearer {tk}"} if tk else {}
    hd.update(headers or {})
    return m.post("/mcp", json=body, headers=hd)


def call(name, args=None, tk=FULL):
    res = rpc("tools/call", {"name": name, "arguments": args or {}}, tk=tk).get_json()["result"]
    txt = res["content"][0]["text"]
    return res["isError"], (txt if res["isError"] else json.loads(txt)), res


chk("ohne Token -> 401", rpc("ping", tk=None).status_code == 401)
chk("falsches Token -> 401", rpc("ping", tk="ra_bogus").status_code == 401)
chk("Session-Cookie allein reicht nicht", c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401)
chk("fremder Origin -> 403", rpc("ping", headers={"Origin": "http://evil.com"}).status_code == 403)
chk("GET /mcp -> 405", m.get("/mcp", headers={"Authorization": f"Bearer {FULL}"}).status_code == 405)
j = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}).get_json()
chk("initialize", j["result"]["protocolVersion"] == "2025-06-18" and "tools" in j["result"]["capabilities"])
chk("Notification -> 202", rpc("notifications/initialized", rid=None).status_code == 202)
names = [x["name"] for x in rpc("tools/list").get_json()["result"]["tools"]]
print("Tools:", len(names), names)
chk("unbekannte Methode", rpc("foo").get_json()["error"]["code"] == -32601)

e, o, _ = call("update_settings", {"values": {"company_name": "Muster GmbH", "payment_due_days": 14, "invoice_number_pattern": "RG-{NNNNN}"}})
chk("update_settings", not e and o["settings"]["company_name"] == "Muster GmbH" and o["settings"]["payment_due_days"] == "14", o)
chk("unbekannte Einstellung abgelehnt", call("update_settings", {"values": {"evil": "1"}})[0])
e, o, _ = call("create_customer", {"name": "Erika Muster", "company": "Muster KG", "city": "Heroldstatt", "zip": "72535"})
chk("create_customer", not e and o["customer"]["number"].startswith("KD-"), o)
cid = o["customer"]["id"]
chk("Pflichtfeld fehlt -> Fehler", call("create_customer", {})[0])
chk("unbekanntes Feld -> Fehler", call("create_customer", {"name": "x", "bogus": 1})[0])
e, o, _ = call("update_customer", {"id": cid, "phone": "0123"})
chk("update_customer teilweise", not e and o["customer"]["phone"] == "0123" and o["customer"]["company"] == "Muster KG", o)
e, o, _ = call("create_invoice", {"customer_id": cid, "service_date": "05.11.2026", "items": [
    {"description": "Stammtisch", "quantity": 3, "unit_price": 120}, {"description": "Material", "unit_price": 45.5}]})
chk("create_invoice (3x120 + 45,50 = 405,50)", not e and o["invoice"]["total"] == 405.5 and o["invoice"]["number"] == "RG-00001", o)
iid = o["invoice"]["id"]
chk("Rechnung für unbekannten Kunden", call("create_invoice", {"customer_id": 999, "items": [{"description": "a", "unit_price": 1}]})[0])
e, o, _ = call("create_invoice", {"customer_id": cid, "invoice_date": "31.02.2026", "items": [{"description": "a", "unit_price": 1}]})
chk("ungültiges Datum -> Fehlermeldung statt Absturz", e and "Datum" in o, o)
e, o, _ = call("update_invoice", {"id": iid, "discount_percent": 10})
chk("update_invoice behält Positionen", not e and len(o["invoice"]["items"]) == 2 and o["invoice"]["total"] == 364.95, o)
e, o, _ = call("update_invoice", {"id": iid, "items": [{"description": "Neu", "quantity": 2, "unit_price": 50}]})
chk("update_invoice ersetzt Positionen", not e and o["invoice"]["subtotal"] == 100, o)
e, o, _ = call("list_invoices")
chk("list_invoices", not e and o["stats"]["count"] == 1, o)
chk("get_invoice", call("get_invoice", {"id": iid})[1]["invoice"]["customer"]["company"] == "Muster KG")
chk("search", len(call("search", {"query": "heroldstatt"})[1]["customers"]) == 1)
chk("get_overview", call("get_overview")[1]["stats"]["invoices"] == 1)
e, o, _ = call("set_invoice_status", {"id": iid, "status": "bezahlt"})
chk("bezahlt setzen sperrt", not e and o["invoice"]["locked"])
e, o, _ = call("update_invoice", {"id": iid, "notes": "x"})
chk("bezahlte Rechnung gesperrt", e and "gesperrt" in o)
e, o, _ = call("delete_customer", {"id": cid, "confirm": True})
chk("Kunde mit Rechnungen nicht löschbar", e and "Rechnungen" in o, o)
chk("Löschen braucht confirm", call("delete_invoice", {"id": iid, "confirm": False})[0])

# --- PDF mit Download-Information ---
e, o, res = call("get_invoice_pdf", {"id": iid})
chk("PDF-Info", not e and o["size_bytes"] > 1000 and o["download_url"].startswith("http") and o["filename"].endswith(".pdf"), o)
path = "/" + o["download_url"].split("/", 3)[3]
r = m.get(path)
chk("Download ohne Anmeldung", r.status_code == 200 and r.data[:5] == b"%PDF-" and "attachment" in r.headers["Content-Disposition"] and len(r.data) == o["size_bytes"])
chk("SHA-256 stimmt", hashlib.sha256(r.data).hexdigest() == o["sha256"])
chk("?inline=1 -> Vorschau", "inline" in m.get(path + "?inline=1").headers["Content-Disposition"])
chk("manipulierter Link -> 404", m.get(path[:-3] + "abc").status_code == 404)
res = call("get_invoice_pdf", {"id": iid, "embed": True})[2]
chk("embed liefert Base64-Ressource", len(res["content"]) == 2 and res["content"][1]["resource"]["mimeType"] == "application/pdf")
chk("PDF für unbekannte Rechnung", call("get_invoice_pdf", {"id": 999})[0])
old = M.DOWNLOAD_TTL_SECONDS
M.DOWNLOAD_TTL_SECONDS = -1
chk("abgelaufener Link -> 410", m.get(path).status_code == 410)
M.DOWNLOAD_TTL_SECONDS = old

# --- Rechte ---
chk("read-Token darf lesen", not call("list_customers", tk=READ)[0])
e, o, _ = call("create_customer", {"name": "Z"}, tk=READ)
chk("read-Token darf nicht schreiben", e and "schreibgeschützt" in o)
chk("read-Token darf nicht löschen", call("delete_invoice", {"id": iid, "confirm": True}, tk=READ)[0])

call("set_invoice_status", {"id": iid, "status": "offen"})
chk("Rechnung löschen", not call("delete_invoice", {"id": iid, "confirm": True})[0])
chk("Kunde löschen", not call("delete_customer", {"id": cid, "confirm": True})[0])

# --- Widerruf / Ablauf ---
e, o, res = call("get_invoice_pdf", {"id": 1})  # Rechnung existiert nicht mehr -> neu anlegen für Linktest
cid2 = call("create_customer", {"name": "B"})[1]["customer"]["id"]
iid2 = call("create_invoice", {"customer_id": cid2, "items": [{"description": "a", "unit_price": 1}]})[1]["invoice"]["id"]
path2 = "/" + call("get_invoice_pdf", {"id": iid2})[1]["download_url"].split("/", 3)[3]
con = A.get_db()
tid = con.execute("select id from api_tokens where name='Claude'").fetchone()[0]
con.close()
c.post(f"/account/tokens/{tid}/revoke", data={"csrf_token": tok(c.get("/account").get_data(as_text=True))})
chk("widerrufenes Token abgelehnt", rpc("ping").status_code == 401)
chk("Link eines widerrufenen Tokens tot", m.get(path2).status_code == 410)
con = A.get_db()
con.execute("update api_tokens set expires_at='2000-01-01 00:00:00' where name='Reader'")
con.commit()
con.close()
chk("abgelaufenes Token abgelehnt", rpc("ping", tk=READ).status_code == 401)
chk("Web-UI weiter per Login geschützt", m.get("/customers").status_code == 302)

print("\nALLE TESTS BESTANDEN" if not failed else f"\n{len(failed)} FEHLER: {failed}")
sys.exit(1 if failed else 0)
