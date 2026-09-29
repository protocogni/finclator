"""Vercel Python function: the admin panel behind the session cookie set by api/auth.js.

Same HMAC scheme as auth.js (SESSION_SECRET over "session|email|exp", base64url payload "." signature). The DB is
Postgres (Neon) via DATABASE_URL — the same one the daily pipeline writes to. Local-only widgets (process probes,
live log, vendor balance, rebuild links) are neutralised in-process.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
SECRET = os.environ.get("SESSION_SECRET", "")
OWNER = os.environ.get("OWNER_EMAIL", "").lower()


# ── DB ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _connect():
    import sys
    sys.path.insert(0, str(ROOT))
    from src.db import connect
    return connect()


# ── session ──────────────────────────────────────────────────────────────────────────────────────────────────────
def _session_email(cookie_header: str) -> str | None:
    tok = None
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "fc_session":
            tok = v
    if not tok or "." not in tok or not SECRET:
        return None
    p, _, sig = tok.partition(".")
    try:
        payload = base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)).decode()
    except Exception:  # noqa: BLE001
        return None
    want = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    if not hmac.compare_digest(want, sig):
        return None
    purpose, email, exp = payload.split("|")
    if purpose != "session" or float(exp) < time.time() * 1000:
        return None
    return email


def _approved(email: str) -> bool:
    """Re-check approval on every request so a revoked user is out immediately (users.json in the private Blob)."""
    if email == OWNER:
        return True
    try:
        import urllib.request
        # private blobs are not fetchable by URL; ask our own auth function instead (same deployment, cookie forwarded)
        req = urllib.request.Request(f"{os.environ.get('SITE_URL', 'https://finclator.com')}/auth/me")
        req.add_header("Cookie", _current_cookie)
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200 and json.load(r).get("email") == email
    except Exception:  # noqa: BLE001
        return False


_current_cookie = ""


# ── panel rendering: reuse src.admin; chrome via admin.CHROME (ContextVar), local-only helpers stubbed ───────────
def _render(path: str, qs: str, email: str) -> tuple[int, str, str]:
    if "FINCLATOR_ACTIVE_MODEL" not in os.environ and os.environ.get("PANEL_MODEL"):
        os.environ["FINCLATOR_ACTIVE_MODEL"] = os.environ["PANEL_MODEL"]  # else models.DEFAULT_MODEL applies
    import sys
    sys.path.insert(0, str(ROOT))
    from src import admin, audit  # noqa: PLC0415

    # plain replacements (not wrappers) — safe to apply on every request
    admin._proc_running = lambda pattern: False
    admin._credits = lambda: None
    admin._log_tail = lambda n=200: "(live pipeline log is only available on the operator's machine)"
    admin.connect = _connect
    audit.connect = _connect
    audit.OUT = Path("/tmp/audit.html")
    who = (f"<span style='margin-left:18px;color:#9aa'>{admin.html.escape(email)} · "
           f"<a href='/auth/logout' style='color:#9ecbff'>sign out</a> · <a href='/' style='color:#9ecbff'>site</a></span>")
    # Never wrap admin._page: a wrapper left behind by a failed request (Neon cold start) nested one more sign-out
    # block per request in the warm function. CHROME is set here and always reset in finally.
    token = admin.CHROME.set({"prefix": "/panel", "who": who, "readonly": True, "refresh": False})
    conn = None
    try:
        conn = _connect()
        if path in ("", "/", "progress"):
            return 200, "text/html; charset=utf-8", admin.page_progress(conn)
        if path == "matrix":
            return 200, "text/html; charset=utf-8", admin.page_matrix(conn)
        if path == "accounts":
            return 200, "text/html; charset=utf-8", admin.page_accounts(conn)
        if path == "audit":
            q = parse_qs(qs)
            acc = (q.get("account") or [None])[0]
            return 200, "text/html; charset=utf-8", admin._page("audit", audit.body(conn, limit=600, account=acc), "/audit")
        if path == "architecture":
            return 200, "text/html; charset=utf-8", admin.page_architecture(conn)
        if path == "tables":
            return 200, "text/html; charset=utf-8", admin.page_tables(conn, qs)
        if path == "api/status":
            return 200, "application/json", json.dumps(admin.status(conn), indent=1)
        if path == "api/log":
            return 200, "text/plain; charset=utf-8", admin._log_tail()
        if path == "api/matrix":
            p = ROOT / "data" / "matrix.json"
            return 200, "application/json", p.read_text() if p.exists() else "{}"
        if path == "api/clicks":
            return 200, "application/json", json.dumps(admin.click_stats(conn, days=int(parse_qs(qs).get("days", ["30"])[0] or 30)), indent=1)
        return 404, "text/plain; charset=utf-8", "not found"
    finally:
        admin.CHROME.reset(token)
        if conn is not None:
            conn.close()


class handler(BaseHTTPRequestHandler):  # noqa: N801 — Vercel's Python runtime looks for `handler`
    def log_message(self, format, *args):  # noqa: A002
        pass

    def _report(self, q):
        """/panel/report — daily ops email, fired by the Vercel cron (Authorization: Bearer CRON_SECRET).
        `?dry=1` renders the HTML without sending or persisting state (same auth)."""
        secret = os.environ.get("CRON_SECRET", "")
        auth = self.headers.get("Authorization", "")
        if not secret or not hmac.compare_digest(auth, f"Bearer {secret}"):
            return self._send(401, "text/plain; charset=utf-8", "unauthorized")
        import sys
        sys.path.insert(0, str(ROOT))
        from src import report  # noqa: PLC0415
        dry = (q.get("dry") or ["0"])[0] == "1"
        try:
            out = report.run(dry=dry)
        except Exception:  # noqa: BLE001
            import traceback
            return self._send(500, "text/plain; charset=utf-8", "report error\n\n" + traceback.format_exc())
        if dry:
            return self._send(200, "text/html; charset=utf-8", out["html"])
        return self._send(200, "application/json", json.dumps(out, indent=1))

    def _send(self, code: int, ctype: str, body: str, extra: dict | None = None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "private, no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        """/panel/api/click — influencer-link click beacon. Signed-in users are attributed; the public site posts the
        same body with no cookie and is stored anonymously (handle must be on the roster, so it cannot be spammed
        with junk; a tiny table either way)."""
        global _current_cookie
        u = urlparse(self.path)
        sub = (parse_qs(u.query).get("p") or [""])[0].strip("/")
        if sub != "api/click":
            return self._send(404, "text/plain; charset=utf-8", "not found")
        _current_cookie = self.headers.get("Cookie", "")
        email = _session_email(_current_cookie)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            body = {}
        import sys
        sys.path.insert(0, str(ROOT))
        from src import admin  # noqa: PLC0415
        conn = _connect()
        try:
            ok = admin.record_click_body(conn, body, email)
        finally:
            conn.close()
        self._send(200 if ok else 400, "application/json", json.dumps({"ok": ok}))

    def do_GET(self):
        global _current_cookie
        u = urlparse(self.path)
        q = parse_qs(u.query)
        sub = (q.get("p") or [""])[0].strip("/")
        # pass the remaining query (minus p) to the page, e.g. tables?t=calls&o=200
        qs = "&".join(f"{k}={v}" for k, vs in q.items() if k != "p" for v in vs)
        if sub == "report":
            return self._report(q)
        _current_cookie = self.headers.get("Cookie", "")
        email = _session_email(_current_cookie)
        if not email:
            return self._send(302, "text/plain", "", {"Location": f"/login?next=/panel/{sub}"})
        if not _approved(email):
            return self._send(302, "text/plain", "", {"Location": "/login?e=denied"})
        try:
            code, ctype, body = _render(sub, qs, email)
        except Exception:  # noqa: BLE001
            import traceback
            code, ctype, body = 500, "text/plain; charset=utf-8", "panel error\n\n" + traceback.format_exc()
        self._send(code, ctype, body)
