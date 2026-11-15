"""Key login for the local UI: each user's own ed25519 key, a one-time challenge, a session.

Flow:
1. ``omli-ui init`` makes the user's keypair, protected by a passphrase the user
   types twice. The private key goes in the ``.omli`` folder of the user's home folder (or
   wherever ``OMLI_UI_KEY`` points) and never under OMLI_HOME. The public key is appended to
   ``OMLI_HOME/ui/keys.jsonl``.
2. The login page asks the server for a challenge: random, single use, short-lived.
3. The user signs it on the command line, ``omli-ui sign <challenge>``. The private
   key is read there and nowhere else; the browser only ever holds the signed token.
4. The page sends the challenge and the token. The server burns the challenge whatever the
   outcome, checks the signature against a registered, unrevoked public key, and on success
   sets a random session token in an HttpOnly, SameSite=Strict cookie.

There are no account passwords and no default account: with no registered key nobody can
log in. The passphrase only unlocks the private key file on the user's own machine.
``OMLI_UI_KEY_PASSPHRASE`` supplies it without a prompt; it is meant for scripts only,
because the passphrase then stays in the process environment, where other programs of the
same user can read it.

Open challenges and live sessions are held in memory, capped, and swept of expired entries
on every use. At a cap, new ones are refused; live ones are never evicted.

The signed bytes are domain-separated (``omli-ui-login-v1``) so a login signature can never
pass as a seal signature or the reverse. ``cryptography`` is imported only when a key is
made, used or checked, so importing this module costs nothing on a plain install.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from omli.ui.home import ui_home

KEY_PATH_ENV = "OMLI_UI_KEY"
PASSPHRASE_ENV = "OMLI_UI_KEY_PASSPHRASE"
KEY_DIR = ".omli"
KEY_FILE = "ui_login.key"
REGISTRY = "ui/keys.jsonl"
REPLACE_COMMAND = "omli-ui init --replace"

LOGIN_CONTEXT = b"omli-ui-login-v1"
CHALLENGE_TTL_SECONDS = 120
SESSION_TTL_SECONDS = 8 * 3600
# Caps on what an unauthenticated caller (challenges) or repeated logins (sessions) can make
# the server hold. At a cap a new one is refused; a live one is never evicted.
MAX_OPEN_CHALLENGES = 32
MAX_SESSIONS = 32
SESSION_COOKIE = "omli_session"
# A login ticket carries a proven sign-in from the command line to the browser: one use,
# a short life, and held by the server only as a hash.
TICKET_TTL_SECONDS = 30
MAX_OPEN_TICKETS = 8


class LoginKeyError(Exception):
    """The private login key cannot be used: missing, unencrypted, or a wrong passphrase.
    The message is one line, safe to show the user, and never contains key material."""


class ChallengeLimitError(Exception):
    """Too many sign-in codes are open."""


class SessionLimitError(Exception):
    """Too many sessions are signed in."""


class TicketLimitError(Exception):
    """Too many login tickets are open."""


def _ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as exc:
        raise RuntimeError("the UI login needs the 'cryptography' package "
                           "(pip install 'omli[ui]')") from exc
    return serialization, Ed25519PrivateKey, Ed25519PublicKey


def default_key_path() -> Path:
    return Path.home() / KEY_DIR / KEY_FILE


def key_path(path: str | Path | None = None) -> Path:
    """The private key: explicit argument, else ``OMLI_UI_KEY``, else the per-user default."""
    if path is not None:
        return Path(path).expanduser().resolve()
    override = os.environ.get("OMLI_UI_KEY")
    return (Path(override).expanduser() if override else default_key_path()).resolve()


def registry_path(home: str | Path | None = None) -> Path:
    return ui_home(home) / REGISTRY


def _fingerprint(public_raw: bytes) -> str:
    # Same id scheme as omli.integrity.signer, so a key has one id everywhere.
    return hashlib.sha256(public_raw).hexdigest()[:16]


def _passphrase(passphrase: Optional[str]) -> Optional[str]:
    return passphrase if passphrase is not None else os.environ.get("OMLI_UI_KEY_PASSPHRASE")


def check_new_passphrase(passphrase: Optional[str]) -> str:
    """A usable passphrase for a new key, or ValueError. Empty or blank is refused."""
    if passphrase is None or not passphrase.strip():
        raise ValueError("a passphrase is required to protect the login key "
                         "(empty or blank is not allowed)")
    return passphrase


def create_login_key(priv_path: str | Path | None = None,
                     home: str | Path | None = None,
                     passphrase: Optional[str] = None,
                     *, replace: bool = False) -> Tuple[Path, str]:
    """First run: make this user's passphrase-protected keypair and register the public half.
    Returns (private key path, pubkey id).

    Refuses a private key path under OMLI_HOME and, unless ``replace``, an existing private
    key. ``replace`` is the deliberate re-creation: it revokes every key registered so far
    (appending revocation records) and then registers the new one, so this key becomes the
    only one that can sign in.
    """
    serialization, Ed25519PrivateKey, _ = _crypto()
    pw = check_new_passphrase(_passphrase(passphrase))
    priv = key_path(priv_path)
    registry = registry_path(home)
    root = registry.parent.parent
    if priv == root or root in priv.parents:
        raise ValueError("the private login key must live outside OMLI_HOME; "
                         f"set {KEY_PATH_ENV} to a path under your home folder")
    if priv.exists() and not replace:
        raise FileExistsError(f"a login key already exists at {priv}; to make a new one "
                              f"deliberately, run: {REPLACE_COMMAND}")

    key = Ed25519PrivateKey.generate()
    priv_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.BestAvailableEncryption(pw.encode("utf-8")))
    pub = key.public_key()
    pub_raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub_pem = pub.public_bytes(serialization.Encoding.PEM,
                               serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    pubkey_id = _fingerprint(pub_raw)

    priv.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    # A new key is created exclusively, so it can never overwrite one. A replacement is
    # written beside the old key and swapped in, after the old registrations are revoked.
    target = priv.with_name(priv.name + f".new-{secrets.token_hex(4)}") if replace else priv
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(priv_pem)
        fh.flush()
        os.fsync(fh.fileno())
    if replace:
        for old in registered_keys(home):
            _append(registry, {"type": "revoke", "pubkey_id": old, "revoked_at": now,
                               "reason": "replaced by init --replace"})
        os.replace(target, priv)
    _append(registry, {"type": "key", "pubkey_id": pubkey_id, "pubkey_pem": pub_pem,
                       "created_at": now})
    return priv, pubkey_id


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def registered_keys(home: str | Path | None = None) -> Dict[str, str]:
    """pubkey id -> public key PEM for every registered key that is not revoked, from the
    append-only registry. A line that cannot be read fails closed (raises)."""
    path = registry_path(home)
    if not path.exists():
        return {}
    keys: Dict[str, str] = {}
    revoked = set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            raise ValueError(f"{REGISTRY} line {n} is not JSON") from None
        kind = rec.get("type") if isinstance(rec, dict) else None
        if kind == "key":
            keys[str(rec["pubkey_id"])] = str(rec["pubkey_pem"])
        elif kind == "revoke":
            revoked.add(str(rec["pubkey_id"]))
        else:
            raise ValueError(f"{REGISTRY} line {n} is not a key or revocation record")
    return {k: v for k, v in keys.items() if k not in revoked}


def _message(challenge: str) -> bytes:
    return LOGIN_CONTEXT + b"\n" + challenge.encode("utf-8")


def read_private_key_file(priv_path: str | Path | None = None) -> Tuple[Path, bytes]:
    """The key file's bytes, refusing a missing or unencrypted key. Needs no passphrase, so
    a caller can refuse an unusable key before asking for one."""
    serialization, _, _ = _crypto()
    priv = key_path(priv_path)
    try:
        data = priv.read_bytes()
    except FileNotFoundError:
        raise LoginKeyError("no login key here; run: omli-ui init") from None
    except OSError as exc:
        raise LoginKeyError(f"the login key cannot be read ({type(exc).__name__})") from None
    try:
        serialization.load_pem_private_key(data, password=None)
    except TypeError:
        return priv, data  # encrypted, as required
    except ValueError:
        raise LoginKeyError("the login key file cannot be parsed") from None
    raise LoginKeyError("this login key has no passphrase and will not be used; "
                        f"make a protected one with: {REPLACE_COMMAND}")


def unlock_private_key(priv_path: str | Path | None = None, passphrase: Optional[str] = None):
    """The private key, unlocked with the passphrase. Command line only."""
    serialization, _, _ = _crypto()
    _, data = read_private_key_file(priv_path)
    pw = _passphrase(passphrase)
    if not pw:
        raise LoginKeyError("a passphrase is needed to unlock the login key")
    try:
        return serialization.load_pem_private_key(data, password=pw.encode("utf-8"))
    except (ValueError, TypeError):
        raise LoginKeyError("wrong passphrase for the login key") from None


def sign_challenge(challenge: str, priv_path: str | Path | None = None,
                   passphrase: Optional[str] = None) -> str:
    """Sign a challenge with this user's private key; returns the token the browser sends.
    Runs on the command line only — the server never reads the private key."""
    serialization, _, _ = _crypto()
    key = unlock_private_key(priv_path, passphrase)
    pub_raw = key.public_key().public_bytes(serialization.Encoding.Raw,
                                            serialization.PublicFormat.Raw)
    body = {"challenge": challenge,
            "sig": base64.b64encode(key.sign(_message(challenge))).decode("ascii"),
            "pubkey_id": _fingerprint(pub_raw)}
    return base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()


def _decode_token(token: str) -> Optional[dict]:
    try:
        data = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def verify_token(pubkey_pem: str, challenge: str, token: str) -> bool:
    """True only if ``token`` is this key's signature over exactly ``challenge``."""
    data = _decode_token(token)
    if data is None or data.get("challenge") != challenge:
        return False
    try:
        serialization, _, _ = _crypto()
        from cryptography.exceptions import InvalidSignature

        pub = serialization.load_pem_public_key(pubkey_pem.encode("ascii"))
        pub.verify(base64.b64decode(data["sig"], validate=True), _message(challenge))
    except (InvalidSignature, KeyError, TypeError, ValueError, binascii.Error):
        return False
    return True


