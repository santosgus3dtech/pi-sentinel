from datetime import datetime, timezone

import pytest

from pisentinel import device_enrichment
from pisentinel.api import create_app
from pisentinel.config import Settings
from pisentinel.db import Database


def test_bounded_service_scan_reports_only_open_common_ports(monkeypatch):
    seen = []

    def fake_probe(host, port, service, timeout):
        seen.append((host, port, timeout))
        if port == 80:
            return device_enrichment.OpenService(80, service, 1.25, 200, "test", "text/html")
        return None

    monkeypatch.setattr(device_enrichment, "_probe", fake_probe)
    result = device_enrichment.scan_device_services(
        "192.168.10.8", timeout=.2, services=((22, "SSH"), (80, "HTTP")), max_workers=2,
    )
    assert result == [{
        "port": 80, "transport": "tcp", "service": "HTTP", "latency_ms": 1.25,
        "http_status": 200, "server": "test", "content_type": "text/html",
    }]
    assert sorted(port for _, port, _ in seen) == [22, 80]


def test_service_scan_refuses_public_or_unbounded_targets():
    with pytest.raises(ValueError, match="privados"):
        device_enrichment.scan_device_services("8.8.8.8", services=((80, "HTTP"),))
    with pytest.raises(ValueError, match="64"):
        device_enrichment.scan_device_services("192.168.10.8", services=tuple((port, "x") for port in range(1, 66)))


def test_device_details_combine_identity_history_incidents_and_services(tmp_path):
    database = Database(tmp_path / "details.db", failure_threshold=1, recovery_threshold=1)
    device_id = database.upsert_device("192.168.10.8", "02:11:22:33:44:55", "phone",
                                       now="2026-09-28T19:00:00Z")
    database.record_probe("device", device_id, True, 4.5, "2026-09-28T19:05:00Z")
    database.record_probe("device", device_id, False, now="2026-09-28T19:06:00Z")
    database.record_probe("device", device_id, True, 5.5, "2026-09-28T19:07:00Z")
    database.set_device_services(device_id, [{
        "port": 80, "transport": "tcp", "service": "HTTP", "latency_ms": 1.2,
        "http_status": 200, "server": "appliance", "content_type": "text/html",
    }], checked_at="2026-09-28T19:08:00Z")

    details = database.device_details(device_id, now=datetime(2026, 9, 28, 20, tzinfo=timezone.utc))
    assert details["mac_address_type"] == "private"
    assert details["availability"]["24h"] == {
        "samples": 3, "successes": 2, "loss_pct": 33.33,
        "avg_rtt_ms": 5.0, "min_rtt_ms": 4.5, "max_rtt_ms": 5.5,
    }
    assert details["services"][0]["http_status"] == 200
    assert details["recent_incidents"][0]["duration_seconds"] == 60
    assert details["checked_tcp_ports"] == len(device_enrichment.COMMON_TCP_SERVICES)


def test_device_details_endpoint_and_not_found(tmp_path):
    settings = Settings(db_path=tmp_path / "api.db", cidr="192.168.10.0/24", interface="wlan0")
    database = Database(settings.db_path)
    device_id = database.upsert_device("192.168.10.9", "00:11:22:33:44:55")
    database.set_device_services(device_id, [], checked_at="2026-09-28T19:08:00Z")
    from fastapi.testclient import TestClient
    client = TestClient(create_app(settings, database))
    response = client.get(f"/api/devices/{device_id}")
    assert response.status_code == 200
    assert response.json()["checked_tcp_ports"] == len(device_enrichment.COMMON_TCP_SERVICES)
    assert client.get("/api/devices/9999").status_code == 404
