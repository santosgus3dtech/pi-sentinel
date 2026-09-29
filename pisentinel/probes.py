"""Bounded network probes; commands never pass through a shell."""
from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
MAC_PATTERN = re.compile(r'^(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$')


class ProbeUnavailable(RuntimeError):
    """Collector/tool failure, rather than evidence that a host is down."""


class TargetProbeFailed(RuntimeError):
    """A bounded target test failed; this is a target observation."""


@dataclass(frozen=True)
class ProbeResult:
    success: bool
    rtt_ms: float | None = None
    error: str | None = None


def run(args: list[str], timeout: float = 8) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, errors='replace',
                                timeout=timeout, env={**os.environ, 'LC_ALL': 'C'},
                                creationflags=0x08000000 if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeUnavailable(f'{args[0]} indisponível: {type(exc).__name__}') from exc
    if result.returncode:
        raise ProbeUnavailable(f'{args[0]}: {result.stderr.strip()[:240] or result.stdout.strip()[:240]}')
    return result.stdout


def validate_network(cidr: str, max_hosts: int = 1024) -> ipaddress.IPv4Network:
    network = ipaddress.ip_network(cidr, strict=False)
    if network.version != 4 or not any(network.subnet_of(item) for item in PRIVATE_NETWORKS):
        raise ValueError('A descoberta exige uma sub-rede IPv4 privada (RFC1918).')
    if network.num_addresses > max_hosts:
        raise ValueError(f'A descoberta está limitada a {max_hosts} endereços.')
    return network


def detect_network(configured_cidr: str = '', configured_interface: str = '', max_hosts: int = 1024) -> dict:
    if platform.system() == 'Windows':
        # The script is fixed; no request or user string is inserted into PowerShell.
        script = "$r=Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Where-Object {$_.NextHop -ne '0.0.0.0'} | Sort-Object RouteMetric | Select-Object -First 1; $a=Get-NetIPAddress -InterfaceIndex $r.InterfaceIndex -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '169.254.*'} | Select-Object -First 1; @{interface=$a.InterfaceAlias; ip=$a.IPAddress; prefix=$a.PrefixLength; gateway=$r.NextHop} | ConvertTo-Json -Compress"
        value = json.loads(run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], 15))
        try:
            ssid_output = run(['netsh', 'wlan', 'show', 'interfaces'])
            match = re.search(r'^\s*SSID\s*:\s*(.+)$', ssid_output, re.M)
            ssid = match.group(1).strip() if match else None
        except ProbeUnavailable:
            ssid = None
    else:
        routes = json.loads(run(['ip', '-j', '-4', 'route', 'show', 'default']))
        routes = [route for route in routes if route.get('dev') and (not configured_interface or route['dev'] == configured_interface)]
        if not routes:
            raise ProbeUnavailable('Nenhuma rota IPv4 padrão disponível na interface configurada.')
        route = min(routes, key=lambda item: item.get('metric', 0))
        addresses = json.loads(run(['ip', '-j', '-4', 'addr', 'show', 'dev', route['dev']]))
        address = next((item for link in addresses for item in link.get('addr_info', []) if item.get('scope') == 'global'), None)
        if not address:
            raise ProbeUnavailable('A interface não tem endereço IPv4 global.')
        value = {'interface': route['dev'], 'ip': address['local'], 'prefix': address['prefixlen'], 'gateway': route.get('gateway')}
        try:
            ssid = run(['iwgetid', route['dev'], '-r']).strip() or None
        except ProbeUnavailable:
            try:
                connection = run(['nmcli', '-g', 'GENERAL.CONNECTION', 'device', 'show', route['dev']]).strip()
                ssid = run(['nmcli', '-g', '802-11-wireless.ssid', 'connection', 'show', connection]).strip() or None
            except ProbeUnavailable:
                ssid = None  # A wired interface cannot establish a Wi-Fi SSID.
    network = validate_network(configured_cidr or f"{value['ip']}/{value['prefix']}", max_hosts)
    if ipaddress.ip_address(value['ip']) not in network:
        raise ProbeUnavailable('O endereço desta máquina está fora da sub-rede autorizada.')
    if configured_interface and configured_interface != value['interface']:
        raise ProbeUnavailable('A interface padrão não corresponde à interface configurada.')
    return {**value, 'cidr': str(network), 'connected_ssid': ssid}