@dataclass(frozen=True)
class Session:
    pubkey_id: str
    expires_at: float


class AuthState:
    """Open challenges and live sessions for one server process. They are deliberately not
    persisted: a restart logs everyone out, and nothing here is workflow state."""

    def __init__(self, home: str | Path | None = None,
                 clock: Optional[Callable[[], float]] = None,
                 max_challenges: Optional[int] = None,
                 max_sessions: Optional[int] = None):
        self._home = home
        self._clock = clock or time.time
        # The module constants are read here, not at import, so a deployment can set them.
        self._max_challenges = MAX_OPEN_CHALLENGES if max_challenges is None else max_challenges
        self._max_sessions = MAX_SESSIONS if max_sessions is None else max_sessions
        self._max_tickets = MAX_OPEN_TICKETS
        self._challenges: Dict[str, float] = {}
        self._sessions: Dict[str, Session] = {}
        # sha256(ticket) -> (expires at, pubkey id). The ticket itself is never kept.
        self._tickets: Dict[str, Tuple[float, str]] = {}
        self._lock = threading.Lock()

    def _sweep(self, now: float) -> None:
        # Caller holds the lock.
        self._challenges = {c: t for c, t in self._challenges.items() if t > now}
        self._sessions = {k: s for k, s in self._sessions.items() if s.expires_at > now}
        self._tickets = {h: v for h, v in self._tickets.items() if v[0] > now}

    def open_tickets(self) -> int:
        with self._lock:
            self._sweep(self._clock())
            return len(self._tickets)

    def open_challenges(self) -> int:
        with self._lock:
            self._sweep(self._clock())
            return len(self._challenges)

    def live_sessions(self) -> int:
        with self._lock:
            self._sweep(self._clock())
            return len(self._sessions)

    def has_keys(self) -> bool:
        return bool(registered_keys(self._home))

    def issue_challenge(self) -> Optional[str]:
        """A fresh challenge; None when no key is registered (nobody could answer it).
        Raises ChallengeLimitError at the cap rather than evicting a live challenge."""
        if not self.has_keys():
            return None
        with self._lock:
            now = self._clock()
            self._sweep(now)
            if len(self._challenges) >= self._max_challenges:
                raise ChallengeLimitError("too many sign-in codes are open; "
                                          "wait two minutes and try again")
            challenge = secrets.token_urlsafe(32)
            self._challenges[challenge] = now + CHALLENGE_TTL_SECONDS
        return challenge

    def _check_answer(self, challenge: object, token: object) -> Optional[str]:
        """Burn the challenge, then check the token. The pubkey id of a registered key whose
        signature over exactly this challenge is valid, else None."""
        if not isinstance(challenge, str) or not isinstance(token, str):
            return None
        with self._lock:
            self._sweep(self._clock())
            expires = self._challenges.pop(challenge, None)
        if expires is None:
            return None
        data = _decode_token(token)
        if data is None:
            return None
        pem = registered_keys(self._home).get(str(data.get("pubkey_id")))
        if pem is None or not verify_token(pem, challenge, token):
            return None
        return str(data["pubkey_id"])

    def _new_session(self, pubkey_id: str) -> Tuple[str, Session]:
        session_token = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._sweep(now)
            if len(self._sessions) >= self._max_sessions:
                raise SessionLimitError("too many sessions are signed in; "
                                        "sign out elsewhere or wait for one to expire")
            session = Session(pubkey_id=pubkey_id, expires_at=now + SESSION_TTL_SECONDS)
            self._sessions[session_token] = session
        return session_token, session

    def login(self, challenge: object, token: object) -> Optional[Tuple[str, Session]]:
        """Burn the challenge, then check the token. Returns (session token, session) or
        None. Raises SessionLimitError when a valid login would exceed the session cap."""
        pubkey_id = self._check_answer(challenge, token)
        return None if pubkey_id is None else self._new_session(pubkey_id)

    def issue_ticket(self, challenge: object, token: object) -> Optional[str]:
        """A one-time login ticket for a proven key, or None. The command line earns it the
        same way a login does (a valid signature over a fresh challenge); the browser trades
        it for a session once, within TICKET_TTL_SECONDS. Only its hash is kept.
        Raises TicketLimitError at the cap rather than evicting an open ticket."""
        pubkey_id = self._check_answer(challenge, token)
        if pubkey_id is None:
            return None
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._sweep(now)
            if len(self._tickets) >= self._max_tickets:
                raise TicketLimitError("too many sign-in links are open; "
                                       "wait a minute and try again")
            self._tickets[_ticket_hash(ticket)] = (now + TICKET_TTL_SECONDS, pubkey_id)
        return ticket

    def redeem_ticket(self, ticket: object) -> Optional[Tuple[str, Session]]:
        """Trade a ticket for a session. The ticket is removed whatever the outcome; an
        unknown, used or expired ticket, or one whose key is no longer registered, is None."""
        if not isinstance(ticket, str) or not ticket:
            return None
        with self._lock:
            self._sweep(self._clock())
            held = self._tickets.pop(_ticket_hash(ticket), None)
        if held is None:
            return None
        pubkey_id = held[1]
        try:
            if pubkey_id not in registered_keys(self._home):
                return None
        except (ValueError, KeyError, TypeError, OSError):
            return None
        return self._new_session(pubkey_id)

    def session(self, token: Optional[str]) -> Optional[Session]:
        """The live session for ``token``, checked against the key registry on every call:
        a session whose key was revoked or replaced is dropped, and a registry that cannot be
        read refuses every session (fails closed)."""
        with self._lock:
            self._sweep(self._clock())
            found = self._sessions.get(token) if token else None
        if found is None:
            return None
        try:
            live = found.pubkey_id in registered_keys(self._home)
        except (ValueError, KeyError, TypeError, OSError):
            return None
        if not live:
            with self._lock:
                self._sessions = {k: s for k, s in self._sessions.items()
                                  if s.pubkey_id != found.pubkey_id}
            return None
        return found

    def logout(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)


__all__ = ["AuthState", "Session", "LoginKeyError", "ChallengeLimitError",
           "SessionLimitError", "TicketLimitError", "TICKET_TTL_SECONDS", "MAX_OPEN_TICKETS",
           "create_login_key", "registered_keys", "sign_challenge",
           "verify_token", "unlock_private_key", "key_path", "default_key_path", "read_private_key_file",
           "check_new_passphrase", "CHALLENGE_TTL_SECONDS", "SESSION_TTL_SECONDS",
           "MAX_OPEN_CHALLENGES", "MAX_SESSIONS", "SESSION_COOKIE"]
