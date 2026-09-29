"""Read-only discovery for a stock Creality K1C on the local network.

The discovery service deliberately keeps detection separate from PrinterAdapter:
it gathers evidence, but it never sends printer commands or assumes a protocol.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from pisentinel.probes import ProbeResult, ProbeUnavailable, ping, probe_target

DEFAULT_PORTS = (80, 443, 8080, 9999, 7125)
MAX_BODY_BYTES = 1_048_576
MAX_ASSETS = 12
MAX_DISCOVERED_PROBES = 12

_SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "cookie",
    "key", "password", "secret", "session", "signature", "token",
}
_SAFE_API_WORDS = ("api", "device", "info", "printer", "server", "state", "status")
_UNSAFE_PATH_SEGMENTS = {
    "cancel", "command", "control", "delete", "gcode", "home", "move",
    "pause", "print", "reboot", "restart", "resume", "start", "stop",
    "update", "upgrade", "upload",
}
_CAMERA_WORDS = ("camera", "mjpeg", "snapshot", "stream", "video", "webcam", ".m3u8")


@dataclass(frozen=True)
class DiscoveryHttpResponse:
    """Small, bounded HTTP response used by discovery and its mocked tests."""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes = b""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    def text(self) -> str:
        charset = "utf-8"
        match = re.search(r"charset=([^;\s]+)", self.headers.get("content-type", ""), re.I)
        if match:
            charset = match.group(1).strip("\"'")
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag.lower() == "script" and values.get("src"):
            self.scripts.append(values["src"])
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)

    @property
    def title(self) -> str | None:
        value = " ".join(" ".join(self.title_parts).split())[:200]
        return value or None


PingProbe = Callable[[str], ProbeResult]
TcpProbe = Callable[[dict[str, Any]], ProbeResult]
HttpFetcher = Callable[[str, str, float, int], DiscoveryHttpResponse]
WebSocketProbe = Callable[[str, float], bool]


class CrealityK1CDiscoveryService:
    """Collect bounded, read-only evidence about services exposed by one K1C."""

    def __init__(
        self,
        host: str,
        *,
        timeout: float = 2.0,
        ports: tuple[int, ...] = DEFAULT_PORTS,
        ping_probe: PingProbe = ping,
        tcp_probe: TcpProbe = probe_target,
        http_fetcher: HttpFetcher | None = None,
        websocket_probe: WebSocketProbe | None = None,
    ) -> None:
        address = ipaddress.ip_address(host)
        if not isinstance(address, ipaddress.IPv4Address) or not address.is_private:
            raise ValueError("O discovery exige um endereço IPv4 privado.")
        if not 0.1 <= timeout <= 10:
            raise ValueError("O timeout deve estar entre 0,1 e 10 segundos.")
        if not ports or len(ports) > 12 or any(port < 1 or port > 65535 for port in ports):
            raise ValueError("Informe de 1 a 12 portas TCP válidas.")

        self.host = str(address)
        self.timeout = float(timeout)
        self.ports = tuple(dict.fromkeys(ports))
        self._ping_probe = ping_probe
        self._tcp_probe = tcp_probe
        self._http_fetcher = http_fetcher or self._fetch_http
        self._websocket_probe = websocket_probe or self._probe_websocket

    def discover(self) -> dict[str, Any]:
        result = self._empty_result()
        try:
            icmp = self._ping_probe(self.host)
            result["reachability"]["icmp"] = bool(icmp.success)
        except ProbeUnavailable:
            result["reachability"]["icmp"] = None

        open_ports: set[int] = set()
        for port in self.ports:
            try:
                observation = self._tcp_probe({"kind": "tcp", "host": self.host, "port": port})
                is_open = bool(observation.success)
            except ProbeUnavailable:
                is_open = None
            result["ports"].append({
                "port": port,
                "open": is_open,
                "label": self._port_label(port),
            })
            if is_open:
                open_ports.add(port)

        result["reachability"]["any_tcp"] = bool(open_ports)
        result["reachable"] = result["reachability"]["icmp"] is True or bool(open_ports)
        if not result["reachable"]:
            result["notes"].append(
                "Nenhuma resposta foi obtida nos testes limitados; isso não prova que a impressora esteja desligada."
            )
            return result

        sources: list[tuple[str, str]] = []
        discovered_urls: set[str] = set()
        fetched_urls: set[str] = set()

        for port, service_name, scheme in (
            (80, "http", "http"),
            (443, "https", "https"),
            (8080, "http_alternative", "http"),
        ):
            if port not in open_ports:
                continue
            root_url = self._build_url(scheme, port, "/")
            response = self._safe_fetch(root_url, "GET")
            if response is None:
                continue
            fetched_urls.add(root_url)
            service = self._http_service(response, port)
            result["services"][service_name] = service
            text = response.text()
            sources.append((root_url, text))

            if response.content_type in ("text/html", "application/xhtml+xml") or "<html" in text[:500].lower():
                parser = _AssetParser()
                parser.feed(text)
                if parser.title:
                    service["title"] = self._redact_text(parser.title)
                for source in parser.scripts[:MAX_ASSETS]:
                    asset_url = self._same_host_url(root_url, source, schemes=("http", "https"))
                    if not asset_url or asset_url in fetched_urls:
                        continue
                    fetched_urls.add(asset_url)
                    asset = self._safe_fetch(asset_url, "GET")
                    if asset is None or asset.status >= 400:
                        continue
                    sources.append((asset_url, asset.text()))
                    result["web"]["assets"].append(self._sanitize_url(asset_url))

        for base_url, source in sources:
            discovered_urls.update(self._extract_urls(base_url, source))

        sanitized_endpoints = sorted({self._sanitize_url(url) for url in discovered_urls})
        result["web"]["endpoints"] = sanitized_endpoints
        self._discover_api(result, discovered_urls, fetched_urls, open_ports)
        self._discover_websocket(result, discovered_urls, sources)
        self._discover_camera(result, discovered_urls)
        return result

    def _empty_result(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "host": self.host,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "reachable": False,
            "reachability": {"icmp": None, "any_tcp": False},
            "ports": [],
            "services": {
                "http": {"available": False, "state": "not_found"},
                "https": {"available": False, "state": "not_found"},
            },
            "web": {"assets": [], "endpoints": []},
            "api": {"available": False, "state": "not_found", "type": None, "url": None, "evidence": []},
            "moonraker": {"available": False, "state": "not_found", "evidence": []},
            "websocket": {
                "detected": False, "available": None, "state": "not_found", "urls": [], "evidence": [],
            },
            "camera": {
                "detected": False, "available": None, "state": "not_found",
                "protocol": None, "url": None, "evidence": [],
            },
            "notes": [],
        }

    def _discover_api(
        self,
        result: dict[str, Any],
        discovered_urls: set[str],
        fetched_urls: set[str],
        open_ports: set[int],
    ) -> None:
        candidates = [url for url in sorted(discovered_urls) if self._is_safe_api_candidate(url)]
        if 7125 in open_ports:
            candidates.insert(0, self._build_url("http", 7125, "/server/info"))

        for url in list(dict.fromkeys(candidates))[:MAX_DISCOVERED_PROBES]:
            if url in fetched_urls:
                continue
            response = self._safe_fetch(url, "GET")
            if response is None or not 200 <= response.status < 300:
                continue
            payload = self._parse_json(response)
            if payload is None:
                continue
            safe_url = self._sanitize_url(url)
            api_type = "moonraker" if self._is_moonraker_payload(payload) else "structured_json"
            if not result["api"]["available"]:
                result["api"].update({
                    "available": True,
                    "state": "available",
                    "type": api_type,
                    "url": safe_url,
                    "evidence": ["Resposta JSON estruturada recebida por GET."],
                })
            if api_type == "moonraker":
                result["moonraker"].update({
                    "available": True,
                    "state": "available",
                    "evidence": [f"Assinatura Moonraker confirmada em {safe_url}."],
                })

        if 7125 in open_ports and not result["moonraker"]["available"]:
            result["moonraker"]["evidence"].append(
                "A porta 7125 respondeu, mas nenhuma assinatura Moonraker foi confirmada."
            )

    def _discover_websocket(
        self,
        result: dict[str, Any],
        discovered_urls: set[str],
        sources: list[tuple[str, str]],
    ) -> None:
        urls = sorted(url for url in discovered_urls if urlsplit(url).scheme in ("ws", "wss"))
        dynamic_reference = any(re.search(r"\b(?:new\s+)?WebSocket\s*\(", source, re.I) for _, source in sources)
        if not urls and not dynamic_reference:
            return
        result["websocket"]["detected"] = True
        result["websocket"]["state"] = "detected"
        result["websocket"]["urls"] = [self._sanitize_url(url) for url in urls]
        if dynamic_reference:
            result["websocket"]["evidence"].append("Uso de WebSocket encontrado nos recursos da interface.")
        if not urls:
            result["websocket"]["evidence"].append("A URL é construída dinamicamente e não pôde ser confirmada.")
            return
        for url in urls[:3]:
            try:
                if self._websocket_probe(url, self.timeout):
                    result["websocket"]["available"] = True
                    result["websocket"]["state"] = "available"
                    result["websocket"]["evidence"].append("Handshake HTTP 101 confirmado sem enviar mensagens.")
                    return
            except (OSError, TimeoutError, ssl.SSLError):
                continue
        result["websocket"]["available"] = False
        result["websocket"]["evidence"].append("As URLs encontradas não aceitaram o handshake durante o teste.")

    def _discover_camera(self, result: dict[str, Any], discovered_urls: set[str]) -> None:
        candidates = [url for url in sorted(discovered_urls) if self._looks_like_camera(url)]
        if not candidates:
            return
        result["camera"]["detected"] = True
        result["camera"]["state"] = "detected"
        for url in candidates[:6]:
            scheme = urlsplit(url).scheme
            safe_url = self._sanitize_url(url)
            if scheme == "rtsp":
                result["camera"].update({"protocol": "rtsp", "url": safe_url})
                result["camera"]["evidence"].append("Referência RTSP encontrada; o stream não foi aberto.")
                continue
            if scheme not in ("http", "https"):
                continue
            response = self._safe_fetch(url, "GET", max_bytes=4096)
            if response is None or not 200 <= response.status < 400:
                continue
            protocol = self._camera_protocol(response.content_type, url)
            if not protocol:
                continue
            result["camera"].update({
                "available": True,
                "state": "available",
                "protocol": protocol,
                "url": safe_url,
                "evidence": [f"Resposta de câmera confirmada pelo Content-Type {response.content_type or 'desconhecido'}."],
            })
            return
        if result["camera"]["url"] is None:
            result["camera"]["url"] = self._sanitize_url(candidates[0])
        result["camera"]["evidence"].append("A referência foi encontrada, mas a disponibilidade não foi confirmada.")

    def _extract_urls(self, base_url: str, source: str) -> set[str]:
        raw_values: set[str] = set()
        patterns = (
            r"\b(?:fetch|WebSocket)\s*\(\s*['\"]([^'\"]+)",
            r"\baxios(?:\.[a-z]+)?\s*\(\s*['\"]([^'\"]+)",
            r"\.open\s*\(\s*['\"]GET['\"]\s*,\s*['\"]([^'\"]+)",
            r"['\"]((?:https?|wss?|rtsp)://[^'\"\s<>]+)['\"]",
            r"['\"]((?:/|https?://|wss?://|rtsp://)[^'\"]*(?:/api/|/server/|/printer/|camera|webcam|snapshot|mjpeg|\.m3u8)[^'\"]*)['\"]",
        )
        for pattern in patterns:
            raw_values.update(match for match in re.findall(pattern, source, re.I) if match)
        raw_values.update(re.findall(r"`((?:https?|wss?|rtsp)://\$\{[^}]+\}[^`]*)`", source, re.I))
        normalized: set[str] = set()
        for raw in raw_values:
            value = raw.replace("\\/", "/").replace("&amp;", "&")
            template = re.fullmatch(r"((?:https?|wss?|rtsp)://)\$\{[^}]+\}([^\s]*)", value, re.I)
            if template:
                value = f"{template.group(1)}{self.host}{template.group(2)}"
            url = self._same_host_url(base_url, value, schemes=("http", "https", "ws", "wss", "rtsp"))
            if url:
                normalized.add(url)
        return normalized

    def _same_host_url(self, base_url: str, value: str, *, schemes: tuple[str, ...]) -> str | None:
        if (not value or len(value) > 2048 or any(char.isspace() for char in value)
                or any(marker in value for marker in ("${", "{{", "<%", "(", ")", "[", "]", "{", "}", ";"))):
            return None
        url = urljoin(base_url, value)
        parsed = urlsplit(url)
        if parsed.scheme not in schemes or parsed.hostname != self.host:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))

    def _is_safe_api_candidate(self, url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or parsed.hostname != self.host:
            return False
        segments = {segment.lower() for segment in parsed.path.split("/") if segment}
        if segments & _UNSAFE_PATH_SEGMENTS:
            return False
        path = parsed.path.lower()
        return any(word in path for word in _SAFE_API_WORDS)

    def _looks_like_camera(self, url: str) -> bool:
        parsed = urlsplit(url)
        value = (parsed.path + "?" + parsed.query).lower()
        return parsed.scheme == "rtsp" or any(word in value for word in _CAMERA_WORDS)

    def _safe_fetch(self, url: str, method: str, *, max_bytes: int = MAX_BODY_BYTES) -> DiscoveryHttpResponse | None:
        parsed = urlsplit(url)
        if parsed.hostname != self.host or parsed.scheme not in ("http", "https"):
            return None
        try:
            return self._http_fetcher(url, method, self.timeout, max_bytes)
        except (OSError, TimeoutError, ValueError, http.client.HTTPException, ssl.SSLError):
            return None

    @staticmethod
    def _fetch_http(url: str, method: str, timeout: float, max_bytes: int) -> DiscoveryHttpResponse:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        kwargs: dict[str, Any] = {"timeout": timeout}
        if parsed.scheme == "https":
            kwargs["context"] = ssl._create_unverified_context()  # Local devices commonly use self-signed certificates.
        connection = connection_class(parsed.hostname, port, **kwargs)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            connection.request(method, path, headers={"Accept": "*/*", "User-Agent": "PiSentinel-Discovery/1"})
            response = connection.getresponse()
            headers = {
                key.lower(): value[:500]
                for key, value in response.getheaders()
                if key.lower() in {"content-type", "location", "server", "upgrade"}
            }
            body = response.read(max_bytes) if method != "HEAD" else b""
            return DiscoveryHttpResponse(url=url, status=response.status, headers=headers, body=body)
        finally:
            connection.close()

    def _probe_websocket(self, url: str, timeout: float) -> bool:
        parsed = urlsplit(url)
        if parsed.hostname != self.host or parsed.scheme not in ("ws", "wss"):
            return False
        http_scheme = "https" if parsed.scheme == "wss" else "http"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        connection_class = http.client.HTTPSConnection if http_scheme == "https" else http.client.HTTPConnection
        kwargs: dict[str, Any] = {"timeout": timeout}
        if http_scheme == "https":
            kwargs["context"] = ssl._create_unverified_context()
        connection = connection_class(parsed.hostname, port, **kwargs)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        key = base64.b64encode(hashlib.sha256(url.encode()).digest()[:16]).decode()
        try:
            connection.request("GET", path, headers={
                "Connection": "Upgrade",
                "Upgrade": "websocket",
                "Sec-WebSocket-Key": key,
                "Sec-WebSocket-Version": "13",
                "User-Agent": "PiSentinel-Discovery/1",
            })
            response = connection.getresponse()
            return response.status == 101 and response.getheader("Upgrade", "").lower() == "websocket"
        finally:
            connection.close()

    @staticmethod
    def _parse_json(response: DiscoveryHttpResponse) -> dict[str, Any] | list[Any] | None:
        if "json" not in response.content_type and not response.body.lstrip().startswith((b"{", b"[")):
            return None
        try:
            payload = json.loads(response.text())
        except (json.JSONDecodeError, UnicodeError):
            return None
        return payload if isinstance(payload, (dict, list)) else None

    @staticmethod
    def _is_moonraker_payload(payload: dict[str, Any] | list[Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        result = payload.get("result")
        if not isinstance(result, dict):
            return False
        keys = {str(key).lower() for key in result}
        return bool(keys & {"moonraker_version", "api_version"}) and bool(
            keys & {"components", "klippy_connected", "klippy_state", "hostname"}
        )

    @staticmethod
    def _camera_protocol(content_type: str, url: str) -> str | None:
        lowered = content_type.lower()
        if lowered.startswith("multipart/x-mixed-replace"):
            return "mjpeg"
        if lowered in ("application/vnd.apple.mpegurl", "application/x-mpegurl") or urlsplit(url).path.lower().endswith(".m3u8"):
            return "hls"
        if lowered in ("image/jpeg", "image/png", "image/webp"):
            return "snapshot"
        if lowered.startswith("video/"):
            return "http-video"
        return None

    @staticmethod
    def _http_service(response: DiscoveryHttpResponse, port: int) -> dict[str, Any]:
        return {
            "available": True,
            "state": "available",
            "port": port,
            "status": response.status,
            "content_type": response.content_type or None,
            "server": CrealityK1CDiscoveryService._redact_text(response.headers.get("server")) or None,
        }

    def _build_url(self, scheme: str, port: int, path: str) -> str:
        default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        authority = self.host if default else f"{self.host}:{port}"
        return f"{scheme}://{authority}{path}"

    @staticmethod
    def _port_label(port: int) -> str:
        return {
            80: "HTTP",
            443: "HTTPS",
            8080: "câmera candidata",
            9999: "WebSocket de telemetria",
            7125: "candidato Moonraker",
        }.get(port, "TCP")

    @staticmethod
    def _sanitize_url(url: str) -> str:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        authority = hostname
        if parsed.port:
            authority += f":{parsed.port}"
        path = re.sub(
            r"(?i)(/(?:token|password|secret|api[_-]?key|authorization|session|signature)/)[^/]+",
            lambda match: f"{match.group(1)}<redacted>",
            parsed.path,
        )
        query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            query.append((key, "<redacted>" if key.lower() in _SENSITIVE_QUERY_KEYS else value))
        return urlunsplit((parsed.scheme, authority, path, urlencode(query), ""))

    @staticmethod
    def _redact_text(value: str | None) -> str:
        if not value:
            return ""
        redacted = re.sub(
            r"(?i)\b(token|password|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;&]+",
            lambda match: f"{match.group(1)}=<redacted>",
            value,
        )
        return redacted[:500]
