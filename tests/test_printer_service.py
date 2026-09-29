import sqlite3

import pytest

from pisentinel.db import Database
from pisentinel.printers.base import PrinterAdapter, PrinterFeatureUnavailable
from pisentinel.printers.mock import MockPrinterAdapter
from pisentinel.printers.models import PrinterDefinition
from pisentinel.printers.service import PrinterService


class BrokenAdapter(PrinterAdapter):
    def capabilities(self):
        raise RuntimeError("adapter failed")

    def health_check(self):
        raise RuntimeError("adapter failed")

    def get_status(self):
        raise RuntimeError("adapter failed")

    def get_temperatures(self):
        raise RuntimeError("adapter failed")

    def get_current_job(self):
        raise RuntimeError("adapter failed")

    def get_camera_info(self):
        raise RuntimeError("adapter failed")


class StartAdapter(MockPrinterAdapter):
    def capabilities(self):
        return {**super().capabilities(), "start_print": True, "controls": True}

    def start_print(self, path, options):
        return {"accepted": True, "file": path, "options": dict(options)}


def test_printer_is_linked_to_device_without_copying_network_fields(tmp_path):
    database = Database(tmp_path / "printers.db")
    device_id = database.upsert_device("192.168.10.155", "fc:ee:28:00:00:01", "K1C")
    printer_id = database.create_printer("K1C mock", "Creality", "K1C", "mock", device_id=device_id,
                                         adapter_config={"state": "IDLE"})
    stored = database.get_printer(printer_id)
    assert stored["device_id"] == device_id
    assert stored["ip"] == "192.168.10.155"
    assert stored["mac"] == "fc:ee:28:00:00:01"
    with sqlite3.connect(database.path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(printers)")}
    assert "ip" not in columns and "mac" not in columns


def test_database_adds_printer_table_without_losing_existing_devices(tmp_path):
    path = tmp_path / "migration.db"
    database = Database(path)
    device_id = database.upsert_device("192.168.10.156", "9c:13:9e:00:00:01")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE printers")
    migrated = Database(path)
    assert migrated.list_devices()[0]["id"] == device_id
    assert migrated.list_printers() == []


def test_service_contains_one_adapter_failure_and_keeps_other_printers(tmp_path):
    database = Database(tmp_path / "service.db")
    good = database.create_printer("Good", "PiSentinel", "Mock", "mock",
                                   adapter_config={"state": "PRINTING"})
    broken = database.create_printer("Broken", "Example", "B1", "broken")
    service = PrinterService(database, adapters={"broken": BrokenAdapter})
    results = {item["id"]: item for item in service.list_printers()}
    assert results[good]["state"] == "PRINTING" and results[good]["online"] is True
    assert results[broken]["state"] == "ERROR" and results[broken]["online"] is False
    assert "adapter failed" in results[broken]["error"]


def test_service_exposes_opt_in_demo_without_writing_database(tmp_path):
    database = Database(tmp_path / "demo.db")
    service = PrinterService(database, include_mock=True)
    result = service.get_printer(0)
    assert result["name"] == "Impressora de demonstração"
    assert result["state"] == "PRINTING"
    assert database.list_printers() == []


def test_ensure_real_k1c_reuses_device_and_printer(tmp_path):
    database = Database(tmp_path / "k1c.db")
    device_id = database.upsert_device("192.168.10.155", "FC:EE:28:00:00:01", "K1C-891F")
    first = database.ensure_printer("Creality K1C", "Creality", "K1C", "creality_k1c",
                                    device_id, camera_enabled=True)
    second = database.ensure_printer("Creality K1C", "Creality", "K1C", "creality_k1c",
                                        database.get_device_by_mac("fc-ee-28-00-00-01")["id"], camera_enabled=True)

    assert first == second
    assert len(database.list_devices()) == 1
    assert len(database.list_printers()) == 1
    assert database.get_printer(first)["adapter_type"] == "creality_k1c"


def test_ensure_real_a1_reuses_device_and_does_not_duplicate_k1c(tmp_path):
    database = Database(tmp_path / "a1.db")
    k1c_device = database.upsert_device("192.168.10.155", "FC:EE:28:00:00:01", "K1C-891F")
    a1_device = database.upsert_device("192.168.10.156", "9C:13:9E:00:00:01", "Bambu-A1")
    k1c = database.ensure_printer("Creality K1C", "Creality", "K1C", "creality_k1c", k1c_device)
    first = database.ensure_printer("Bambu Lab A1", "Bambu Lab", "A1", "bambu_a1", a1_device)
    second = database.ensure_printer(
        "Bambu Lab A1", "Bambu Lab", "A1", "bambu_a1",
        database.get_device_by_mac("9c-13-9e-00-00-01")["id"],
    )

    assert first == second
    assert len(database.list_devices()) == 2
    assert {item["id"] for item in database.list_printers()} == {k1c, first}
    assert database.get_printer(first)["adapter_type"] == "bambu_a1"


def test_service_keeps_physical_controls_disabled_until_explicitly_enabled(tmp_path):
    database = Database(tmp_path / "controls.db")
    printer_id = database.create_printer("Guarded", "PiSentinel", "Mock", "guarded")
    disabled = PrinterService(database, adapters={"guarded": StartAdapter})
    enabled = PrinterService(database, adapters={"guarded": StartAdapter}, controls_enabled=True)

    assert disabled.get_printer(printer_id)["capabilities"]["controls"] is False
    with pytest.raises(PrinterFeatureUnavailable, match="desativados"):
        disabled.start_print(printer_id, "fixture.gcode", {})

    assert enabled.get_printer(printer_id)["capabilities"]["controls"] is True
    result = enabled.start_print(printer_id, "fixture.gcode", {"bed_leveling": True})
    assert result["accepted"] is True
    assert result["options"] == {"bed_leveling": True}
