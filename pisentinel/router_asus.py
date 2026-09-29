"""Read-only ASUSWRT telemetry used by the PiSentinel collector.

The client deliberately exposes only GET endpoints and the read-only
``update.cgi`` traffic query used by ASUS' own Traffic Monitor page.  It never
calls ``apply.cgi`` or any endpoint that changes router configuration.
"""
from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


class RouterError(RuntimeError):
    """A bounded router request or parser failed."""


MAC_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$", re.I)


def _float(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return round(parsed, 3) if math.isfinite(parsed) and parsed >= 0 else None


def _integer(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _duration_seconds(value: Any) -> int | None:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", str(value or "").strip())
    if not match:
        return None
    hours, minutes, seconds = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _json_value(source: str, marker: str) -> Any:
    """Return the first balanced JSON object/array following *marker*."""
    position = source.find(marker)
    if position < 0:
        raise RouterError(f"Campo ASUSWRT ausente: {marker}")
    start_candidates = [value for value in (source.find("[", position), source.find("{", position)) if value >= 0]
    if not start_candidates:
        raise RouterError(f"Valor ASUSWRT inválido: {marker}")
    start = min(start_candidates)
    opening = source[start]
    closing = "]" if opening == "[" else "}"
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(source[start:index + 1])
                except json.JSONDecodeError as exc:
                    raise RouterError(f"JSON ASUSWRT inválido: {marker}") from exc
    raise RouterError(f"Valor ASUSWRT incompleto: {marker}")


def parse_client_inventory(source: str) -> list[dict[str, Any]]:
    declaration = re.search(r"\bfromNetworkmapd\s*:\s*\[", source)
    if not declaration:
        raise RouterError("Inventário de clientes ASUSWRT ausente.")
    records = _json_value(source[declaration.start():], "fromNetworkmapd")
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        raise RouterError("Inventário de clientes ASUSWRT inválido.")
    clients: list[dict[str, Any]] = []
    for key, raw in records[0].items():
        if not MAC_RE.fullmatch(str(key)) or not isinstance(raw, dict):
            continue
        mac = str(raw.get("mac") or key).lower()
        if not MAC_RE.fullmatch(mac):
            continue
        wireless_code = str(raw.get("isWL") or "0")
        interface = {"0": "wired", "1": "wifi_2_4", "2": "wifi_5"}.get(wireless_code, "unknown")
        rssi = _integer(raw.get("rssi"))
        if interface == "wired" or rssi == 0:
            rssi = None
        ip = str(raw.get("ip") or "").strip() or None
        try:
            ip = str(ipaddress.ip_address(ip)) if ip else None
        except ValueError:
            ip = None
        name = str(raw.get("nickName") or raw.get("name") or "").strip()[:253] or None
        vendor = str(raw.get("vendor") or "").strip()[:160] or None
        clients.append({
            "mac": mac,
            "ip": ip,
            "hostname": name,
            "vendor": vendor,
            "online": str(raw.get("isOnline")) == "1",
            "interface": interface,
            "ssid": str(raw.get("ssid") or "").strip()[:64] or None,
            "rssi_dbm": rssi,
            "tx_rate_mbps": _float(raw.get("curTx")),
            "rx_rate_mbps": _float(raw.get("curRx")),
            "connected_seconds": _duration_seconds(raw.get("wlConnectTime")),
            "ip_method": str(raw.get("ipMethod") or "").strip()[:20] or None,
            "internet_allowed": str(raw.get("internetState")) != "0",
            "router_device_type": _integer(raw.get("type")),
        })
    return clients


_STATION_RE = re.compile(
    r"^\s*((?:[0-9A-F]{2}:){5}[0-9A-F]{2})\s+Yes\s+Yes\s+"
    r"(-?\d+)dBm\s+(\S+)\s+(Yes|No)\s+(Yes|No)\s+(Yes|No)\s+(Yes|No)\s+"
    r"(\d+)\s+(\d+)M\s+([\d.]+)M\s+([\d.]+)M\s+(\d+:\d{2}:\d{2})\s*$",
    re.I,
)


def parse_wireless_log(source: str) -> dict[str, Any]:
    source = source.replace("%25", "%").replace("%5F", "_").replace("%5f", "_")
    starts = list(re.finditer(r'SSID:\s*"([^"]*)"', source))
    bands: list[dict[str, Any]] = []
    stations: dict[str, dict[str, Any]] = {}
    for number, match in enumerate(starts):
        section = source[match.start(): starts[number + 1].start() if number + 1 < len(starts) else len(source)]
        chanspec = re.search(r"Chanspec:\s*(2\.4GHz|5GHz)\s+channel\s+(\d+)\s+(\d+)MHz", section, re.I)
        band = "2.4 GHz" if chanspec and chanspec.group(1).startswith("2.4") else "5 GHz"
        noise = re.search(r"noise:\s*(-?\d+)\s*dBm", section, re.I)
        primary = re.search(r"Primary channel:\s*(\d+)", section, re.I)
        utilization = re.search(r"Channel Utilization:\s*[^\n]*\((\d+)\s*%\)", section, re.I)
        interference = re.search(r"Interference Level:\s*([^\r\n]+)", section, re.I)
        bands.append({
            "band": band,
            "ssid": match.group(1),
            "channel": int(primary.group(1)) if primary else (int(chanspec.group(2)) if chanspec else None),
            "channel_width_mhz": int(chanspec.group(3)) if chanspec else None,
            "noise_dbm": int(noise.group(1)) if noise else None,
            "utilization_pct": int(utilization.group(1)) if utilization else None,
            "interference": interference.group(1).strip()[:40] if interference else None,
        })
        for line in section.splitlines():
            row = _STATION_RE.match(line)
            if not row:
                continue
            mac, rssi, phy, psm, sgi, stbc, mubf, nss, width, tx_rate, rx_rate, connected = row.groups()
            stations[mac.lower()] = {
                "interface": "wifi_2_4" if band == "2.4 GHz" else "wifi_5",
                "ssid": match.group(1),
                "rssi_dbm": int(rssi),
                "phy_mode": phy.lower(),
                "power_save": psm.lower() == "yes",
                "short_guard_interval": sgi.lower() == "yes",
                "stbc": stbc.lower() == "yes",
                "mu_beamforming": mubf.lower() == "yes",
                "spatial_streams": int(nss),
                "channel_width_mhz": int(width),
                "tx_rate_mbps": float(tx_rate),
                "rx_rate_mbps": float(rx_rate),
                "connected_seconds": _duration_seconds(connected),
            }
    return {"bands": bands, "stations": stations}


def parse_dhcp_leases(source: str) -> dict[str, dict[str, Any]]:
    textarea = re.search(r"<textarea\b[^>]*>(.*?)</textarea>", source, re.I | re.S)
    text = html.unescape(textarea.group(1) if textarea else source)
    leases: dict[str, dict[str, Any]] = {}
    row_pattern = re.compile(
        r"^\s*(\S+)\s+(\d{1,3}(?:\.\d{1,3}){3})\s+((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\s+(\d+:\d{2}:\d{2})\s*$",
        re.I,
    )
    for line in text.splitlines():
        match = row_pattern.match(line)
        if not match:
            continue
        hostname, ip, mac, expires = match.groups()
        leases[mac.lower()] = {
            "hostname": None if hostname == "*" else hostname[:253],
            "ip": ip,
            "dhcp_expires_seconds": _duration_seconds(expires),
        }
    return leases


def parse_cpu_memory(source: str) -> dict[str, Any]:
    cpu = _json_value(source, "cpuInfo =")
    memory_assignment = re.search(r"\bmemInfo\s*=\s*\{", source)
    if not memory_assignment:
        raise RouterError("Telemetria de memória ASUSWRT ausente.")
    memory = _json_value(source[memory_assignment.start():], "memInfo =")
    cpu_percent: list[float] = []
    if isinstance(cpu, dict):
        for key in sorted(cpu):
            values = cpu[key]
            total = _float(values.get("total")) if isinstance(values, dict) else None
            usage = _float(values.get("usage")) if isinstance(values, dict) else None
            cpu_percent.append(round(usage * 100 / total, 1) if total and usage is not None else 0.0)
    total_kb = _integer(memory.get("total")) if isinstance(memory, dict) else None
    used_kb = _integer(memory.get("used")) if isinstance(memory, dict) else None
    free_kb = _integer(memory.get("free")) if isinstance(memory, dict) else None
    return {
        "cpu_percent": cpu_percent,
        "memory_total_mb": round(total_kb / 1024, 1) if total_kb is not None else None,
        "memory_used_mb": round(used_kb / 1024, 1) if used_kb is not None else None,
        "memory_free_mb": round(free_kb / 1024, 1) if free_kb is not None else None,
        "memory_used_pct": round(used_kb * 100 / total_kb, 1) if total_kb and used_kb is not None else None,
    }


def parse_traffic_counters(source: str) -> dict[str, dict[str, int]]:
    results: dict[str, dict[str, int]] = {}
    for name, rx, tx in re.findall(r"'([A-Z0-9_]+)'\s*:\s*\{rx:(0x[0-9a-f]+|\d+),tx:(0x[0-9a-f]+|\d+)\}", source, re.I):
        results[name.lower()] = {"rx_bytes": int(rx, 0), "tx_bytes": int(tx, 0)}
    if not results:
        raise RouterError("Contadores de tráfego ASUSWRT indisponíveis.")
    return results


_MONTHS = {name: index for index, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_WIFI_EVENT_RE = re.compile(
    r"^(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<clock>\d{2}:\d{2}:\d{2})\s+"
    r"wlceventd:.*?:\s+\S+:\s+(?P<event>Auth|Assoc|ReAssoc|Disassoc|Deauth_ind)\s+"
    r"(?P<mac>(?:[0-9A-F]{2}:){5}[0-9A-F]{2})(?P<tail>.*)$",
    re.I,
)


def parse_wireless_events(source: str, *, now: datetime | None = None,
                          limit: int = 20_000) -> list[dict[str, Any]]:
    """Extract connection events without retaining the router's raw system log."""
    textareas = re.findall(r"<textarea\b[^>]*>(.*?)</textarea>", source, re.I | re.S)
    text = html.unescape(next((item for item in textareas if "wlceventd" in item), source))
    local_now = now or datetime.now().astimezone()
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=timezone.utc)
    events: list[dict[str, Any]] = []
    labels = {
        "auth": "authentication", "assoc": "associated", "reassoc": "reassociated",
        "disassoc": "disconnected", "deauth_ind": "deauthenticated",
    }
    for line in text.splitlines():
        match = _WIFI_EVENT_RE.match(line.strip())
        if not match:
            continue
        month = _MONTHS.get(match.group("month").title())
        if not month:
            continue
        hour, minute, second = map(int, match.group("clock").split(":"))
        try:
            occurred = datetime(local_now.year, month, int(match.group("day")), hour, minute, second,
                                tzinfo=local_now.tzinfo)
        except ValueError:
            continue
        if occurred - local_now > timedelta(days=1):
            occurred = occurred.replace(year=occurred.year - 1)
        tail = match.group("tail")
        reason = re.search(r"reason:\s*(.*?)(?:,\s*rssi:|$)", tail, re.I)
        rssi = re.search(r"rssi:\s*(-?\d+)", tail, re.I)
        rssi_value = int(rssi.group(1)) if rssi and int(rssi.group(1)) != 0 else None
        event = labels[match.group("event").lower()]
        mac = match.group("mac").lower()
        reason_value = reason.group(1).strip()[:160] if reason else None
        timestamp = occurred.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        identity = f"{timestamp}|{mac}|{event}|{reason_value or ''}|{rssi_value or ''}"
        events.append({
            "event_key": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            "timestamp": timestamp, "mac": mac, "event": event,
            "reason": reason_value, "rssi_dbm": rssi_value,
        })
    return events[-limit:]


@dataclass(slots=True)
class AsusRouterClient:
    host: str
    username: str
    password: str
    timeout: float = 4.0
    opener_factory: Callable[..., Any] = build_opener
    base_url: str = field(init=False)
    _opener: Any = field(init=False, repr=False)
    _authenticated: bool = field(init=False, default=False, repr=False)
    _http_id: str | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.host.strip())
        except ValueError as exc:
            raise ValueError("O roteador ASUS deve ser configurado com um IP privado.") from exc
        if address.version != 4 or not address.is_private:
            raise ValueError("O roteador ASUS deve ser configurado com um IPv4 privado.")
        self.host = str(address)
        self.username = self.username.strip()
        if not self.username or not self.password:
            raise ValueError("Usuário e senha do roteador são obrigatórios.")
        self.base_url = f"http://{self.host}"
        self._opener = self.opener_factory(HTTPCookieProcessor(CookieJar()))
        self._authenticated = False
        self._http_id: str | None = None

    @staticmethod
    def _looks_like_login(content: str) -> bool:
        return "login_authorization" in content and "login_passwd" in content

    def _request(self, path: str, *, data: dict[str, str] | None = None,
                 referer: str | None = None, max_bytes: int = 2_000_000) -> str:
        headers = {
            "User-Agent": "PiSentinel/0.6 (read-only ASUSWRT telemetry)",
            "Accept": "text/html,application/json,text/plain,*/*",
            "Cache-Control": "no-cache",
        }
        if referer:
            headers["Referer"] = self.base_url + referer
        payload = urlencode(data).encode("ascii") if data is not None else None
        request = Request(self.base_url + path, data=payload, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.read(max_bytes).decode("utf-8", "replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RouterError(f"Roteador ASUS indisponível: {type(exc).__name__}") from exc

    def login(self) -> None:
        authorization = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        content = self._request("/login.cgi", data={
            "group_id": "",
            "action_mode": "",
            "action_script": "",
            "action_wait": "5",
            "current_page": "Main_Login.asp",
            "next_page": "index.asp",
            "login_authorization": authorization,
            "login_captcha": "",
        }, referer="/Main_Login.asp")
        if self._looks_like_login(content):
            raise RouterError("O roteador ASUS recusou a autenticação.")
        self._authenticated = True

    def _get(self, path: str, *, referer: str) -> str:
        if not self._authenticated:
            self.login()
        content = self._request(path, referer=referer)
        if self._looks_like_login(content):
            self._authenticated = False
            self.login()
            content = self._request(path, referer=referer)
        if self._looks_like_login(content):
            raise RouterError("A sessão ASUSWRT expirou.")
        return content

    def _traffic(self) -> dict[str, dict[str, int]] | None:
        try:
            if not self._http_id:
                hook = quote("nvram_get(http_id)", safe="()")
                response = self._get(f"/appGet.cgi?hook={hook}", referer="/Main_TrafficMonitor_realtime.asp")
                value = json.loads(response).get("http_id")
                if not isinstance(value, str) or not value.startswith("TID") or len(value) > 128:
                    return None
                self._http_id = value
            content = self._request("/update.cgi", data={"output": "netdev", "_http_id": self._http_id},
                                    referer="/Main_TrafficMonitor_realtime.asp")
            return parse_traffic_counters(content)
        except (RouterError, ValueError, json.JSONDecodeError):
            self._http_id = None
            return None

    def snapshot(self) -> dict[str, Any]:
        started = time.monotonic()
        index = self._get("/index.asp", referer="/index.asp")
        state_source = self._get("/state.js", referer="/index.asp")
        clients_source = self._get("/client_function.js", referer="/index.asp")
        wireless_source = self._get("/wl_log.asp", referer="/Main_WStatus_Content.asp")
        leases_source = self._get("/Main_DHCPStatus_Content.asp", referer="/Main_DHCPStatus_Content.asp")
        cpu_source = self._get(f"/cpu_ram_status.asp?_={int(time.time() * 1000)}",
                               referer="/device-map/router_status.asp")

        clients = parse_client_inventory(clients_source)
        wireless = parse_wireless_log(wireless_source)
        leases = parse_dhcp_leases(leases_source)
        for client in clients:
            mac = client["mac"]
            client.update(wireless["stations"].get(mac, {}))
            lease = leases.get(mac, {})
            client["dhcp_expires_seconds"] = lease.get("dhcp_expires_seconds")
            client["hostname"] = client.get("hostname") or lease.get("hostname")
            client["ip"] = client.get("ip") or lease.get("ip")

        plain_index = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", index)))
        model = re.search(r"\b(RT-[A-Z0-9-]+)\b", plain_index)
        firmware_match = re.search(r"\bfirmver\s*=\s*['\"]([^'\"]+)", state_source)
        build_match = re.search(r"\bbuildno\s*=\s*['\"]([^'\"]+)", state_source)
        extend_match = re.search(r"\bextendno\s*=\s*['\"]([^'\"]+)", state_source)
        firmware = None
        if firmware_match and build_match:
            firmware = f"{firmware_match.group(1)}.{build_match.group(1)}"
            if extend_match and extend_match.group(1):
                firmware += f"_{extend_match.group(1).split('-g', 1)[0]}"
        status = {
            "available": True,
            "host": self.host,
            "manufacturer": "ASUS",
            "model": model.group(1) if model else None,
            "firmware": firmware,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "cpu_memory": parse_cpu_memory(cpu_source),
            "bands": wireless["bands"],
            "traffic_counters": self._traffic(),
            "client_counts": {
                "online": sum(bool(item["online"]) for item in clients),
                "known": len(clients),
                "wired": sum(item["online"] and item["interface"] == "wired" for item in clients),
                "wifi_2_4": sum(item["online"] and item["interface"] == "wifi_2_4" for item in clients),
                "wifi_5": sum(item["online"] and item["interface"] == "wifi_5" for item in clients),
            },
        }
        return {"router": status, "clients": clients}

    def wireless_events(self) -> list[dict[str, Any]]:
        # The general-log page loads its content asynchronously from this
        # read-only nvram_dump hook; its textarea is empty in the initial HTML.
        hook = quote('nvram_dump("syslog.log","syslog.sh")', safe="()")
        if not self._authenticated:
            self.login()
        source = self._request(f"/appGet.cgi?hook={hook}",
                               referer="/Main_LogStatus_Content.asp", max_bytes=4_000_000)
        if self._looks_like_login(source):
            self._authenticated = False
            self.login()
            source = self._request(f"/appGet.cgi?hook={hook}",
                                   referer="/Main_LogStatus_Content.asp", max_bytes=4_000_000)
        if self._looks_like_login(source):
            raise RouterError("A sessão ASUSWRT expirou.")
        return parse_wireless_events(source)
