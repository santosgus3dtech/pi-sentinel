import pytest

from pisentinel.printers.mock import MockPrinterAdapter
from pisentinel.printers.models import ConnectionStatus, PrinterDefinition, PrinterState


def definition(state: str = "IDLE") -> PrinterDefinition:
    return PrinterDefinition(id=1, device_id=None, name="Demo", manufacturer="PiSentinel", model="Mock",
                             adapter_type="mock", enabled=True, camera_enabled=False,
                             adapter_config={"state": state})


@pytest.mark.parametrize("state", ["IDLE", "PRINTING", "PAUSED", "ERROR", "OFFLINE"])
def test_mock_printer_supports_required_states(state):
    adapter = MockPrinterAdapter(definition(state))
    assert adapter.get_status() is PrinterState(state)
    assert adapter.capabilities()["controls"] is False


def test_mock_printing_has_job_progress_and_temperatures():
    adapter = MockPrinterAdapter(definition("PRINTING"))
    job = adapter.get_current_job()
    temperatures = adapter.get_temperatures()
    assert job is not None
    assert job.file_name == "peca-demonstracao.3mf"
    assert job.progress == 42.5 and job.remaining_seconds == 3480
    assert temperatures.nozzle.current == 214.6 and temperatures.nozzle.target == 220.0
    assert temperatures.bed.current == 59.8 and temperatures.bed.target == 60.0
    assert adapter.health_check().connection_status is ConnectionStatus.HEALTHY


def test_mock_offline_has_no_live_values():
    adapter = MockPrinterAdapter(definition("OFFLINE"))
    assert adapter.health_check().online is False
    assert adapter.get_current_job() is None
    assert adapter.get_temperatures().nozzle.current is None


def test_mock_rejects_unknown_state():
    with pytest.raises(ValueError, match="Unsupported mock printer state"):
        MockPrinterAdapter(definition("WARMING"))
