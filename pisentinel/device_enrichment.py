"""Conservative, bounded enrichment for devices already found on the LAN.

This module deliberately checks only a documented set of common TCP ports. It
does not attempt authentication, OS fingerprinting, vulnerability detection or
an exhaustive port scan.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
import time
from typing import Any, Callable


COMMON_TCP_SERVICES: tuple[tuple[int, str], ...] = (
    (22, "SSH"),
    (53, "DNS"),
    (80, "HTTP"),
    (139, "NetBIOS"),
    (443, "HTTPS"),
    (445, "SMB"),
    (515, "LPD"),
    (554, "RTSP"),
    (631, "IPP"),
    (1883, "MQTT"),
    (2022, "SSH alternativo"),
    (3000, "HTTP alternativo"),
    (5000, "HTTP alternativo"),
    (5357, "Web Services for Devices"),
    (7125, "Moonraker"),
    (8000, "HTTP alternativo"),
    (8080, "HTTP alternativo"),
    (8081, "HTTP alternativo"),
    (8883, "MQTT TLS"),
    (8888, "HTTP alternativo"),
    (9090, "HTTP alternativo"),
    (9100, "JetDirect"),
    (9999, "Serviço de impressora 3D"),
    (10001, "Serviço Bambu Lab"),
)

HTTP_PORTS = {80, 3000, 5000, 8000, 8080, 8081, 8888, 9090}
HTTPS_PORTS = {443}


@dataclass(frozen=True, slots=True)
class OpenService:
    port: int
    service: str
    latency_ms: float
    http_status: int | None = None
    server: str | None = None
    content_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "transport": "tcp",
            "service": self.service,
            "latency_ms": self.latency_ms,
            "http_status": self.http_status,
            "server": self.server,
            "content_type": self.content_type,
        }


def _private_ipv4(host: str) -> str:
    address = ipaddress.ip_address(host)
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local:
        raise ValueError("O enriquecimento aceita somente endereços IPv4 privados da rede local.")
    return str(address)


def _short_header(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(value.split())[:160]
    return cleaned or None


def _http_metadata(host: str, port: int, secure: bool, timeout: float) -> dict[str, Any]:
    connection: http.client.HTTPConnection
    if secure:
        # Local appliances frequently use self-signed certificates. The TLS
        # handshake is used only to read public response headers; no secret is sent.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("HEAD", "/", headers={"User-Agent": "PiSentinel-Discovery/1.0", "Accept": "*/*"})
        response = connection.getresponse()
        return {
            "http_status": int(response.status),
            "server": _short_header(response.getheader("Server")),
            "content_type": _short_header(response.getheader("Content-Type")),
        }
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
        return {}
    finally:
        connection.close()


def _probe(host: str, port: int, service: str, timeout: float,
           connector: Callable[..., Any] = socket.create_connection) -> OpenService | None:
    started = time.perf_counter()
    try:
        connection = connector((host, port), timeout=timeout)
        connection.close()
    except (OSError, TimeoutError):
        return None
    latency = round((time.perf_counter() - started) * 1000, 3)
    metadata: dict[str, Any] = {}
    if port in HTTP_PORTS or port in HTTPS_PORTS:
        metadata = _http_metadata(host, port, port in HTTPS_PORTS, min(1.5, max(0.5, timeout * 3)))
    return OpenService(port, service, latency, **metadata)


def scan_device_services(host: str, *, timeout: float = 0.45,
                         services: tuple[tuple[int, str], ...] = COMMON_TCP_SERVICES,
                         max_workers: int = 12) -> list[dict[str, Any]]:
    """Return open common services for one private IPv4 host.

    Every connection has a short timeout and the list is fixed and reviewable,
    so a discovery cycle cannot turn into an unbounded network scan.
    """
    host = _private_ipv4(host)
    if not 0.05 <= timeout <= 2:
        raise ValueError("O timeout deve ficar entre 0,05 e 2 segundos.")
    if not 1 <= len(services) <= 64 or len({port for port, _ in services}) != len(services):
        raise ValueError("A lista de serviços deve conter entre 1 e 64 portas únicas.")
    if any(not 1 <= port <= 65535 or not name for port, name in services):
        raise ValueError("Serviço TCP inválido.")
    with ThreadPoolExecutor(max_workers=min(max_workers, len(services))) as pool:
        found = list(pool.map(lambda item: _probe(host, item[0], item[1], timeout), services))
    return [item.as_dict() for item in found if item is not None]


def mac_address_type(mac: str | None) -> str:
    if not mac:
        return "unknown"
    try:
        first = int(mac.split(":", 1)[0], 16)
    except (ValueError, IndexError):
        return "unknown"
    if first & 0x01:
        return "multicast"
    return "private" if first & 0x02 else "global"
