import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from pisentinel.api import create_app
from pisentinel.config import Settings
from pisentinel.db import Database
from pisentinel.router_asus import (
    parse_client_inventory,
    parse_cpu_memory,
    parse_dhcp_leases,
    parse_traffic_counters,
    parse_wireless_events,
    parse_wireless_log,
)


CLIENTS = '''
var originData = {
fromNetworkmapd : [{
  "AA:BB:CC:DD:EE:01": {"name":"Notebook","ip":"192.168.10.10","mac":"AA:BB:CC:DD:EE:01","vendor":"Example","isWL":"2","isOnline":"1","rssi":"-61","curTx":"960.7","curRx":"720.6","wlConnectTime":"06:31:03","ipMethod":"DHCP","internetState":"1","type":"30"},
  "AA:BB:CC:DD:EE:02": {"name":"Servidor","ip":"192.168.10.11","mac":"AA:BB:CC:DD:EE:02","vendor":"","isWL":"0","isOnline":"1","rssi":"0","curTx":"","curRx":"","wlConnectTime":"","ipMethod":"DHCP","internetState":"1","type":"0"},
  "maclist":["AA:BB:CC:DD:EE:01","AA:BB:CC:DD:EE:02"]
}], other: []};
'''

WIRELESS = '''SSID: "Lab-Network-5G"
noise: -85 dBm Channel: 149/80
Chanspec: 5GHz channel 155 80MHz (0xe09b)
Primary channel: 149
QBSS Channel Utilization: 0x2b (16 %25)
Interference Level: Acceptable
Stations List
idx MAC Associated Authorized RSSI PHY PSM SGI STBC MUBF NSS BW Tx rate Rx rate Connect Time
 AA:BB:CC:DD:EE:01 Yes Yes -61dBm ax No Yes Yes No 2 80M 960.7M 720.6M 06:31:03
'''

DHCP = '''<textarea>Hostname IP Address MAC Address Expires
Notebook 192.168.10.10 aa:bb:cc:dd:ee:01 12:30:00
* 192.168.10.11 aa:bb:cc:dd:ee:02 00:30:00
</textarea>'''

WIFI_EVENTS = '''<textarea>
Sep 28 20:50:27 wlceventd: wlceventd_proc_event(645): eth5: Deauth_ind AA:BB:CC:DD:EE:01, status: 0, reason: Unspecified reason (1), rssi:0
Sep 28 20:50:28 wlceventd: wlceventd_proc_event(685): eth5: ReAssoc AA:BB:CC:DD:EE:01, status: Successful (0), rssi:-61
Sep 28 20:51:27 wlceventd: wlceventd_proc_event(645): eth5: Disassoc AA:BB:CC:DD:EE:01, status: 0, reason: Disassociated due to inactivity (4), rssi:-74
Sep 28 20:52:27 wlceventd: wlceventd_proc_event(645): eth5: Deauth_ind AA:BB:CC:DD:EE:01, status: 0, reason: Previous authentication no longer valid (2), rssi:-55
</textarea>'''


def test_asus_parsers_normalize_clients_wifi_dhcp_and_router_metrics():
    clients = parse_client_inventory(CLIENTS)
    assert len(clients) == 2
    assert clients[0]["interface"] == "wifi_5"
    assert clients[0]["connected_seconds"] == 23463
    assert clients[1]["rssi_dbm"] is None

    wireless = parse_wireless_log(WIRELESS)
    assert wireless["bands"] == [{
        "band": "5 GHz", "ssid": "Lab-Network-5G", "channel": 149,
        "channel_width_mhz": 80, "noise_dbm": -85, "utilization_pct": 16,
        "interference": "Acceptable",
    }]
    station = wireless["stations"]["aa:bb:cc:dd:ee:01"]
    assert station["phy_mode"] == "ax" and station["spatial_streams"] == 2
    assert station["tx_rate_mbps"] == 960.7 and station["rx_rate_mbps"] == 720.6

    leases = parse_dhcp_leases(DHCP)
    assert leases["aa:bb:cc:dd:ee:01"]["dhcp_expires_seconds"] == 45000
    assert leases["aa:bb:cc:dd:ee:02"]["hostname"] is None

    metrics = parse_cpu_memory('''cpuInfo = {"cpu0":{"total":"100","usage":"25"},"cpu1":{"total":"200","usage":"10"}};
memInfo = {"total":"524288","free":"131072","used":"393216"};''')
    assert metrics["cpu_percent"] == [25.0, 5.0]
    assert metrics["memory_used_pct"] == 75.0
    traffic = parse_traffic_counters("netdev = {'INTERNET':{rx:0x10,tx:0x20},'WIRELESS0':{rx:48,tx:64}}")
    assert traffic["internet"] == {"rx_bytes": 16, "tx_bytes": 32}

    events = parse_wireless_events(WIFI_EVENTS, now=datetime(2026, 9, 28, 21, tzinfo=timezone.utc))
    assert [item["event"] for item in events] == [
        "deauthenticated", "reassociated", "disconnected", "deauthenticated",
    ]
    assert events[0]["rssi_dbm"] is None
    assert events[2]["reason"] == "Disassociated due to inactivity (4)"
    assert events[3]["rssi_dbm"] == -55


