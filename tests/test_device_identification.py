import sqlite3

from pisentinel import collector as collector_module
from pisentinel.collector import Collector
from pisentinel.config import Settings
from pisentinel.db import Database
from pisentinel.device_identification import identify_device, lookup_mac_vendor


def test_lookup_vendor_uses_local_oui_file_and_respects_private_mac(tmp_path):
    oui = tmp_path / "ieee-oui.txt"
    oui.write_text("102B41\tSamsung Electronics Co.,Ltd\nAEB0F8\tHidden Vendor\n", encoding="utf-8")
    assert lookup_mac_vendor("10:2b:41:00:00:01", oui) == "Samsung Electronics Co.,Ltd"
    assert lookup_mac_vendor("02:11:22:33:44:09", oui) is None


def test_identification_uses_confirmed_printer_and_hostname_signals():
    k1c = identify_device(ip="192.168.10.155", mac="FC:EE:28:00:00:01", hostname=None)
    assert (k1c.auto_name, k1c.auto_group, k1c.vendor) == ("Creality K1C", "impressoras_3d", "Creality")
    assert identify_device(ip=None, mac=None, hostname="iPhone.local").auto_group == "celular"
    assert identify_device(ip=None, mac=None, hostname="Nintendo-Switch").auto_group == "consoles"
    assert identify_device(ip=None, mac=None, hostname="chefinghoserver").auto_group == "computadores"
    assert identify_device(ip=None, mac=None, hostname="RT-AX82U-7140").auto_group == "infraestrutura"


def test_automatic_group_never_overwrites_a_user_choice(tmp_path):
    database = Database(tmp_path / "devices.db")
    device_id = database.upsert_device("192.168.10.97", "02:11:22:33:44:09", "iPhone")
    detected = database.set_device_identification(
        device_id, auto_name="iPhone", auto_group="celular", vendor=None,
        source="hostname", now="2026-09-28T12:00:00Z",
    )
    assert detected["group"] == "celular"
    assert database.update_device(device_id, {"group": "visitantes"})["group"] == "visitantes"
    refreshed = database.set_device_identification(
        device_id, auto_name="iPhone", auto_group="celular", vendor=None,
        source="hostname", now="2026-09-28T12:05:00Z",
    )
    assert refreshed["group"] == "visitantes"
    assert refreshed["auto_group"] == "celular"


def test_online_devices_are_listed_before_offline_devices(tmp_path):
    database = Database(tmp_path / "order.db", failure_threshold=1)
    offline_id = database.upsert_device("192.168.10.20", "00:11:22:33:44:20")
    online_id = database.upsert_device("192.168.10.21", "00:11:22:33:44:21")
    database.record_probe("device", offline_id, False)
    assert [item["id"] for item in database.list_devices()] == [online_id, offline_id]


def test_existing_database_migration_preserves_manual_groups(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE devices (
                id INTEGER PRIMARY KEY, ip TEXT, mac TEXT, hostname TEXT,
                name TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                group_name TEXT NOT NULL DEFAULT 'sem_grupo', status TEXT NOT NULL DEFAULT 'unknown',
                last_seen TEXT, last_checked TEXT, rtt_ms REAL,
                failure_streak INTEGER NOT NULL DEFAULT 0, success_streak INTEGER NOT NULL DEFAULT 0,
                first_failed_at TEXT, ever_online INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            INSERT INTO devices(ip,mac,group_name,created_at)
            VALUES ('192.168.10.9','00:11:22:33:44:55','computadores','2026-01-01T00:00:00Z');
        """)
    migrated = Database(path).list_devices()[0]
    assert migrated["group"] == "computadores"
    assert migrated["group_customized"] is True


def test_collector_identifies_first_contact_and_backfills_existing_rows(tmp_path, monkeypatch):
    settings = Settings(db_path=tmp_path / "collector.db", cidr="192.168.10.0/24", interface="wlan0")
    collector = Collector(settings)
    collector.network = {"cidr": settings.cidr, "interface": settings.interface}
    existing_id = collector.db.upsert_device("192.168.10.97", "02:11:22:33:44:09", "iPhone")
    monkeypatch.setattr(collector_module, "arp_discover_details", lambda _network: {
        "192.168.10.110": {"mac": "78:81:8c:00:00:01", "vendor": "Nintendo Co., Ltd."}
    })
    monkeypatch.setattr(collector_module, "reverse_name", lambda _ip: "Nintendo-Switch")
    collector.discover()
    devices = {item["id"]: item for item in collector.db.list_devices()}
    assert devices[existing_id]["group"] == "celular"
    switch = next(item for item in devices.values() if item["ip"] == "192.168.10.110")
    assert switch["group"] == "consoles"
    assert switch["vendor"] == "Nintendo Co., Ltd."
