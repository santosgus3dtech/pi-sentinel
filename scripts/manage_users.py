#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pisentinel.auth import AuthService  # noqa: E402
from pisentinel.config import get_settings  # noqa: E402
from pisentinel.db import Database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Cria um administrador local do PiSentinel.")
    parser.add_argument("create", choices=("create",))
    parser.add_argument("--username", required=True)
    parser.add_argument("--must-change-password", action="store_true")
    args = parser.parse_args()
    password = getpass.getpass("Senha: ")
    confirmation = getpass.getpass("Confirme a senha: ")
    if password != confirmation:
        parser.error("As senhas não conferem.")
    settings = get_settings()
    database = Database(
        settings.db_path,
        settings.failure_threshold,
        settings.recovery_threshold,
        settings.retention_days,
        settings.incident_retention_days,
    )
    try:
        user = AuthService(database, settings.auth_session_hours).create_admin(
            args.username,
            password,
            must_change_password=args.must_change_password,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Administrador criado: {user['username']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