def parse_arp_scan_details(output: str, cidr: str) -> dict[str, dict[str, str | None]]:
    network = validate_network(cidr)
    found = {}
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not MAC_PATTERN.match(parts[1]):
            continue
        try:
            ip = ipaddress.ip_address(parts[0])
        except ValueError:
            continue
        if ip in network and ip not in (network.network_address, network.broadcast_address):
            vendor = parts[2].strip()[:160] if len(parts) > 2 else None
            if vendor and ("unknown" in vendor.casefold() or "private" in vendor.casefold()):
                vendor = None
            found[str(ip)] = {"mac": parts[1].lower().replace('-', ':'), "vendor": vendor}
    return found


def parse_arp_scan(output: str, cidr: str) -> dict[str, str]:
    return {ip: str(details["mac"]) for ip, details in parse_arp_scan_details(output, cidr).items()}


def windows_arp(ip: str) -> str | None:
    destination = int.from_bytes(socket.inet_aton(ip), 'little')
    output = (ctypes.c_ubyte * 8)()
    length = ctypes.c_ulong(6)
    status = ctypes.windll.iphlpapi.SendARP(destination, 0, ctypes.byref(output), ctypes.byref(length))
    if status == 0 and length.value == 6:
        return ':'.join(f'{byte:02x}' for byte in output[:6])
    return None


def windows_arp_bounded(addresses: list[str], timeout: float = 65) -> dict[str, str]:
    """Isolate synchronous SendARP calls so the collector always has a deadline."""
    payload = json.dumps(addresses)
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pisentinel.probes', '--windows-arp-worker'],
            input=payload, capture_output=True, text=True, timeout=timeout,
            creationflags=0x08000000,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeUnavailable('A descoberta ARP excedeu o tempo limite.') from exc
    except OSError as exc:
        raise ProbeUnavailable('Não foi possível iniciar a descoberta ARP.') from exc
    if result.returncode:
        raise ProbeUnavailable(f'Descoberta ARP falhou: {result.stderr.strip()[:240]}')
    try:
        found = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeUnavailable('A descoberta ARP devolveu uma resposta inválida.') from exc
    if not isinstance(found, dict):
        raise ProbeUnavailable('A descoberta ARP devolveu uma resposta inválida.')
    return {str(ipaddress.ip_address(ip)): mac for ip, mac in found.items() if MAC_PATTERN.match(mac)}


def arp_discover_details(network: dict, hosts: list[str] | None = None) -> dict[str, dict[str, str | None]]:
    cidr = validate_network(network['cidr'])
    if hosts is not None:
        hosts = [ip for ip in hosts if ip and ipaddress.ip_address(ip) in cidr]
        if not hosts:
            return {}
    if platform.system() == 'Windows':
        addresses = hosts if hosts is not None else [str(ip) for ip in cidr.hosts()]
        return {ip: {"mac": mac, "vendor": None}
                for ip, mac in windows_arp_bounded(addresses, timeout=20 if hosts is not None else 65).items()}
    output = run(['arp-scan', '--plain', '--ignoredups', '--retry=2', '--timeout=500',
                  '--bandwidth=64000', '--interface', network['interface'], *(hosts or [str(cidr)])], timeout=45)
    return parse_arp_scan_details(output, str(cidr))


def arp_discover(network: dict, hosts: list[str] | None = None) -> dict[str, str]:
    return {ip: str(details["mac"]) for ip, details in arp_discover_details(network, hosts).items()}


