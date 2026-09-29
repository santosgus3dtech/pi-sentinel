"""Creality K1C integration using interfaces observed on stock firmware."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import ssl
import struct
import time
from urllib.parse import quote
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, BinaryIO

from .base import PrinterAdapter, PrinterOperationError
from .models import (
    AdapterHealth,
    CameraInfo,
    ComponentHealth,
    ConnectionStatus,
    CurrentJob,
    PrinterDefinition,
    PrinterState,
    PrinterTemperatures,
    TemperatureReading,
)

K1C_WEBSOCKET_PORT = 9999
K1C_WEB_PORT = 80
K1C_CAMERA_PORT = 8080
K1C_CAMERA_PATH = "/?action=stream"
MAX_WEBSOCKET_FRAME = 2_000_000
MAX_PREVIEW_BYTES = 8 * 1024 * 1024


class K1CProtocolError(RuntimeError):
    """The observed K1C interface answered with an invalid or unexpected frame."""


class CameraUnavailable(RuntimeError):
    """The camera endpoint is currently unavailable or returned an unsafe type."""


@dataclass(frozen=True, slots=True)
class K1CTelemetry:
    payload: Mapping[str, Any]
    latency_ms: float
    received_at: str


@dataclass(frozen=True, slots=True)
class CameraProbe:
    available: bool
    protocol: str | None = None
    content_type: str | None = None
    latency_ms: float | None = None
    error: str | None = None


@dataclass(slots=True)
class CameraStream:
    content_type: str
    chunks: Iterator[bytes]


class K1CWebSocketClient:
    """Receive the stock firmware's initial JSON snapshot without sending commands."""

    def __init__(self, timeout: float = 2.5, retries: int = 1,
                 connection_factory: Callable[..., socket.socket] = socket.create_connection):
        self.timeout = min(8.0, max(0.5, float(timeout)))
        self.retries = min(1, max(0, int(retries)))
        self._connection_factory = connection_factory

    def fetch(self, host: str, port: int = K1C_WEBSOCKET_PORT) -> K1CTelemetry:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._fetch_once(host, port)
            except (OSError, TimeoutError, json.JSONDecodeError, K1CProtocolError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.1)
        assert last_error is not None
        raise K1CProtocolError(f"WebSocket K1C indisponível: {type(last_error).__name__}") from last_error

    def _fetch_once(self, host: str, port: int) -> K1CTelemetry:
        started = time.perf_counter()
        sock = self._connection_factory((host, port), timeout=self.timeout)
        try:
            sock.settimeout(self.timeout)
            reader = sock.makefile("rb")
            try:
                self._handshake(sock, reader, host, port)
                payload = self._receive_snapshot(sock, reader)
            finally:
                reader.close()
        finally:
            sock.close()
        received_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return K1CTelemetry(payload, round((time.perf_counter() - started) * 1000, 3), received_at)

    def request(self, host: str, message: Mapping[str, Any], response_keys: tuple[str, ...] = (),
                port: int = K1C_WEBSOCKET_PORT) -> dict[str, Any]:
        """Send one command copied from the stock UI and return its response."""
        sock = self._connection_factory((host, port), timeout=self.timeout)
        try:
            sock.settimeout(self.timeout)
            reader = sock.makefile("rb")
            try:
                self._handshake(sock, reader, host, port)
                self._send_json(sock, message)
                if not response_keys:
                    return {"accepted": True}
                deadline = time.monotonic() + max(self.timeout, 4.0)
                while time.monotonic() < deadline:
                    sock.settimeout(max(0.1, deadline - time.monotonic()))
                    opcode, payload = self._read_frame(reader)
                    if opcode == 8:
                        break
                    if opcode == 9:
                        self._send_frame(sock, 10, payload)
                        continue
                    if opcode != 1:
                        continue
                    value = json.loads(payload.decode("utf-8"))
                    if isinstance(value, dict) and any(key in value for key in response_keys):
                        return value
            finally:
                reader.close()
        except (OSError, TimeoutError, json.JSONDecodeError, K1CProtocolError) as exc:
            raise K1CProtocolError(f"Comando WebSocket K1C indisponível: {type(exc).__name__}") from exc
        finally:
            sock.close()
        raise K1CProtocolError("A K1C não respondeu ao comando no prazo.")

    @staticmethod
    def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
        if len(payload) > 65535:
            raise K1CProtocolError("Comando WebSocket excede o limite.")
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        else:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        sock.sendall(header + mask + masked)

    @classmethod
    def _send_json(cls, sock: socket.socket, message: Mapping[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cls._send_frame(sock, 1, payload)

    @staticmethod
    def _handshake(sock: socket.socket, reader: BinaryIO, host: str, port: int) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: PiSentinel-K1C/1\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        status_line = reader.readline(4097)
        if len(status_line) > 4096 or b" 101 " not in status_line:
            raise K1CProtocolError("Handshake WebSocket não retornou HTTP 101.")
        headers: dict[str, str] = {}
        total = len(status_line)
        while True:
            line = reader.readline(4097)
            total += len(line)
            if total > 16384 or len(line) > 4096:
                raise K1CProtocolError("Cabeçalhos WebSocket excederam o limite.")
            if line in (b"\r\n", b"\n", b""):
                break
            name, separator, value = line.decode("iso-8859-1", errors="replace").partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("upgrade", "").lower() != "websocket" or headers.get("sec-websocket-accept") != expected:
            raise K1CProtocolError("Handshake WebSocket inválido.")

    def _receive_snapshot(self, sock: socket.socket, reader: BinaryIO) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        combined: dict[str, Any] = {}
        for _ in range(12):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            opcode, payload = self._read_frame(reader)
            if opcode == 8:
                break
            if opcode != 1:
                continue
            value = json.loads(payload.decode("utf-8"))
            if isinstance(value, dict):
                combined.update(value)
                if "state" in combined or "model" in combined:
                    return combined
        if combined:
            return combined
        raise K1CProtocolError("Nenhum snapshot JSON foi recebido.")

    @staticmethod
    def _read_frame(reader: BinaryIO) -> tuple[int, bytes]:
        header = reader.read(2)
        if len(header) != 2:
            raise K1CProtocolError("Frame WebSocket incompleto.")
        first, second = header
        opcode = first & 0x0F
        length = second & 0x7F
        masked = bool(second & 0x80)
        if length == 126:
            raw = reader.read(2)
            if len(raw) != 2:
                raise K1CProtocolError("Tamanho WebSocket incompleto.")
            length = struct.unpack("!H", raw)[0]
        elif length == 127:
            raw = reader.read(8)
            if len(raw) != 8:
                raise K1CProtocolError("Tamanho WebSocket incompleto.")
            length = struct.unpack("!Q", raw)[0]
        if length > MAX_WEBSOCKET_FRAME:
            raise K1CProtocolError("Frame WebSocket excede o limite.")
        mask = reader.read(4) if masked else b""
        if masked and len(mask) != 4:
            raise K1CProtocolError("Máscara WebSocket incompleta.")
        payload = reader.read(length)
        if len(payload) != length:
            raise K1CProtocolError("Payload WebSocket incompleto.")
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload


class K1CCameraProvider:
    """Proxy the camera URL embedded by the stock K1C web interface."""

    _ALLOWED_TYPES = ("multipart/x-mixed-replace", "image/jpeg", "image/png", "image/webp")

    def __init__(self, host: str, timeout: float = 3.0, port: int = K1C_CAMERA_PORT,
                 path: str = K1C_CAMERA_PATH):
        self.host = host
        self.timeout = min(8.0, max(0.5, float(timeout)))
        self.port = port
        self.path = path

    @staticmethod
    def _protocol(content_type: str) -> str | None:
        base = content_type.split(";", 1)[0].strip().lower()
        if base == "multipart/x-mixed-replace":
            return "mjpeg"
        if base.startswith("image/"):
            return "snapshot"
        return None

    def probe(self) -> CameraProbe:
        started = time.perf_counter()
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request("GET", self.path, headers={"User-Agent": "PiSentinel-K1C/1"})
            response = connection.getresponse()
            content_type = response.getheader("Content-Type", "")[:200]
            protocol = self._protocol(content_type)
            latency = round((time.perf_counter() - started) * 1000, 3)
            if not 200 <= response.status < 300 or not protocol:
                return CameraProbe(False, content_type=content_type or None, latency_ms=latency,
                                   error="A câmera respondeu com formato não suportado.")
            return CameraProbe(True, protocol, content_type, latency)
        except (OSError, TimeoutError, http.client.HTTPException):
            return CameraProbe(False, error="Endpoint de câmera sem resposta.")
        finally:
            connection.close()

    def open_stream(self) -> CameraStream:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request("GET", self.path, headers={"User-Agent": "PiSentinel-K1C/1"})
            response = connection.getresponse()
            content_type = response.getheader("Content-Type", "")[:200]
            if not 200 <= response.status < 300 or not self._protocol(content_type):
                response.close()
                connection.close()
                raise CameraUnavailable("A câmera não retornou um stream de imagem permitido.")
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            connection.close()
            raise CameraUnavailable("A câmera da K1C não respondeu.") from exc

        def chunks() -> Iterator[bytes]:
            try:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            except (OSError, TimeoutError, http.client.HTTPException):
                return
            finally:
                response.close()
                connection.close()

        return CameraStream(content_type, chunks())


class CrealityK1CAdapter(PrinterAdapter):
    """Normalize the observed K1C WebSocket telemetry into PiSentinel models."""

    STATE_MAP = {
        0: PrinterState.IDLE,
        1: PrinterState.PRINTING,
        2: PrinterState.COMPLETED,
        3: PrinterState.ERROR,
        4: PrinterState.ERROR,
        5: PrinterState.PAUSED,
    }

    def __init__(self, printer: PrinterDefinition, *, status_client: K1CWebSocketClient | None = None,
                 camera_provider: K1CCameraProvider | None = None,
                 web_probe: Callable[[str, float], tuple[bool, float | None]] | None = None):
        super().__init__(printer)
        if not printer.ip:
            raise ValueError("A Creality K1C precisa estar associada a um device com IP.")
        config = printer.adapter_config
        timeout = self._bounded_float(config.get("timeout", 2.5), 0.5, 8.0, 2.5)
        retries = self._bounded_int(config.get("retries", 1), 0, 1, 1)
        self.host = printer.ip
        self.status_client = status_client or K1CWebSocketClient(timeout, retries)
        self.camera_provider = camera_provider or K1CCameraProvider(self.host, timeout)
        self.web_probe = web_probe or self._probe_web
        self._loaded = False
        self._payload: Mapping[str, Any] = {}
        self._telemetry: K1CTelemetry | None = None
        self._api_error: str | None = None
        self._web_available = False
        self._web_latency: float | None = None
        self._camera = CameraProbe(False, error="Câmera ainda não verificada.")
        self._health: AdapterHealth | None = None

    @staticmethod
    def _bounded_float(value: Any, minimum: float, maximum: float, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if minimum <= parsed <= maximum else default

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int, default: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if minimum <= parsed <= maximum else default

    @staticmethod
    def _number(value: Any) -> float | None:
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed == parsed and abs(parsed) != float("inf") else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        number = CrealityK1CAdapter._number(value)
        return max(0, int(number)) if number is not None else None

    @staticmethod
    def _probe_web(host: str, timeout: float) -> tuple[bool, float | None]:
        started = time.perf_counter()
        connection = http.client.HTTPConnection(host, K1C_WEB_PORT, timeout=timeout)
        try:
            connection.request("HEAD", "/", headers={"User-Agent": "PiSentinel-K1C/1"})
            response = connection.getresponse()
            return 100 <= response.status < 500, round((time.perf_counter() - started) * 1000, 3)
        except (OSError, TimeoutError, http.client.HTTPException):
            return False, None
        finally:
            connection.close()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        timeout = self.status_client.timeout
        try:
            self._telemetry = self.status_client.fetch(self.host, K1C_WEBSOCKET_PORT)
            self._payload = self._telemetry.payload
        except (OSError, TimeoutError, K1CProtocolError) as exc:
            self._api_error = f"{type(exc).__name__}: telemetria WebSocket indisponível"
        self._web_available, self._web_latency = self.web_probe(self.host, timeout)
        if self.printer.camera_enabled:
            self._camera = self.camera_provider.probe()
        else:
            self._camera = CameraProbe(False, error="Câmera desativada no cadastro.")
        self._health = self._build_health()

    def _build_health(self) -> AdapterHealth:
        api_available = self._telemetry is not None
        online = api_available or self._web_available
        latency = self._telemetry.latency_ms if self._telemetry else self._web_latency
        last_seen = self._telemetry.received_at if self._telemetry else (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            if self._web_available else None
        )
        components = {
            "network": ComponentHealth(
                ConnectionStatus.HEALTHY if online else ConnectionStatus.UNAVAILABLE,
                online,
                latency,
                None if online else "A K1C não respondeu nas interfaces monitoradas.",
            ),
            "web": ComponentHealth(
                ConnectionStatus.HEALTHY if self._web_available else ConnectionStatus.UNAVAILABLE,
                self._web_available,
                self._web_latency,
                None if self._web_available else "Interface HTTP sem resposta.",
            ),
            "api": ComponentHealth(
                ConnectionStatus.HEALTHY if api_available else ConnectionStatus.UNAVAILABLE,
                api_available,
                self._telemetry.latency_ms if self._telemetry else None,
                self._api_error,
            ),
            "camera": ComponentHealth(
                ConnectionStatus.HEALTHY if self._camera.available else (
                    ConnectionStatus.UNAVAILABLE if self.printer.camera_enabled else ConnectionStatus.DISABLED
                ),
                self._camera.available if self.printer.camera_enabled else None,
                self._camera.latency_ms,
                self._camera.error,
            ),
        }
        if not online:
            overall = ConnectionStatus.UNAVAILABLE
            message = "K1C offline nas interfaces verificadas."
        elif not api_available or not self._web_available or (self.printer.camera_enabled and not self._camera.available):
            overall = ConnectionStatus.DEGRADED
            failed = [name for name, value in components.items() if value.status == ConnectionStatus.UNAVAILABLE]
            message = "Componentes indisponíveis: " + ", ".join(failed) + "."
        else:
            overall = ConnectionStatus.HEALTHY
            message = None
        return AdapterHealth(online, overall, last_seen, message, latency, components)

    def get_status(self) -> PrinterState:
        self._load()
        if self._telemetry is None:
            return PrinterState.UNKNOWN if self._web_available else PrinterState.OFFLINE
        raw = self._payload.get("state")
        try:
            return self.STATE_MAP.get(int(raw), PrinterState.UNKNOWN)
        except (TypeError, ValueError):
            return PrinterState.UNKNOWN

    def get_temperatures(self) -> PrinterTemperatures:
        self._load()
        return PrinterTemperatures(
            TemperatureReading(self._number(self._payload.get("nozzleTemp")),
                               self._number(self._payload.get("targetNozzleTemp"))),
            TemperatureReading(self._number(self._payload.get("bedTemp0")),
                               self._number(self._payload.get("targetBedTemp0"))),
        )

    def get_current_job(self) -> CurrentJob | None:
        state = self.get_status()
        if state not in {PrinterState.PRINTING, PrinterState.PAUSED}:
            return None
        raw_name = self._payload.get("printFileName")
        file_name = PurePosixPath(str(raw_name)).name if raw_name else None
        progress = self._number(self._payload.get("printProgress"))
        if progress is not None:
            progress = min(100.0, max(0.0, progress))
        return CurrentJob(file_name, progress, self._integer(self._payload.get("printLeftTime")))

    def get_camera_info(self) -> CameraInfo:
        self._load()
        return CameraInfo(self._camera.available, self._camera.error, self._camera.protocol)

    def health_check(self) -> AdapterHealth:
        self._load()
        assert self._health is not None
        return self._health

    def capabilities(self) -> Mapping[str, bool]:
        return {
            "camera": self.printer.camera_enabled,
            "temperatures": True,
            "job_status": True,
            "progress": True,
            "remaining_time": True,
            "files": True,
            "file_metadata": True,
            "history": True,
            "start_print": True,
            "controls": True,
        }

    _PARAMETER_KEYS = (
        "state", "printProgress", "printFileName", "printFileType", "printId",
        "printJobTime", "printLeftTime", "printStartTime", "layer", "TotalLayer",
        "nozzleTemp", "targetNozzleTemp", "bedTemp0", "targetBedTemp0", "boxTemp",
        "targetBoxTemp", "maxNozzleTemp", "maxBedTemp", "maxBoxTemp", "realTimeSpeed",
        "realTimeFlow", "curFeedratePct", "curFlowratePct", "usedMaterialLength",
        "fan", "modelFanPct", "auxiliaryFanPct", "caseFanPct", "fanAuxiliary",
        "fanCase", "materialDetect", "materialDetector1", "materialStatus", "powerLoss",
        "lightSw", "tfCard", "video", "video1", "videoElapse", "webrtcSupport",
        "aiDetection", "aiFirstFloor", "aiPausePrint", "aiSw", "autoLevelResult",
        "autohome", "enableSelfTest", "withSelfTest", "features", "feedState",
        "deviceState", "err", "repoPlrStatus", "upgradeStatus", "hostname", "model",
        "modelVersion", "connectionCount", "curPosition", "velocityLimits",
        "accelerationLimits", "accelToDecelLimits", "cornerVelocityLimits",
        "pressureAdvance", "smoothTime", "nozzleTempAutoPid", "bedTempAutoPid",
    )

    def get_details(self) -> Mapping[str, Any]:
        self._load()
        parameters = {key: self._payload[key] for key in self._PARAMETER_KEYS if key in self._payload}
        return {
            "source": "WebSocket local da interface oficial Creality",
            "parameters": parameters,
            "job": {
                "current_layer": self._integer(self._payload.get("layer")),
                "total_layers": self._integer(self._payload.get("TotalLayer")),
                "elapsed_seconds": self._integer(self._payload.get("printJobTime")),
                "started_at_epoch": self._integer(self._payload.get("printStartTime")),
                "used_filament_mm": self._number(self._payload.get("usedMaterialLength")),
            },
            "performance": {
                "speed": self._number(self._payload.get("realTimeSpeed")),
                "flow": self._number(self._payload.get("realTimeFlow")),
                "feedrate_percent": self._number(self._payload.get("curFeedratePct")),
                "flow_percent": self._number(self._payload.get("curFlowratePct")),
            },
            "fans": {
                "model_percent": self._number(self._payload.get("modelFanPct")),
                "auxiliary_percent": self._number(self._payload.get("auxiliaryFanPct")),
                "case_percent": self._number(self._payload.get("caseFanPct")),
            },
        }

    @staticmethod
    def _file_entry(raw: Mapping[str, Any]) -> dict[str, Any]:
        path = str(raw.get("path") or "")
        layer_height = CrealityK1CAdapter._number(raw.get("layerHeight"))
        if not layer_height:
            encoded_height = CrealityK1CAdapter._number(raw.get("floorHeight"))
            layer_height = encoded_height / 100 if encoded_height is not None else None
        return {
            "path": path,
            "name": str(raw.get("name") or PurePosixPath(path).name),
            "size": CrealityK1CAdapter._integer(raw.get("file_size")),
            "modified_at_epoch": CrealityK1CAdapter._integer(raw.get("create_time")),
            "print_ready": path.endswith(".gcode") and bool(raw.get("file_size")),
            "preview_available": bool(raw.get("preview") or raw.get("thumbnail")),
            "metadata": {
                "estimated_seconds": CrealityK1CAdapter._integer(raw.get("timeCost")),
                "filament_mm": CrealityK1CAdapter._number(raw.get("consumables")),
                "filament_weight_g": raw.get("filamentWeight"),
                "material": raw.get("material"),
                "material_colors": raw.get("materialColors"),
                "layer_height_mm": layer_height,
                "nozzle_temperature": (
                    CrealityK1CAdapter._number(raw.get("nozzleTemp")) / 100
                    if CrealityK1CAdapter._number(raw.get("nozzleTemp")) is not None else None
                ),
                "bed_temperature": (
                    CrealityK1CAdapter._number(raw.get("bedTemp")) / 100
                    if CrealityK1CAdapter._number(raw.get("bedTemp")) is not None else None
                ),
                "software": raw.get("software"),
            },
            "_preview": raw.get("preview"),
            "_thumbnail": raw.get("thumbnail"),
        }

    def list_files(self) -> list[dict[str, Any]]:
        response = self.status_client.request(
            self.host,
            {"method": "get", "params": {"reqGcodeFile": 1}},
            ("retGcodeFileInfo2", "retGcodeFileInfo"),
        )
        if isinstance(response.get("retGcodeFileInfo2"), list):
            rows = [self._file_entry(item) for item in response["retGcodeFileInfo2"] if isinstance(item, dict)]
        else:
            info = response.get("retGcodeFileInfo")
            raw_text = info.get("fileInfo", "") if isinstance(info, dict) else ""
            rows = []
            for value in str(raw_text).split(";"):
                parts = value.split(":")
                if len(parts) < 2:
                    continue
                path = f"{parts[0].rstrip('/')}/{parts[1]}"
                rows.append({
                    "path": path,
                    "name": parts[1],
                    "size": self._integer(parts[2]) if len(parts) > 2 else None,
                    "modified_at_epoch": self._integer(parts[4]) if len(parts) > 4 else None,
                    "print_ready": path.endswith(".gcode"),
                    "preview_available": len(parts) > 6 and bool(parts[6]),
                    "metadata": {"layer_height_mm": (
                                     self._number(parts[3]) / 100
                                     if len(parts) > 3 and self._number(parts[3]) is not None else None
                                 ),
                                 "filament_mm": self._number(parts[5]) if len(parts) > 5 else None},
                    "_thumbnail": parts[6] if len(parts) > 6 else None,
                    "_preview": None,
                })
        return [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]

    def _raw_file(self, path: str) -> dict[str, Any]:
        response = self.status_client.request(
            self.host,
            {"method": "get", "params": {"reqGcodeFile": 1}},
            ("retGcodeFileInfo2", "retGcodeFileInfo"),
        )
        if isinstance(response.get("retGcodeFileInfo2"), list):
            for item in response["retGcodeFileInfo2"]:
                if isinstance(item, dict) and str(item.get("path")) == path:
                    return self._file_entry(item)
        raise KeyError(path)

    def inspect_file(self, path: str) -> dict[str, Any]:
        item = self._raw_file(path)
        return {
            "file": {key: value for key, value in item.items() if not key.startswith("_")},
            "plates": [],
            "print_options": {"confirmation_required": True},
        }

    def list_history(self) -> list[dict[str, Any]]:
        response = self.status_client.request(
            self.host,
            {"method": "get", "params": {"reqHistory": 1}},
            ("historyList",),
        )
        history = response.get("historyList")
        if not isinstance(history, list):
            return []
        results = []
        for item in history:
            if not isinstance(item, dict):
                continue
            results.append({
                "id": item.get("id"),
                "file_name": PurePosixPath(str(item.get("filename") or "")).name or None,
                "started_at": item.get("dateTime"),
                "duration_seconds": self._integer(item.get("usagetime")),
                "filament_mm": self._number(item.get("usagematerial")),
                "completed": bool(item.get("printfinish")),
                "size": self._integer(item.get("size")),
            })
        return results

    def open_file_preview(self, path: str, plate: str | None = None) -> tuple[str, bytes]:
        del plate
        item = self._raw_file(path)
        candidates = []
        for source, folder in ((item.get("_preview"), "original"), (item.get("_thumbnail"), "humbnail")):
            if source:
                candidates.append(f"/downloads/{folder}/{quote(PurePosixPath(str(source)).name)}")
        for candidate in candidates:
            connection = http.client.HTTPConnection(self.host, K1C_WEB_PORT, timeout=self.status_client.timeout)
            try:
                connection.request("GET", candidate, headers={"User-Agent": "PiSentinel-K1C/1"})
                response = connection.getresponse()
                content_type = response.getheader("Content-Type", "").split(";", 1)[0].lower()
                content = response.read(MAX_PREVIEW_BYTES + 1)
                if 200 <= response.status < 300 and content_type in {"image/png", "image/jpeg", "image/webp"} and len(content) <= MAX_PREVIEW_BYTES:
                    return content_type, content
            except (OSError, TimeoutError, http.client.HTTPException):
                pass
            finally:
                connection.close()
        raise PrinterOperationError("A miniatura deste arquivo não está disponível.")

    def start_print(self, path: str, options: Mapping[str, Any]) -> dict[str, Any]:
        del options
        state = self.get_status()
        if state in {PrinterState.PRINTING, PrinterState.PAUSED}:
            raise PrinterOperationError("A K1C já está imprimindo ou pausada.")
        if state not in {PrinterState.IDLE, PrinterState.COMPLETED}:
            raise PrinterOperationError("Não foi possível confirmar que a K1C está ociosa.")
        item = self._raw_file(path)
        if not item.get("print_ready") or not path.startswith("/usr/data/printer_data/gcodes/"):
            raise PrinterOperationError("Selecione um G-code válido que esteja no armazenamento da K1C.")
        if any(char in path for char in "\r\n:\0"):
            raise PrinterOperationError("O caminho do G-code não pode ser enviado com segurança.")
        self.status_client.request(
            self.host,
            {"method": "set", "params": {"opGcodeFile": f"printprt:{path}"}},
        )
        return {"accepted": True, "file": item["name"],
                "message": "Comando de impressão enviado à Creality K1C."}

    def open_camera_stream(self) -> CameraStream:
        if not self.printer.camera_enabled:
            raise CameraUnavailable("A câmera está desativada no cadastro da impressora.")
        return self.camera_provider.open_stream()
