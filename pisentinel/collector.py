"""Independent, low-rate collector. Run: python -m pisentinel.collector."""
from __future__ import annotations

import argparse
import contextlib
import ipaddress
import logging
import os
from pathlib import Path
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .config import Settings
from .db import Database
from .device_identification import identify_device, lookup_mac_vendor
from .device_enrichment import scan_device_services
from .probes import (ProbeUnavailable, arp_discover, arp_discover_details, detect_network,
                     ping, probe_target, reverse_name)
from .router_asus import AsusRouterClient

LOG = logging.getLogger('pisentinel.collector')


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextlib.contextmanager
def collector_lock(path: Path):
    """One collector per database, with OS-managed release on process exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    try:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'0')
                handle.flush()
                handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


class Collector:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path, failure_threshold=settings.failure_threshold,
                           recovery_threshold=settings.recovery_threshold,
                           retention_days=settings.retention_days,
                           incident_retention_days=settings.incident_retention_days)
        self.network: dict = {}
        self.next_discovery = 0.0
        self.next_router_poll = 0.0
        self.next_router_log_poll = 0.0
        self.router = (AsusRouterClient(settings.router_host, settings.router_username,
                                        settings.router_password)
                       if settings.router_host else None)
        self.stopped = False

    def configure_network(self) -> None:
        detected = detect_network(self.settings.cidr, self.settings.interface, self.settings.max_hosts)
        ssid = detected.get('connected_ssid')
        if ssid and self.settings.ssids and ssid not in self.settings.ssids:
            raise ProbeUnavailable('A máquina está em outro Wi-Fi; coleta pausada para respeitar a rede configurada.')
        if not ssid and not self.settings.cidr:
            raise ProbeUnavailable('Conexão sem SSID detectável: configure PISENTINEL_CIDR para autorizar a rede cabeada.')
        self.network = {**detected, 'label': self.settings.label, 'ssids': list(self.settings.ssids)}
        self.db.set_meta('network', self.network)

    def seed_targets(self) -> None:
        if self.db.get_meta('targets_seeded', False):
            return
        targets = []
        if self.network.get('gateway'):
            targets.append({'name': 'Roteador', 'host': self.network['gateway'], 'kind': 'icmp', 'scope': 'local'})
        targets += [
            {'name': 'Internet · Cloudflare', 'host': '1.1.1.1', 'kind': 'icmp', 'scope': 'external'},
            {'name': 'DNS · Cloudflare', 'host': '1.1.1.1', 'kind': 'dns', 'query': 'example.com', 'scope': 'external'},
            {'name': 'Internet · TCP 443', 'host': '1.1.1.1', 'kind': 'tcp', 'port': 443, 'scope': 'external'},
        ]
        for target in targets:
            self.db.create_target(**target)
        self.db.set_meta('targets_seeded', True)

    def discover(self) -> dict[str, str]:
        self.db.set_meta('discovery_running', True)
        try:
            details = arp_discover_details(self.network)
            found = {ip: str(item["mac"]) for ip, item in details.items()}
            with ThreadPoolExecutor(max_workers=12) as pool:
                names = list(pool.map(reverse_name, found))
            for (ip, mac), hostname in zip(found.items(), names):
                self.db.upsert_device(ip, mac=mac, hostname=hostname, now=utc_now())
            self.identify_inventory(details)
            self.db.set_meta('last_discovery', utc_now())
            self.db.set_meta('discovery_error', None)
            LOG.info('Descoberta concluída: %s respostas ARP em %s', len(found), self.network['cidr'])
            return found
        except Exception as exc:
            self.db.set_meta('discovery_error', str(exc)[:500])
            raise
        finally:
            self.db.set_meta('discovery_running', False)

    def identify_inventory(self, discovered: dict[str, dict[str, str | None]]) -> None:
        printers = {item["device_id"]: item for item in self.db.list_printers() if item.get("device_id")}
        for device in self.db.list_devices():
            observed = discovered.get(device.get("ip") or "", {})
            vendor = observed.get("vendor") or device.get("vendor") or lookup_mac_vendor(device.get("mac"))
            identity = identify_device(
                ip=device.get("ip"), mac=device.get("mac"), hostname=device.get("hostname"),
                vendor=vendor, printer=printers.get(device["id"]),
            )
            self.db.set_device_identification(
                device["id"], auto_name=identity.auto_name, auto_group=identity.auto_group,
                vendor=identity.vendor, source=identity.source, now=utc_now(),
            )

    def enrich_inventory(self, present_ips: set[str], *, force: bool = False) -> None:
        """Refresh a bounded common-service inventory for currently present devices."""
        now = datetime.now(timezone.utc)
        for device in self.db.list_devices():
            ip = device.get("ip")
            if not ip or ip not in present_ips:
                continue
            checked = device.get("services_checked_at")
            if checked and not force:
                try:
                    previous = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
                    if (now - previous).total_seconds() < 1800:
                        continue
                except (TypeError, ValueError):
                    pass
            try:
                services = scan_device_services(ip)
                self.db.set_device_services(device["id"], services, checked_at=now)
                LOG.info("Serviços de %s: %s porta(s) comum(ns) aberta(s)", ip, len(services))
            except Exception as exc:
                self.db.set_device_services(device["id"], [], checked_at=now,
                                            error=f"{type(exc).__name__}: {exc}")
                LOG.warning("Enriquecimento de %s falhou: %s", ip, exc)

    def collect_router(self) -> None:
        if self.router is None or time.monotonic() < self.next_router_poll:
            return
        self.next_router_poll = time.monotonic() + self.settings.router_poll_seconds
        try:
            snapshot = self.router.snapshot()
            self.db.set_router_snapshot(snapshot["router"], snapshot["clients"], now=utc_now())
            LOG.info("Roteador ASUS: %s cliente(s) online", snapshot["router"]["client_counts"]["online"])
            if time.monotonic() >= self.next_router_log_poll:
                self.next_router_log_poll = time.monotonic() + self.settings.router_log_poll_seconds
                try:
                    events = self.router.wireless_events()
                    inserted = self.db.record_router_wifi_events(events)
                    LOG.info("Roteador ASUS: %s evento(s) Wi-Fi novo(s)", inserted)
                except Exception as exc:
                    LOG.warning("Eventos Wi-Fi do roteador indisponíveis: %s", exc)
        except Exception as exc:
            # Router telemetry is supplemental. It must never stop ARP, probes,
            # incidents, printers or speed tests.
            self.db.set_router_error(f"{type(exc).__name__}: {exc}", now=utc_now())
            LOG.warning("Telemetria do roteador indisponível: %s", exc)

    def cycle(self) -> None:
        self.db.set_meta('collector_cycle_started', utc_now())
        # Check the allowed network on every cycle, including after Wi-Fi changes.
        self.configure_network()
        self.collect_router()
        requested = self.db.consume_discovery_request(now=utc_now())
        if not self.network or time.monotonic() >= self.next_discovery or requested:
            self.seed_targets()
            found = self.discover()
            self.enrich_inventory(set(found), force=bool(requested))
            self.next_discovery = time.monotonic() + self.settings.discovery_interval_seconds
        network = ipaddress.ip_network(self.network['cidr'])
        devices = [device for device in self.db.list_devices()
                   if device.get('ip') and ipaddress.ip_address(device['ip']) in network]
        # An ARP reply establishes local presence even when a printer blocks ICMP.
        present = arp_discover(self.network, [device['ip'] for device in devices]) if devices else {}
        for ip, mac in present.items():
            self.db.upsert_device(ip, mac=mac, now=utc_now())
        # Re-read after MAC/IP reconciliation; do not apply a new peer's reply to an old identity.
        devices = [device for device in self.db.list_devices()
                   if device.get('ip') and ipaddress.ip_address(device['ip']) in network]

        def check_device(device):
            matching_arp = present.get(device['ip'])
            if matching_arp and (not device.get('mac') or device['mac'].lower() == matching_arp):
                return device, True, None
            result = ping(device['ip'])
            return device, result.success, result.error

        with ThreadPoolExecutor(max_workers=12) as pool:
            for device, success, error in pool.map(check_device, devices):
                self.db.record_probe('device', device['id'], success, now=utc_now(), error=error)
        targets = [target for target in self.db.list_targets() if target.get('enabled', True)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            for target, result in zip(targets, pool.map(probe_target, targets)):
                self.db.record_probe('target', target['id'], result.success, result.rtt_ms,
                                     now=utc_now(), error=result.error)
        self.db.prune(now=utc_now())
        self.db.set_meta('collector_error', None)
        self.db.set_meta('collector_last_seen', utc_now())

    def run(self, once: bool = False) -> int:
        while not self.stopped:
            started = time.monotonic()
            try:
                self.cycle()
            except Exception as exc:
                LOG.exception('Ciclo não concluído')
                self.db.set_meta('collector_error', str(exc)[:500])
                if once:
                    return 1
            if once:
                return 0
            delay = max(1, self.settings.interval_seconds - (time.monotonic() - started))
            deadline = time.monotonic() + delay
            while not self.stopped and time.monotonic() < deadline:
                time.sleep(min(1, max(0.01, deadline - time.monotonic())))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Executar um ciclo e sair')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    settings = Settings.from_env()
    with collector_lock(Path(str(settings.db_path) + '.collector.lock')):
        collector = Collector(settings)
        def stop(*_):
            collector.stopped = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        return collector.run(once=args.once)


if __name__ == '__main__':
    raise SystemExit(main())
