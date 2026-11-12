"""Benutzerverwaltung und Zugriffsschutz.

Es gibt genau einen Benutzer (DB-seitig erzwungen: users.id = 1). Solange keiner existiert,
zeigt der Anmeldedialog die Registrierung; danach ist sie dauerhaft gesperrt.

Sicherheitsmerkmale:
- Passwort-Hash mit scrypt (Werkzeug), Mindestlänge 12 Zeichen, Zeitangleichung bei unbekanntem Benutzer
- Sitzung als signiertes Cookie (HttpOnly, SameSite=Lax, optional Secure), feste Laufzeit,
  Session-Reset bei Login (gegen Session Fixation), Ungültigwerden aller Sitzungen bei Passwortwechsel
- CSRF-Token für alle schreibenden Requests
- Begrenzung von Fehlversuchen pro IP und global (persistiert in der DB)
- Sicherheits-Header (CSP, X-Frame-Options, nosniff, Referrer-Policy, no-store)
- Open-Redirect-Schutz für ?next=

API/MCP: Endpunkte mit Blueprint-Namen "api" bzw. "mcp" sind bewusst NICHT per Sitzung geschützt
(siehe API_BLUEPRINTS), sondern authentifizieren sich per Bearer-Token (authenticate_bearer) und
brauchen daher keinen CSRF-Schutz. Tokens werden nur als SHA-256-Hash gespeichert, haben einen
Geltungsbereich (read/full) und optional ein Ablaufdatum.
"""
import hashlib
import hmac
import time
from datetime import datetime, timezone
import os
import secrets
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit

