import hashlib
import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from pisentinel.api import create_app
from pisentinel.auth import AuthService, COOKIE_NAME
from pisentinel.config import Settings
from pisentinel.db import Database


PASSWORD = "Strong-Password-2026!"
NEW_PASSWORD = "New-Strong-Password-2026!"


def auth_client(tmp_path, *, must_change_password=False, mock_printer_enabled=False):
    settings = Settings(
        db_path=tmp_path / "auth.db",
        auth_enabled=True,
        auth_session_hours=12,
        mock_printer_enabled=mock_printer_enabled,
    )
    database = Database(settings.db_path)
    AuthService(database, settings.auth_session_hours).create_admin(
        "admin", PASSWORD, must_change_password=must_change_password,
    )
    return TestClient(create_app(settings, database)), database, settings


def login(client, password=PASSWORD, *, remember=False):
    return client.post("/api/auth/login", json={
        "username": "admin", "password": password, "remember": remember,
    })


def test_authentication_is_required_but_health_remains_public(tmp_path):
    client, _database, _settings = auth_client(tmp_path)
    assert client.get("/api/health").status_code == 200
    session = client.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json() == {
        "auth_enabled": True,
        "authenticated": False,
        "role": None,
        "username": None,
        "must_change_password": False,
    }
    assert client.get("/api/devices").status_code == 401
    assert client.post("/api/discovery", json={}).status_code == 401


def test_guest_sees_only_sanitized_summary_and_printer_cards(tmp_path):
    client, _database, _settings = auth_client(tmp_path, mock_printer_enabled=True)
    response = client.post("/api/auth/guest", json={})
    assert response.status_code == 200 and response.json()["role"] == "guest"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    summary = client.get("/api/summary")
    assert summary.status_code == 200
    assert summary.json()["network"]["cidr"] == ""
    printers = client.get("/api/printers")
    assert printers.status_code == 200
    printer = printers.json()[0]
    assert printer["ip"] is None and printer["mac"] is None and printer["error"] is None
    assert printer["camera"]["stream_url"] is None
    assert printer["capabilities"]["camera"] is False
    assert printer["capabilities"]["files"] is False
    assert printer["capabilities"]["file_metadata"] is False
    assert printer["capabilities"]["start_print"] is False
    assert client.get("/api/devices").status_code == 403
    assert client.get("/api/printers/0/details").status_code == 403
    assert client.post("/api/discovery", json={}).status_code == 403


def test_admin_login_uses_hashed_password_and_server_side_session(tmp_path):
    client, _database, settings = auth_client(tmp_path)
    assert login(client, "Incorrect-Password-2026!").status_code == 401
    response = login(client)
    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert client.get("/api/devices").status_code == 200
    token = client.cookies.get(COOKIE_NAME)
    assert token
    with sqlite3.connect(settings.db_path) as conn:
        password_hash = conn.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()[0]
        token_hash = conn.execute("SELECT token_hash FROM auth_sessions WHERE role='admin'").fetchone()[0]
    assert PASSWORD not in password_hash
    assert token not in token_hash
    assert token_hash == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert client.post("/api/auth/logout", json={}).status_code == 200
    assert client.get("/api/devices").status_code == 401


def test_login_cookie_is_session_only_unless_remembered(tmp_path):
    client, _database, settings = auth_client(tmp_path)
    response = login(client)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "max-age=" not in cookie and "expires=" not in cookie
    with sqlite3.connect(settings.db_path) as conn:
        expires_at = conn.execute(
            "SELECT expires_at FROM auth_sessions WHERE role='admin' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    remaining_hours = (datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 11.9 <= remaining_hours <= 12.0

    assert client.post("/api/auth/logout", json={}).status_code == 200
    remembered = login(client, remember=True)
    cookie = remembered.headers["set-cookie"].lower()
    assert "max-age=2592000" in cookie
    with sqlite3.connect(settings.db_path) as conn:
        expires_at = conn.execute(
            "SELECT expires_at FROM auth_sessions WHERE role='admin' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    remaining_days = (datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 29.9 <= remaining_days <= 30.0


def test_login_rejects_non_boolean_remember_value(tmp_path):
    client, _database, _settings = auth_client(tmp_path)
    response = client.post("/api/auth/login", json={
        "username": "admin", "password": PASSWORD, "remember": "true",
    })
    assert response.status_code == 422


def test_required_password_change_revokes_sessions_and_old_password(tmp_path):
    client, _database, _settings = auth_client(tmp_path, must_change_password=True)
    response = login(client)
    assert response.status_code == 200 and response.json()["must_change_password"] is True
    assert client.get("/api/devices").status_code == 403
    changed = client.post("/api/auth/change-password", json={
        "current_password": PASSWORD,
        "new_password": NEW_PASSWORD,
    })
    assert changed.status_code == 200 and changed.json()["authenticated"] is False
    assert login(client).status_code == 401
    assert login(client, NEW_PASSWORD).status_code == 200
    assert client.get("/api/devices").status_code == 200


def test_repeated_bad_logins_are_temporarily_locked(tmp_path):
    client, _database, _settings = auth_client(tmp_path)
    statuses = [login(client, "Incorrect-Password-2026!").status_code for _ in range(5)]
    assert statuses == [401, 401, 401, 401, 429]
    locked = login(client)
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
