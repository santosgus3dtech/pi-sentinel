"""Bambu Lab A1 integration over its verified local MQTT/TLS and FTPS services."""
from __future__ import annotations

import hashlib
import ftplib
import json
import os
import random
import re
import socket
import ssl
import threading
import time
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import paho.mqtt.client as mqtt

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

BAMBU_MQTT_PORT = 8883
MAX_MQTT_PAYLOAD = 1_000_000
MAX_ARCHIVE_BYTES = 700 * 1024 * 1024
MAX_PREVIEW_BYTES = 8 * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class BambuProtocolError(RuntimeError):
    """The local MQTT service did not produce a usable telemetry response."""


class BambuAuthenticationError(BambuProtocolError):
    """The broker rejected the configured LAN access code."""


class BambuCertificateError(BambuProtocolError):
    """The printer certificate did not match the configured SHA-256 pin."""


@dataclass(frozen=True, slots=True)
class BambuTelemetry:
    payload: Mapping[str, Any]
    latency_ms: float
    received_at: str


class BambuMQTTClient:
    """Maintain one event-driven MQTT session for telemetry and confirmed commands."""

    def __init__(self, timeout: float = 5.0, freshness_seconds: float = 30.0):
        self.timeout = min(10.0, max(1.0, float(timeout)))
        self.freshness_seconds = min(120.0, max(5.0, float(freshness_seconds)))
        self._query_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._connect_event = threading.Event()
        self._snapshot_event = threading.Event()
        self._client: mqtt.Client | None = None
        self._identity: tuple[str, str, str, bytes] | None = None
        self._serial = ""
        self._expected_fingerprint = ""
        self._report_topic = ""
        self._request_topic = ""
        self._connected = False
        self._connect_error: BambuProtocolError | None = None
        self._payload: dict[str, Any] = {}
        self._received_at: str | None = None
        self._received_monotonic: float | None = None
        self._latency_ms: float | None = None
        self._query_started: float | None = None
        self._command_event = threading.Event()
        self._pending_command: tuple[str, str] | None = None
        self._command_response: dict[str, Any] | None = None

    @staticmethod
    def _reason_value(reason_code: Any) -> int:
        value = getattr(reason_code, "value", reason_code)
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def _on_socket_open(self, _client: mqtt.Client, _userdata: Any, sock: Any) -> None:
        certificate = sock.getpeercert(binary_form=True)
        actual = hashlib.sha256(certificate).hexdigest().upper() if certificate else ""
        if not actual or actual != self._expected_fingerprint:
            raise BambuCertificateError("Certificado MQTT da A1 não corresponde ao pin configurado.")

    def _on_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any,
                    reason_code: Any, _properties: Any) -> None:
        code = self._reason_value(reason_code)
        with self._state_lock:
            self._connected = code == 0
            if code in {4, 5, 134, 135}:
                self._connect_error = BambuAuthenticationError(
                    "A autenticação MQTT da A1 foi recusada."
                )
            elif code != 0:
                self._connect_error = BambuProtocolError(
                    f"O broker MQTT da A1 recusou a conexão (código {code})."
                )
        if code == 0:
            result, _mid = client.subscribe(self._report_topic, qos=0)
            if result != mqtt.MQTT_ERR_SUCCESS:
                with self._state_lock:
                    self._connect_error = BambuProtocolError("Não foi possível assinar a telemetria da A1.")
        self._connect_event.set()

    def _on_subscribe(self, client: mqtt.Client, _userdata: Any, _mid: int,
                      _reason_codes: Any, _properties: Any) -> None:
        self._request_snapshot(client)

    def _on_disconnect(self, _client: mqtt.Client, _userdata: Any, _flags: Any,
                       reason_code: Any, _properties: Any) -> None:
        if self._reason_value(reason_code) != 0:
            with self._state_lock:
                self._connected = False

    def _on_message(self, _client: mqtt.Client, _userdata: Any, message: Any) -> None:
        if message.topic != self._report_topic or len(message.payload) > MAX_MQTT_PAYLOAD:
            return
        try:
            envelope = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        report = envelope.get("print") if isinstance(envelope, dict) else None
        if not isinstance(report, dict):
            return
        now = time.monotonic()
        with self._state_lock:
            self._payload.update(report)
            self._received_at = _utc_now()
            self._received_monotonic = now
            if self._query_started is not None and "gcode_state" in report:
                self._latency_ms = round((now - self._query_started) * 1000, 3)
                self._query_started = None
                self._snapshot_event.set()
            pending = self._pending_command
            if pending and str(report.get("sequence_id")) == pending[0] and report.get("command") == pending[1]:
                self._command_response = dict(report)
                self._command_event.set()

    def _request_snapshot(self, client: mqtt.Client | None = None) -> None:
        active = client or self._client
        if active is None:
            return
        with self._state_lock:
            self._query_started = time.monotonic()
            self._snapshot_event.clear()
        payload = json.dumps(
            {"pushing": {"sequence_id": "pisentinel", "command": "pushall"}},
            separators=(",", ":"),
        )
        result = active.publish(self._request_topic, payload, qos=0, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            with self._state_lock:
                self._connect_error = BambuProtocolError("A consulta de status da A1 não foi enviada.")
            self._snapshot_event.set()

    def _close(self) -> None:
        client = self._client
        self._client = None
        self._connected = False
        if client is None:
            return
        try:
            client.disconnect()
        except (OSError, mqtt.WebsocketConnectionError):
            pass
        try:
            client.loop_stop()
        except RuntimeError:
            pass

    def close(self) -> None:
        with self._query_lock:
            self._close()
            self._identity = None

    def _connect(self, host: str, serial: str, access_code: str, fingerprint: str) -> None:
        self._close()
        with self._state_lock:
            self._connect_event.clear()
            self._snapshot_event.clear()
            self._connect_error = None
            self._payload = {}
            self._received_at = None
            self._received_monotonic = None
            self._latency_ms = None
            self._command_event.clear()
            self._pending_command = None
            self._command_response = None
            self._serial = serial
            self._expected_fingerprint = fingerprint
            self._report_topic = f"device/{serial}/report"
            self._request_topic = f"device/{serial}/request"

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv311,
            reconnect_on_failure=True,
        )
        client.connect_timeout = self.timeout
        client.username_pw_set("bblp", access_code)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        client.tls_set_context(context)
        client.tls_insecure_set(True)
        client.on_socket_open = self._on_socket_open
        client.on_connect = self._on_connect
        client.on_subscribe = self._on_subscribe
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        try:
            client.connect(host, BAMBU_MQTT_PORT, keepalive=30)
            client.loop_start()
        except (OSError, ssl.SSLError, BambuCertificateError) as exc:
            self._close()
            if isinstance(exc, BambuCertificateError):
                raise
            raise BambuProtocolError(f"MQTT/TLS da A1 indisponível: {type(exc).__name__}") from exc
        if not self._connect_event.wait(self.timeout):
            self._close()
            raise BambuProtocolError("O broker MQTT da A1 não confirmou a conexão no prazo.")
        with self._state_lock:
            error = self._connect_error
            connected = self._connected
        if error or not connected:
            self._close()
            raise error or BambuProtocolError("O broker MQTT da A1 não confirmou a conexão.")

    def fetch(self, host: str, serial: str, access_code: str,
              certificate_sha256: str) -> BambuTelemetry:
        secret_marker = hashlib.sha256(access_code.encode("utf-8")).digest()
        identity = (host, serial, certificate_sha256, secret_marker)
        with self._query_lock:
            client = self._client
            fresh = False
            with self._state_lock:
                if self._received_monotonic is not None:
                    fresh = time.monotonic() - self._received_monotonic <= self.freshness_seconds
                complete = "gcode_state" in self._payload
            if identity != self._identity or client is None or not client.is_connected():
                self._connect(host, serial, access_code, certificate_sha256)
                self._identity = identity
            elif fresh and complete:
                return self._telemetry()
            else:
                self._request_snapshot()

            if not self._snapshot_event.wait(self.timeout):
                with self._state_lock:
                    has_partial = bool(self._payload)
                if not has_partial:
                    self._close()
                    raise BambuProtocolError("A A1 não enviou telemetria MQTT no prazo.")
            with self._state_lock:
                error = self._connect_error
            if error:
                raise error
            return self._telemetry()

    def _telemetry(self) -> BambuTelemetry:
        with self._state_lock:
            if not self._payload or self._received_at is None:
                raise BambuProtocolError("A A1 não forneceu um snapshot de telemetria.")
            return BambuTelemetry(
                dict(self._payload),
                self._latency_ms if self._latency_ms is not None else 0.0,
                self._received_at,
            )

    def command(self, host: str, serial: str, access_code: str, certificate_sha256: str,
                command: Mapping[str, Any]) -> dict[str, Any]:
        print_command = command.get("print") if isinstance(command, Mapping) else None
        if not isinstance(print_command, Mapping):
            raise BambuProtocolError("Comando MQTT inválido.")
        sequence_id = str(print_command.get("sequence_id") or "")
        command_name = str(print_command.get("command") or "")
        if not sequence_id or not command_name:
            raise BambuProtocolError("Comando MQTT sem identificação.")
        secret_marker = hashlib.sha256(access_code.encode("utf-8")).digest()
        identity = (host, serial, certificate_sha256, secret_marker)
        with self._query_lock:
            client = self._client
            if identity != self._identity or client is None or not client.is_connected():
                self._connect(host, serial, access_code, certificate_sha256)
                self._identity = identity
                client = self._client
            if client is None:
                raise BambuProtocolError("Conexão MQTT da A1 indisponível.")
            with self._state_lock:
                self._pending_command = (sequence_id, command_name)
                self._command_response = None
                self._command_event.clear()
            payload = json.dumps(command, separators=(",", ":"), ensure_ascii=False)
            result = client.publish(self._request_topic, payload, qos=0, retain=False)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                with self._state_lock:
                    self._pending_command = None
                raise BambuProtocolError("O comando não foi enviado ao broker da A1.")
            if not self._command_event.wait(self.timeout):
                with self._state_lock:
                    self._pending_command = None
                raise BambuProtocolError("A A1 não confirmou o comando no prazo.")
            with self._state_lock:
                response = dict(self._command_response or {})
                self._pending_command = None
                self._command_response = None
            result_text = str(response.get("result", "")).strip().lower()
            if result_text and result_text not in {"success", "ok"}:
                code = response.get("reason") or response.get("result")
                raise BambuProtocolError(f"A A1 recusou o comando ({str(code)[:80]}).")
            return response


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """ftplib variant for the implicit TLS service exposed on port 990."""

    def connect(self, host: str = "", port: int = 0, timeout: float | object = -999,
                source_address: tuple[str, int] | None = None) -> str:
        if host:
            self.host = host
        if port:
            self.port = port
        if timeout != -999:
            self.timeout = timeout  # type: ignore[assignment]
        if source_address is not None:
            self.source_address = source_address
        self.sock = socket.create_connection(
            (self.host, self.port), self.timeout, source_address=self.source_address
        )
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


