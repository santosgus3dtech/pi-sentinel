from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from .auth import (
    COOKIE_NAME,
    AuthService,
    AuthenticationError,
    AuthenticationLocked,
    Principal,
    validate_password,
)
from .config import GROUPS, PROJECT_ROOT, Settings, get_settings, validate_host
from .db import Database, utc_datetime
from .printers.creality_k1c import CameraUnavailable
from .printers.base import PrinterFeatureUnavailable, PrinterOperationError
from .printers.service import PrinterService


class DevicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    group: str | None = None

    @field_validator("name", "notes", "group", mode="before")
    @classmethod
    def non_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Use texto vazio para limpar um campo.")
        return value

    @field_validator("group")
    @classmethod
    def known_group(cls, value: str | None) -> str | None:
        if value not in GROUPS:
            raise ValueError("Grupo inválido.")
        return value


class TargetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=253)
    scope: Literal["local", "external"] = "external"
    kind: Literal["icmp", "dns", "tcp"] = "icmp"
    port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    query: str | None = Field(default=None, min_length=1, max_length=253)
    enabled: StrictBool = True

    @field_validator("host", "query")
    @classmethod
    def hostname(cls, value: str | None) -> str | None:
        return validate_host(value) if value is not None else None


class TargetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    host: str | None = Field(default=None, min_length=1, max_length=253)
    scope: Literal["local", "external"] | None = None
    kind: Literal["icmp", "dns", "tcp"] | None = None
    port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    query: str | None = Field(default=None, min_length=1, max_length=253)
    enabled: StrictBool | None = None

    @field_validator("name", "host", "scope", "kind", "enabled", mode="before")
    @classmethod
    def non_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Este campo não pode ser nulo.")
        return value

    @field_validator("host", "query")
    @classmethod
    def hostname(cls, value: str | None) -> str | None:
        return validate_host(value) if value is not None else None


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpeedtestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpeedtestConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool | None = None
    interval_minutes: int | None = Field(default=None, ge=60, le=10080, strict=True)
    download_min_mbps: float | None = Field(default=None, gt=0, le=100000)
    upload_min_mbps: float | None = Field(default=None, gt=0, le=100000)
    failure_threshold: int | None = Field(default=None, ge=1, le=10, strict=True)
    recovery_threshold: int | None = Field(default=None, ge=1, le=10, strict=True)
    retention_days: int | None = Field(default=None, ge=1, le=365, strict=True)

    @field_validator("enabled", "interval_minutes", "failure_threshold", "recovery_threshold", "retention_days", mode="before")
    @classmethod
    def non_null_fields(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Este campo não pode ser nulo.")
        return value


class PrinterStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    confirmation: Literal["IMPRIMIR"]
    plate: str | None = Field(default=None, max_length=256)
    timelapse: StrictBool = False
    bed_leveling: StrictBool = True
    flow_calibration: StrictBool = False
    vibration_calibration: StrictBool = True
    use_ams: StrictBool = False
    ams_mapping: list[int] = Field(default_factory=list, max_length=16)

    @field_validator("path", "plate")
    @classmethod
    def safe_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(char in value for char in "\r\n\0"):
            raise ValueError("Caminho de arquivo inválido.")
        return value

    @field_validator("ams_mapping")
    @classmethod
    def valid_ams_mapping(cls, value: list[int]) -> list[int]:
        if any(item < -1 or item > 255 for item in value):
            raise ValueError("Mapeamento AMS inválido.")
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    remember: StrictBool = False


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)

    @field_validator("new_password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        return validate_password(value)

def create_app(settings: Settings | None = None, database: Database | None = None,
               printer_service: PrinterService | None = None) -> FastAPI:
    settings = settings or get_settings()
    database = database or Database(settings.db_path, failure_threshold=settings.failure_threshold,
                                    recovery_threshold=settings.recovery_threshold,
                                    retention_days=settings.retention_days,
                                    incident_retention_days=settings.incident_retention_days)
    printer_service = printer_service or PrinterService(
        database,
        include_mock=settings.mock_printer_enabled,
        controls_enabled=settings.printer_controls_enabled,
    )
    app = FastAPI(title="Pi Sentinel", version="0.6.0", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.database = database
    app.state.printer_service = printer_service
    auth_service = AuthService(database, settings.auth_session_hours, settings.auth_remember_days)
    app.state.auth_service = auth_service

    public_api_paths = {
        "/api/health",
        "/api/auth/session",
        "/api/auth/login",
        "/api/auth/guest",
        "/api/auth/logout",
    }
    guest_get_paths = {"/api/summary", "/api/printers"}

    def request_principal(request: Request) -> Principal:
        principal = getattr(request.state, "principal", None)
        if principal is None:
            raise HTTPException(401, "Faça login para continuar.")
        return principal

    def auth_payload(principal: Principal | None) -> dict[str, Any]:
        return {
            "auth_enabled": settings.auth_enabled,
            "authenticated": principal is not None,
            "role": principal.role if principal else None,
            "username": principal.username if principal else None,
            "must_change_password": principal.must_change_password if principal else False,
        }

    def set_session_cookie(response: Response, request: Request, token: str, *, remember: bool = False) -> None:
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=settings.auth_remember_days * 86400 if remember else None,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        if settings.auth_enabled:
            principal = auth_service.principal_for_token(request.cookies.get(COOKIE_NAME))
        else:
            principal = Principal("admin", "local", None, False)
        request.state.principal = principal
        if request.url.path.startswith("/api/") and request.url.path not in public_api_paths:
            if principal is None:
                return JSONResponse({"detail": "Faça login para continuar."}, status_code=401,
                                    headers={"Cache-Control": "no-store"})
            if principal.must_change_password and request.url.path != "/api/auth/change-password":
                return JSONResponse({"detail": "Altere sua senha antes de continuar."}, status_code=403,
                                    headers={"Cache-Control": "no-store"})
            if principal.role == "guest" and not (
                request.method == "GET" and request.url.path in guest_get_paths
            ):
                return JSONResponse({"detail": "Este recurso é restrito ao administrador."}, status_code=403,
                                    headers={"Cache-Control": "no-store"})
        if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                return JSONResponse({"detail": "Requisições de alteração exigem application/json."}, status_code=415)
            origin = request.headers.get("origin")
            if origin:
                supplied = urlsplit(origin)
                expected = urlsplit(str(request.url))
                try:
                    supplied_port = supplied.port or (443 if supplied.scheme == "https" else 80)
                    expected_port = expected.port or (443 if expected.scheme == "https" else 80)
                    same = (supplied.scheme, supplied.hostname, supplied_port) == (expected.scheme, expected.hostname, expected_port)
                except ValueError:
                    same = False
                if not same or supplied.username or supplied.password or supplied.path not in {"", "/"} or supplied.query or supplied.fragment:
                    return JSONResponse({"detail": "Origem não autorizada."}, status_code=403)
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site and fetch_site not in {"same-origin", "none"}:
                return JSONResponse({"detail": "Origem não autorizada."}, status_code=403)
            # Non-browser collectors/CLI clients have no Origin. They still need
            # the JSON content type, which a cross-origin HTML form cannot send.
            if len(await request.body()) > 16384:
                return JSONResponse({"detail": "Corpo da requisição excede 16 KiB."}, status_code=413)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def collector_state() -> dict[str, Any]:
        last_seen = database.get_meta("collector_last_seen")
        stale = True
        if isinstance(last_seen, str):
            try:
                age = (datetime.now(timezone.utc) - utc_datetime(last_seen)).total_seconds()
                stale = age > settings.stale_after_seconds or age < -60
            except (ValueError, TypeError, OverflowError):
                pass
        return {"last_seen": last_seen, "stale": stale, "error": database.get_meta("collector_error")}

    def network_state() -> dict[str, Any]:
        network = {"label": settings.label, "cidr": settings.cidr, "interface": settings.interface, "ssids": list(settings.ssids)}
        discovered = database.get_meta("network", {})
        if isinstance(discovered, dict):
            for key in network:
                value = discovered.get(key)
                if key == "ssids" and isinstance(value, list) and all(isinstance(item, str) for item in value):
                    network[key] = value
                elif key != "ssids" and isinstance(value, str) and value:
                    network[key] = value
        return network

    def devices(stale: bool | None = None) -> list[dict[str, Any]]:
        if stale is None:
            stale = collector_state()["stale"]
        items = database.list_devices()
        if stale:
            for item in items:
                item["status"] = "unknown"
        return items

    def targets(stale: bool | None = None) -> list[dict[str, Any]]:
        if stale is None:
            stale = collector_state()["stale"]
        items = database.list_targets()
        if stale:
            for item in items:
                item["status"] = "unknown"
        return items

    def diagnosis(collector: dict[str, Any], monitored: list[dict[str, Any]]) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        if collector["stale"]:
            results.append({"level": "warning", "title": "Aguardando o coletor", "detail": "Sem observação recente. Os estados atuais são desconhecidos; isso não confirma uma queda na rede."})
        if collector["error"]:
            results.append({"level": "warning", "title": "Erro na coleta", "detail": str(collector["error"])[:500]})
        discovery_error = database.get_meta("discovery_error")
        if discovery_error:
            results.append({"level": "warning", "title": "Descoberta incompleta", "detail": str(discovery_error)[:500]})
        if collector["stale"]:
            return results
        enabled = [target for target in monitored if target["enabled"]]
        local = [target for target in enabled if target["scope"] == "local"]
        external = [target for target in enabled if target["scope"] == "external"]
        offline = [target for target in enabled if target["status"] == "offline"]
        if local and all(target["status"] == "offline" for target in local):
            results.append({"level": "critical", "title": "Destinos locais sem resposta", "detail": "Todos os destinos locais monitorados falharam. Verifique a conexão do Raspberry e os equipamentos locais."})
        elif external and all(target["status"] == "offline" for target in external) and any(target["status"] == "online" for target in local):
            results.append({"level": "warning", "title": "Possível falha no acesso externo", "detail": "Um destino local responde, mas os destinos externos monitorados falharam. As sondagens não identificam sozinhas a causa."})
        elif offline:
            results.append({"level": "warning", "title": "Destinos indisponíveis", "detail": f"{len(offline)} de {len(enabled)} destinos ativos atingiram o limite de falhas consecutivas."})
        elif enabled and all(target["status"] == "online" for target in enabled):
            results.append({"level": "ok", "title": "Destinos monitorados respondendo", "detail": "As últimas sondagens confirmadas estão dentro dos critérios de disponibilidade."})
        elif enabled:
            results.append({"level": "info", "title": "Coletando primeiras observações", "detail": "Ainda não há observações suficientes para confirmar todos os estados."})
        else:
            results.append({"level": "info", "title": "Nenhum destino ativo", "detail": "Cadastre destinos para acompanhar conectividade local, externa e DNS."})
        return results

    def guest_summary_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            **payload,
            "network": {"label": payload["network"]["label"], "cidr": "", "interface": "", "ssids": []},
            "collector": {**payload["collector"], "error": None},
            "last_discovery": None,
            "discovery_error": None,
            "discovery_running": False,
        }

    def guest_printer_payload(item: dict[str, Any]) -> dict[str, Any]:
        safe = dict(item)
        safe.update({
            "device_id": None,
            "ip": None,
            "mac": None,
            "network_status": None,
            "network_last_seen": None,
            "latency_ms": None,
            "error": None,
        })
        safe["camera"] = {"available": False, "reason": "Recurso restrito ao administrador.",
                          "protocol": None, "stream_url": None}
        safe["capabilities"] = {
            **safe.get("capabilities", {}),
            "camera": False,
            "files": False,
            "file_metadata": False,
            "history": False,
            "start_print": False,
            "controls": False,
        }
        safe["health"] = {"overall": safe.get("connection_status", "unknown"), "components": {}}
        return safe

    @app.get("/api/health")
    def health():
        database.get_meta("collector_last_seen")
        return {"status": "ok", "collector": collector_state()}

    @app.get("/api/auth/session")
    def auth_session(request: Request):
        principal = getattr(request.state, "principal", None)
        return auth_payload(principal)

    @app.post("/api/auth/login")
    def auth_login(payload: LoginRequest, request: Request):
        client_ip = request.client.host if request.client else "unknown"
        subject = hashlib.sha256(f"{client_ip}:{payload.username.strip().lower()}".encode("utf-8")).hexdigest()
        try:
            token, principal = auth_service.authenticate(
                payload.username, payload.password, subject, remember=payload.remember,
            )
        except AuthenticationLocked as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after_seconds)}) from None
        except AuthenticationError as exc:
            raise HTTPException(401, str(exc)) from None
        response = JSONResponse(auth_payload(principal))
        set_session_cookie(response, request, token, remember=payload.remember)
        return response

    @app.post("/api/auth/guest")
    def auth_guest(request: Request):
        token, principal = auth_service.create_guest_session()
        response = JSONResponse(auth_payload(principal))
        set_session_cookie(response, request, token)
        return response

    @app.post("/api/auth/logout")
    def auth_logout(request: Request):
        auth_service.logout(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE_NAME, path="/", samesite="strict")
        return response

    @app.post("/api/auth/change-password")
    def auth_change_password(payload: PasswordChangeRequest, request: Request):
        principal = request_principal(request)
        try:
            updated = auth_service.change_password(principal, payload.current_password, payload.new_password)
        except AuthenticationError as exc:
            raise HTTPException(401, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        # Password changes revoke every existing session, including this one.
        response = JSONResponse({**auth_payload(updated), "authenticated": False,
                                 "detail": "Senha alterada. Entre novamente."})
        response.delete_cookie(COOKIE_NAME, path="/", samesite="strict")
        return response

    @app.get("/api/settings")
    def public_settings():
        return {**network_state(), "interval_seconds": settings.interval_seconds,
                "discovery_interval_seconds": settings.discovery_interval_seconds,
                "retention_days": settings.retention_days, "failure_threshold": settings.failure_threshold,
                "recovery_threshold": settings.recovery_threshold}

    @app.get("/api/summary")
    def summary(request: Request):
        collector = collector_state()
        inventory = devices(collector["stale"])
        monitored = targets(collector["stale"])
        payload = {"network": network_state(), "collector": collector,
                   "counts": {"devices": len(inventory),
                              **{status: sum(item["status"] == status for item in inventory) for status in ("online", "offline", "unknown")},
                              "open_incidents": database.open_incident_count()},
                   "diagnosis": diagnosis(collector, monitored),
                   "last_discovery": database.get_meta("last_discovery"),
                   "discovery_error": database.get_meta("discovery_error"),
                   "discovery_running": bool(database.get_meta("discovery_running", False))}
        return guest_summary_payload(payload) if request_principal(request).role == "guest" else payload

    @app.get("/api/devices")
    def get_devices():
        return devices()

    @app.get("/api/devices/{entity_id}")
    def get_device(entity_id: int):
        try:
            item = database.device_details(entity_id)
        except KeyError:
            raise HTTPException(404, "Dispositivo não encontrado.") from None
        if collector_state()["stale"]:
            item["status"] = "unknown"
        return item

    @app.get("/api/router")
    def router_dashboard():
        return database.router_dashboard()

    @app.patch("/api/devices/{entity_id}")
    def patch_device(entity_id: int, patch: DevicePatch):
        try:
            item = database.update_device(entity_id, patch.model_dump(exclude_unset=True))
        except KeyError:
            raise HTTPException(404, "Dispositivo não encontrado.") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if collector_state()["stale"]:
            item["status"] = "unknown"
        return item

    @app.get("/api/targets")
    def get_targets():
        return targets()

    @app.post("/api/targets", status_code=201)
    def post_target(target: TargetCreate):
        try:
            entity_id = database.create_target(**target.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return next(item for item in targets() if item["id"] == entity_id)

    @app.patch("/api/targets/{entity_id}")
    def patch_target(entity_id: int, patch: TargetPatch):
        try:
            item = database.update_target(entity_id, patch.model_dump(exclude_unset=True))
        except KeyError:
            raise HTTPException(404, "Destino não encontrado.") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if collector_state()["stale"]:
            item["status"] = "unknown"
        return item

    @app.delete("/api/targets/{entity_id}", status_code=204)
    def delete_target(entity_id: int):
        if not database.delete_target(entity_id):
            raise HTTPException(404, "Destino não encontrado.")
        return Response(status_code=204)

    @app.get("/api/targets/{entity_id}/history")
    def history(entity_id: int, hours: int = Query(default=24, ge=1, le=settings.retention_days * 24)):
        try:
            return database.target_history(entity_id, hours)
        except KeyError:
            raise HTTPException(404, "Destino não encontrado.") from None

    @app.get("/api/incidents")
    def incidents(limit: int = Query(default=100, ge=1, le=500)):
        return database.list_incidents(limit)

    @app.get("/api/speedtests")
    def speedtests(days: int = Query(default=7, ge=1, le=30)):
        try:
            return database.speedtest_dashboard(days)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/speedtests/config")
    def speedtest_config():
        return database.get_speedtest_config()

    @app.patch("/api/speedtests/config")
    def patch_speedtest_config(patch: SpeedtestConfigPatch):
        changes = patch.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(422, "Informe ao menos uma configuração.")
        try:
            return database.update_speedtest_config(changes)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/speedtests/run", status_code=202)
    def request_speedtest(_request: SpeedtestRequest = Body(default_factory=SpeedtestRequest)):
        job = database.request_speedtest("manual")
        return {"id": job["id"], "status": job["status"], "trigger": job["trigger"],
                "requested_at": job["requested_at"], "already_pending": job["already_pending"]}

    @app.get("/api/printers")
    def printers(request: Request):
        items = printer_service.list_printers()
        return [guest_printer_payload(item) for item in items] if request_principal(request).role == "guest" else items

    @app.get("/api/printers/{printer_id}")
    def printer(printer_id: int):
        try:
            return printer_service.get_printer(printer_id)
        except KeyError:
            raise HTTPException(404, "Impressora não encontrada.") from None

    @app.get("/api/printers/{printer_id}/status")
    def printer_status(printer_id: int):
        try:
            return printer_service.get_status(printer_id)
        except KeyError:
            raise HTTPException(404, "Impressora não encontrada.") from None

    @app.get("/api/printers/{printer_id}/capabilities")
    def printer_capabilities(printer_id: int):
        try:
            return printer_service.get_capabilities(printer_id)
        except KeyError:
            raise HTTPException(404, "Impressora não encontrada.") from None

    @app.get("/api/printers/{printer_id}/health")
    def printer_health(printer_id: int):
        try:
            return printer_service.get_health(printer_id)
        except KeyError:
            raise HTTPException(404, "Impressora não encontrada.") from None

    @app.get("/api/printers/{printer_id}/details")
    def printer_details(printer_id: int):
        try:
            return printer_service.get_details(printer_id)
        except KeyError:
            raise HTTPException(404, "Impressora não encontrada.") from None
        except PrinterFeatureUnavailable as exc:
            raise HTTPException(409, str(exc)) from None
        except PrinterOperationError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.get("/api/printers/{printer_id}/files")
    def printer_files(printer_id: int):
        try:
            return printer_service.list_files(printer_id)
        except KeyError:
            raise HTTPException(404, "Impressora não encontrada.") from None
        except PrinterFeatureUnavailable as exc:
            raise HTTPException(409, str(exc)) from None
        except PrinterOperationError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.get("/api/printers/{printer_id}/files/inspect")
    def printer_file_details(printer_id: int, path: str = Query(min_length=1, max_length=1024)):
        try:
            return printer_service.inspect_file(printer_id, path)
        except KeyError:
            raise HTTPException(404, "Impressora ou arquivo não encontrado.") from None
        except PrinterFeatureUnavailable as exc:
            raise HTTPException(409, str(exc)) from None
        except PrinterOperationError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/printers/{printer_id}/files/preview")
    def printer_file_preview(printer_id: int, path: str = Query(min_length=1, max_length=1024),
                             plate: str | None = Query(default=None, max_length=256)):
        try:
            content_type, content = printer_service.open_file_preview(printer_id, path, plate)
        except KeyError:
            raise HTTPException(404, "Impressora ou miniatura não encontrada.") from None
        except PrinterFeatureUnavailable as exc:
            raise HTTPException(409, str(exc)) from None
        except PrinterOperationError as exc:
            raise HTTPException(422, str(exc)) from None
        return Response(content, media_type=content_type, headers={"Cache-Control": "private, max-age=30"})

    @app.get("/api/printers/{printer_id}/history")
    def printer_history(printer_id: int):
        try:
            return printer_service.list_history(printer_id)
        except KeyError:
            raise HTTPException(404, "Impressora não encontrada.") from None
        except PrinterFeatureUnavailable as exc:
            raise HTTPException(409, str(exc)) from None
        except PrinterOperationError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.post("/api/printers/{printer_id}/print", status_code=202)
    def printer_start(printer_id: int, payload: PrinterStartRequest, request: Request):
        # Physical printer actions are accepted only from this web interface.
        if not request.headers.get("origin"):
            raise HTTPException(403, "Abra o PiSentinel no navegador para confirmar a impressão.")
        try:
            return printer_service.start_print(
                printer_id,
                payload.path,
                payload.model_dump(exclude={"path", "confirmation"}),
            )
        except KeyError:
            raise HTTPException(404, "Impressora ou arquivo não encontrado.") from None
        except PrinterFeatureUnavailable as exc:
            raise HTTPException(409, str(exc)) from None
        except PrinterOperationError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/printers/{printer_id}/camera")
    def printer_camera(printer_id: int):
        try:
            stream = printer_service.open_camera(printer_id)
        except KeyError:
            raise HTTPException(404, "Impressora não encontrada.") from None
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from None
        except CameraUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        return StreamingResponse(
            stream.chunks,
            headers={
                "Content-Type": stream.content_type,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/discovery", status_code=202)
    def request_discovery(_request: DiscoveryRequest = Body(default_factory=DiscoveryRequest)):
        # Only enqueue. Network discovery is exclusively the collector's job.
        job = database.request_discovery()
        return {"status": "queued", "queued": True, "id": job["id"], "requested_at": job["requested_at"]}

    static_path = PROJECT_ROOT / "frontend" / "dist"
    if static_path.is_dir():
        app.mount("/", StaticFiles(directory=static_path, html=True), name="frontend")
    return app


app = create_app()
