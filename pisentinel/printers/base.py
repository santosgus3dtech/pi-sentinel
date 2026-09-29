from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from .models import AdapterHealth, CameraInfo, CurrentJob, PrinterDefinition, PrinterState, PrinterTemperatures


class PrinterFeatureUnavailable(LookupError):
    """Raised when an adapter does not expose a requested optional feature."""


class PrinterOperationError(RuntimeError):
    """Safe, user-facing failure for a printer file or control operation."""


class PrinterAdapter(ABC):
    """Status contract implemented by each printer integration.

    Adapters translate manufacturer-specific responses into small normalized
    fragments. The service combines them and contains failures so one printer
    cannot make the complete API unavailable.
    """

    def __init__(self, printer: PrinterDefinition):
        self.printer = printer

    @abstractmethod
    def get_status(self) -> PrinterState:
        raise NotImplementedError

    @abstractmethod
    def get_temperatures(self) -> PrinterTemperatures:
        raise NotImplementedError

    @abstractmethod
    def get_current_job(self) -> CurrentJob | None:
        raise NotImplementedError

    @abstractmethod
    def get_camera_info(self) -> CameraInfo:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> AdapterHealth:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> Mapping[str, bool]:
        raise NotImplementedError

    def get_details(self) -> Mapping[str, Any]:
        """Return safe telemetry that does not fit the normalized core model."""
        return {}

    def list_files(self) -> list[dict[str, Any]]:
        raise PrinterFeatureUnavailable("O adapter não oferece acesso aos arquivos da impressora.")

    def inspect_file(self, path: str) -> dict[str, Any]:
        raise PrinterFeatureUnavailable("O adapter não oferece inspeção de arquivos.")

    def list_history(self) -> list[dict[str, Any]]:
        raise PrinterFeatureUnavailable("O adapter não oferece histórico da impressora.")

    def open_file_preview(self, path: str, plate: str | None = None) -> tuple[str, bytes]:
        raise PrinterFeatureUnavailable("O adapter não oferece miniaturas de arquivos.")

    def start_print(self, path: str, options: Mapping[str, Any]) -> dict[str, Any]:
        raise PrinterFeatureUnavailable("O adapter não permite iniciar impressões.")