class BambuFTPSClient:
    """Read the A1 SD card over its observed implicit FTPS service."""

    def __init__(self, timeout: float = 8.0):
        self.timeout = min(15.0, max(2.0, float(timeout)))

    def _connect(self, host: str, access_code: str, certificate_sha256: str) -> _ImplicitFTP_TLS:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        ftp = _ImplicitFTP_TLS(context=context, timeout=self.timeout)
        try:
            ftp.connect(host, 990)
            certificate = ftp.sock.getpeercert(binary_form=True) if ftp.sock else None
            actual = hashlib.sha256(certificate).hexdigest().upper() if certificate else ""
            if not actual or actual != certificate_sha256:
                raise BambuCertificateError("Certificado FTPS da A1 não corresponde ao pin configurado.")
            ftp.login("bblp", access_code)
            ftp.prot_p()
            return ftp
        except Exception:
            ftp.close()
            raise

    def list_root(self, host: str, access_code: str, certificate_sha256: str) -> list[dict[str, Any]]:
        ftp = self._connect(host, access_code, certificate_sha256)
        try:
            lines: list[str] = []
            ftp.retrlines("LIST", lines.append)
        except (OSError, ssl.SSLError, ftplib.Error) as exc:
            raise BambuProtocolError(f"Não foi possível listar o cartão da A1: {type(exc).__name__}") from exc
        finally:
            ftp.close()
        files = []
        for line in lines:
            parts = line.split(maxsplit=8)
            if len(parts) < 9 or parts[0].startswith("d"):
                continue
            name = parts[8]
            try:
                size = int(parts[4])
            except ValueError:
                size = None
            files.append({
                "path": name,
                "name": name,
                "size": size,
                "modified": " ".join(parts[5:8]),
                "print_ready": bool(size) and name.lower().endswith((".gcode.3mf", ".3mf")),
                "preview_available": bool(size) and name.lower().endswith((".gcode.3mf", ".3mf")),
                "metadata": {},
            })
        return files

    def download(self, host: str, access_code: str, certificate_sha256: str,
                 path: str) -> tempfile.SpooledTemporaryFile:
        if not path or path.startswith(("/", "\\")) or "/" in path or "\\" in path or path in {".", ".."}:
            raise PrinterOperationError("O arquivo precisa estar na raiz do cartão da A1.")
        target = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        total = 0

        def write(chunk: bytes) -> None:
            nonlocal total
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise PrinterOperationError("O arquivo excede o limite seguro de inspeção.")
            target.write(chunk)

        ftp = self._connect(host, access_code, certificate_sha256)
        try:
            ftp.retrbinary(f"RETR {path}", write, blocksize=64 * 1024)
        except PrinterOperationError:
            raise
        except (OSError, ssl.SSLError, ftplib.Error) as exc:
            raise PrinterOperationError(f"Não foi possível ler o arquivo no cartão: {type(exc).__name__}.") from exc
        finally:
            ftp.close()
        target.seek(0)
        return target


