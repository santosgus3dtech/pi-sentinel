"""Conservative device identification from local, already-observed evidence."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
import unicodedata
from typing import Any, Mapping


OUI_PATHS = (
    Path("/usr/share/arp-scan/ieee-oui.txt"),
    Path("/usr/share/ieee-data/oui.txt"),
)

# Configured hardware: these entries also identify historical rows while a
# printer is offline and therefore absent from the current ARP scan.
KNOWN_DEVICES = {
    "fc:ee:28:00:00:01": ("Creality K1C", "Creality", "impressoras_3d"),
    "9c:13:9e:00:00:01": ("Bambu Lab A1", "Bambu Lab", "impressoras_3d"),
}


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    auto_name: str | None
    auto_group: str
    vendor: str | None
    source: str


def _searchable(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def _clean_vendor(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip(" \t,-")[:160]
    if not cleaned or "unknown" in cleaned.casefold() or "private" in cleaned.casefold():
        return None
    return cleaned


def _normalize_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    normalized = mac.strip().lower().replace("-", ":")
    return normalized if re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", normalized) else None


@lru_cache(maxsize=4)
def _load_oui_file(path: str) -> dict[str, str]:
    vendors: dict[str, str] = {}
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = re.match(r"^\s*([0-9A-Fa-f]{6})\s+(.+?)\s*$", line)
                if match:
                    vendor = _clean_vendor(match.group(2))
                    if vendor:
                        vendors[match.group(1).upper()] = vendor
    except OSError:
        pass
    return vendors


def lookup_mac_vendor(mac: str | None, oui_path: str | Path | None = None) -> str | None:
    normalized = _normalize_mac(mac)
    if not normalized:
        return None
    # A locally administered/randomized MAC deliberately hides its manufacturer.
    if int(normalized[:2], 16) & 0x02:
        return None
    prefix = normalized.replace(":", "")[:6].upper()
    paths = (Path(oui_path),) if oui_path is not None else OUI_PATHS
    for path in paths:
        vendor = _load_oui_file(str(path)).get(prefix)
        if vendor:
            return vendor
    return None


def _hostname_group(hostname: str | None) -> str | None:
    text = _searchable(hostname)
    rules = (
        ("impressoras_3d", ("bambu", "creality", "k1c", "ender", "prusa", "klipper", "octoprint", "voron", "ultimaker", "anycubic")),
        ("infraestrutura", ("router", "roteador", "gateway", "access-point", "accesspoint", "rt-ax", "unifi", "mikrotik")),
        ("consoles", ("nintendo", "switch", "xbox", "playstation", "ps4", "ps5", "steam-deck", "steamdeck")),
        ("celular", ("iphone", "android", "galaxy", "pixel", "redmi", "poco", "oneplus", "motorola", "smartphone", "celular", "samsung")),
        ("computadores", ("server", "desktop", "notebook", "laptop", "macbook", "imac", "windows", "linux")),
    )
    return next((group for group, terms in rules if any(term in text for term in terms)), None)


def _vendor_group(vendor: str | None) -> str | None:
    text = _searchable(vendor)
    rules = (
        ("impressoras_3d", ("bambu", "creality", "prusa", "ultimaker", "anycubic")),
        ("infraestrutura", ("asustek", "ubiquiti", "mikrotik", "netgear", "tp-link", "aruba networks", "zyxel", "arris")),
        ("consoles", ("nintendo",)),
        ("computadores", ("giga-byte", "gigabyte", "dell", "lenovo", "intel corporate")),
    )
    return next((group for group, terms in rules if any(term in text for term in terms)), None)


def _short_vendor(vendor: str) -> str:
    shortened = re.split(r"\s+(?:co\.?|corporation|corp\.?|inc\.?|ltd\.?)\b|,", vendor, maxsplit=1, flags=re.I)[0]
    return shortened.strip()[:60] or vendor[:60]


def identify_device(*, ip: str | None, mac: str | None, hostname: str | None,
                    vendor: str | None = None, printer: Mapping[str, Any] | None = None) -> DeviceIdentity:
    normalized_mac = _normalize_mac(mac)
    known = KNOWN_DEVICES.get(normalized_mac or "")
    vendor = _clean_vendor(vendor) or lookup_mac_vendor(normalized_mac)

    if printer:
        manufacturer = str(printer.get("manufacturer") or "").strip()
        model = str(printer.get("model") or "").strip()
        auto_name = str(printer.get("name") or f"{manufacturer} {model}").strip()[:100] or None
        return DeviceIdentity(auto_name, "impressoras_3d", vendor or manufacturer or None, "cadastro_impressora")
    if known:
        return DeviceIdentity(known[0], known[2], vendor or known[1], "mac_conhecido")

    clean_hostname = (hostname or "").strip().rstrip(".")[:253] or None
    if clean_hostname and clean_hostname.casefold().endswith(".local"):
        clean_hostname = clean_hostname[:-6].rstrip(".") or None
    auto_group = _hostname_group(clean_hostname) or _vendor_group(vendor) or "sem_grupo"
    if clean_hostname:
        auto_name = clean_hostname[:100]
        source = "hostname_oui" if vendor else "hostname"
    elif vendor and normalized_mac:
        auto_name = f"{_short_vendor(vendor)} · {normalized_mac[-5:].upper()}"[:100]
        source = "oui"
    else:
        auto_name = None
        source = "nao_identificado"
    return DeviceIdentity(auto_name, auto_group, vendor, source)
