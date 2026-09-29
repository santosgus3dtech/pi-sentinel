from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database, utc_datetime, utc_timestamp


COOKIE_NAME = "pisentinel_session"
PASSWORD_ROUNDS = 260_000
MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_LOCKOUT = timedelta(minutes=15)


class AuthenticationError(ValueError):
    pass


class AuthenticationLocked(AuthenticationError):
    def __init__(self, retry_after_seconds: int):
        super().__init__("Muitas tentativas. Aguarde antes de tentar novamente.")
        self.retry_after_seconds = max(1, retry_after_seconds)


@dataclass(frozen=True, slots=True)
class Principal:
    role: str
    username: str
    user_id: int | None
    must_change_password: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def normalize_username(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", value):
        raise ValueError("O usuário deve ter de 3 a 64 caracteres: letras, números, ponto, hífen ou sublinhado.")
    return value


def validate_password(value: str) -> str:
    if not 12 <= len(value) <= 256:
        raise ValueError("A senha deve ter entre 12 e 256 caracteres.")
    checks = (re.search(r"[a-z]", value), re.search(r"[A-Z]", value), re.search(r"\d", value), re.search(r"[^A-Za-z0-9]", value))
    if not all(checks):
        raise ValueError("Use letras minúsculas, maiúsculas, número e símbolo.")
    return value


def hash_password(password: str, *, salt: bytes | None = None, rounds: int = PASSWORD_ROUNDS) -> str:
    validate_password(password)
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "$".join(("pbkdf2_sha256", str(rounds), base64.urlsafe_b64encode(salt).decode("ascii"), base64.urlsafe_b64encode(digest).decode("ascii")))


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds_text, salt_text, expected_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_text)
        if not 100_000 <= rounds <= 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(candidate, expected)
    except (ValueError, TypeError):
        return False


class AuthService:
    def __init__(self, database: Database, session_hours: int = 12, remember_days: int = 30):
        self.database = database
        self.session_hours = session_hours
        self.remember_days = remember_days
        self._dummy_password_hash = hash_password("PiSentinel-Dummy-Password-2026!")

    def create_admin(self, username: str, password: str, *, must_change_password: bool = False) -> dict[str, Any]:
        username = normalize_username(username)
        encoded = hash_password(password)
        return self.database.create_user(username, encoded, "admin", must_change_password)

    def authenticate(self, username: str, password: str, subject: str, *, remember: bool = False) -> tuple[str, Principal]:
        try:
            username = normalize_username(username)
        except ValueError:
            username = "invalid"
        now = datetime.now(timezone.utc)
        guard = self.database.get_auth_login_attempt(subject)
        if guard and guard.get("locked_until"):
            locked_until = utc_datetime(guard["locked_until"])
            if locked_until > now:
                raise AuthenticationLocked(int((locked_until - now).total_seconds()) + 1)
        user = self.database.get_user_by_username(username)
        encoded = user["password_hash"] if user and user["active"] else self._dummy_password_hash
        if not verify_password(password, encoded) or not user or not user["active"]:
            state = self.database.record_auth_login_failure(subject, MAX_LOGIN_FAILURES, LOGIN_WINDOW, LOGIN_LOCKOUT)
            if state.get("locked_until"):
                locked_until = utc_datetime(state["locked_until"])
                if locked_until > now:
                    raise AuthenticationLocked(int((locked_until - now).total_seconds()) + 1)
            raise AuthenticationError("Usuário ou senha inválidos.")
        self.database.clear_auth_login_attempt(subject)
        self.database.record_user_login(user["id"])
        principal = Principal("admin", user["username"], user["id"], bool(user["must_change_password"]))
        return self._create_session(principal, remember=remember), principal

    def create_guest_session(self) -> tuple[str, Principal]:
        principal = Principal("guest", "Visitante", None, False)
        return self._create_session(principal), principal

    def _create_session(self, principal: Principal, *, remember: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = datetime.now(timezone.utc)
        lifetime = timedelta(days=self.remember_days) if remember else timedelta(hours=self.session_hours)
        self.database.create_auth_session(
            token_hash,
            principal.role,
            principal.user_id,
            utc_timestamp(now),
            utc_timestamp(now + lifetime),
        )
        return token

    def principal_for_token(self, token: str | None) -> Principal | None:
        if not token or len(token) > 256:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        session = self.database.get_auth_session(token_hash)
        if not session:
            return None
        if session["role"] == "guest":
            return Principal("guest", "Visitante", None, False)
        if not session.get("active"):
            self.database.delete_auth_session(token_hash)
            return None
        return Principal("admin", session["username"], session["user_id"], bool(session["must_change_password"]))

    def logout(self, token: str | None) -> None:
        if token:
            self.database.delete_auth_session(hashlib.sha256(token.encode("utf-8")).hexdigest())

    def change_password(self, principal: Principal, current_password: str, new_password: str) -> Principal:
        if not principal.is_admin or principal.user_id is None:
            raise AuthenticationError("Acesso restrito ao administrador.")
        user = self.database.get_user(principal.user_id)
        if not user or not verify_password(current_password, user["password_hash"]):
            raise AuthenticationError("A senha atual está incorreta.")
        if verify_password(new_password, user["password_hash"]):
            raise ValueError("A nova senha deve ser diferente da senha atual.")
        self.database.update_user_password(principal.user_id, hash_password(new_password))
        return Principal("admin", user["username"], user["id"], False)
