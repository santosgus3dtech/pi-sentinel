import json
import io
import zipfile
from pathlib import Path

import pytest

from pisentinel.printers.bambu_a1 import (
    BambuA1Adapter,
    BambuAuthenticationError,
    BambuProtocolError,
    BambuTelemetry,
)
from pisentinel.printers.models import ConnectionStatus, PrinterDefinition, PrinterState

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "bambu_a1_status_printing.json").read_text())
TEST_ENV = {
    "BAMBU_A1_HOST": "192.0.2.156",
    "BAMBU_A1_SERIAL": "TESTA1SERIAL001",
    "BAMBU_A1_ACCESS_CODE": "test-code",
    "BAMBU_A1_CERT_SHA256": "A" * 64,
}


def definition(**overrides):
    values = dict(
        id=2,
        device_id=4,
        name="Bambu Lab A1",
        manufacturer="Bambu Lab",
        model="A1",
        adapter_type="bambu_a1",
        enabled=True,
        camera_enabled=False,
        adapter_config={"timeout": 5.0, "retries": 1},
        ip="192.0.2.156",
        mac="9c:13:9e:00:00:01",
    )
    values.update(overrides)
    return PrinterDefinition(**values)


class FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload = dict(FIXTURE if payload is None else payload)
        self.error = error
        self.calls = 0
        self.closed = 0
        self.commands = []

    def fetch(self, host, serial, access_code, certificate_sha256):
        self.calls += 1
        assert host == TEST_ENV["BAMBU_A1_HOST"]
        assert serial == TEST_ENV["BAMBU_A1_SERIAL"]
        assert access_code == TEST_ENV["BAMBU_A1_ACCESS_CODE"]
        assert certificate_sha256 == TEST_ENV["BAMBU_A1_CERT_SHA256"]
        if self.error:
            raise self.error
        return BambuTelemetry(self.payload, 18.4, "2026-09-24T21:30:00.000Z")

    def close(self):
        self.closed += 1

    def command(self, host, serial, access_code, certificate_sha256, command):
        assert host == TEST_ENV["BAMBU_A1_HOST"]
        assert serial == TEST_ENV["BAMBU_A1_SERIAL"]
        assert access_code == TEST_ENV["BAMBU_A1_ACCESS_CODE"]
        assert certificate_sha256 == TEST_ENV["BAMBU_A1_CERT_SHA256"]
        self.commands.append(command)
        return {"command": command["print"]["command"], "result": "success"}


