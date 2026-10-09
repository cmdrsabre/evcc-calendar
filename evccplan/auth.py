"""Einfache Anmeldung: ein fester Benutzer "admin", Passwort wird beim ersten Aufruf vergeben.

Gespeichert wird nur ein PBKDF2-Hash. Sitzungs-Tokens liegen nur als SHA-256-Hash in der Datenbank.
Passwort vergessen: state.db loeschen (setzt auch alle Vorgaben aus der Oberflaeche zurueck).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Optional

USER = "admin"
MIN_LENGTH = 8
SESSION_DAYS = 30
ITERATIONS = 600_000                 # OWASP-Empfehlung fuer PBKDF2-HMAC-SHA256; Tests duerfen niedriger setzen
LOCK_AFTER = 5                       # Fehlversuche in Folge
MAX_LOCK_S = 15 * 60


def _hash(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Auth:
    def __init__(self, store, now=time.time):
        self.store, self.now = store, now
        self._lock = threading.Lock()
        self._fails: dict = {}        # Client -> (Anzahl, gesperrt bis)

    # ------------------------------------------------------------ Zustand
    def setup_needed(self) -> bool:
        return self.store.get("auth") is None

    def password_problem(self, password: str) -> Optional[str]:
        if not isinstance(password, str) or len(password) < MIN_LENGTH:
            return "Das Passwort braucht mindestens %d Zeichen." % MIN_LENGTH
        if len(password) > 200:
            return "Das Passwort ist zu lang."
        if password.lower() in (USER, "password", "passwort", "12345678"):
            return "Dieses Passwort ist zu einfach."
        return None

    # ------------------------------------------------------------ Einrichten und Anmelden
    def setup(self, password: str) -> Optional[str]:
        """-> Session-Token oder None, wenn schon eingerichtet. Die Pruefung ist atomar."""
        with self._lock:
            if self.store.get("auth") is not None:
                return None
            salt = secrets.token_bytes(16)
            self.store.put("auth", {"salt": salt.hex(), "iter": ITERATIONS, "hash": _hash(password, salt, ITERATIONS)})
        return self.new_session()

    def locked_for(self, client: str) -> int:
        with self._lock:
            n, until = self._fails.get(client, (0, 0.0))
        return max(0, int(until - self.now() + 0.999)) if until > self.now() else 0

    def login(self, password: str, client: str = "") -> Optional[str]:
        if self.locked_for(client):
            return None
        rec = self.store.get("auth")
        ok = False
        if rec and isinstance(password, str) and len(password) <= 200:
            calc = _hash(password, bytes.fromhex(rec["salt"]), int(rec["iter"]))
            ok = hmac.compare_digest(calc, rec["hash"])
        with self._lock:
            if ok:
                self._fails.pop(client, None)
            else:
                n = self._fails.get(client, (0, 0.0))[0] + 1
                lock = 0.0
                if n >= LOCK_AFTER:
                    lock = min(MAX_LOCK_S, 15 * 2 ** (n - LOCK_AFTER))
                self._fails[client] = (n, self.now() + lock if lock else 0.0)
        return self.new_session() if ok else None

    # ------------------------------------------------------------ Sitzungen
    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.store.sessions_prune()
        self.store.session_add(_token_hash(token), self.now() + SESSION_DAYS * 86400)
        return token

    def valid(self, token: Optional[str]) -> bool:
        return bool(token) and self.store.session_valid(_token_hash(token))

    def logout(self, token: Optional[str]) -> None:
        if token:
            self.store.session_delete(_token_hash(token))