def reverse_name(ip: str) -> str | None:
    try:
        import dns.reversename
        import dns.resolver
        answer = dns.resolver.resolve(dns.reversename.from_address(ip), 'PTR', lifetime=1.2)
        return str(answer[0]).rstrip('.')[:253]
    except Exception:
        return None


def ping(host: str) -> ProbeResult:
    args = ['ping', '-n', '1', '-w', '1200', host] if platform.system() == 'Windows' else ['ping', '-n', '-c', '1', '-W', '2', host]
    try:
        result = subprocess.run(args, capture_output=True, text=True, errors='replace', timeout=4,
                                env={**os.environ, 'LC_ALL': 'C'}, creationflags=0x08000000 if os.name == 'nt' else 0)
    except FileNotFoundError as exc:
        raise ProbeUnavailable('O comando ping não está instalado.') from exc
    except subprocess.TimeoutExpired:
        return ProbeResult(False, error='Tempo limite ICMP')
    combined = result.stdout + result.stderr
    if any(message in combined.lower() for message in ('operation not permitted', 'permission denied', 'socket: permission')):
        raise ProbeUnavailable('Sem permissão para executar testes ICMP.')
    # Windows may return code 0 for a router's Destination Unreachable reply.
    timing = re.search(r'(?:time|tempo|tiempo|temps)\s*([=<])\s*(\d+(?:[.,]\d+)?)\s*ms', combined, re.I)
    ttl = re.search(r'\bttl[= ]\d+', combined, re.I)
    succeeded = result.returncode == 0 and (bool(ttl) if platform.system() == 'Windows' else True)
    rtt = float(timing.group(2).replace(',', '.')) if timing and succeeded else None
    if timing and timing.group(1) == '<':
        rtt = 0.5
    return ProbeResult(succeeded, rtt, None if succeeded else 'Sem resposta ICMP')


def resolve_host(host: str) -> str:
    """Resolve a target under an explicit deadline before socket/ping probes."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        import dns.resolver
        answer = dns.resolver.resolve(host, 'A', lifetime=2, search=False)
        return str(ipaddress.ip_address(str(answer[0])))
    except ModuleNotFoundError as exc:
        raise ProbeUnavailable('O módulo de testes DNS não está instalado.') from exc
    except Exception as exc:
        raise TargetProbeFailed(f'Não foi possível resolver {host}: {type(exc).__name__}') from exc


def probe_target(target: dict) -> ProbeResult:
    start = time.perf_counter()
    try:
        if target['kind'] == 'icmp':
            return ping(resolve_host(target['host']))
        elif target['kind'] == 'tcp':
            address = resolve_host(target['host'])
            with socket.create_connection((address, target.get('port') or 443), timeout=2):
                pass
        elif target['kind'] == 'dns':
            import dns.resolver
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = [resolve_host(target['host'])]
            resolver.resolve(target.get('query') or 'example.com', 'A', lifetime=2, search=False)
        else:
            raise ProbeUnavailable('Tipo de teste desconhecido.')
        return ProbeResult(True, round((time.perf_counter() - start) * 1000, 3))
    except ProbeUnavailable:
        raise
    except Exception as exc:
        return ProbeResult(False, error=str(exc)[:240] or f'{target["kind"].upper()}: {type(exc).__name__}')


def _windows_worker() -> int:
    try:
        addresses = json.loads(sys.stdin.read(65537))
        if not isinstance(addresses, list) or len(addresses) > 1024:
            raise ValueError('invalid address list')
        validated = [str(ipaddress.IPv4Address(item)) for item in addresses]
        with ThreadPoolExecutor(max_workers=24) as pool:
            results = list(pool.map(windows_arp, validated))
        print(json.dumps({ip: mac for ip, mac in zip(validated, results) if mac}))
        return 0
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__' and sys.argv[1:] == ['--windows-arp-worker']:
    raise SystemExit(_windows_worker())
