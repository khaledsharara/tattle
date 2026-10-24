"""Admin sessions for editing (renaming, merging, settings). Viewing needs no login.

The admin password is chosen in the browser on first run, after typing a
one-time setup code that Tattle prints in its log and writes next to its
database (the installer shows it), so whoever opens the page first on the
network can't claim it. It is stored as an scrypt hash. TATTLE_ADMIN_PASSWORD,
when set, overrides it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

COOKIE_NAME = "tattle_admin"
SESSION_SECONDS = 30 * 86400
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300
MIN_PASSWORD_LENGTH = 8
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        **_SCRYPT, salt=base64.b64encode(salt).decode(), digest=base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), dklen=len(expected), n=int(n), r=int(r), p=int(p)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


class AdminAuth:
    def __init__(self, password: str, secret: bytes, stored_hash: str | None = None):
        self._password = password  # from the environment; wins over the stored hash
        self._hash = stored_hash
        self._secret = secret
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self.setup_code: str | None = None if self.enabled else f"{secrets.randbelow(10**6):06d}"

    @property
    def enabled(self) -> bool:
        return bool(self._password or self._hash)

    @property
    def from_environment(self) -> bool:
        return bool(self._password)

    def locked_out(self, client: str) -> bool:
        with self._lock:
            count, until = self._failures.get(client, (0, 0.0))
            return count >= MAX_FAILURES and time.time() < until

    def _record(self, client: str, ok: bool) -> None:
        with self._lock:
            if ok:
                self._failures.pop(client, None)
            else:
                count, _ = self._failures.get(client, (0, 0.0))
                self._failures[client] = (count + 1, time.time() + LOCKOUT_SECONDS)

    def check_password(self, client: str, password: str) -> bool:
        if self._password:
            ok = hmac.compare_digest(password.encode(), self._password.encode())
        else:
            ok = bool(self._hash) and verify_password(password, self._hash or "")
        self._record(client, ok)
        return ok

    def check_setup_code(self, client: str, code: str) -> bool:
        ok = self.setup_code is not None and hmac.compare_digest(code.strip().encode(), self.setup_code.encode())
        self._record(client, ok)
        return ok

    def set_password(self, password: str) -> str:
        """Hash and use a new password; returns the hash to store. Everyone else is logged out."""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Use at least {MIN_PASSWORD_LENGTH} characters")
        self._hash = hash_password(password)
        self.setup_code = None
        return self._hash

    def issue(self) -> str:
        expires = int(time.time()) + SESSION_SECONDS
        nonce = secrets.token_hex(8)
        return f"{expires}.{nonce}.{self._sign(f'{expires}.{nonce}')}"

    def valid(self, token: str | None) -> bool:
        if not token or not self.enabled:
            return False
        try:
            expires, nonce, signature = token.split(".")
        except ValueError:
            return False
        if not hmac.compare_digest(signature, self._sign(f"{expires}.{nonce}")):
            return False
        return int(expires) > time.time()

    def _sign(self, payload: str) -> str:
        # The password is part of the key, so changing it logs everybody out.
        key = self._secret + hashlib.sha256((self._password or self._hash or "").encode()).digest()
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