from flask import (abort, flash, g, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ts ON login_attempts(ts);
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('read', 'full')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    last_used_at TEXT
);
"""

MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 256
SESSION_HOURS = 8
MAX_TOKENS = 20
TOKEN_PREFIX = "ra_"
TOKEN_EXPIRY_CHOICES = {"30": 30, "90": 90, "365": 365, "never": None}
MAX_FAILS_PER_IP = 5
MAX_FAILS_GLOBAL = 20
LOCK_WINDOW_SECONDS = 15 * 60

PUBLIC_ENDPOINTS = {"static", "login", "register"}
API_BLUEPRINTS = {"api", "mcp"}  # später: eigene Token-Authentifizierung

# Hash für unbekannte Benutzernamen, damit die Antwortzeit nichts verrät
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


def _load_secret_key(base_dir):
    """Persistenter, zufälliger Signaturschlüssel (Datei neben der App, nicht im Repo)."""
    path = os.path.join(base_dir, "secret.key")
    try:
        with open(path, "r", encoding="ascii") as fh:
            key = fh.read().strip()
            if len(key) >= 64:
                return key
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key)
    return key


def _hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _fingerprint(password_hash):
    return hashlib.sha256(password_hash.encode()).hexdigest()[:24]


def init_auth(app, get_db, base_dir):
    app.secret_key = _load_secret_key(base_dir)
    app.config.update(
        SESSION_COOKIE_NAME="rechnungsapp_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("RECHNUNGSAPP_HTTPS") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )

    # ---- Datenzugriff ----------------------------------------------------
    def get_user():
        conn = get_db()
        try:
            return conn.execute("SELECT * FROM users WHERE id = 1").fetchone()
        finally:
            conn.close()

    def client_ip():
        return request.remote_addr or "unknown"

    def lock_state():
        """True, wenn die Anmeldung wegen zu vieler Fehlversuche vorübergehend gesperrt ist."""
        now = int(time.time())
        since = now - LOCK_WINDOW_SECONDS
        conn = get_db()
        try:
            conn.execute("DELETE FROM login_attempts WHERE ts < ?", (since,))
            conn.commit()
            per_ip = conn.execute("SELECT COUNT(*) c FROM login_attempts WHERE ip = ?",
                                  (client_ip(),)).fetchone()["c"]
            total = conn.execute("SELECT COUNT(*) c FROM login_attempts").fetchone()["c"]
        finally:
            conn.close()
        return per_ip >= MAX_FAILS_PER_IP or total >= MAX_FAILS_GLOBAL

    def record_fail():
        conn = get_db()
        try:
            conn.execute("INSERT INTO login_attempts(ip, ts) VALUES (?, ?)",
                         (client_ip(), int(time.time())))
            conn.commit()
        finally:
            conn.close()

    def clear_fails():
        conn = get_db()
        try:
            conn.execute("DELETE FROM login_attempts WHERE ip = ?", (client_ip(),))
            conn.commit()
        finally:
            conn.close()

    def password_problem(password, confirm, username):
        if password != confirm:
            return "Die Passwörter stimmen nicht überein."
        if len(password) < MIN_PASSWORD_LEN:
            return f"Das Passwort muss mindestens {MIN_PASSWORD_LEN} Zeichen lang sein."
        if len(password) > MAX_PASSWORD_LEN:
            return f"Das Passwort darf höchstens {MAX_PASSWORD_LEN} Zeichen lang sein."
        if password.casefold() == (username or "").casefold():
            return "Das Passwort darf nicht dem Benutzernamen entsprechen."
        if len(set(password)) < 5:
            return "Das Passwort ist zu einfach (zu wenige verschiedene Zeichen)."
        return None

    def start_session(user):
        session.clear()
        session.permanent = True
        session["uid"] = user["id"]
        session["fp"] = _fingerprint(user["password_hash"])
        session["csrf"] = secrets.token_urlsafe(32)

    def safe_next(target):
        if not target:
            return url_for("index")
        parts = urlsplit(target)
        if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//") \
                or "\\" in target:
            return url_for("index")
        return target

    # ---- API-Tokens (Bearer) ---------------------------------------------
    def authenticate_bearer():
        """Prüft 'Authorization: Bearer ra_...'. Liefert die Token-Zeile oder None."""
        header = request.headers.get("Authorization", "")
        scheme, _, raw = header.partition(" ")
        raw = raw.strip()
        if scheme.lower() != "bearer" or not raw.startswith(TOKEN_PREFIX) or len(raw) > 200:
            return None
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM api_tokens WHERE token_hash = ?",
                               (_hash_token(raw),)).fetchone()
            if row is None:
                return None
            if row["expires_at"] and row["expires_at"] <= _utcnow().isoformat(sep=" "):
                return None
            conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                         (_utcnow().isoformat(sep=" "), row["id"]))
            conn.commit()
            return row
        finally:
            conn.close()

    def token_is_active(token_id):
        conn = get_db()
        try:
            row = conn.execute("SELECT expires_at FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
        finally:
            conn.close()
        return bool(row) and not (row["expires_at"] and row["expires_at"] <= _utcnow().isoformat(sep=" "))

    app.extensions["auth"] = SimpleNamespace(authenticate_bearer=authenticate_bearer,
                                             token_is_active=token_is_active)

    def list_tokens():
        conn = get_db()
        try:
            return conn.execute("SELECT * FROM api_tokens ORDER BY id DESC").fetchall()
        finally:
            conn.close()

    def render_account(error=None, token_error=None, new_token=None, status=200):
        return render_template("account.html", error=error, token_error=token_error,
                               new_token=new_token, tokens=list_tokens(), min_len=MIN_PASSWORD_LEN,
                               mcp_url=os.environ.get("RECHNUNGSAPP_PUBLIC_URL", request.url_root).rstrip("/") + "/mcp",
                               now=_utcnow().isoformat(sep=" ")), status

    # ---- Request-Hooks ---------------------------------------------------
    @app.before_request
    def guard():
        g.user = None
        if request.blueprint in API_BLUEPRINTS:
            return None  # eigene Token-Authentifizierung (noch nicht implementiert)

        # CSRF: alle schreibenden Requests brauchen das Token aus der Sitzung
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
            expected = session.get("csrf", "")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, "Ungültiges oder fehlendes Sicherheits-Token. Bitte Seite neu laden.")

        if request.endpoint == "static":
            return None
        user = get_user()
        if user is not None and session.get("uid") == user["id"] \
                and hmac.compare_digest(session.get("fp", ""), _fingerprint(user["password_hash"])):
            g.user = user
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if g.user is None:
            if "uid" in session:
                session.clear()
            if request.method == "GET":
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            return redirect(url_for("login"))
        return None

    @app.after_request
    def secure_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # Inline-Skripte/-Styles werden von den vorhandenen Templates genutzt (onclick, style=...).
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject_auth():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf"], "current_user": g.get("user")}

    # ---- Routen ----------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        user = get_user()
        nxt = request.values.get("next", "")
        if g.user is not None:
            return redirect(safe_next(nxt))
        if user is None:  # noch niemand registriert -> Registrierung anbieten
            return render_template("login.html", mode="register", next=nxt, min_len=MIN_PASSWORD_LEN)
        error = None
        if request.method == "POST":
            if lock_state():
                error = "Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen."
                return render_template("login.html", mode="login", error=error, next=nxt), 429
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")[:MAX_PASSWORD_LEN]
            name_ok = hmac.compare_digest(username.casefold().encode(), user["username"].casefold().encode())
            pw_ok = check_password_hash(user["password_hash"] if name_ok else _DUMMY_HASH, password)
            if name_ok and pw_ok:
                clear_fails()
                start_session(user)
                return redirect(safe_next(nxt))
            record_fail()
            error = "Benutzername oder Passwort ist falsch."
            return render_template("login.html", mode="login", error=error, next=nxt), 401
        return render_template("login.html", mode="login", next=nxt)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "GET":
            return redirect(url_for("login"))
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        error = None
        if not 3 <= len(username) <= 50:
            error = "Der Benutzername muss 3 bis 50 Zeichen lang sein."
        else:
            error = password_problem(password, confirm, username)
        if error:
            return render_template("login.html", mode="register", error=error, username=username,
                                   min_len=MIN_PASSWORD_LEN), 400
        conn = get_db()
        try:
            # users.id = 1 (CHECK + PRIMARY KEY): ein zweiter Eintrag scheitert auch bei Race Conditions
            conn.execute("INSERT INTO users(id, username, password_hash) VALUES (1, ?, ?)",
                         (username, generate_password_hash(password)))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            abort(403, "Es ist bereits ein Benutzer registriert.")
        conn.close()
        start_session(get_user())
        return redirect(url_for("index"))

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/account", methods=["GET", "POST"])
    def account():
        error = None
        if request.method == "POST":
            current = request.form.get("current", "")[:MAX_PASSWORD_LEN]
            new = request.form.get("password", "")
            confirm = request.form.get("confirm", "")
            if lock_state():
                error = "Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen."
            elif not check_password_hash(g.user["password_hash"], current):
                record_fail()
                error = "Das aktuelle Passwort ist falsch."
            else:
                error = password_problem(new, confirm, g.user["username"])
                if not error and check_password_hash(g.user["password_hash"], new):
                    error = "Das neue Passwort muss sich vom alten unterscheiden."
            if not error:
                conn = get_db()
                conn.execute("UPDATE users SET password_hash = ? WHERE id = 1",
                             (generate_password_hash(new),))
                conn.commit()
                conn.close()
                start_session(get_user())  # alle anderen Sitzungen werden damit ungültig
                flash("Passwort geändert.")
                return redirect(url_for("account"))
            return render_account(error=error, status=400)
        return render_account()

    @app.route("/account/tokens", methods=["POST"])
    def token_create():
        name = request.form.get("name", "").strip()
        scope = request.form.get("scope", "full")
        expiry = request.form.get("expiry", "90")
        if not 1 <= len(name) <= 60:
            return render_account(token_error="Bitte einen Namen (1–60 Zeichen) angeben.", status=400)
        if scope not in ("read", "full") or expiry not in TOKEN_EXPIRY_CHOICES:
            abort(400)
        if len(list_tokens()) >= MAX_TOKENS:
            return render_account(token_error=f"Maximal {MAX_TOKENS} Tokens. Bitte erst alte widerrufen.", status=400)
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        days = TOKEN_EXPIRY_CHOICES[expiry]
        expires = (_utcnow() + timedelta(days=days)).isoformat(sep=" ") if days else None
        conn = get_db()
        conn.execute("INSERT INTO api_tokens(name, token_hash, prefix, scope, expires_at, created_at) "
                     "VALUES (?,?,?,?,?,?)",
                     (name, _hash_token(raw), raw[:8], scope, expires, _utcnow().isoformat(sep=" ")))
        conn.commit()
        conn.close()
        # Klartext-Token wird nur in dieser Antwort angezeigt (nicht gespeichert, nicht im Cookie)
        return render_account(new_token=raw)

    @app.route("/account/tokens/<int:tid>/revoke", methods=["POST"])
    def token_revoke(tid):
        conn = get_db()
        conn.execute("DELETE FROM api_tokens WHERE id = ?", (tid,))
        conn.commit()
        conn.close()
        flash("Token widerrufen.")
        return redirect(url_for("account"))


def init_auth_db(conn):
    conn.executescript(SCHEMA)


def reset_user(get_db):
    """CLI-Notausgang (Passwort vergessen): löscht den Benutzer, die Registrierung ist dann wieder offen."""
    conn = get_db()
    conn.execute("DELETE FROM users")
    conn.execute("DELETE FROM login_attempts")
    conn.execute("DELETE FROM api_tokens")
    conn.commit()
    conn.close()
