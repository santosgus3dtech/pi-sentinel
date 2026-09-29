#!/usr/bin/env python3
"""Idempotently link the real Creality K1C to its existing PiSentinel device."""
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
    parser = argparse.ArgumentParser(description="Cadastra a K1C sem duplicar o device identificado pelo MAC.")
    parser.add_argument("--database", type=Path, required=True, help="Caminho do banco SQLite do PiSentinel")
    parser.add_argument("--ip", default="192.168.10.155")
    parser.add_argument("--mac", default="FC:EE:28:00:00:01")
    parser.add_argument("--name", default="Creality K1C")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = Database(args.database)
    created_device = False
    try:
        device = database.get_device_by_mac(args.mac)
        device_id = int(device["id"])
    except KeyError:
        device_id = database.upsert_device(args.ip, args.mac, "K1C-891F")
        created_device = True
    printer_id = database.ensure_printer(
        args.name,
        "Creality",
        "K1C",
        "creality_k1c",
        device_id,
        enabled=True,
        camera_enabled=True,
        adapter_config={"timeout": 2.5, "retries": 1},
    )
    print(json.dumps({
        "printer_id": printer_id,
        "device_id": device_id,
        "device_created": created_device,
        "adapter_type": "creality_k1c",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