def test_router_snapshot_enriches_existing_devices_and_calculates_rates(tmp_path):
    database = Database(tmp_path / "router.db")
    device_id = database.upsert_device("192.168.10.10", "aa:bb:cc:dd:ee:01", "Notebook",
                                       now="2026-09-28T20:00:00Z")
    router = {
        "available": True, "host": "192.168.10.1", "model": "RT-AX82U",
        "traffic_counters": {"internet": {"rx_bytes": 1000, "tx_bytes": 2000}},
        "client_counts": {"online": 1, "known": 1, "wired": 0, "wifi_2_4": 0, "wifi_5": 1},
    }
    client = {
        "mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.10.10", "hostname": "Notebook",
        "online": True, "interface": "wifi_5", "ssid": "rede_5G", "rssi_dbm": -61,
        "phy_mode": "ax", "spatial_streams": 2, "channel_width_mhz": 80,
        "tx_rate_mbps": 960.7, "rx_rate_mbps": 720.6, "connected_seconds": 300,
        "ip_method": "DHCP", "dhcp_expires_seconds": 3600, "internet_allowed": True,
    }
    database.set_router_snapshot(router, [client], now="2026-09-28T20:00:00Z")
    router["traffic_counters"]["internet"] = {"rx_bytes": 11_001_000, "tx_bytes": 7_502_000}
    result = database.set_router_snapshot(router, [client], now="2026-09-28T20:00:10Z")
    assert result["traffic_rates"]["internet"] == {"rx_mbps": 8.8, "tx_mbps": 6.0}

    details = database.device_details(device_id)
    assert details["router"]["interface"] == "wifi_5"
    assert details["router"]["router_online"] is True
    assert details["router"]["rssi_dbm"] == -61
    dashboard = database.router_dashboard()
    assert dashboard["router"]["model"] == "RT-AX82U"
    assert dashboard["clients"][0]["display_name"] == "Notebook"
    assert "password" not in json.dumps(dashboard).lower()

    events = parse_wireless_events(WIFI_EVENTS, now=datetime(2026, 9, 28, 21, tzinfo=timezone.utc))
    assert database.record_router_wifi_events(events) == 4
    assert database.record_router_wifi_events(events) == 0
    dashboard = database.router_dashboard(now="2026-09-28T21:00:00Z")
    assert len(dashboard["wifi_events"]) == 4
    assert dashboard["wifi_events"][0]["display_name"] == "Notebook"
    assert dashboard["wifi_alerts"][0]["event_count"] == 3
    assert dashboard["wifi_alerts"][0]["min_rssi_dbm"] == -74


def test_router_endpoint_returns_data_and_is_protected_when_auth_is_enabled(tmp_path):
    settings = Settings(db_path=tmp_path / "api.db")
    database = Database(settings.db_path)
    database.set_router_error("aguardando", now="2026-09-28T20:00:00Z")
    client = TestClient(create_app(settings, database))
    response = client.get("/api/router")
    assert response.status_code == 200
    assert response.json()["router"]["available"] is False
    assert response.json()["wifi_events"] == []
    protected = TestClient(create_app(Settings(db_path=tmp_path / "protected.db", auth_enabled=True)))
    assert protected.get("/api/router").status_code == 401
