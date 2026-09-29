from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .base import PrinterAdapter
from .models import (
    AdapterHealth,
    CameraInfo,
    ConnectionStatus,
    CurrentJob,
    PrinterDefinition,
    PrinterState,
    PrinterTemperatures,
    TemperatureReading,
)


class MockPrinterAdapter(PrinterAdapter):
    """Deterministic adapter for API/frontend development without hardware."""

    def __init__(self, printer: PrinterDefinition, state: PrinterState | str | None = None):
        super().__init__(printer)
        configured = state or printer.adapter_config.get("state", PrinterState.IDLE.value)
        try:
            self.state = configured if isinstance(configured, PrinterState) else PrinterState(str(configured).upper())
        except ValueError as exc:
            raise ValueError(f"Unsupported mock printer state: {configured}") from exc

    def get_status(self) -> PrinterState:
        return self.state

    def get_temperatures(self) -> PrinterTemperatures:
        presets: dict[PrinterState, tuple[tuple[float | None, float | None], tuple[float | None, float | None]]] = {
            PrinterState.PRINTING: ((214.6, 220.0), (59.8, 60.0)),
            PrinterState.PAUSED: ((168.4, 170.0), (55.1, 55.0)),
            PrinterState.IDLE: ((28.2, None), (26.7, None)),
            PrinterState.COMPLETED: ((38.0, None), (34.0, None)),
            PrinterState.ERROR: ((31.0, None), (29.0, None)),
            PrinterState.OFFLINE: ((None, None), (None, None)),
            PrinterState.UNKNOWN: ((None, None), (None, None)),
        }
        nozzle, bed = presets[self.state]
        return PrinterTemperatures(TemperatureReading(*nozzle), TemperatureReading(*bed))

    def get_current_job(self) -> CurrentJob | None:
        config: Mapping[str, Any] = self.printer.adapter_config
        if self.state == PrinterState.PRINTING:
            return CurrentJob(
                file_name=str(config.get("current_file", "peca-demonstracao.3mf")),
                progress=float(config.get("progress", 42.5)),
                remaining_seconds=int(config.get("remaining_seconds", 3480)),
            )
        if self.state == PrinterState.PAUSED:
            return CurrentJob(
                file_name=str(config.get("current_file", "peca-demonstracao.3mf")),
                progress=float(config.get("progress", 63.0)),
                remaining_seconds=int(config.get("remaining_seconds", 2100)),
            )
        return None

    def get_camera_info(self) -> CameraInfo:
        return CameraInfo(False, "Câmera real não está disponível no adapter de demonstração.")

    def health_check(self) -> AdapterHealth:
        if self.state == PrinterState.OFFLINE:
            return AdapterHealth(False, ConnectionStatus.UNAVAILABLE)
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if self.state == PrinterState.ERROR:
            return AdapterHealth(True, ConnectionStatus.DEGRADED, now, "Erro simulado pela impressora.")
        return AdapterHealth(True, ConnectionStatus.HEALTHY, now)

    def capabilities(self) -> Mapping[str, bool]:
        return {
            "temperatures": True,
            "current_job": True,
            "camera": False,
            "controls": False,
        }