_SHARED_CLIENTS: dict[tuple[str, str], BambuMQTTClient] = {}
_SHARED_CLIENTS_LOCK = threading.Lock()


def _shared_client(host: str, serial: str, timeout: float) -> BambuMQTTClient:
    key = (host, serial)
    with _SHARED_CLIENTS_LOCK:
        client = _SHARED_CLIENTS.get(key)
        if client is None:
            client = BambuMQTTClient(timeout=timeout)
            _SHARED_CLIENTS[key] = client
        return client


class BambuA1Adapter(PrinterAdapter):
    """Normalize A1 telemetry and expose guarded SD-card print operations."""

    STATE_MAP = {
        "IDLE": PrinterState.IDLE,
        "RUNNING": PrinterState.PRINTING,
        "PRINTING": PrinterState.PRINTING,
        "PAUSE": PrinterState.PAUSED,
        "PAUSED": PrinterState.PAUSED,
        "FINISH": PrinterState.COMPLETED,
        "FINISHED": PrinterState.COMPLETED,
        "COMPLETED": PrinterState.COMPLETED,
        "FAILED": PrinterState.ERROR,
        "ERROR": PrinterState.ERROR,
        "OFFLINE": PrinterState.OFFLINE,
    }

    def __init__(self, printer: PrinterDefinition, *, status_client: BambuMQTTClient | None = None,
                 file_client: BambuFTPSClient | None = None,
                 network_probe: Callable[[str, float], tuple[bool, float | None]] | None = None,
                 environ: Mapping[str, str] | None = None):
        super().__init__(printer)
        env = os.environ if environ is None else environ
        config = printer.adapter_config
        self.timeout = self._bounded_float(config.get("timeout", 5.0), 1.0, 10.0, 5.0)
        self.retries = self._bounded_int(config.get("retries", 1), 0, 1, 1)
        self.host = str(env.get("BAMBU_A1_HOST", "")).strip() or (printer.ip or "")
        self.serial = str(env.get("BAMBU_A1_SERIAL", "")).strip().upper()
        self.access_code = str(env.get("BAMBU_A1_ACCESS_CODE", "")).strip()
        raw_fingerprint = str(env.get("BAMBU_A1_CERT_SHA256", "")).strip()
        self.certificate_sha256 = re.sub(r"[^0-9A-Fa-f]", "", raw_fingerprint).upper()
        self._configuration_error = self._validate_configuration()
        self.status_client = status_client or _shared_client(self.host, self.serial, self.timeout)
        self.file_client = file_client or BambuFTPSClient(max(self.timeout, 8.0))
        self.network_probe = network_probe or self._probe_network
        self._loaded = False
        self._telemetry: BambuTelemetry | None = None
        self._payload: Mapping[str, Any] = {}
        self._protocol_error: str | None = None
        self._network_available = False
        self._network_latency: float | None = None
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
        number = BambuA1Adapter._number(value)
        return max(0, int(number)) if number is not None else None

    def _validate_configuration(self) -> str | None:
        if not self.host:
            return "BAMBU_A1_HOST não configurado e device sem IP."
        if not re.fullmatch(r"[A-Z0-9]{8,32}", self.serial):
            return "BAMBU_A1_SERIAL ausente ou inválido."
        if not 1 <= len(self.access_code) <= 128 or any(char in self.access_code for char in "\r\n"):
            return "BAMBU_A1_ACCESS_CODE ausente ou inválido."
        if not re.fullmatch(r"[0-9A-F]{64}", self.certificate_sha256):
            return "BAMBU_A1_CERT_SHA256 ausente ou inválido."
        return None

    @staticmethod
    def _probe_network(host: str, timeout: float) -> tuple[bool, float | None]:
        started = time.perf_counter()
        try:
            with socket.create_connection((host, BAMBU_MQTT_PORT), timeout=timeout):
                return True, round((time.perf_counter() - started) * 1000, 3)
        except (OSError, TimeoutError):
            return False, None

    @staticmethod
    def _safe_code(value: Any) -> str | None:
        if value in (None, "", 0, "0", "00000000"):
            return None
        text = str(value).strip()
        return text[:64] if text else None

    def _reported_error(self) -> str | None:
        codes = []
        for key in ("print_error", "fail_reason", "mc_print_error_code"):
            code = self._safe_code(self._payload.get(key))
            if code:
                codes.append(f"{key}={code}")
        hms = self._payload.get("hms")
        if isinstance(hms, list) and hms:
            codes.append(f"hms={len(hms)} alerta(s)")
        return "Erro reportado pela A1: " + ", ".join(codes) if codes else None

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._configuration_error is None:
            for attempt in range(self.retries + 1):
                try:
                    self._telemetry = self.status_client.fetch(
                        self.host, self.serial, self.access_code, self.certificate_sha256
                    )
                    self._payload = self._telemetry.payload
                    break
                except (BambuAuthenticationError, BambuCertificateError) as exc:
                    self._protocol_error = str(exc)
                    break
                except (BambuProtocolError, OSError, TimeoutError) as exc:
                    self._protocol_error = f"{type(exc).__name__}: telemetria MQTT indisponível"
                    if attempt < self.retries:
                        close = getattr(self.status_client, "close", None)
                        if callable(close):
                            close()
                        time.sleep(0.1)
        else:
            self._protocol_error = self._configuration_error
        if self._telemetry is not None:
            self._network_available = True
            self._network_latency = self._telemetry.latency_ms
        elif self.host:
            self._network_available, self._network_latency = self.network_probe(self.host, self.timeout)
        self._health = self._build_health()

    def _build_health(self) -> AdapterHealth:
        protocol_available = self._telemetry is not None
        online = protocol_available or self._network_available
        last_seen = self._telemetry.received_at if self._telemetry else (_utc_now() if online else None)
        latency = self._telemetry.latency_ms if self._telemetry else self._network_latency
        camera_message = (
            "Liveview local da A1 não foi integrado: o protocolo da porta 6000 não foi confirmado para proxy seguro."
        )
        components = {
            "network": ComponentHealth(
                ConnectionStatus.HEALTHY if self._network_available else ConnectionStatus.UNAVAILABLE,
                self._network_available,
                self._network_latency,
                None if self._network_available else "A porta MQTT/TLS da A1 não respondeu.",
            ),
            "api": ComponentHealth(
                ConnectionStatus.HEALTHY if protocol_available else ConnectionStatus.UNAVAILABLE,
                protocol_available,
                self._telemetry.latency_ms if self._telemetry else None,
                self._protocol_error,
            ),
            "camera": ComponentHealth(ConnectionStatus.DISABLED, None, None, camera_message),
        }
        printer_error = self._reported_error() if protocol_available else None
        if not online:
            overall = ConnectionStatus.UNAVAILABLE
            message = "Bambu Lab A1 offline na interface MQTT/TLS."
        elif not protocol_available:
            overall = ConnectionStatus.DEGRADED
            message = self._protocol_error or "Telemetria MQTT indisponível."
        else:
            overall = ConnectionStatus.HEALTHY
            message = printer_error
        return AdapterHealth(online, overall, last_seen, message, latency, components)

    def get_status(self) -> PrinterState:
        self._load()
        if self._telemetry is None:
            return PrinterState.UNKNOWN if self._network_available else PrinterState.OFFLINE
        raw = str(self._payload.get("gcode_state", "")).strip().upper()
        state = self.STATE_MAP.get(raw, PrinterState.UNKNOWN)
        if self._reported_error() and state not in {PrinterState.PRINTING, PrinterState.PAUSED}:
            return PrinterState.ERROR
        return state

    def get_temperatures(self) -> PrinterTemperatures:
        self._load()
        return PrinterTemperatures(
            TemperatureReading(
                self._number(self._payload.get("nozzle_temper")),
                self._number(self._payload.get("nozzle_target_temper")),
            ),
            TemperatureReading(
                self._number(self._payload.get("bed_temper")),
                self._number(self._payload.get("bed_target_temper")),
            ),
        )

    def get_current_job(self) -> CurrentJob | None:
        state = self.get_status()
        if state not in {PrinterState.PRINTING, PrinterState.PAUSED}:
            return None
        raw_name = self._payload.get("subtask_name") or self._payload.get("gcode_file")
        file_name = PurePosixPath(str(raw_name)).name if raw_name else None
        progress = self._number(self._payload.get("mc_percent"))
        if progress is not None:
            progress = min(100.0, max(0.0, progress))
        remaining_minutes = self._integer(self._payload.get("mc_remaining_time"))
        remaining_seconds = remaining_minutes * 60 if remaining_minutes is not None else None
        return CurrentJob(file_name, progress, remaining_seconds)

    def get_camera_info(self) -> CameraInfo:
        return CameraInfo(
            False,
            "O liveview LAN da A1 ainda não possui um provider confirmado e seguro no PiSentinel.",
            None,
        )

    def health_check(self) -> AdapterHealth:
        self._load()
        assert self._health is not None
        return self._health

    def capabilities(self) -> Mapping[str, bool]:
        return {
            "camera": False,
            "temperatures": True,
            "job_status": True,
            "progress": True,
            "remaining_time": True,
            "files": True,
            "file_metadata": True,
            "history": False,
            "start_print": True,
            "controls": True,
        }

    _PARAMETER_KEYS = (
        "gcode_state", "mc_percent", "mc_remaining_time", "layer_num", "total_layer_num",
        "gcode_start_time", "mc_print_stage", "mc_print_sub_stage", "print_type", "gcode_file",
        "subtask_name", "nozzle_temper", "nozzle_target_temper", "bed_temper",
        "bed_target_temper", "heatbreak_fan_speed", "cooling_fan_speed", "big_fan1_speed",
        "big_fan2_speed", "fan_gear", "spd_lvl", "spd_mag", "wifi_signal", "sdcard",
        "print_error", "fail_reason", "mc_print_error_code", "hms", "lights_report",
        "home_flag", "hw_switch_state", "nozzle_diameter", "nozzle_type", "maintain",
        "xcam_status", "ipcam", "xcam", "ams", "vt_tray", "online", "upgrade_state",
        "ams_status", "ams_rfid_status", "queue_number", "stg", "stg_cur", "lifecycle",
        "mess_production_state", "gcode_file_prepare_percent", "print_gcode_action",
        "print_real_action", "aux_part_fan", "chamber_temper", "upload",
    )

    @staticmethod
    def _fan_percent(value: Any) -> float | None:
        number = BambuA1Adapter._number(value)
        return round(min(15.0, max(0.0, number)) / 15 * 100, 1) if number is not None else None

    def get_details(self) -> Mapping[str, Any]:
        self._load()
        parameters = {key: self._payload[key] for key in self._PARAMETER_KEYS if key in self._payload}
        return {
            "source": "MQTT 3.1.1 local sobre TLS",
            "parameters": parameters,
            "job": {
                "current_layer": self._integer(self._payload.get("layer_num")),
                "total_layers": self._integer(self._payload.get("total_layer_num")),
                "started_at_epoch": self._integer(self._payload.get("gcode_start_time")),
                "stage": self._payload.get("mc_print_stage"),
                "sub_stage": self._payload.get("mc_print_sub_stage"),
            },
            "performance": {
                "speed_level": self._integer(self._payload.get("spd_lvl")),
                "speed_percent": self._number(self._payload.get("spd_mag")),
                "wifi_signal": self._payload.get("wifi_signal"),
            },
            "fans": {
                "part_percent": self._fan_percent(self._payload.get("cooling_fan_speed")),
                "hotend_percent": self._fan_percent(self._payload.get("heatbreak_fan_speed")),
                "auxiliary_percent": self._fan_percent(self._payload.get("big_fan1_speed")),
                "chamber_percent": self._fan_percent(self._payload.get("big_fan2_speed")),
            },
            "filament": {"ams": self._payload.get("ams"), "external_spool": self._payload.get("vt_tray")},
            "alerts": self._payload.get("hms") if isinstance(self._payload.get("hms"), list) else [],
        }

    def _require_file_configuration(self) -> None:
        if self._configuration_error:
            raise PrinterOperationError(self._configuration_error)

    def list_files(self) -> list[dict[str, Any]]:
        self._require_file_configuration()
        return self.file_client.list_root(
            self.host, self.access_code, self.certificate_sha256
        )

    @staticmethod
    def _safe_zip_names(archive: zipfile.ZipFile) -> list[str]:
        names = archive.namelist()
        if len(names) > 5000 or sum(item.file_size for item in archive.infolist()) > 3_000_000_000:
            raise PrinterOperationError("O arquivo 3MF excede os limites seguros de inspeção.")
        return names

    def _inspect_archive(self, path: str, include_archive: bool = False) -> tuple[dict[str, Any], tempfile.SpooledTemporaryFile | None]:
        files = self.list_files()
        item = next((entry for entry in files if entry["path"] == path), None)
        if item is None:
            raise KeyError(path)
        if not item.get("print_ready"):
            raise PrinterOperationError("O arquivo selecionado não é um projeto 3MF imprimível.")
        stream = self.file_client.download(self.host, self.access_code, self.certificate_sha256, path)
        try:
            with zipfile.ZipFile(stream) as archive:
                names = self._safe_zip_names(archive)
                plate_paths = sorted(
                    (name for name in names if re.fullmatch(r"Metadata/plate_\d+\.gcode", name)),
                    key=lambda value: int(re.search(r"\d+", value).group()),
                )
                if not plate_paths:
                    raise PrinterOperationError("O 3MF não contém G-code de placa compatível.")
                slice_metadata: dict[int, dict[str, str]] = {}
                if "Metadata/slice_info.config" in names:
                    raw_xml = archive.read("Metadata/slice_info.config")
                    if len(raw_xml) <= 2_000_000:
                        try:
                            root = ET.fromstring(raw_xml)
                            for plate_node in root.findall("plate"):
                                values = {node.get("key", ""): node.get("value", "") for node in plate_node.findall("metadata")}
                                try:
                                    index = int(values.get("index", "-1"))
                                except ValueError:
                                    continue
                                slice_metadata[index] = values
                        except ET.ParseError:
                            pass
                plates = []
                for plate_path in plate_paths:
                    index = int(re.search(r"plate_(\d+)", plate_path).group(1))
                    metadata: dict[str, Any] = {}
                    json_name = f"Metadata/plate_{index}.json"
                    if json_name in names and archive.getinfo(json_name).file_size <= 1_000_000:
                        try:
                            value = json.loads(archive.read(json_name))
                            if isinstance(value, dict):
                                metadata = value
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            pass
                    sliced = slice_metadata.get(index, {})
                    plates.append({
                        "index": index,
                        "path": plate_path,
                        "bed_type": metadata.get("bed_type"),
                        "filament_colors": metadata.get("filament_colors", []),
                        "filament_ids": metadata.get("filament_ids", []),
                        "nozzle_diameter": metadata.get("nozzle_diameter"),
                        "estimated_seconds": self._integer(sliced.get("prediction")),
                        "filament_weight_g": self._number(sliced.get("weight")),
                        "preview_available": any(
                            candidate in names for candidate in (
                                f"Metadata/plate_{index}.png", f"Metadata/plate_{index}_small.png",
                            )
                        ) or any(f"plate_{index}" in name and name.lower().endswith((".png", ".jpg", ".jpeg")) for name in names),
                    })
        except zipfile.BadZipFile as exc:
            stream.close()
            raise PrinterOperationError("O arquivo no cartão não é um 3MF válido.") from exc
        result = {
            "file": item,
            "plates": plates,
            "print_options": {
                "confirmation_required": True,
                "timelapse": False,
                "bed_leveling": True,
                "flow_calibration": False,
                "vibration_calibration": True,
                "use_ams": False,
            },
        }
        if include_archive:
            stream.seek(0)
            return result, stream
        stream.close()
        return result, None

    def inspect_file(self, path: str) -> dict[str, Any]:
        result, _stream = self._inspect_archive(path)
        return result

    def open_file_preview(self, path: str, plate: str | None = None) -> tuple[str, bytes]:
        result, stream = self._inspect_archive(path, include_archive=True)
        assert stream is not None
        try:
            selected = plate or (result["plates"][0]["path"] if result["plates"] else "")
            match = re.search(r"plate_(\d+)", selected)
            if not match:
                raise PrinterOperationError("Placa inválida para a miniatura.")
            index = int(match.group(1))
            with zipfile.ZipFile(stream) as archive:
                names = self._safe_zip_names(archive)
                candidates = [
                    f"Metadata/plate_{index}.png", f"Metadata/plate_{index}_small.png",
                    f"Metadata/plate_{index}.jpg", f"Metadata/plate_{index}.jpeg",
                ]
                candidates.extend(
                    name for name in names
                    if f"plate_{index}" in name and name.lower().endswith((".png", ".jpg", ".jpeg"))
                )
                image_name = next((name for name in candidates if name in names), None)
                if image_name is None or archive.getinfo(image_name).file_size > MAX_PREVIEW_BYTES:
                    raise PrinterOperationError("Este projeto não contém miniatura da placa.")
                content = archive.read(image_name)
                content_type = "image/png" if image_name.lower().endswith(".png") else "image/jpeg"
                return content_type, content
        finally:
            stream.close()

    def start_print(self, path: str, options: Mapping[str, Any]) -> dict[str, Any]:
        state = self.get_status()
        if state in {PrinterState.PRINTING, PrinterState.PAUSED}:
            raise PrinterOperationError("A Bambu A1 já está imprimindo ou pausada.")
        if state not in {PrinterState.IDLE, PrinterState.COMPLETED}:
            raise PrinterOperationError("Não foi possível confirmar que a Bambu A1 está ociosa.")
        result, _stream = self._inspect_archive(path)
        plates = result["plates"]
        requested_plate = options.get("plate")
        if requested_plate is None and len(plates) != 1:
            raise PrinterOperationError("Escolha qual placa do projeto deve ser impressa.")
        plate = next((item for item in plates if item["path"] == requested_plate), None) if requested_plate else plates[0]
        if plate is None:
            raise PrinterOperationError("A placa escolhida não existe dentro do projeto.")
        filament_ids = plate.get("filament_ids") if isinstance(plate.get("filament_ids"), list) else []
        filament_count = max(1, len(filament_ids))
        use_ams = bool(options.get("use_ams"))
        requested_mapping = options.get("ams_mapping")
        mapping = [int(value) for value in requested_mapping] if isinstance(requested_mapping, list) else []
        if use_ams:
            if len(mapping) != filament_count or any(value < 0 for value in mapping):
                raise PrinterOperationError(
                    f"Mapeie os {filament_count} filamentos do projeto para as bandejas do AMS Lite."
                )
            mapping2 = [{"ams_id": value // 4, "slot_id": value % 4} for value in mapping]
        else:
            if filament_count > 1:
                raise PrinterOperationError("Este projeto usa mais de um filamento; habilite e mapeie o AMS Lite.")
            mapping = [-1]
            mapping2 = [{"ams_id": 255, "slot_id": 0}]
        sequence_id = str(random.randint(20000, 29999))
        command = {
            "print": {
                "sequence_id": sequence_id,
                "command": "project_file",
                "param": plate["path"],
                "url": f"ftp://{path}",
                "file": path,
                "md5": "",
                "bed_type": "auto",
                "timelapse": bool(options.get("timelapse")),
                "bed_leveling": bool(options.get("bed_leveling", True)),
                "auto_bed_leveling": 1 if options.get("bed_leveling", True) else 0,
                "flow_cali": bool(options.get("flow_calibration")),
                "vibration_cali": bool(options.get("vibration_calibration", True)),
                "layer_inspect": False,
                "use_ams": use_ams,
                "cfg": "0",
                "extrude_cali_flag": 0,
                "extrude_cali_manual_mode": 0,
                "nozzle_offset_cali": 2,
                "subtask_name": PurePosixPath(path).stem,
                "profile_id": "0",
                "project_id": "0",
                "subtask_id": "0",
                "task_id": "0",
                "ams_mapping": mapping,
                "ams_mapping2": mapping2,
            }
        }
        try:
            self.status_client.command(
                self.host, self.serial, self.access_code, self.certificate_sha256, command
            )
        except (BambuProtocolError, OSError, TimeoutError) as exc:
            raise PrinterOperationError(str(exc)) from exc
        return {"accepted": True, "file": path, "plate": plate["path"],
                "message": "A Bambu A1 confirmou o comando de impressão."}
