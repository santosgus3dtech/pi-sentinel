import json
from pathlib import Path

import pytest

from pisentinel.printers.creality_k1c import (
    CameraProbe,
    CrealityK1CAdapter,
    K1CProtocolError,
    K1CTelemetry,
)
from pisentinel.printers.models import ConnectionStatus, PrinterDefinition, PrinterState

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "k1c_status_printing.json").read_text())


def definition(**overrides):
    values = dict(
        id=1,
        device_id=3,
        name="Creality K1C",
        manufacturer="Creality",
        model="K1C",
        adapter_type="creality_k1c",
        enabled=True,
        camera_enabled=True,
        adapter_config={"timeout": 2.5, "retries": 1},
        ip="192.168.10.155",
        mac="fc:ee:28:00:00:01",
    )
    values.update(overrides)
    return PrinterDefinition(**values)


class FakeClient:
    timeout = 2.5

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0
        self.requests = []

    def fetch(self, host, port):
        self.calls += 1
        if self.error:
            raise self.error
        return K1CTelemetry(self.payload, 7.462, "2026-09-24T20:26:20.000Z")

    def request(self, host, message, response_keys=(), port=9999):
        self.requests.append(message)
        params = message.get("params", {})
        if params.get("reqGcodeFile"):
            return {"retGcodeFileInfo2": [{
                "path": "/usr/data/printer_data/gcodes/fixture.gcode",
                "name": "fixture.gcode", "file_size": 12345, "create_time": 1770000000,
                "timeCost": 3600, "consumables": 4321, "floorHeight": 20,
                "material": "PLA", "nozzleTemp": 22000, "bedTemp": 6000,
                "preview": "fixture.png", "software": "Creality Print",
            }]}
        if params.get("reqHistory"):
            return {"historyList": [{
                "id": 9, "filename": "/data/fixture.gcode", "dateTime": "2026-09-24 18:00",
                "usagetime": 3500, "usagematerial": 4200, "printfinish": 1, "size": 12345,
            }]}
        return {"accepted": True}


class FakeCamera:
    def __init__(self, available=True):
        self.available = available

    def probe(self):
        return CameraProbe(
            self.available,
            "mjpeg" if self.available else None,
            "multipart/x-mixed-replace; boundary=frame" if self.available else None,
            4.2 if self.available else None,
            None if self.available else "Endpoint de câmera sem resposta.",
        )


def adapter(payload=None, *, client=None, camera=True, web=True):
    client = client or FakeClient(payload if payload is not None else dict(FIXTURE))
    return CrealityK1CAdapter(
        definition(),
        status_client=client,
        camera_provider=FakeCamera(camera),
        web_probe=lambda _host, _timeout: (web, 3.1 if web else None),
    )


@pytest.mark.parametrize(
    ("raw_state", "expected"),
    [(0, PrinterState.IDLE), (1, PrinterState.PRINTING), (5, PrinterState.PAUSED)],
)
def test_k1c_normalizes_observed_state_codes(raw_state, expected):
    payload = {**FIXTURE, "state": raw_state}
    assert adapter(payload).get_status() is expected


def test_k1c_printing_maps_real_websocket_fields():
    printer = adapter()

    assert printer.get_status() is PrinterState.PRINTING
    assert printer.get_temperatures().nozzle.current == 214.6
    assert printer.get_temperatures().nozzle.target == 220.0
    assert printer.get_temperatures().bed.current == 59.8
    job = printer.get_current_job()
    assert job is not None
    assert job.file_name == "fixture-part.gcode"
    assert job.progress == 42.5
    assert job.remaining_seconds == 3480
    assert printer.health_check().connection_status is ConnectionStatus.HEALTHY


def test_k1c_paused_keeps_current_job():
    printer = adapter({**FIXTURE, "state": 5, "printProgress": 63, "printLeftTime": 2100})
    assert printer.get_status() is PrinterState.PAUSED
    assert printer.get_current_job().progress == 63


def test_k1c_idle_has_no_current_job_but_keeps_temperatures():
    printer = adapter({**FIXTURE, "state": 0, "printProgress": 0, "printLeftTime": 0})
    assert printer.get_status() is PrinterState.IDLE
    assert printer.get_current_job() is None
    assert printer.get_temperatures().bed.current == 59.8


def test_k1c_offline_is_contained():
    printer = adapter(client=FakeClient(error=K1CProtocolError("offline")), camera=False, web=False)
    health = printer.health_check()

    assert health.online is False
    assert health.connection_status is ConnectionStatus.UNAVAILABLE
    assert printer.get_status() is PrinterState.OFFLINE
    assert printer.get_temperatures().nozzle.current is None


def test_k1c_api_unavailable_falls_back_to_partial_web_health():
    printer = adapter(client=FakeClient(error=K1CProtocolError("api down")), camera=False, web=True)
    health = printer.health_check()

    assert health.online is True
    assert health.connection_status is ConnectionStatus.DEGRADED
    assert health.components["web"].available is True
    assert health.components["api"].available is False
    assert printer.get_status() is PrinterState.UNKNOWN


def test_k1c_camera_failure_does_not_make_printer_offline():
    printer = adapter(camera=False)
    health = printer.health_check()

    assert health.online is True
    assert health.connection_status is ConnectionStatus.DEGRADED
    assert health.components["api"].available is True
    assert health.components["camera"].available is False
    assert printer.get_camera_info().available is False


def test_k1c_incomplete_response_returns_null_values():
    printer = adapter({"model": "K1C", "state": "unexpected"})

    assert printer.get_status() is PrinterState.UNKNOWN
    assert printer.get_current_job() is None
    assert printer.get_temperatures().nozzle.current is None
    assert printer.get_temperatures().bed.target is None


def test_k1c_capabilities_are_read_only_and_match_observed_fields():
    capabilities = adapter().capabilities()
    assert capabilities == {
        "camera": True,
        "temperatures": True,
        "job_status": True,
        "progress": True,
        "remaining_time": True,
        "files": True,
        "file_metadata": True,
        "history": True,
        "start_print": True,
        "controls": True,
    }


def test_k1c_files_metadata_and_history_use_stock_websocket_messages():
    printer = adapter()

    files = printer.list_files()
    assert files[0]["metadata"]["layer_height_mm"] == 0.2
    assert files[0]["metadata"]["nozzle_temperature"] == 220
    assert printer.inspect_file(files[0]["path"])["file"]["name"] == "fixture.gcode"
    history = printer.list_history()
    assert history[0]["file_name"] == "fixture.gcode"
    assert history[0]["completed"] is True


def test_k1c_start_print_reuses_exact_stock_ui_command_when_idle():
    client = FakeClient({**FIXTURE, "state": 0, "printProgress": 0})
    printer = adapter(client=client)

    result = printer.start_print("/usr/data/printer_data/gcodes/fixture.gcode", {})

    assert result["accepted"] is True
    assert client.requests[-1] == {
        "method": "set",
        "params": {"opGcodeFile": "printprt:/usr/data/printer_data/gcodes/fixture.gcode"},
    }


def test_k1c_refuses_start_while_printer_is_busy():
    with pytest.raises(RuntimeError, match="já está imprimindo"):
        adapter().start_print("/usr/data/printer_data/gcodes/fixture.gcode", {})


def test_k1c_refuses_start_when_idle_state_cannot_be_confirmed():
    printer = adapter({**FIXTURE, "state": 999})
    with pytest.raises(RuntimeError, match="confirmar.*ociosa"):
        printer.start_print("/usr/data/printer_data/gcodes/fixture.gcode", {})
