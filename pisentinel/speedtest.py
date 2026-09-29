from __future__ import annotations

import json
import math
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .db import utc_timestamp


class SpeedtestError(RuntimeError):
    """A bounded, user-safe failure while invoking the Ookla client."""


def _finite(value: Any, minimum: float = 0, maximum: float = 1_000_000) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and minimum <= number <= maximum else None


def _bytes(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _safe_text(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, (str, int)):
        return None
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()
    return text[:limit] or None


def parse_ookla_json(payload: str) -> dict[str, Any]:
    """Normalize documented Ookla JSON and omit client IP/MAC/result URLs."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SpeedtestError("O Ookla retornou JSON inválido.") from exc
    if not isinstance(data, Mapping) or data.get("type") not in {None, "result"}:
        raise SpeedtestError("O Ookla retornou um formato inesperado.")
    ping = data.get("ping") if isinstance(data.get("ping"), Mapping) else {}
    download = data.get("download") if isinstance(data.get("download"), Mapping) else {}
    upload = data.get("upload") if isinstance(data.get("upload"), Mapping) else {}
    server = data.get("server") if isinstance(data.get("server"), Mapping) else {}
    down_bandwidth = _finite(download.get("bandwidth"), maximum=10**12)
    up_bandwidth = _finite(upload.get("bandwidth"), maximum=10**12)
    download_bytes, upload_bytes = _bytes(download.get("bytes")), _bytes(upload.get("bytes"))
    if down_bandwidth is None or up_bandwidth is None:
        raise SpeedtestError("O resultado não contém download e upload válidos.")
    return {
        "success": True,
        "download_mbps": round(down_bandwidth * 8 / 1_000_000, 3),
        "upload_mbps": round(up_bandwidth * 8 / 1_000_000, 3),
        "ping_ms": _finite(ping.get("latency")),
        "jitter_ms": _finite(ping.get("jitter")),
        "packet_loss_pct": _finite(data.get("packetLoss"), maximum=100),
        "download_bytes": download_bytes,
        "upload_bytes": upload_bytes,
        "total_bytes": (download_bytes + upload_bytes) if download_bytes is not None and upload_bytes is not None else None,
        "server_id": _safe_text(server.get("id"), 50),
        "server_name": _safe_text(server.get("name")),
        "server_location": _safe_text(server.get("location")),
        "server_country": _safe_text(server.get("country"), 100),
        "isp": _safe_text(data.get("isp")),
        "error": None,
    }


@dataclass(slots=True)
class OoklaSpeedtestRunner:
    executable: str = "speedtest"
    timeout_seconds: int = 180
    attempts: int = 2
    retry_delay_seconds: float = 2
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def command(self) -> list[str]:
        return [self.executable, "--accept-license", "--accept-gdpr", "--format=json"]

    def run(self) -> dict[str, Any]:
        first_started = self.clock()
        last_error = "Falha desconhecida no Ookla."
        for attempt in range(self.attempts):
            try:
                completed = self.command_runner(
                    self.command(), capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=self.timeout_seconds, check=False,
                )
                if completed.returncode != 0:
                    detail = _safe_text(completed.stderr or completed.stdout, 300)
                    raise SpeedtestError(detail or f"Ookla encerrou com código {completed.returncode}.")
                measurement = parse_ookla_json(completed.stdout)
                finished = self.clock()
                measurement.update({
                    "started_at": utc_timestamp(first_started),
                    "completed_at": utc_timestamp(finished),
                    "duration_seconds": max(0, round((finished - first_started).total_seconds(), 3)),
                })
                return measurement
            except FileNotFoundError:
                last_error = "Executável oficial do Ookla não encontrado."
                break
            except subprocess.TimeoutExpired:
                last_error = f"O teste excedeu {self.timeout_seconds} segundos."
            except (OSError, SpeedtestError) as exc:
                last_error = str(exc)[:500]
            if attempt + 1 < self.attempts:
                self.sleeper(self.retry_delay_seconds)
        finished = self.clock()
        return {
            "success": False,
            "started_at": utc_timestamp(first_started),
            "completed_at": utc_timestamp(finished),
            "duration_seconds": max(0, round((finished - first_started).total_seconds(), 3)),
            "download_mbps": None, "upload_mbps": None, "ping_ms": None, "jitter_ms": None,
            "packet_loss_pct": None, "download_bytes": None, "upload_bytes": None, "total_bytes": None,
            "server_id": None, "server_name": None, "server_location": None, "server_country": None,
            "isp": None, "error": last_error,
        }
