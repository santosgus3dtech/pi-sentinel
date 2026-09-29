#!/usr/bin/env python3
"""Run safe, read-only discovery against one Creality K1C."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pisentinel.printers.discovery import CrealityK1CDiscoveryService  # noqa: E402


def _status(value: bool | None, *, detected: bool = False) -> str:
    if value is True:
        return "OK"
    if value is False:
        return "NÃO ENCONTRADO"
    return "DETECTADO, NÃO CONFIRMADO" if detected else "INCONCLUSIVO"


def _print_row(label: str, value: str) -> None:
    print(f"{label:<18}{value}")


def print_human(result: dict[str, Any]) -> None:
    services = result["services"]
    api = result["api"]
    websocket = result["websocket"]
    camera = result["camera"]
    moonraker = result["moonraker"]

    print("Creality K1C Discovery")
    print()
    _print_row("Host", f"{_status(result['reachable'])} ({result['host']})")
    _print_row("HTTP", _status(services["http"]["available"]))
    _print_row("HTTPS", _status(services["https"]["available"]))
    api_detail = f"{_status(api['available'])}"
    if api["available"]:
        api_detail += f" ({api['type']}: {api['url']})"
    _print_row("API", api_detail)
    ws_detail = _status(websocket["available"], detected=websocket["detected"])
    if websocket["urls"]:
        ws_detail += f" ({websocket['urls'][0]})"
    _print_row("WebSocket", ws_detail)
    camera_detail = _status(camera["available"], detected=camera["detected"])
    if camera["protocol"]:
        camera_detail += f" ({camera['protocol']})"
    _print_row("Câmera", camera_detail)
    _print_row("Moonraker", _status(moonraker["available"]))
    print()
    print("Portas testadas:")
    for item in result["ports"]:
        state = _status(item["open"])
        _print_row(f"  {item['port']}/{item['label']}", state)
    if result["notes"]:
        print()
        print("Observações:")
        for note in result["notes"]:
            print(f"- {note}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Descoberta segura e somente leitura dos serviços de uma Creality K1C.",
    )
    parser.add_argument("--host", required=True, help="IPv4 privado da impressora, por exemplo 192.168.10.155")
    parser.add_argument("--timeout", type=float, default=2.0, help="Timeout por operação em segundos (0,1 a 10; padrão: 2)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emite somente JSON estruturado")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = CrealityK1CDiscoveryService(args.host, timeout=args.timeout).discover()
    except ValueError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
