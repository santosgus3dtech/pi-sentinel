from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
GROUPS = frozenset({"computadores", "consoles", "impressoras_3d", "infraestrutura", "celular", "visitantes", "sem_grupo"})


def validate_host(value: str) -> str:
    """Accept an IP or a DNS hostname, never a URL or command argument."""
    value = value.strip()
    if not value or len(value) > 253:
        raise ValueError("O endereço deve ter entre 1 e 253 caracteres.")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if any(char in value for char in "/\\:@%?#") or re.search(r"\s", value):
        raise ValueError("Use apenas um IP ou nome de host, sem URL, porta ou espaços.")
    candidate = value.rstrip(".")
    if re.fullmatch(r"[\d.]+", candidate):
        raise ValueError("Endereço IP inválido.")
    try:
        candidate = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Nome de host inválido.") from exc
    if len(candidate) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in candidate.split(".")
    ):
        raise ValueError("Nome de host inválido.")
    return candidate


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(f"PISENTINEL_{name}", str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"PISENTINEL_{name} deve ser inteiro.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"PISENTINEL_{name} deve estar entre {minimum} e {maximum}.")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(f"PISENTINEL_{name}")
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"PISENTINEL_{name} deve ser true ou false.")


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "pisentinel.db")
    label: str = "Rede local"
    cidr: str = ""
    interface: str = ""
    ssids: tuple[str, ...] = ()
    interval_seconds: int = 30
    discovery_interval_seconds: int = 300
    retention_days: int = 30
    incident_retention_days: int = 90
    failure_threshold: int = 3
    recovery_threshold: int = 2
    stale_after_seconds: int = 120
    max_hosts: int = 1024
    mock_printer_enabled: bool = False
    printer_controls_enabled: bool = False
    speedtest_executable: str = "speedtest"
    speedtest_timeout_seconds: int = 180
    speedtest_poll_seconds: int = 15
    auth_enabled: bool = False
    auth_session_hours: int = 12
    auth_remember_days: int = 30
    router_host: str = ""
    router_username: str = ""
    router_password: str = field(default="", repr=False)
    router_poll_seconds: int = 30
    router_log_poll_seconds: int = 300

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", Path(self.db_path))
        if not self.label.strip() or len(self.label) > 100:
            raise ValueError("O nome da rede deve ter entre 1 e 100 caracteres.")
        if self.interface and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", self.interface):
            raise ValueError("Interface de rede inválida.")
        if len(self.ssids) > 16 or any(not item.strip() or len(item) > 64 for item in self.ssids):
            raise ValueError("Informe no máximo 16 SSIDs com até 64 caracteres.")
        limits = {
            "interval_seconds": (5, 3600),
            "discovery_interval_seconds": (30, 86400),
            "retention_days": (1, 365),
            "incident_retention_days": (1, 3650),
            "failure_threshold": (1, 20),
            "recovery_threshold": (1, 20),
            "stale_after_seconds": (10, 86400),
            "max_hosts": (1, 1024),
            "speedtest_timeout_seconds": (30, 600),
            "speedtest_poll_seconds": (5, 300),
            "auth_session_hours": (1, 168),
            "auth_remember_days": (1, 365),
            "router_poll_seconds": (15, 3600),
            "router_log_poll_seconds": (60, 3600),
        }
        for key, (minimum, maximum) in limits.items():
            if not minimum <= getattr(self, key) <= maximum:
                raise ValueError(f"{key} deve estar entre {minimum} e {maximum}.")
        if self.cidr:
            network = ipaddress.ip_network(self.cidr, strict=False)
            if network.version != 4:
                raise ValueError("A descoberta requer uma rede IPv4.")
            host_count = network.num_addresses if network.prefixlen >= 31 else network.num_addresses - 2
            if host_count > self.max_hosts:
                raise ValueError("A rede excede PISENTINEL_MAX_HOSTS.")
            object.__setattr__(self, "cidr", str(network))
        if not self.speedtest_executable.strip() or len(self.speedtest_executable) > 512:
            raise ValueError("speedtest_executable inválido.")
        router_values = (self.router_host.strip(), self.router_username.strip(), self.router_password)
        if any(router_values) and not all(router_values):
            raise ValueError("Configure host, usuário e senha do roteador em conjunto.")
        if self.router_host:
            address = ipaddress.ip_address(self.router_host.strip())
            if address.version != 4 or not address.is_private:
                raise ValueError("O roteador deve usar um IPv4 privado.")
            object.__setattr__(self, "router_host", str(address))
        if len(self.router_username) > 100 or len(self.router_password) > 256:
            raise ValueError("Credenciais do roteador excedem o limite permitido.")

    @classmethod
    def from_env(cls) -> Settings:
        interval = _integer("INTERVAL_SECONDS", 30, 5, 3600)
        return cls(
            db_path=Path(os.getenv("PISENTINEL_DB_PATH", str(PROJECT_ROOT / "data" / "pisentinel.db"))),
            label=os.getenv("PISENTINEL_LABEL", "Rede local").strip(),
            cidr=os.getenv("PISENTINEL_CIDR", "").strip(),
            interface=os.getenv("PISENTINEL_INTERFACE", "").strip(),
            ssids=tuple(item.strip() for item in os.getenv("PISENTINEL_SSIDS", "").split(",") if item.strip()),
            interval_seconds=interval,
            discovery_interval_seconds=_integer("DISCOVERY_INTERVAL_SECONDS", 300, 30, 86400),
            retention_days=_integer("RETENTION_DAYS", 30, 1, 365),
            incident_retention_days=_integer("INCIDENT_RETENTION_DAYS", 90, 1, 3650),
            failure_threshold=_integer("FAILURE_THRESHOLD", 3, 1, 20),
            recovery_threshold=_integer("RECOVERY_THRESHOLD", 2, 1, 20),
            stale_after_seconds=_integer("STALE_AFTER_SECONDS", max(120, interval * 3), 10, 86400),
            max_hosts=_integer("MAX_HOSTS", 1024, 1, 1024),
            mock_printer_enabled=_boolean("MOCK_PRINTER_ENABLED", False),
            printer_controls_enabled=_boolean("PRINTER_CONTROLS_ENABLED", False),
            speedtest_executable=os.getenv("PISENTINEL_SPEEDTEST_EXECUTABLE", "speedtest").strip(),
            speedtest_timeout_seconds=_integer("SPEEDTEST_TIMEOUT_SECONDS", 180, 30, 600),
            speedtest_poll_seconds=_integer("SPEEDTEST_POLL_SECONDS", 15, 5, 300),
            auth_enabled=_boolean("AUTH_ENABLED", False),
            auth_session_hours=_integer("AUTH_SESSION_HOURS", 12, 1, 168),
            auth_remember_days=_integer("AUTH_REMEMBER_DAYS", 30, 1, 365),
            router_host=os.getenv("PISENTINEL_ROUTER_HOST", "").strip(),
            router_username=os.getenv("PISENTINEL_ROUTER_USERNAME", "").strip(),
            router_password=os.getenv("PISENTINEL_ROUTER_PASSWORD", ""),
            router_poll_seconds=_integer("ROUTER_POLL_SECONDS", 30, 15, 3600),
            router_log_poll_seconds=_integer("ROUTER_LOG_POLL_SECONDS", 300, 60, 3600),
        )


def get_settings() -> Settings:
    return Settings.from_env()
