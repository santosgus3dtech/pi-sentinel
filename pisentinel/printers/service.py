from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..db import Database
from .base import PrinterAdapter, PrinterFeatureUnavailable, PrinterOperationError
from .bambu_a1 import BambuA1Adapter
from .creality_k1c import CameraStream, CrealityK1CAdapter
from .mock import MockPrinterAdapter
from .models import (
    CameraInfo,
    ConnectionStatus,
    NormalizedPrinter,
    PrinterDefinition,
    PrinterState,
    PrinterTemperatures,
)

AdapterFactory = Callable[[PrinterDefinition], PrinterAdapter]


class PrinterService:
    """Loads printer definitions, selects adapters and contains failures."""

    def __init__(self, database: Database, adapters: Mapping[str, AdapterFactory] | None = None,
                 include_mock: bool = False, controls_enabled: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        self.database = database
        self.adapters: dict[str, AdapterFactory] = {
            "mock": MockPrinterAdapter,
            "creality_k1c": CrealityK1CAdapter,
            "bambu_a1": BambuA1Adapter,
        }
        if adapters:
            self.adapters.update(adapters)
        self.include_mock = include_mock
        self.controls_enabled = controls_enabled
        self._clock = clock
        self._cache: dict[int, tuple[float, NormalizedPrinter]] = {}
        self._cache_lock = threading.RLock()

    @staticmethod
    def _demo_definition() -> PrinterDefinition:
        return PrinterDefinition(
            id=0,
            device_id=None,
            name="Impressora de demonstração",
            manufacturer="PiSentinel",
            model="Mock",
            adapter_type="mock",
            enabled=True,
            camera_enabled=False,
            adapter_config={"state": "PRINTING", "progress": 42.5,
                            "current_file": "peca-demonstracao.3mf", "remaining_seconds": 3480},
        )

    def _definitions(self) -> list[PrinterDefinition]:
        definitions = [PrinterDefinition.from_record(row) for row in self.database.list_printers()]
        if self.include_mock and not any(item.adapter_type == "mock" for item in definitions):
            definitions.append(self._demo_definition())
        return definitions

    def _definition(self, printer_id: int) -> PrinterDefinition:
        if printer_id == 0 and self.include_mock:
            return self._demo_definition()
        try:
            return PrinterDefinition.from_record(self.database.get_printer(printer_id))
        except KeyError:
            raise

    def _adapter(self, printer_id: int) -> PrinterAdapter:
        definition = self._definition(printer_id)
        factory = self.adapters.get(definition.adapter_type)
        if factory is None:
            raise PrinterFeatureUnavailable(f"Adapter não registrado: {definition.adapter_type}")
        return factory(definition)

    @staticmethod
    def _cache_seconds(snapshot: NormalizedPrinter) -> float:
        if snapshot.state in {PrinterState.PRINTING, PrinterState.PAUSED}:
            return 8.0
        if snapshot.state == PrinterState.OFFLINE:
            return 15.0
        return 20.0

    def _snapshot(self, definition: PrinterDefinition) -> NormalizedPrinter:
        now = self._clock()
        with self._cache_lock:
            cached = self._cache.get(definition.id)
            if cached and cached[0] > now:
                return cached[1]
            snapshot = self._uncached_snapshot(definition)
            self._cache[definition.id] = (now + self._cache_seconds(snapshot), snapshot)
            return snapshot

    def _uncached_snapshot(self, definition: PrinterDefinition) -> NormalizedPrinter:
        if not definition.enabled:
            return NormalizedPrinter(
                definition=definition,
                online=False,
                connection_status=ConnectionStatus.DISABLED,
                state=PrinterState.OFFLINE,
            )
        try:
            factory = self.adapters.get(definition.adapter_type)
            if factory is None:
                raise LookupError(f"Adapter não registrado: {definition.adapter_type}")
            adapter = factory(definition)
            health = adapter.health_check()
            capabilities = dict(adapter.capabilities())
            capabilities["controls"] = bool(
                self.controls_enabled and capabilities.get("start_print", False)
            )
            camera = adapter.get_camera_info()
            if not health.online:
                return NormalizedPrinter(
                    definition=definition,
                    online=False,
                    connection_status=health.connection_status,
                    state=PrinterState.OFFLINE,
                    last_seen=health.last_seen,
                    capabilities=capabilities,
                    camera=camera,
                    latency_ms=health.latency_ms,
                    health_components=health.components,
                    error=health.message,
                )
            state = adapter.get_status()
            return NormalizedPrinter(
                definition=definition,
                online=True,
                connection_status=health.connection_status,
                state=state,
                temperatures=adapter.get_temperatures(),
                current_job=adapter.get_current_job(),
                last_seen=health.last_seen,
                capabilities=capabilities,
                camera=camera,
                latency_ms=health.latency_ms,
                health_components=health.components,
                error=health.message,
            )
        except Exception as exc:
            return NormalizedPrinter(
                definition=definition,
                online=False,
                connection_status=ConnectionStatus.UNAVAILABLE,
                state=PrinterState.ERROR,
                temperatures=PrinterTemperatures(),
                camera=CameraInfo(False, "Adapter indisponível."),
                error=f"{type(exc).__name__}: {exc}"[:300],
            )

    def list_printers(self) -> list[dict[str, Any]]:
        return [self._snapshot(definition).to_dict() for definition in self._definitions()]

    def get_printer(self, printer_id: int) -> dict[str, Any]:
        return self._snapshot(self._definition(printer_id)).to_dict()

    def get_status(self, printer_id: int) -> dict[str, Any]:
        snapshot = self.get_printer(printer_id)
        keys = ("id", "online", "connection_status", "state", "progress", "current_file",
                "remaining_seconds", "temperatures", "last_seen", "latency_ms", "health", "error")
        return {key: snapshot[key] for key in keys}

    def get_capabilities(self, printer_id: int) -> dict[str, Any]:
        snapshot = self.get_printer(printer_id)
        return {"id": snapshot["id"], "adapter_type": snapshot["adapter_type"],
                "capabilities": snapshot["capabilities"]}

    def get_health(self, printer_id: int) -> dict[str, Any]:
        snapshot = self.get_printer(printer_id)
        return {
            "id": snapshot["id"],
            "online": snapshot["online"],
            "connection_status": snapshot["connection_status"],
            "last_seen": snapshot["last_seen"],
            "latency_ms": snapshot["latency_ms"],
            "health": snapshot["health"],
        }

    def get_details(self, printer_id: int) -> dict[str, Any]:
        adapter = self._adapter(printer_id)
        try:
            details = dict(adapter.get_details())
        except PrinterFeatureUnavailable:
            raise
        except Exception as exc:
            raise PrinterOperationError(f"Não foi possível obter os parâmetros: {type(exc).__name__}.") from exc
        return {"id": printer_id, "details": details}

    def list_files(self, printer_id: int) -> dict[str, Any]:
        adapter = self._adapter(printer_id)
        try:
            files = adapter.list_files()
        except PrinterFeatureUnavailable:
            raise
        except Exception as exc:
            raise PrinterOperationError(f"Não foi possível consultar o cartão: {type(exc).__name__}.") from exc
        return {"id": printer_id, "files": files, "count": len(files)}

    def inspect_file(self, printer_id: int, path: str) -> dict[str, Any]:
        adapter = self._adapter(printer_id)
        try:
            return {"id": printer_id, **adapter.inspect_file(path)}
        except (PrinterFeatureUnavailable, PrinterOperationError):
            raise
        except Exception as exc:
            raise PrinterOperationError(f"Não foi possível inspecionar o arquivo: {type(exc).__name__}.") from exc

    def list_history(self, printer_id: int) -> dict[str, Any]:
        adapter = self._adapter(printer_id)
        try:
            items = adapter.list_history()
        except PrinterFeatureUnavailable:
            raise
        except Exception as exc:
            raise PrinterOperationError(f"Não foi possível consultar o histórico: {type(exc).__name__}.") from exc
        return {"id": printer_id, "history": items, "count": len(items)}

    def open_file_preview(self, printer_id: int, path: str, plate: str | None = None) -> tuple[str, bytes]:
        adapter = self._adapter(printer_id)
        try:
            return adapter.open_file_preview(path, plate)
        except (PrinterFeatureUnavailable, PrinterOperationError):
            raise
        except Exception as exc:
            raise PrinterOperationError(f"Não foi possível carregar a miniatura: {type(exc).__name__}.") from exc

    def start_print(self, printer_id: int, path: str, options: Mapping[str, Any]) -> dict[str, Any]:
        if not self.controls_enabled:
            raise PrinterFeatureUnavailable("Os controles de impressão estão desativados neste servidor.")
        adapter = self._adapter(printer_id)
        try:
            result = adapter.start_print(path, options)
        except (PrinterFeatureUnavailable, PrinterOperationError):
            raise
        except Exception as exc:
            raise PrinterOperationError(f"A impressora recusou a solicitação: {type(exc).__name__}.") from exc
        with self._cache_lock:
            self._cache.pop(printer_id, None)
        return {"id": printer_id, **result}

    def open_camera(self, printer_id: int) -> CameraStream:
        adapter = self._adapter(printer_id)
        opener = getattr(adapter, "open_camera_stream", None)
        if not callable(opener):
            raise LookupError("O adapter não oferece câmera.")
        return opener()
