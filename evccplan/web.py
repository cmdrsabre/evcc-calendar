"""Web UI: JSON API plus static single-page app (evccplan/static), standard library only."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, i18n
from .auth import MIN_LENGTH, SESSION_DAYS, Auth

log = logging.getLogger("evccplan")
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "evccplan_session"
MAX_BODY = 4096
TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".json": "application/json",
}
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
}
TARGETS = list(range(5, 101, 5))


class RunRequester:
    """Coalesces several changes in quick succession into as few runs as possible."""

    def __init__(self, runner):
        self.runner = runner
        self._lock = threading.Lock()
        self._pending = False
        self._live = False                 # at least one request demands a regular run
        self._worker = None

    @property
    def pending(self) -> bool:
        return self._pending or bool(self._worker and self._worker.is_alive())

    def request(self, force_dry: bool = False) -> None:
        with self._lock:
            self._pending = True
            self._live = self._live or not force_dry
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._loop, daemon=True)
            self._worker.start()

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    return
                self._pending, dry, self._live = False, not self._live, False
            try:
                self.runner.run(force_dry=dry)
            except Exception:                                  # the runner catches errors itself; this is only a safeguard
                log.exception("Run from the web UI failed")


def _bad_target(value) -> bool:
    return value is not None and (isinstance(value, bool) or not isinstance(value, int) or value not in TARGETS)


def make_server(runner, cfg) -> ThreadingHTTPServer:
    auth = Auth(runner.store)
    runs = RunRequester(runner)

    class H(BaseHTTPRequestHandler):
        server_version = "evccplan"
        sys_version = ""

        # ------------------------------------------------------------ Helpers
        def _send(self, code, ctype, data: bytes, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in HEADERS.items():
                self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj, extra=None):
            self._send(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode(),
                       {"Cache-Control": "no-store", **(extra or {})})

        def _token(self):
            raw = self.headers.get("Cookie") or ""
            try:
                c = SimpleCookie(raw)
            except Exception:
                return None
            return c[COOKIE].value if COOKIE in c else None

        def _authed(self) -> bool:
            return auth.valid(self._token())

        def _cookie(self, token, clear=False) -> dict:
            parts = ["%s=%s" % (COOKIE, "" if clear else token), "HttpOnly", "SameSite=Strict", "Path=/",
                     "Max-Age=%d" % (0 if clear else SESSION_DAYS * 86400)]
            if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
                parts.append("Secure")
            return {"Set-Cookie": "; ".join(parts)}

        def T(self, key: str, **kw) -> str:
            return i18n.t(cfg.language, key, **kw)

        def _client(self) -> str:
            return self.client_address[0]

        def _body(self):
            """Read the JSON body; None on error (the response has already been sent)."""
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                self._json(415, {"error": self.T("api.json_expected")})
                return None
            if self.headers.get("X-CSRF") != "1":
                self._json(403, {"error": self.T("api.request_rejected")})
                return None
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != (self.headers.get("Host") or ""):
                self._json(403, {"error": self.T("api.request_rejected")})
                return None
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 < n <= MAX_BODY:
                self._json(400, {"error": self.T("api.invalid_request")})
                return None
            try:
                data = json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                self._json(400, {"error": self.T("api.invalid_json")})
                return None
            if not isinstance(data, dict):
                self._json(400, {"error": self.T("api.invalid_request")})
                return None
            return data

        # ------------------------------------------------------------ GET
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                return self._send(200, "text/plain", b"ok")
            if path == "/api/me":
                return self._json(200, {"setup_needed": auth.setup_needed(), "authed": self._authed(),
                                        "min_password": MIN_LENGTH, "language": cfg.language,
                                        "languages": i18n.available()})
            if path == "/api/status":
                if not self._authed():
                    return self._json(401, {"error": self.T("api.login_required")})
                res = dict(runner.last() or {})
                res.update({"busy": runner.busy() or runs.pending, "config_dry_run": cfg.dry_run,
                            "timezone": cfg.timezone, "reserve_soc": cfg.reserve_soc, "targets": TARGETS,
                            "manual_drive_min": cfg.manual_drive_min, "language": cfg.language, "version": __version__, "now": datetime.now(timezone.utc).isoformat()})
                return self._json(200, res)
            if path == "/api/settings":
                if not self._authed():
                    return self._json(401, {"error": self.T("api.login_required")})
                return self._json(200, self._settings_dict())
            if path == "/api/history":
                if not self._authed():
                    return self._json(401, {"error": self.T("api.login_required")})
                return self._json(200, {"runs": runner.store.runs(100)})
            if path == "/api/personal/car":
                if not self._authed():
                    return self._json(401, {"error": self.T("api.login_required")})
                return self._personal_car()
            if path.startswith("/api/"):
                return self._json(404, {"error": self.T("api.not_found")})
            self._static(path)

        def _personal_car(self):
            """Custom vehicle image from the data folder. Visible only when logged in; fixed file names, no path taken from the request."""
            for name in ("car.webp", "car.png", "car.jpg", "car.jpeg"):
                f = Path(cfg.personal_path) / name
                try:
                    if f.is_file() and f.stat().st_size <= 8 * 1024 * 1024:
                        return self._send(200, TYPES[f.suffix], f.read_bytes(), {"Cache-Control": "private, max-age=300"})
                except OSError:
                    pass
            self._json(404, {"error": self.T("api.no_custom_image")})

        def _static(self, path: str):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            try:
                f = (STATIC / rel).resolve()
                f.relative_to(STATIC)
            except (ValueError, OSError):
                return self._send(404, "text/plain", b"not found")
            if not f.is_file() or f.suffix not in TYPES:
                return self._send(404, "text/plain", b"not found")
            cache = "public, max-age=86400" if f.suffix in (".webp", ".png", ".jpg", ".jpeg", ".woff2", ".ico") else "no-cache"
            self._send(200, TYPES[f.suffix], f.read_bytes(), {"Cache-Control": cache})

        # ------------------------------------------------------------ POST
        def do_POST(self):
            path = urlsplit(self.path).path
            data = self._body()
            if data is None:
                return
            if path == "/api/setup":
                if not auth.setup_needed():
                    return self._json(409, {"error": self.T("api.password_already_set")})
                pw = data.get("password")
                problem = auth.password_problem(pw, cfg.language)
                if problem:
                    return self._json(400, {"error": problem})
                token = auth.setup(pw)
                if token is None:
                    return self._json(409, {"error": self.T("api.password_already_set")})
                log.info("Web UI password has been set")
                return self._json(200, {"ok": True}, self._cookie(token))
            if path == "/api/login":
                wait = auth.locked_for(self._client())
                if wait:
                    return self._json(429, {"error": self.T("api.too_many_attempts", wait=wait)})
                token = auth.login(data.get("password"), self._client())
                if token is None:
                    log.warning("Failed login from %s", self._client())
                    return self._json(401, {"error": self.T("api.wrong_password")})
                return self._json(200, {"ok": True}, self._cookie(token))
            if path == "/api/logout":
                auth.logout(self._token())
                return self._json(200, {"ok": True}, self._cookie("", clear=True))
            if not self._authed():
                return self._json(401, {"error": self.T("api.login_required")})
            if path == "/api/run":
                runs.request(force_dry=True)
                return self._json(202, {"ok": True})
            if path == "/api/override":
                return self._override(data)
            if path == "/api/settings":
                return self._settings(data)
            self._json(404, {"error": self.T("api.not_found")})

        def _settings_dict(self) -> dict:
            st = runner.settings
            return {"values": st.values(), "defaults": st.base, "changed": st.changed(), "limits": st.limits(),
                    "languages": i18n.available()}

        def _settings(self, data: dict):
            try:
                if data.get("reset") is True:
                    runner.settings.reset()
                else:
                    runner.settings.update(data.get("values"))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            runs.request(force_dry=False)        # new values take effect in the configured mode
            self._json(200, self._settings_dict())

        def _override(self, data: dict):
            key, mode, target = data.get("key"), data.get("mode"), data.get("target")
            if not isinstance(key, str) or mode not in (None, "car", "none") or _bad_target(target):
                return self._json(400, {"error": self.T("api.invalid_values")})
            item = next((i for i in (runner.last() or {}).get("items") or [] if i.get("key") == key), None)
            if item is None:
                return self._json(404, {"error": self.T("api.event_gone")})
            if target is not None and mode == "none":
                mode = None                      # a target means: charge here
            runner.store.set_override(key, mode, target, item.get("title") or "", item.get("start") or "")
            runs.request(force_dry=False)        # acts in the configured mode (dry run or live)
            self._json(202, {"ok": True})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((cfg.web_host, cfg.web_port), H)
    srv.auth, srv.runs = auth, runs
    return srv