class FakeFileClient:
    def __init__(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("Metadata/plate_1.gcode", "; test gcode")
            archive.writestr("Metadata/plate_1.json", json.dumps({
                "bed_type": "Textured PEI Plate",
                "filament_colors": ["#FFFFFF"],
                "filament_ids": ["GFL99"],
                "nozzle_diameter": 0.4,
            }))
            archive.writestr(
                "Metadata/slice_info.config",
                '<config><plate><metadata key="index" value="1"/>'
                '<metadata key="prediction" value="3600"/>'
                '<metadata key="weight" value="18.5"/></plate></config>',
            )
            archive.writestr("Metadata/plate_1.png", b"\x89PNG\r\n\x1a\npreview")
        self.archive = stream.getvalue()

    def list_root(self, host, access_code, certificate_sha256):
        assert host == TEST_ENV["BAMBU_A1_HOST"]
        assert access_code == TEST_ENV["BAMBU_A1_ACCESS_CODE"]
        assert certificate_sha256 == TEST_ENV["BAMBU_A1_CERT_SHA256"]
        return [{
            "path": "fixture.gcode.3mf", "name": "fixture.gcode.3mf",
            "size": len(self.archive), "modified": "Sep 24 21:00",
            "print_ready": True, "preview_available": True, "metadata": {},
        }]

    def download(self, host, access_code, certificate_sha256, path):
        assert path == "fixture.gcode.3mf"
        self.list_root(host, access_code, certificate_sha256)
        return io.BytesIO(self.archive)


def adapter(payload=None, *, client=None, file_client=None, network=True, env=None):
    client = client or FakeClient(payload)
    return BambuA1Adapter(
        definition(),
        status_client=client,
        file_client=file_client,
        network_probe=lambda _host, _timeout: (network, 2.2 if network else None),
        environ=TEST_ENV if env is None else env,
    )


@pytest.mark.parametrize(
    ("raw_state", "expected"),
    [
        ("IDLE", PrinterState.IDLE),
        ("RUNNING", PrinterState.PRINTING),
        ("PAUSE", PrinterState.PAUSED),
        ("FINISH", PrinterState.COMPLETED),
        ("FAILED", PrinterState.ERROR),
    ],
)
def test_a1_normalizes_mqtt_states(raw_state, expected):
    assert adapter({**FIXTURE, "gcode_state": raw_state}).get_status() is expected


def test_a1_printing_maps_real_mqtt_fields_and_minutes_to_seconds():
    printer = adapter()

    assert printer.get_status() is PrinterState.PRINTING
    assert printer.get_temperatures().nozzle.current == 219.8125
    assert printer.get_temperatures().nozzle.target == 220.0
    assert printer.get_temperatures().bed.current == 59.9375
    job = printer.get_current_job()
    assert job is not None
    assert job.file_name == "fixture-part.3mf"
    assert job.progress == 73
    assert job.remaining_seconds == 1620
    assert printer.health_check().connection_status is ConnectionStatus.HEALTHY


def test_a1_paused_keeps_current_job():
    printer = adapter({**FIXTURE, "gcode_state": "PAUSE", "mc_percent": 42})
    assert printer.get_status() is PrinterState.PAUSED
    assert printer.get_current_job().progress == 42


def test_a1_idle_has_no_job_and_keeps_temperatures():
    printer = adapter({**FIXTURE, "gcode_state": "IDLE", "mc_percent": 0})
    assert printer.get_status() is PrinterState.IDLE
    assert printer.get_current_job() is None
    assert printer.get_temperatures().bed.target == 60


def test_a1_offline_is_contained():
    client = FakeClient(error=BambuProtocolError("offline"))
    printer = adapter(client=client, network=False)
    health = printer.health_check()

    assert health.online is False
    assert health.connection_status is ConnectionStatus.UNAVAILABLE
    assert printer.get_status() is PrinterState.OFFLINE
    assert client.calls == 2


def test_a1_invalid_authentication_is_partial_without_leaking_secret():
    client = FakeClient(error=BambuAuthenticationError("A autenticação MQTT da A1 foi recusada."))
    printer = adapter(client=client, network=True)
    health = printer.health_check()

    assert health.online is True
    assert health.connection_status is ConnectionStatus.DEGRADED
    assert health.components["api"].available is False
    assert printer.get_status() is PrinterState.UNKNOWN
    assert TEST_ENV["BAMBU_A1_ACCESS_CODE"] not in (health.message or "")
    assert client.calls == 1


def test_a1_protocol_unavailable_keeps_network_health_partial():
    printer = adapter(client=FakeClient(error=BambuProtocolError("protocol unavailable")), network=True)
    health = printer.health_check()

    assert health.online is True
    assert health.connection_status is ConnectionStatus.DEGRADED
    assert health.components["network"].available is True
    assert health.components["api"].available is False


def test_a1_incomplete_response_returns_unknown_and_null_values():
    printer = adapter({"nozzle_temper": 28.25})

    assert printer.get_status() is PrinterState.UNKNOWN
    assert printer.get_temperatures().nozzle.current == 28.25
    assert printer.get_temperatures().bed.target is None
    assert printer.get_current_job() is None


def test_a1_camera_is_explicitly_unavailable_and_capability_is_false():
    printer = adapter()
    camera = printer.get_camera_info()

    assert camera.available is False
    assert camera.protocol is None
    assert printer.health_check().components["camera"].status is ConnectionStatus.DISABLED
    assert printer.capabilities() == {
        "camera": False,
        "temperatures": True,
        "job_status": True,
        "progress": True,
        "remaining_time": True,
        "files": True,
        "file_metadata": True,
        "history": False,
        "start_print": True,
        "controls": True,
    }


def test_a1_missing_configuration_is_partial_when_broker_is_reachable():
    printer = adapter(client=FakeClient(), network=True, env={"BAMBU_A1_HOST": "192.0.2.156"})
    health = printer.health_check()

    assert health.connection_status is ConnectionStatus.DEGRADED
    assert health.components["api"].available is False
    assert "BAMBU_A1_SERIAL" in (health.message or "")


def test_a1_lists_and_inspects_real_3mf_structure_without_hardware():
    printer = adapter(file_client=FakeFileClient())

    assert printer.list_files()[0]["name"] == "fixture.gcode.3mf"
    inspected = printer.inspect_file("fixture.gcode.3mf")
    assert inspected["plates"] == [{
        "index": 1,
        "path": "Metadata/plate_1.gcode",
        "bed_type": "Textured PEI Plate",
        "filament_colors": ["#FFFFFF"],
        "filament_ids": ["GFL99"],
        "nozzle_diameter": 0.4,
        "estimated_seconds": 3600,
        "filament_weight_g": 18.5,
        "preview_available": True,
    }]
    content_type, content = printer.open_file_preview("fixture.gcode.3mf", "Metadata/plate_1.gcode")
    assert content_type == "image/png"
    assert content.startswith(b"\x89PNG")


def test_a1_start_print_uses_verified_file_and_waits_for_mqtt_ack():
    client = FakeClient({**FIXTURE, "gcode_state": "IDLE", "mc_percent": 0})
    printer = adapter(client=client, file_client=FakeFileClient())

    result = printer.start_print("fixture.gcode.3mf", {
        "plate": "Metadata/plate_1.gcode",
        "bed_leveling": True,
        "vibration_calibration": True,
        "flow_calibration": False,
        "timelapse": False,
        "use_ams": False,
        "ams_mapping": [],
    })

    command = client.commands[0]["print"]
    assert result["accepted"] is True
    assert command["command"] == "project_file"
    assert command["url"] == "ftp://fixture.gcode.3mf"
    assert command["param"] == "Metadata/plate_1.gcode"
    assert command["use_ams"] is False


def test_a1_refuses_start_while_printer_is_busy():
    printer = adapter(file_client=FakeFileClient())
    with pytest.raises(RuntimeError, match="já está imprimindo"):
        printer.start_print("fixture.gcode.3mf", {})


def test_a1_refuses_start_when_idle_state_cannot_be_confirmed():
    printer = adapter({"gcode_state": "UNKNOWN"}, file_client=FakeFileClient())
    with pytest.raises(RuntimeError, match="confirmar.*ociosa"):
        printer.start_print("fixture.gcode.3mf", {})
