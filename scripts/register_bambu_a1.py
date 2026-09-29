#!/usr/bin/env python3
"""Idempotently link the Bambu Lab A1 to its existing PiSentinel device."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pisentinel.db import Database  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cadastra a A1 sem duplicar o device identificado pelo MAC.")
    parser.add_argument("--database", type=Path, required=True, help="Caminho do banco SQLite do PiSentinel")
    parser.add_argument("--ip", default="192.168.10.156")
    parser.add_argument("--mac", default="9C:13:9E:00:00:01")
    parser.add_argument("--name", default="Bambu Lab A1")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = Database(args.database)
    created_device = False
    try:
        device = database.get_device_by_mac(args.mac)
        device_id = int(device["id"])
    except KeyError:
        device_id = database.upsert_device(args.ip, args.mac, "Bambu-A1")
        created_device = True
    printer_id = database.ensure_printer(
        args.name,
        "Bambu Lab",
        "A1",
        "bambu_a1",
        device_id,
        enabled=True,
        camera_enabled=False,
        adapter_config={"timeout": 5.0, "retries": 1},
    )
    print(json.dumps({
        "printer_id": printer_id,
        "device_id": device_id,
        "device_created": created_device,
        "adapter_type": "bambu_a1",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
