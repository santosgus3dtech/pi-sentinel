import pytest

from pisentinel.printers.models import (
    ConnectionStatus,
    CurrentJob,
    NormalizedPrinter,
    PrinterDefinition,
    PrinterState,
    PrinterTemperatures,
    TemperatureReading,
)


def definition(**overrides):
    values = dict(id=7, device_id=3, name="Printer", manufacturer="Example", model="P1",
                  adapter_type="mock", enabled=True, camera_enabled=False, ip="192.168.10.20",
                  mac="aa:bb:cc:dd:ee:ff", network_status="online")
    values.update(overrides)
    return PrinterDefinition(**values)


def test_normalized_printer_serializes_stable_contract():
    printer = NormalizedPrinter(
        definition=definition(),
        online=True,
        connection_status=ConnectionStatus.HEALTHY,
        state=PrinterState.PRINTING,
        temperatures=PrinterTemperatures(TemperatureReading(215.2, 220), TemperatureReading(59.8, 60)),
        current_job=CurrentJob("part.3mf", 25.5, 900),
        last_seen="2026-01-01T00:00:00Z",
        capabilities={"temperatures": True, "controls": False},
    ).to_dict()
    assert printer["state"] == "PRINTING"
    assert printer["connection_status"] == "healthy"
    assert printer["progress"] == 25.5
    assert printer["temperatures"]["nozzle"] == {"current": 215.2, "target": 220}
    assert printer["health"]["overall"] == "HEALTHY"
    assert printer["health"]["connection_status"] == "healthy"
    assert printer["ip"] == "192.168.10.20"
    assert printer["network_status"] == "online"


@pytest.mark.parametrize("progress", [-0.1, 100.1])
def test_current_job_rejects_invalid_progress(progress):
    with pytest.raises(ValueError, match="between 0 and 100"):
        CurrentJob("part.3mf", progress, 60)
