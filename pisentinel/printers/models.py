from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class PrinterState(str, Enum):
    IDLE = "IDLE"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


class ConnectionStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TemperatureReading:
    current: float | None = None
    target: float | None = None

    def to_dict(self) -> dict[str, float | None]:
        return {"current": self.current, "target": self.target}


@dataclass(frozen=True, slots=True)
class PrinterTemperatures:
    nozzle: TemperatureReading = field(default_factory=TemperatureReading)
    bed: TemperatureReading = field(default_factory=TemperatureReading)

    def to_dict(self) -> dict[str, dict[str, float | None]]:
        return {"nozzle": self.nozzle.to_dict(), "bed": self.bed.to_dict()}


@dataclass(frozen=True, slots=True)
class CurrentJob:
    file_name: str | None = None
    progress: float | None = None
    remaining_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.progress is not None and not 0 <= self.progress <= 100:
            raise ValueError("Printer progress must be between 0 and 100.")
        if self.remaining_seconds is not None and self.remaining_seconds < 0:
            raise ValueError("Remaining time cannot be negative.")


@dataclass(frozen=True, slots=True)
class CameraInfo:
    available: bool = False
    reason: str | None = None
    protocol: str | None = None


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    status: ConnectionStatus
    available: bool | None
    latency_ms: float | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "available": self.available,
            "latency_ms": self.latency_ms,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    online: bool
    connection_status: ConnectionStatus
    last_seen: str | None = None
    message: str | None = None
    latency_ms: float | None = None
    components: Mapping[str, ComponentHealth] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PrinterDefinition:
    id: int
    device_id: int | None
    name: str
    manufacturer: str
    model: str
    adapter_type: str
    enabled: bool
    camera_enabled: bool
    adapter_config: Mapping[str, Any] = field(default_factory=dict)
    ip: str | None = None
    mac: str | None = None
    network_status: str | None = None
    network_last_seen: str | None = None

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> PrinterDefinition:
        return cls(
            id=int(record["id"]),
            device_id=record.get("device_id"),
            name=str(record["name"]),
            manufacturer=str(record["manufacturer"]),
            model=str(record["model"]),
            adapter_type=str(record["adapter_type"]),
            enabled=bool(record["enabled"]),
            camera_enabled=bool(record["camera_enabled"]),
            adapter_config=dict(record.get("adapter_config") or {}),
            ip=record.get("ip"),
            mac=record.get("mac"),
            network_status=record.get("network_status"),
            network_last_seen=record.get("network_last_seen"),
        )


@dataclass(frozen=True, slots=True)
class NormalizedPrinter:
    definition: PrinterDefinition
    online: bool
    connection_status: ConnectionStatus
    state: PrinterState
    temperatures: PrinterTemperatures = field(default_factory=PrinterTemperatures)
    current_job: CurrentJob | None = None
    last_seen: str | None = None
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    camera: CameraInfo = field(default_factory=CameraInfo)
    latency_ms: float | None = None
    health_components: Mapping[str, ComponentHealth] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        job = self.current_job
        overall_health = {
            ConnectionStatus.HEALTHY: "HEALTHY",
            ConnectionStatus.DEGRADED: "PARTIAL",
        }.get(self.connection_status, "OFFLINE")
        return {
            "id": self.definition.id,
            "device_id": self.definition.device_id,
            "name": self.definition.name,
            "manufacturer": self.definition.manufacturer,
            "model": self.definition.model,
            "adapter_type": self.definition.adapter_type,
            "enabled": self.definition.enabled,
            "camera_enabled": self.definition.camera_enabled,
            "online": self.online,
            "connection_status": self.connection_status.value,
            "state": self.state.value,
            "progress": job.progress if job else None,
            "current_file": job.file_name if job else None,
            "remaining_seconds": job.remaining_seconds if job else None,
            "temperatures": self.temperatures.to_dict(),
            "last_seen": self.last_seen,
            "capabilities": dict(self.capabilities),
            "camera": {
                "available": self.camera.available,
                "reason": self.camera.reason,
                "protocol": self.camera.protocol,
                "stream_url": f"/api/printers/{self.definition.id}/camera" if self.definition.camera_enabled else None,
            },
            "latency_ms": self.latency_ms,
            "health": {
                "overall": overall_health,
                "connection_status": self.connection_status.value,
                "components": {name: value.to_dict() for name, value in self.health_components.items()},
            },
            "ip": self.definition.ip,
            "mac": self.definition.mac,
            "network_status": self.definition.network_status,
            "network_last_seen": self.definition.network_last_seen,
            "error": self.error,
        }
